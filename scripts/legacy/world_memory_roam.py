#!/usr/bin/env python3
"""Long surface-only free roam for WorldMemory observation.

No mining/digging: the bot only walks on the surface, scans blocks/entities,
eats/heals, and lets WorldMemory record anchors, chunks, POIs and habitats.
Designed to run for ~90 minutes in the background.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import random
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BOT = "MapTest_01"
STATE_DIR = ROOT / ".state-maptest"
MEM_PATH = STATE_DIR / f"world_memory_{BOT}.json"
LOG_DIR = ROOT / "runs"
STAMP = time.strftime("%Y%m%d-%H%M%S")
JSONL_PATH = LOG_DIR / f"world-memory-roam-{STAMP}.jsonl"
REPORT_PATH = LOG_DIR / f"world-memory-roam-{STAMP}.json"
DURATION_S = float(os.environ.get("ROAM_DURATION_S", str(90 * 60)))
KEEP_MEMORY = os.environ.get("ROAM_KEEP_MEMORY", "0") == "1"
NO_PREPARE = os.environ.get("ROAM_NO_PREPARE", "0") == "1"
STEP_MIN_M = 48.0
STEP_MAX_M = 72.0
POI_TYPES = [
    "water", "lava", "sugar_cane", "pumpkin", "hay_block", "lectern",
    "chest", "barrel", "nether_portal", "gold_block", "obsidian",
    "enchanting_table", "crafting_table", "furnace",
]
CARDINALS = {
    "north": (0, -1),
    "east": (1, 0),
    "south": (0, 1),
    "west": (-1, 0),
    "northeast": (1, -1),
    "southeast": (1, 1),
    "southwest": (-1, 1),
    "northwest": (-1, -1),
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


def prepare() -> list[dict[str, str]]:
    cmds = [
        "spreadplayers -160 -390 8 24 false MapTest_01",
        "gamemode survival MapTest_01",
        "clear MapTest_01",
        "effect clear MapTest_01",
        "time set day",
        "weather clear",
        "effect give MapTest_01 minecraft:resistance 7200 4 true",
        "effect give MapTest_01 minecraft:regeneration 7200 2 true",
        "effect give MapTest_01 minecraft:night_vision 7200 0 true",
        "effect give MapTest_01 minecraft:saturation 60 20 true",
        "give MapTest_01 minecraft:iron_helmet 1",
        "give MapTest_01 minecraft:iron_chestplate 1",
        "give MapTest_01 minecraft:iron_leggings 1",
        "give MapTest_01 minecraft:iron_boots 1",
        "give MapTest_01 minecraft:iron_pickaxe 1",
        "give MapTest_01 minecraft:iron_sword 1",
        "give MapTest_01 minecraft:shield 1",
        "give MapTest_01 minecraft:cooked_beef 64",
        "give MapTest_01 minecraft:bread 32",
        "give MapTest_01 minecraft:torch 64",
        "give MapTest_01 minecraft:cobblestone 128",
        "give MapTest_01 minecraft:oak_planks 32",
        "give MapTest_01 minecraft:crafting_table 1",
        "give MapTest_01 minecraft:furnace 1",
        "give MapTest_01 minecraft:coal 32",
        "give MapTest_01 minecraft:white_bed 1",
        "item replace entity MapTest_01 armor.head with minecraft:iron_helmet 1",
        "item replace entity MapTest_01 armor.chest with minecraft:iron_chestplate 1",
        "item replace entity MapTest_01 armor.legs with minecraft:iron_leggings 1",
        "item replace entity MapTest_01 armor.feet with minecraft:iron_boots 1",
        "item replace entity MapTest_01 weapon.offhand with minecraft:shield 1",
    ]
    return rcon_batch(cmds)


def reapply_effects() -> None:
    try:
        rcon_batch(
            [
                "effect give MapTest_01 minecraft:resistance 7200 4 true",
                "effect give MapTest_01 minecraft:regeneration 7200 2 true",
                "effect give MapTest_01 minecraft:night_vision 7200 0 true",
                "time set day",
            ]
        )
    except Exception:
        pass


def jloads(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"ok": False, "raw": raw[:300]}
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
        return parsed["result"]
    return parsed if isinstance(parsed, dict) else {"ok": False}


def safe_status(bot: Any, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(4):
        try:
            return bot.status()
        except Exception:
            time.sleep(2.0 + attempt * 2.0)
            if attempt >= 1:
                try:
                    bot.recover_quarantine(timeout_s=30.0)
                except Exception:
                    pass
    return fallback or {}


def try_recover(bot: Any) -> None:
    try:
        bot.recover_quarantine(timeout_s=30.0)
    except Exception:
        pass


def choose_target(pos: list[float], memory: Any) -> tuple[str, float, float]:
    dirs = memory.unexplored_directions(pos, dim="overworld")
    # Mix cardinals with diagonals while always preferring the most unexplored cardinal.
    names = dirs + [d for d in CARDINALS if d not in dirs]
    name = names[0] if names else random.choice(list(CARDINALS))
    dx, dz = CARDINALS[name]
    if len(dirs) > 1 and random.random() < 0.45:
        name = random.choice(dirs)
        dx, dz = CARDINALS[name]
    dist = random.uniform(STEP_MIN_M, STEP_MAX_M)
    jitter = random.uniform(-0.35, 0.35)
    # small angular jitter for organic coverage
    nx = dx * math.cos(jitter) - dz * math.sin(jitter)
    nz = dx * math.sin(jitter) + dz * math.cos(jitter)
    return name, float(pos[0]) + nx * dist, float(pos[2]) + nz * dist


def main() -> int:
    os.environ["MC_STATE_DIR"] = str(STATE_DIR)
    os.environ["MC_WORLD_MEMORY_PATH"] = str(MEM_PATH)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MEM_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KEEP_MEMORY:
        MEM_PATH.unlink(missing_ok=True)

    from mc_agent.auth import TokenStore
    from mc_agent.bot import BotSession
    from mc_agent.config import Settings
    from mc_agent.errors import ApiError
    from mc_agent.harness.hud import build_world_hud
    from mc_agent.harness.mc_tools import McToolset

    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    record = TokenStore(STATE_DIR / "tokens.json").get(BOT)
    if record is None:
        raise SystemExit(f"no cached token for {BOT}")
    bot = BotSession(record.token, bot_username=record.bot_username, base_url=settings.mc_api_base_url)
    toolset = McToolset(bot)
    report: dict[str, Any] = {
        "bot": BOT,
        "started_at": STAMP,
        "duration_s": DURATION_S,
        "steps": [],
        "ok": True,
    }
    traj: list[dict[str, Any]] = []

    try:
        if NO_PREPARE:
            report["prepare"] = [{"cmd": "resume", "reply": "kept position/inventory/memory"}]
            reapply_effects()
        else:
            report["prepare"] = prepare()
        st = safe_status(bot)
        if not st.get("ready"):
            try:
                bot.connect(respect_reconnect_interval=False)
                bot.wait_until_ready(timeout_s=45.0)
            except Exception:
                pass
        st = safe_status(bot, st)
        report["start"] = {k: st.get(k) for k in ("pos", "health", "food", "dimension", "biome", "ready")}
        started = time.monotonic()
        next_effects = started + 1800.0
        stuck = 0
        consecutive_errors = 0
        step = 0
        last_surface: tuple[float, float] | None = None
        escape_cooldown_until = 0.0
        with JSONL_PATH.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "start", "status": report["start"]}, ensure_ascii=False) + "\n")
            while time.monotonic() - started < DURATION_S:
                now = time.monotonic()
                if now >= next_effects:
                    reapply_effects()
                    next_effects = now + 1800.0
                st = safe_status(bot, st)
                pos = st.get("pos") or [0.0, 64.0, 0.0]
                if st.get("health", 20) < 14 or st.get("food", 20) < 16 or st.get("in_water") or st.get("in_lava"):
                    try:
                        jloads(toolset.mc_skill_stabilize())
                    except Exception:
                        try_recover(bot)
                    st = safe_status(bot, st)
                    pos = st.get("pos") or pos
                near_hostile = st.get("nearest_hostile") or {}
                if isinstance(near_hostile, dict) and near_hostile.get("name") and float(near_hostile.get("dist", 99)) <= 7.0:
                    try:
                        jloads(toolset.mc_skill_flee(distance=12))
                    except Exception:
                        try_recover(bot)
                    st = safe_status(bot, st)
                    pos = st.get("pos") or pos

                name, tx, tz = choose_target(pos, toolset.world_memory)
                step += 1
                try:
                    raw_move = toolset.mc_move_to(x=tx, z=tz)
                    outer_move = json.loads(raw_move) if isinstance(raw_move, str) else {}
                    move = jloads(raw_move)
                except Exception as exc:  # noqa: BLE001
                    consecutive_errors += 1
                    fh.write(json.dumps({"event": "move_error", "step": step, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
                    fh.flush()
                    try_recover(bot)
                    time.sleep(5.0)
                    if consecutive_errors >= 20:
                        break
                    continue
                moved_ok = bool(outer_move.get("ok") is True or move.get("ok") is True)
                st2 = safe_status(bot, st)
                pos2 = st2.get("pos") or pos
                if float(pos2[1]) >= 58.0:
                    last_surface = (float(pos2[0]), float(pos2[2]))
                dist = math.hypot(float(pos2[0]) - float(pos[0]), float(pos2[2]) - float(pos[2]))
                if dist < 8.0:
                    stuck += 1
                    try:
                        jloads(toolset.mc_skill_unstick())
                    except Exception:
                        pass
                else:
                    stuck = 0
                    consecutive_errors = 0

                # Cave/underground recovery: prefer returning to the last known surface
                # waypoint; only one bounded stair-step pass, then a 5-minute cooldown.
                now_loop = time.monotonic()
                if stuck >= 2 and float(pos2[1]) < 55.0 and now_loop >= escape_cooldown_until:
                    escape_rows: list[dict[str, Any]] = []
                    recovered = False
                    if last_surface is not None:
                        try:
                            back = jloads(toolset.mc_move_to(x=last_surface[0], z=last_surface[1]))
                            st_back = safe_status(bot, st2)
                            pos_back = st_back.get("pos") or pos2
                            if math.hypot(float(pos_back[0]) - float(pos2[0]), float(pos_back[2]) - float(pos2[2])) >= 8.0:
                                st2, pos2 = st_back, pos_back
                                recovered = float(pos_back[1]) >= 50.0
                        except Exception:
                            pass
                    if not recovered:
                        esc_dirs = ["north", "east", "south", "west"]
                        random.shuffle(esc_dirs)
                        for esc_dir in esc_dirs[:2]:
                            try:
                                row = jloads(toolset.mc_skill_staircase(direction=esc_dir, vertical="up", steps=8))
                                escape_rows.append(row)
                            except Exception:
                                try_recover(bot)
                                break
                            st_esc = safe_status(bot, st2)
                            pos_esc = st_esc.get("pos") or pos2
                            if float(pos_esc[1]) >= float(pos2[1]) + 2.0:
                                st2, pos2 = st_esc, pos_esc
                            if float(pos_esc[1]) >= 55.0:
                                recovered = True
                                break
                    escape_cooldown_until = now_loop + 300.0
                    fh.write(json.dumps({"event": "cave_escape", "step": step, "rows": escape_rows[-2:], "recovered": recovered, "last_surface": last_surface}, ensure_ascii=False) + "\n")
                    fh.flush()
                    st2 = safe_status(bot, st2)
                    pos2 = st2.get("pos") or pos2
                    if recovered:
                        stuck = 0
                    if time.monotonic() - started >= DURATION_S:
                        break

                # Observe surface blocks/entities everywhere we go.
                try:
                    jloads(toolset.mc_find_blocks(radius=24, limit=60))
                except Exception:
                    pass
                try:
                    jloads(toolset.mc_find_blocks(radius=24, types=POI_TYPES, limit=20))
                except Exception:
                    pass
                try:
                    jloads(toolset.mc_get_entities(radius=32))
                except Exception:
                    pass
                # Update anchors/chunks/biome and persist often.
                toolset.world_memory.observe_status(st2)
                if step % 3 == 0:
                    toolset.world_memory.save(force=True)
                row = {
                    "t": round(time.monotonic() - started, 1),
                    "step": step,
                    "dir": name,
                    "target": [round(tx, 1), round(tz, 1)],
                    "move_ok": moved_ok,
                    "dist": round(dist, 1),
                    "pos": [round(float(v), 1) for v in pos2],
                    "health": st2.get("health"),
                    "food": st2.get("food"),
                    "dim": st2.get("dimension"),
                    "biome": st2.get("biome"),
                    "chunks": len(toolset.world_memory.visited_chunks),
                    "pois": len(toolset.world_memory.pois),
                    "containers": len(toolset.world_memory.containers),
                    "habitats": sum(len(v) for v in toolset.world_memory.habitats.values()),
                    "stuck": stuck,
                }
                traj.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                if step % 5 == 0:
                    try:
                        hud = build_world_hud(toolset, task="自由地表探索")
                        fh.write(json.dumps({"event": "hud", "step": step, "text": hud}, ensure_ascii=False) + "\n")
                        fh.flush()
                    except Exception:
                        pass
                if stuck >= 3:
                    # Try a lateral escape before continuing.
                    try:
                        jloads(toolset.mc_skill_flee(distance=8))
                    except Exception:
                        pass
                    stuck = 0
                time.sleep(2.0)
        toolset.world_memory.save(force=True)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            toolset.world_memory.save(force=True)
            report["memory"] = {
                "spawn_points": toolset.world_memory.spawn_points,
                "bed_spawn": toolset.world_memory.bed_spawn,
                "home_bases": toolset.world_memory.home_bases,
                "portals": toolset.world_memory.portals,
                "last_death": toolset.world_memory.last_death,
                "containers": [
                    {"pos": [k[1], k[2], k[3]], "items": v.get("items", {})}
                    for k, v in toolset.world_memory.containers.items()
                ],
                "pois": [
                    {"name": e.name, "pos": list(e.pos), "dim": e.dim}
                    for e in toolset.world_memory.pois.values()
                ],
                "biomes": list(toolset.world_memory.biomes),
                "habitats": {
                    f"{k[0]}:{k[1]}": len(v) for k, v in toolset.world_memory.habitats.items()
                },
                "visited_chunks": [[k[0], k[1], k[2]] for k in toolset.world_memory.visited_chunks],
            }
            report["trajectory"] = traj
            st_final = safe_status(bot, {})
            report["final_status"] = {
                k: st_final.get(k)
                for k in ("ready", "pos", "health", "food", "alive", "dimension", "biome")
            }
        except Exception as exc:  # noqa: BLE001
            report["final_error"] = str(exc)
        bot.close()

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report.get("ok"), "steps": len(traj), "report": str(REPORT_PATH), "jsonl": str(JSONL_PATH)}, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
