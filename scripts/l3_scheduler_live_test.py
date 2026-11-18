#!/usr/bin/env python3
"""Live end-to-end verification of [L3-Sched-1/2/3] on nju-beast (Minecraft 1.21.11).

Uses idle bot `MapTest_01` at isolated sky platform (-3800, 150, 3800).
Writes all memory/logs strictly inside `.worktrees/l3-sched/` so the main workspace is untouched.
"""

from __future__ import annotations

import argparse
import json
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
MEM_PATH = LIVE_STATE_DIR / f"world_memory_{BOT}.json"
RUNS_DIR = WT_ROOT / "runs"

BASE = (-3800, 150, 3800)


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


def setup_arena() -> list[dict[str, str]]:
    bx, by, bz = BASE
    fy = by - 1
    cmds = [
        f"forceload add {bx - 16} {bz - 16} {bx + 48} {bz + 24}",
        # Clean arena & build solid grass/stone island
        f"fill {bx - 12} {fy - 2} {bz - 12} {bx + 42} {fy} {bz + 16} minecraft:dirt",
        f"fill {bx - 12} {fy} {bz - 12} {bx + 42} {fy} {bz + 16} minecraft:grass_block",
        f"fill {bx - 12} {by} {bz - 12} {bx + 42} {by + 8} {bz + 16} minecraft:air",
        f"kill @e[type=!player,x={bx},y={by},z={bz},distance=..60]",
        "time set day",
        "weather clear",
        f"tp {BOT} {bx} {by} {bz}",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"advancement revoke {BOT} everything",
        f"advancement grant {BOT} only minecraft:story/root",
        f"advancement grant {BOT} only minecraft:adventure/root",
        f"advancement grant {BOT} only minecraft:husbandry/root",
        f"effect give {BOT} minecraft:resistance 3600 4 true",
        f"effect give {BOT} minecraft:regeneration 3600 2 true",
        f"effect give {BOT} minecraft:saturation 3600 10 true",
        # 1) Storage chest at (bx + 2, by, bz - 2) containing 9 iron_ingot, 1 redstone, 4 coal, 12 emerald
        f"setblock {bx + 2} {by} {bz - 2} minecraft:chest replace",
        f"item replace block {bx + 2} {by} {bz - 2} container.0 with minecraft:iron_ingot 9",
        f"item replace block {bx + 2} {by} {bz - 2} container.1 with minecraft:redstone 1",
        f"item replace block {bx + 2} {by} {bz - 2} container.2 with minecraft:coal 4",
        f"item replace block {bx + 2} {by} {bz - 2} container.3 with minecraft:emerald 12",
        # 2) Oak logs column at (bx - 4, by..by+3, bz)
        f"fill {bx - 4} {by} {bz} {bx - 4} {by + 3} {bz} minecraft:oak_log",
        f"fill {bx - 5} {by} {bz} {bx - 5} {by + 3} {bz} minecraft:oak_log",
        # 3) Exposed stone outcrop (24 blocks) at (bx - 3..bx + 2, by, bz + 4..bz + 7)
        f"fill {bx - 3} {by} {bz + 4} {bx + 2} {by} {bz + 7} minecraft:stone",
        # 4) Contained stationary lava basin at (bx + 24, fy, bz + 4)
        f"fill {bx + 23} {fy - 1} {bz + 3} {bx + 25} {fy} {bz + 5} minecraft:stone_bricks",
        f"setblock {bx + 24} {fy} {bz + 4} minecraft:lava",
        # 5) Contained villager merchant with deterministic paper/emerald trade at (bx + 30, by, bz - 4)
        f"fill {bx + 29} {by} {bz - 5} {bx + 31} {by + 1} {bz - 3} minecraft:oak_fence",
        f"fill {bx + 30} {by} {bz - 4} {bx + 30} {by + 1} {bz - 4} minecraft:air",
        f"setblock {bx + 30} {by} {bz - 6} minecraft:lectern",
        (
            f'summon minecraft:villager {bx + 30}.5 {by} {bz - 4}.5 '
            '{NoAI:1b,Silent:1b,Invulnerable:1b,VillagerData:{profession:"minecraft:librarian",level:2,type:"minecraft:plains"},'
            'Offers:{Recipes:[{buy:{id:"minecraft:emerald",count:1},sell:{id:"minecraft:lantern",count:1},maxUses:12,xp:2}]}}'
        ),
    ]
    return rcon_batch(cmds, timeout=90)


def cleanup_forceload() -> None:
    bx, _, bz = BASE
    try:
        rcon_batch([f"forceload remove {bx - 16} {bz - 16} {bx + 48} {bz + 24}"], timeout=30)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["all", "a", "b"], default="all")
    parser.add_argument("--episodes", type=int, default=3)
    args = parser.parse_args()

    LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    MEM_PATH.unlink(missing_ok=True)
    os.environ["MC_WORLD_MEMORY_PATH"] = str(MEM_PATH)

    import mc_agent
    from mc_agent.auth import TokenStore
    from mc_agent.bot import BotSession
    from mc_agent.config import Settings
    from mc_agent.errors import ApiError
    from mc_agent.harness.curriculum import CurriculumSupervisor, score_completed_advancements
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.harness.model import QwenModel, QwenSpec
    from mc_agent.skills import total_owned_counts

    assert pathlib.Path(mc_agent.__file__).resolve().is_relative_to(WT_ROOT / "src"), (
        f"Not using isolated workspace: {mc_agent.__file__}"
    )

    settings = Settings.load(dotenv_path=ENV_FILE)
    rec = TokenStore(TOKEN_FILE).get(BOT)
    if rec is None:
        raise SystemExit(f"Missing cached token for {BOT} in {TOKEN_FILE}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = RUNS_DIR / f"l3-scheduler-live-{stamp}.json"
    report: dict[str, Any] = {
        "bot": BOT,
        "workspace": str(WT_ROOT),
        "mc_agent_file": str(mc_agent.__file__),
        "started_at": stamp,
        "checks": {},
        "ok": True,
    }

    def check(name: str, detail: Any, cond: bool) -> None:
        report["checks"][name] = {"ok": bool(cond), "detail": detail}
        if not cond:
            report["ok"] = False
        print(f"[{'PASS' if cond else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:260]}", flush=True)

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

        setup_arena()
        time.sleep(1.5)
        st = bot.status()

        toolset = McToolset(bot)
        toolset._update_status_cache(st)

        if args.phase in ("all", "a"):
            # A1: View nearby chest WITHOUT x/y/z -> verify coordinate auto-resolution into WorldMemory!
            wv = json.loads(toolset.mc_window_view(block="chest"))
            check(
                "A1_window_view_no_xyz_syncs_chest",
                {"containers": {str(k): v["items"] for k, v in toolset.world_memory.containers.items()}},
                wv.get("ok") is True and len(toolset.world_memory.containers) >= 1,
            )

            # A2: Bare-hands 0-LLM `ensure_items` full multi-stage chain:
            # chest withdraw (iron/redstone/coal) + chop oak_log + wooden_pickaxe + mine 12+ stone + furnace smelt 12 stone
            # + 3x3 stone_bricks -> stone_brick_slab -> chiseled_stone_bricks -> lodestone + compass + iron_boots(equipped)!
            t0 = time.monotonic()
            ens = toolset.skills.ensure_items(
                {"lodestone": 1, "compass": 1, "iron_boots": 1},
                equip_slots={"iron_boots": "feet"},
                max_duration_s=240.0,
            )
            dt = round(time.monotonic() - t0, 2)
            inv_after_a2 = bot.inventory()
            owned_a2 = total_owned_counts(inv_after_a2)
            adv_a2 = bot.advancements(include_recipes=False)
            done_a2 = {
                (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
                for r in (adv_a2.get("advancements") or [])
                if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
            }
            check(
                "A2_zero_llm_lodestone_compass_boots_chain",
                {
                    "elapsed_s": dt,
                    "ensure_ok": ens.get("ok"),
                    "steps_executed": ens.get("steps_executed"),
                    "remaining_deficit": ens.get("remaining_deficit"),
                    "owned_subset": {k: owned_a2.get(k, 0) for k in ("lodestone", "compass", "iron_boots", "wooden_pickaxe")},
                    "armor": inv_after_a2.get("armor"),
                    "completed_advs": sorted(done_a2),
                },
                bool(
                    ens.get("ok") is True
                    and owned_a2.get("lodestone", 0) >= 1
                    and owned_a2.get("compass", 0) >= 1
                    and owned_a2.get("iron_boots", 0) >= 1
                    and "minecraft:story/obtain_armor" in done_a2
                ),
            )

            # A3: Mob-drop boundary (`crossbow` needs `string` -> returns `requires_world_hunt` immediately)
            t_h0 = time.monotonic()
            ens_hunt = toolset.skills.ensure_items({"crossbow": 1}, max_duration_s=30.0)
            dt_h = round(time.monotonic() - t_h0, 2)
            check(
                "A3_mob_drop_clean_handback",
                {
                    "elapsed_s": dt_h,
                    "stop_reason": ens_hunt.get("stop_reason"),
                    "missing_hunt_items": ens_hunt.get("missing_hunt_items"),
                },
                ens_hunt.get("ok") is False
                and ens_hunt.get("stop_reason") == "requires_world_hunt"
                and "string" in (ens_hunt.get("missing_hunt_items") or {}),
            )

        if args.phase in ("all", "b"):
            # Grant an iron_pickaxe so Stage 0 bootstrap is satisfied and Stage 1 targets run directly;
            # walk slightly east to scan the lava basin & villager into persistent WorldMemory!
            rcon_batch([f"give {BOT} minecraft:iron_pickaxe 1"])
            time.sleep(0.8)
            toolset.mc_find_blocks(radius=40, types=["lava", "lectern", "chest"], limit=20)
            toolset.mc_get_entities(radius=45)

            # Cool down irrelevant unobtainable-on-sky-platform targets so the 3 episodes focus on
            # `use_lodestone` (15pt), `lava_bucket` (15pt), and WorldMemory-boosted `adventure/trade` (15pt)!
            qwen = QwenModel(
                QwenSpec(
                    dotenv_path=str(ENV_FILE),
                    fallback_dotenv_path=str(MAIN_ROOT / ".env"),
                )
            )
            try:
                sup = CurriculumSupervisor(
                    bot,
                    chat_fn=qwen.chat,
                    model_name=f"{qwen.model}@{qwen.base_url}",
                    runs_dir=RUNS_DIR,
                    duration_s=900.0,
                    toolset=toolset,
                    enable_pre_ensure=True,
                    enable_island_recon=False,
                )
                now_m = time.monotonic()
                for skip_k in (
                    "minecraft:story/shiny_gear",
                    "minecraft:husbandry/make_a_sign_glow",
                    "minecraft:story/deflect_arrow",
                    "minecraft:story/form_obsidian",
                    "minecraft:adventure/crafters_crafting_crafters",
                    "minecraft:adventure/ol_betsy",
                    "minecraft:husbandry/tactical_fishing",
                ):
                    sup.cooldowns[skip_k] = now_m + 3600.0

                summary = sup.run(max_episodes=args.episodes)
            finally:
                qwen.close()

            adv_b = bot.advancements(include_recipes=False)
            done_b = {
                (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
                for r in (adv_b.get("advancements") or [])
                if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
            }
            score_b, bd_b = score_completed_advancements(done_b)
            check(
                "B1_live_curriculum_multi_episode_scoring",
                {
                    "episodes": [
                        {
                            "ep": e["episode"],
                            "target": e["target"],
                            "budget": e["budget_class"],
                            "reqs": e["actor_requests"],
                            "gained": e["gained_advancements"],
                        }
                        for e in summary.get("episodes", [])
                    ],
                    "final_score": score_b,
                    "breakdown": bd_b,
                    "memory_pois_retained": len(toolset.world_memory.pois),
                },
                score_b >= 35
                and any(
                    k in done_b
                    for k in ("minecraft:adventure/use_lodestone", "minecraft:story/lava_bucket", "minecraft:adventure/trade")
                ),
            )

    cleanup_forceload()
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[L3 Live Report] {out_path} | ok={report['ok']}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
