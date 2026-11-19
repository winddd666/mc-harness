#!/usr/bin/env python3
"""Real-world competition-fidelity single-concurrency CurriculumSupervisor endurance runner.

- Uses dedicated `MapTest_01` (reusing `.state-maptest/tokens.json` without re-login).
- Picks a dry land biome >= 2000m away from all other online bots on `nju-beast`.
- Zero artificial survival buffs (real normal-difficulty mobs, real hunger, real Y=-54 caves).
- Enforces `RULES.md` 220m `VoidGeofence`, `enable_pre_ensure=True`, `enable_island_recon=True`,
  and `enable_async_world_jobs=True`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

WT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_ROOT = pathlib.Path("/home/xzq/projects/mc")
sys.path.insert(0, str(WT_ROOT / "src"))

BOT = "MapTest_01"
TOKEN_FILE = MAIN_ROOT / ".state-maptest" / "tokens.json"
ENV_FILE = MAIN_ROOT / ".env.beast"

LIVE_STATE_DIR = WT_ROOT / ".state-l3-live"
RUNS_DIR = WT_ROOT / "runs"

CANDIDATE_SITES: list[tuple[int, int]] = [
    (-6400, -6400),
    (6400, -6400),
    (-6400, 6400),
    (-7800, -2800),
    (7800, -2800),
    (-7800, 2800),
    (-5200, -7600),
    (5200, -7600),
    (-8800, 400),
    (8800, -400),
]


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
            "ssh",
            "-T",
            "-F",
            "/home/xzq/.ssh/config",
            "nju-beast",
            "wsl",
            "-d",
            "Ubuntu-22.04",
            "-u",
            "xia",
            "--",
            "python3",
            "-",
        ],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"RCON SSH failed: {proc.stderr.strip()[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def select_dry_isolated_wild_site(bot: Any, bot_name: str, start_idx: int = 0) -> tuple[float, float, float, str]:
    """Find a dry land surface spawn >= 2000m from other bots using vanilla `spreadplayers`."""
    for offset in range(len(CANDIDATE_SITES)):
        cx, cz = CANDIDATE_SITES[(start_idx + offset) % len(CANDIDATE_SITES)]
        rcon_batch(
            [
                f"tp {bot_name} {cx} 120 {cz}",
                f"spreadplayers {cx} {cz} 0 32 false {bot_name}",
            ],
            timeout=45,
        )
        time.sleep(2.0)
        st = bot.status()
        pos = st.get("pos") or [cx, 64, cz]
        biome = str(st.get("biome") or "").lower()
        in_water = bool(st.get("in_water"))
        # Skip pure ocean spawns so we land on a true terrestrial island/continent
        if in_water or ("ocean" in biome) or ("river" in biome and float(pos[1]) <= 63.0):
            continue
        # Ensure tree logs exist within 48m so bare-hand bootstrap isn't on a barren sandbar
        fb = bot.call(
            "find_blocks",
            {
                "radius": 48,
                "types": [
                    "oak_log",
                    "birch_log",
                    "spruce_log",
                    "dark_oak_log",
                    "acacia_log",
                    "jungle_log",
                    "cherry_log",
                    "mangrove_log",
                ],
                "limit": 3,
            },
        )
        if not (fb or {}).get("blocks"):
            continue
        return float(pos[0]), float(pos[1]), float(pos[2]), biome
    raise RuntimeError("Could not find a wooded dry wild spawn site across CANDIDATE_SITES")


def reset_wild_bot(bot_name: str, sx: float, sy: float, sz: float, *, set_day: bool = True) -> None:
    cmds = [
        "difficulty normal",
        "weather clear",
        f"tp {bot_name} {sx:.1f} {sy:.1f} {sz:.1f}",
        f"spawnpoint {bot_name} {int(round(sx))} {int(round(sy))} {int(round(sz))}",
        f"gamemode survival {bot_name}",
        f"clear {bot_name}",
        f"effect clear {bot_name}",
        f"effect give {bot_name} minecraft:instant_health 1 10 true",
        f"effect give {bot_name} minecraft:saturation 2 10 true",
        f"advancement revoke {bot_name} everything",
        f"advancement grant {bot_name} only minecraft:story/root",
        f"advancement grant {bot_name} only minecraft:adventure/root",
        f"advancement grant {bot_name} only minecraft:husbandry/root",
        f"advancement grant {bot_name} only minecraft:nether/root",
        f"advancement grant {bot_name} only minecraft:end/root",
    ]
    if set_day:
        cmds.insert(0, "time set 1000")
    rcon_batch(cmds, timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", type=str, default=BOT)
    parser.add_argument("--token-file", type=str, default=str(TOKEN_FILE))
    parser.add_argument("--rpm-budget", type=int, default=None, help="Per-process RPM cap (e.g. 12 for 2x concurrency)")
    parser.add_argument("--runs-subdir", type=str, default="", help="Optional subdirectory under runs/ for multi-bot isolation")
    parser.add_argument("--duration-s", type=float, default=1800.0, help="Wall-clock budget for this wild run")
    parser.add_argument("--max-episodes", type=int, default=12)
    parser.add_argument("--site-index", type=int, default=0)
    parser.add_argument("--tag", type=str, default="wild")
    parser.add_argument("--keep-time", action="store_true", help="Do not reset world time to morning")
    args = parser.parse_args()

    bot_name = args.bot
    token_file = pathlib.Path(args.token_file)
    runs_dir = (RUNS_DIR / args.runs_subdir) if args.runs_subdir else RUNS_DIR
    LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mem_path = LIVE_STATE_DIR / f"world_memory_{bot_name}_{args.tag}_{stamp}.json"
    os.environ["MC_WORLD_MEMORY_PATH"] = str(mem_path)

    import mc_agent
    from mc_agent.auth import TokenStore
    from mc_agent.bot import BotSession
    from mc_agent.config import Settings
    from mc_agent.errors import ApiError
    from mc_agent.harness.curriculum import CurriculumSupervisor
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.harness.model import QwenModel, QwenSpec
    from mc_agent.skills import total_owned_counts

    assert pathlib.Path(mc_agent.__file__).resolve().is_relative_to(WT_ROOT / "src")

    settings = Settings.load(dotenv_path=ENV_FILE)
    rec = TokenStore(token_file).get(bot_name)
    if rec is None:
        raise SystemExit(f"Missing cached token for {bot_name} in {token_file}")

    out_file = runs_dir / f"wild-endurance-{args.tag}-{stamp}.json"

    with BotSession(rec.token, bot_username=rec.bot_username, base_url=settings.mc_api_base_url) as bot:
        try:
            st = bot.status()
        except ApiError as exc:
            if exc.code == "not_connected":
                st = {"ready": False}
            else:
                raise
        if not st.get("ready"):
            bot.connect(respect_reconnect_interval=False)
            st = bot.wait_until_ready(timeout_s=45.0)

        sx, sy, sz, biome = select_dry_isolated_wild_site(bot, bot_name, start_idx=args.site_index)
        reset_wild_bot(bot_name, sx, sy, sz, set_day=not args.keep_time)
        time.sleep(1.5)

        geofence = VoidGeofence(center_x=sx, center_z=sz, radius=220.0, margin=8.0)
        toolset = McToolset(bot, geofence=geofence, wait_for_cooldown=True)

        print(
            f"[Wild Run Start] bot={bot_name} tag={args.tag} rpm={args.rpm_budget or 'default'} "
            f"spawn=({sx:.1f}, {sy:.1f}, {sz:.1f}) biome={biome} "
            f"duration_s={args.duration_s} max_episodes={args.max_episodes}",
            flush=True,
        )

        qwen = QwenModel(
            QwenSpec(
                dotenv_path=str(ENV_FILE),
                fallback_dotenv_path=str(MAIN_ROOT / ".env"),
                rpm_budget_override=args.rpm_budget,
            )
        )
        try:
            sup = CurriculumSupervisor(
                bot,
                chat_fn=qwen.chat,
                model_name=f"{qwen.model}@{qwen.base_url}",
                runs_dir=runs_dir,
                duration_s=args.duration_s,
                toolset=toolset,
                geofence=geofence,
                enable_pre_ensure=True,
                enable_island_recon=True,
                enable_async_world_jobs=True,
            )
            summary = sup.run(max_episodes=args.max_episodes)
        finally:
            qwen.close()

        # Enrich episode rows with guardian reflex stats from each episode report JSON
        guardian_totals = {"incidents": 0, "tool_preemptions": 0, "boundary_stabilizations": 0, "cancelled_generations": 0}
        for ep in summary.get("episodes", []):
            rfile = ep.get("report_file")
            if rfile and pathlib.Path(rfile).is_file():
                try:
                    rdata = json.loads(pathlib.Path(rfile).read_text(encoding="utf-8"))
                    gstats = rdata.get("guardian") or {}
                    ep["guardian"] = gstats
                    ep["pre_ensure"] = rdata.get("pre_ensure") or rdata.get("zero_llm_pre_ensure")
                    for gk in guardian_totals:
                        guardian_totals[gk] += int(gstats.get(gk, 0) or 0)
                except Exception:
                    pass

        try:
            inv_end = bot.inventory()
            st_end = bot.status()
        except ApiError:
            try:
                bot.connect(respect_reconnect_interval=False)
                st_end = bot.wait_until_ready(timeout_s=30.0)
                inv_end = bot.inventory()
            except Exception:
                inv_end = {}
                st_end = {}
        payload = {
            "bot": bot_name,
            "tag": args.tag,
            "rpm_budget": args.rpm_budget,
            "started_at": stamp,
            "spawn": {"x": round(sx, 1), "y": round(sy, 1), "z": round(sz, 1), "biome": biome},
            "end_status": {
                "pos": st_end.get("pos"),
                "health": st_end.get("health"),
                "food": st_end.get("food"),
                "is_day": st_end.get("is_day"),
            },
            "owned_end": total_owned_counts(inv_end),
            "memory_stats": {
                "pois": len(toolset.world_memory.pois),
                "containers": len(toolset.world_memory.containers),
                "habitats": sum(len(v) for v in toolset.world_memory.habitats.values()),
                "visited_chunks": len(toolset.world_memory.visited_chunks),
            },
            "guardian_totals": guardian_totals,
            "curriculum_summary": summary,
        }
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"\n[Wild Run Complete] {out_file} | final_score={summary.get('final_score')} "
            f"| completed_advs={summary.get('completed_count')} | guardian={guardian_totals}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
