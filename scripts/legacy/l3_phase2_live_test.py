#!/usr/bin/env python3
"""Live end-to-end verification of Phase-2 L3/L4 Core Architecture on nju-beast (Minecraft 1.21.11).

Uses dedicated bot `MapTest_01` (reusing cached token in `.state-maptest/tokens.json` without re-login)
at isolated sky platform (-3800, 150, 3800).
Writes all memory/logs strictly inside `.worktrees/l3-sched/`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
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


def reset_base_arena(*, resistance: bool = True) -> list[dict[str, str]]:
    bx, by, bz = BASE
    fy = by - 1
    cmds = [
        f"forceload add {bx - 16} {bz - 16} {bx + 48} {bz + 24}",
        f"fill {bx - 12} {fy - 2} {bz - 12} {bx + 42} {fy} {bz + 16} minecraft:dirt",
        f"fill {bx - 12} {fy} {bz - 12} {bx + 42} {fy} {bz + 16} minecraft:grass_block",
        f"fill {bx - 12} {by} {bz - 12} {bx + 42} {by + 9} {bz + 16} minecraft:air",
        # Invisible perimeter barrier walls
        f"fill {bx - 12} {by} {bz - 12} {bx + 42} {by + 4} {bz - 12} minecraft:barrier",
        f"fill {bx - 12} {by} {bz + 16} {bx + 42} {by + 4} {bz + 16} minecraft:barrier",
        f"fill {bx - 12} {by} {bz - 12} {bx - 12} {by + 4} {bz + 16} minecraft:barrier",
        f"fill {bx + 42} {by} {bz - 12} {bx + 42} {by + 4} {bz + 16} minecraft:barrier",
        f"kill @e[type=!player,x={bx},y={by},z={bz},distance=..70]",
        "time set day",
        "weather clear",
        "difficulty normal",
        f"tp {BOT} {bx + 0.5} {by} {bz + 0.5}",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"effect give {BOT} minecraft:instant_health 1 10 true",
        f"effect give {BOT} minecraft:saturation 3600 10 true",
        f"advancement revoke {BOT} everything",
        f"advancement grant {BOT} only minecraft:story/root",
        f"advancement grant {BOT} only minecraft:adventure/root",
        f"advancement grant {BOT} only minecraft:husbandry/root",
    ]
    if resistance:
        cmds.extend(
            [
                f"effect give {BOT} minecraft:resistance 3600 4 true",
                f"effect give {BOT} minecraft:regeneration 3600 2 true",
            ]
        )
    return rcon_batch(cmds, timeout=90)


def cleanup_forceload() -> None:
    bx, by, bz = BASE
    try:
        rcon_batch(
            [
                f"kill @e[type=!player,x={bx},y={by},z={bz},distance=..70]",
                f"forceload remove {bx - 16} {bz - 16} {bx + 48} {bz + 24}",
            ],
            timeout=30,
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["all", "a", "b"], default="all")
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
    from mc_agent.harness.curriculum import (
        CurriculumSupervisor,
        WorldJobRegistry,
        score_completed_advancements,
    )
    from mc_agent.harness.events import EventSink
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.guardian import GenerationGuard
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.harness.model import QwenModel, QwenSpec
    from mc_agent.skills import CombatEnv, total_owned_counts
    from mc_agent.skills.ensure import PhysicalPrerequisiteResolver

    assert pathlib.Path(mc_agent.__file__).resolve().is_relative_to(WT_ROOT / "src"), (
        f"Not using isolated workspace: {mc_agent.__file__}"
    )

    settings = Settings.load(dotenv_path=ENV_FILE)
    rec = TokenStore(TOKEN_FILE).get(BOT)
    if rec is None:
        raise SystemExit(f"Missing cached token for {BOT} in {TOKEN_FILE}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = RUNS_DIR / f"l3-phase2-live-{stamp}.json"
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
        print(f"[{'PASS' if cond else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:280]}", flush=True)

    bx, by, bz = BASE
    fy = by - 1
    arena_geofence = VoidGeofence(center_x=bx + 12, center_z=bz + 2, radius=32.0, margin=2.0)

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

        if args.phase in ("all", "a"):
            # =========================================================================
            # A1: Live `CombatEnv` 3D LOS, Ray Probe, and Safe Yaw Scan on 1.21.11
            # =========================================================================
            reset_base_arena(resistance=True)
            rcon_batch(
                [
                    # 2-high solid stone wall north at bz - 2
                    f"fill {bx - 2} {by} {bz - 2} {bx + 2} {by + 1} {bz - 2} minecraft:stone",
                    # Walled husk behind north wall at bz - 4
                    f"summon minecraft:husk {bx + 0.5} {by} {bz - 3.5} {{NoAI:1b,Silent:1b,PersistenceRequired:1b}}",
                    # Open husk to the east at bx + 4.5
                    f"summon minecraft:zombie {bx + 4.5} {by} {bz + 0.5} {{NoAI:1b,Silent:1b,PersistenceRequired:1b,ArmorItems:[{{}},{{}},{{}},{{id:\"minecraft:leather_helmet\",count:1}}]}}",
                ]
            )
            time.sleep(1.2)
            toolset = McToolset(bot, geofence=arena_geofence)
            cenv = CombatEnv(bot, skills=toolset.skills, geofence=arena_geofence)
            st_a1 = bot.status()
            pos_a1 = st_a1.get("pos") or [bx + 0.5, by, bz + 0.5]
            ents = (bot.call("get_entities", {"radius": 12}) or {}).get("entities") or []
            z_open = next((e for e in ents if str(e.get("name", "")).endswith("zombie")), None)
            h_wall = next((e for e in ents if str(e.get("name", "")).endswith("husk")), None)
            los_open = cenv.has_los(pos_a1, z_open) if z_open else {"clear": False}
            los_wall = cenv.has_los(pos_a1, h_wall) if h_wall else {"clear": True}
            ray_north = cenv.probe_ray(pos_a1, 0.0, dist=5.0)  # yaw=0 faces -Z (north wall at bz-2!)
            ray_east = cenv.probe_ray(pos_a1, -math.pi / 2, dist=5.0)  # yaw=-pi/2 faces +X (open grass!)
            safe_scan = cenv.scan_safe_yaws(pos_a1, 0.0, dist=3.5)
            check(
                "A1_live_combat_env_probes",
                {
                    "los_open_zombie": los_open,
                    "los_walled_husk": los_wall,
                    "ray_north": ray_north,
                    "ray_east": ray_east,
                    "safe_scan_best": safe_scan.get("best_yaw"),
                    "cornered": safe_scan.get("cornered"),
                },
                bool(
                    los_open.get("clear") is True
                    and los_wall.get("clear") is False
                    and ray_north.get("blocked_by_wall") is True
                    and ray_east.get("safe") is True
                    and ray_east.get("safe_dist", 0.0) >= 4.0
                    and safe_scan.get("cornered") is False
                ),
            )

            # =========================================================================
            # A2: Live Spinal Reflex Mid-Tool Preemption (Chopping Tree -> Zombie Ambush -> 0-LLM Counter-Kill!)
            # =========================================================================
            reset_base_arena(resistance=False)
            rcon_batch(
                [
                    f"tp {BOT} {bx + 0.5} {by} {bz + 0.5}",
                    f"give {BOT} minecraft:iron_sword 1",
                    f"give {BOT} minecraft:shield 1",
                    f"give {BOT} minecraft:cooked_beef 6",
                    f"fill {bx - 2} {by} {bz} {bx - 2} {by + 6} {bz} minecraft:oak_log",
                ]
            )
            time.sleep(0.8)
            bot.call("equip", {"item": "shield", "slot": "off-hand"})

            def _ambush_zombie() -> None:
                time.sleep(0.9)
                try:
                    rcon_batch(
                        [
                            f'summon minecraft:zombie {bx - 0.8} {by} {bz + 0.5} '
                            '{PersistenceRequired:1b,Health:10f,ArmorItems:[{},{},{},{id:"minecraft:leather_helmet",count:1}]}'
                        ],
                        timeout=20,
                    )
                except Exception:
                    pass

            evts_a2: list[dict[str, Any]] = []
            guard_a2 = GenerationGuard(
                bot,
                EventSink(evts_a2.append),
                checkpoint=lambda: None,
                geofence=arena_geofence,
                skills=toolset.skills,
            )
            t_amb = threading.Thread(target=_ambush_zombie, daemon=True)
            t_amb.start()
            chop_out = guard_a2.run_tool_with_reflex(
                "mc_skill_harvest_logs",
                lambda: json.dumps(toolset.skills.harvest_logs(target_logs=6, radius=10), ensure_ascii=False),
            )
            t_amb.join(timeout=5.0)
            chop_parsed = json.loads(chop_out) if isinstance(chop_out, str) else chop_out
            preempted_a2 = bool(isinstance(chop_parsed, dict) and chop_parsed.get("preempted_by_reflex")) or guard_a2.stats.get("tool_preemptions", 0) >= 1
            ents_after_a2 = (bot.call("get_entities", {"radius": 14}) or {}).get("entities") or []
            zombies_left = [e for e in ents_after_a2 if str(e.get("name", "")).endswith("zombie")]
            st_after_a2 = bot.status()
            check(
                "A2_live_spinal_preempt_mid_chop_counter_kill",
                {
                    "preempted": preempted_a2,
                    "zombies_remaining": len(zombies_left),
                    "alive": st_after_a2.get("alive"),
                    "health_after": st_after_a2.get("health"),
                    "receipt_preview": str(chop_out)[:200],
                },
                bool(preempted_a2 is True and len(zombies_left) == 0 and st_after_a2.get("alive") is True),
            )

            # =========================================================================
            # A3: Live Pre-Hurt Creeper Shield-Evade (Triggers BEFORE `health < 20`!)
            # =========================================================================
            reset_base_arena(resistance=True)
            rcon_batch(
                [
                    f"tp {BOT} {bx + 10.5} {by} {bz + 2.5}",
                    f"give {BOT} minecraft:shield 1",
                ]
            )
            time.sleep(0.6)
            bot.call("equip", {"item": "shield", "slot": "off-hand"})
            evts_a3: list[dict[str, Any]] = []
            guard_a3 = GenerationGuard(
                bot,
                EventSink(evts_a3.append),
                checkpoint=lambda: None,
                geofence=arena_geofence,
                skills=toolset.skills,
            )
            guard_a3.prime()
            rcon_batch(
                [f"summon minecraft:creeper {bx + 12.8} {by} {bz + 2.5} {{NoAI:1b,Silent:1b,PersistenceRequired:1b}}"]
            )
            time.sleep(0.8)
            incident_a3 = guard_a3.poll()
            st_after_a3 = bot.status()
            pos_a3 = st_after_a3.get("pos") or [bx + 10.5, by, bz + 2.5]
            dist_from_creeper = math.hypot(float(pos_a3[0]) - (bx + 12.8), float(pos_a3[2]) - (bz + 2.5))
            rcon_batch([f"kill @e[type=minecraft:creeper,x={bx + 12},y={by},z={bz + 2},distance=..25]"])
            reasons_a3 = (incident_a3 or {}).get("reasons") or []
            check(
                "A3_live_pre_hurt_creeper_shield_retreat",
                {
                    "triggered": incident_a3 is not None,
                    "reasons": reasons_a3,
                    "dist_from_creeper_after": round(dist_from_creeper, 2),
                    "health_unscathed": st_after_a3.get("health"),
                },
                bool(
                    incident_a3 is not None
                    and "pre_hurt_creeper" in reasons_a3
                    and dist_from_creeper >= 5.2
                    and float(st_after_a3.get("health") or 0.0) >= 19.5
                ),
            )

            # =========================================================================
            # A4 & A5: Live Stale Chest Self-Healing + Dual-Furnace Non-Blocking Stone WorldJob
            # =========================================================================
            reset_base_arena(resistance=True)
            rcon_batch(
                [
                    # Empty chest at (bx + 2, by, bz - 2) first to test stale `WorldMemory` self-healing!
                    f"setblock {bx + 2} {by} {bz - 2} minecraft:chest replace",
                    f"give {BOT} minecraft:stone_pickaxe 1",
                    # Oak logs & stone outcrop
                    f"fill {bx - 4} {by} {bz} {bx - 4} {by + 3} {bz} minecraft:oak_log",
                    f"fill {bx - 5} {by} {bz} {bx - 5} {by + 3} {bz} minecraft:oak_log",
                    f"fill {bx - 3} {by} {bz + 4} {bx + 3} {by} {bz + 8} minecraft:stone",
                ]
            )
            time.sleep(1.0)
            MEM_PATH.unlink(missing_ok=True)
            toolset = McToolset(bot, geofence=arena_geofence)
            # Inject stale phantom `iron_ingot: 8` into WorldMemory for the empty chest
            toolset.world_memory.containers[("overworld", bx + 2, by, bz - 2)] = {
                "block": "chest",
                "items": {"iron_ingot": 8},
                "updated_at": time.monotonic(),
            }
            t_a4 = time.monotonic()
            res_a4 = PhysicalPrerequisiteResolver(
                toolset.skills,
                geofence=arena_geofence,
                allow_underground_dfs=False,
            ).ensure_items({"iron_pickaxe": 1}, max_duration_s=25.0)
            dt_a4 = round(time.monotonic() - t_a4, 2)
            healed_items = toolset.world_memory.containers.get(("overworld", bx + 2, by, bz - 2), {}).get("items", {})
            check(
                "A4_live_stale_chest_self_heal_and_zero_blind_dfs",
                {
                    "elapsed_s": dt_a4,
                    "stop_reason": res_a4.get("stop_reason"),
                    "missing_hunt_items": res_a4.get("missing_hunt_items"),
                    "healed_chest_items": healed_items,
                },
                bool(
                    res_a4.get("ok") is False
                    and res_a4.get("stop_reason") == "requires_world_hunt"
                    and healed_items == {}
                    and dt_a4 < 12.0
                ),
            )

            # Now stock the chest with 9 iron_ingot, 1 redstone, 6 coal, sync via `mc_window_view`,
            # and verify `WorldJobRegistry` dual-furnace non-blocking ignition + soft collection!
            rcon_batch(
                [
                    f"item replace block {bx + 2} {by} {bz - 2} container.0 with minecraft:iron_ingot 9",
                    f"item replace block {bx + 2} {by} {bz - 2} container.1 with minecraft:redstone 1",
                    f"item replace block {bx + 2} {by} {bz - 2} container.2 with minecraft:coal 6",
                ]
            )
            time.sleep(0.5)
            toolset.mc_window_view(x=bx + 2, y=by, z=bz - 2, block="chest")
            world_jobs = WorldJobRegistry()
            resolver_a5 = PhysicalPrerequisiteResolver(
                toolset.skills,
                geofence=arena_geofence,
                allow_underground_dfs=False,
                world_jobs=world_jobs,
                parent_target_key="minecraft:adventure/use_lodestone",
                async_smelt_threshold_s=25.0,
            )
            t_a5_start = time.monotonic()
            res_a5_pass1 = resolver_a5.ensure_items({"lodestone": 1, "compass": 1}, max_duration_s=180.0)
            dt_a5_ignite = round(time.monotonic() - t_a5_start, 2)
            active_jobs = list(world_jobs.jobs.values())
            furnace_cnt = len(active_jobs[0].furnaces) if active_jobs else 0
            in_conv = world_jobs.in_conversion_counts(dim="overworld")
            leash_info = world_jobs.active_leash(dim="overworld")

            # Wait only for dual-furnace batch (~62s instead of 122s!) and collect!
            if active_jobs:
                wait_rem = max(0.0, active_jobs[0].ready_at - time.monotonic())
                print(f"[A5] Dual furnaces burning ({furnace_cnt} furnaces); waiting {wait_rem:.1f}s for soft ready collection...", flush=True)
                time.sleep(min(72.0, wait_rem + 1.5))
            collected_a5 = world_jobs.collect_ready_jobs(toolset.skills, dim="overworld")
            res_a5_pass2 = resolver_a5.ensure_items({"lodestone": 1, "compass": 1}, max_duration_s=90.0)
            owned_a5 = total_owned_counts(bot.inventory())
            check(
                "A5_live_world_job_dual_furnace_nonblocking_and_craft",
                {
                    "pass1_elapsed_s": dt_a5_ignite,
                    "pass1_stop_reason": res_a5_pass1.get("stop_reason"),
                    "furnaces_used": furnace_cnt,
                    "in_conversion_during_burn": in_conv,
                    "leash": leash_info,
                    "collected": collected_a5,
                    "pass2_ok": res_a5_pass2.get("ok"),
                    "owned_lodestone": owned_a5.get("lodestone", 0),
                    "owned_compass": owned_a5.get("compass", 0),
                },
                bool(
                    res_a5_pass1.get("stop_reason") == "waiting_for_world_job"
                    and furnace_cnt >= 2
                    and in_conv.get("stone", 0) >= 12
                    and leash_info is not None
                    and res_a5_pass2.get("ok") is True
                    and owned_a5.get("lodestone", 0) >= 1
                    and owned_a5.get("compass", 0) >= 1
                ),
            )

        if args.phase in ("all", "b"):
            qwen = QwenModel(
                QwenSpec(
                    dotenv_path=str(ENV_FILE),
                    fallback_dotenv_path=str(MAIN_ROOT / ".env"),
                )
            )
            try:
                # =========================================================================
                # B1: Live Qwen `bootstrap_iron_age` Objective Gate (`D2` + Immediate 0-Extra-Turn Stop!)
                # =========================================================================
                reset_base_arena(resistance=True)
                time.sleep(0.8)
                rcon_batch(
                    [
                        # Pre-grant historical `story/iron_tools` while bot only holds `stone_pickaxe` + `raw_iron` (post-death tool loss!)
                        f"advancement grant {BOT} only minecraft:story/iron_tools",
                        f"give {BOT} minecraft:stone_pickaxe 1",
                        f"give {BOT} minecraft:raw_iron 4",
                        f"give {BOT} minecraft:stick 4",
                        f"give {BOT} minecraft:coal 2",
                        f"setblock {bx + 2} {by} {bz} minecraft:crafting_table",
                        f"setblock {bx + 2} {by} {bz + 2} minecraft:furnace",
                    ]
                )
                time.sleep(1.0)
                MEM_PATH.unlink(missing_ok=True)
                ts_b1 = McToolset(bot, geofence=arena_geofence)
                sup_b1 = CurriculumSupervisor(
                    bot,
                    chat_fn=qwen.chat,
                    model_name=f"{qwen.model}@{qwen.base_url}",
                    runs_dir=RUNS_DIR,
                    duration_s=300.0,
                    toolset=ts_b1,
                    geofence=arena_geofence,
                    # Disable pre_ensure here so Qwen executes smelt -> craft_gear('iron_pickaxe') and tests `episode_gate` immediate stop!
                    enable_pre_ensure=False,
                )
                sum_b1 = sup_b1.run(max_episodes=1)
                ep_b1 = sum_b1["episodes"][0]
                inv_b1 = bot.inventory()
                owned_b1 = total_owned_counts(inv_b1)
                check(
                    "B1_live_qwen_bootstrap_iron_age_immediate_gate",
                    {
                        "target": ep_b1["target"],
                        "actor_requests": ep_b1["actor_requests"],
                        "objective_verified": ep_b1["objective_verified"],
                        "iron_pickaxe_owned": owned_b1.get("iron_pickaxe", 0),
                    },
                    bool(
                        ep_b1["target"] == "bootstrap_iron_age"
                        and ep_b1["objective_verified"] is True
                        and owned_b1.get("iron_pickaxe", 0) >= 1
                        and int(ep_b1["actor_requests"] or 99) <= 5
                    ),
                )

                # =========================================================================
                # B2: Live Qwen Multi-Episode Regression (`trade` + `lava_bucket` + Async `use_lodestone` `post_ensure_task`)
                # =========================================================================
                reset_base_arena(resistance=True)
                rcon_batch(
                    [
                        f"give {BOT} minecraft:iron_pickaxe 1",
                        f"give {BOT} minecraft:emerald 3",
                        # Storage chest with 9 iron_ingot, 1 redstone, 6 coal, 12 emerald
                        f"setblock {bx + 2} {by} {bz - 2} minecraft:chest replace",
                        f"item replace block {bx + 2} {by} {bz - 2} container.0 with minecraft:iron_ingot 9",
                        f"item replace block {bx + 2} {by} {bz - 2} container.1 with minecraft:redstone 1",
                        f"item replace block {bx + 2} {by} {bz - 2} container.2 with minecraft:coal 6",
                        f"item replace block {bx + 2} {by} {bz - 2} container.3 with minecraft:emerald 12",
                        # Logs & exposed stone
                        f"fill {bx - 4} {by} {bz} {bx - 4} {by + 3} {bz} minecraft:oak_log",
                        f"fill {bx - 5} {by} {bz} {bx - 5} {by + 3} {bz} minecraft:oak_log",
                        f"fill {bx - 3} {by} {bz + 4} {bx + 3} {by} {bz + 8} minecraft:stone",
                        # Contained stationary lava basin at (bx + 24, fy, bz + 4)
                        f"fill {bx + 23} {fy - 1} {bz + 3} {bx + 25} {fy} {bz + 5} minecraft:stone_bricks",
                        f"setblock {bx + 24} {fy} {bz + 4} minecraft:lava",
                        # Contained librarian villager at (bx + 30, by, bz - 4)
                        f"fill {bx + 29} {by} {bz - 5} {bx + 31} {by + 1} {bz - 3} minecraft:oak_fence",
                        f"fill {bx + 30} {by} {bz - 4} {bx + 30} {by + 1} {bz - 4} minecraft:air",
                        f"setblock {bx + 30} {by} {bz - 6} minecraft:lectern",
                        (
                            f'summon minecraft:villager {bx + 30}.5 {by} {bz - 4}.5 '
                            '{NoAI:1b,Silent:1b,Invulnerable:1b,VillagerData:{profession:"minecraft:librarian",level:2,type:"minecraft:plains"},'
                            'Offers:{Recipes:[{buy:{id:"minecraft:emerald",count:1},sell:{id:"minecraft:lantern",count:1},maxUses:12,xp:2}]}}'
                        ),
                    ]
                )
                time.sleep(1.2)
                MEM_PATH.unlink(missing_ok=True)
                ts_b2 = McToolset(bot, geofence=arena_geofence)
                ts_b2.mc_window_view(x=bx + 2, y=by, z=bz - 2, block="chest")
                ts_b2.mc_find_blocks(radius=40, types=["lava", "lectern", "chest"], limit=20)
                ts_b2.mc_get_entities(radius=45)

                sup_b2 = CurriculumSupervisor(
                    bot,
                    chat_fn=qwen.chat,
                    model_name=f"{qwen.model}@{qwen.base_url}",
                    runs_dir=RUNS_DIR,
                    duration_s=900.0,
                    toolset=ts_b2,
                    geofence=arena_geofence,
                    enable_pre_ensure=True,
                    enable_island_recon=False,
                    enable_async_world_jobs=True,
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
                    sup_b2.cooldowns[skip_k] = now_m + 3600.0

                sum_b2 = sup_b2.run(max_episodes=6)
                adv_b2 = bot.advancements(include_recipes=False)
                done_b2 = {
                    (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
                    for r in (adv_b2.get("advancements") or [])
                    if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
                }
                score_b2, bd_b2 = score_completed_advancements(done_b2)
                lode_eps = [
                    e
                    for e in sum_b2.get("episodes", [])
                    if e["target"] == "minecraft:adventure/use_lodestone" and e["status"] != "paused_for_world_job"
                ]
                lode_reqs = int(lode_eps[-1]["actor_requests"] or 99) if lode_eps else 99
                check(
                    "B2_live_qwen_curriculum_lodestone_lava_trade",
                    {
                        "episodes": [
                            {
                                "ep": e["episode"],
                                "target": e["target"],
                                "status": e["status"],
                                "reqs": e["actor_requests"],
                                "gained": e["gained_advancements"],
                            }
                            for e in sum_b2.get("episodes", [])
                        ],
                        "lodestone_final_reqs": lode_reqs,
                        "final_score": score_b2,
                        "breakdown": bd_b2,
                    },
                    bool(
                        "minecraft:adventure/use_lodestone" in done_b2
                        and "minecraft:story/lava_bucket" in done_b2
                        and "minecraft:adventure/trade" in done_b2
                        and lode_reqs <= 6
                        and score_b2 >= 45
                    ),
                )
            finally:
                qwen.close()

    cleanup_forceload()
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[L3 Phase-2 Live Report] {out_path} | ok={report['ok']}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
