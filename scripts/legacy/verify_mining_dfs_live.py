#!/usr/bin/env python3
"""Live end-to-end verification of MiningDFS on nju-beast using a dedicated isolated bot (MineDFS_01).

Strictly isolated from:
- BeastBot_01 / mc-live / mc-console
- FightBot_01 & teammate combat arena at (810, 120, 810)
- Main island around (-158, 76, -374)

Sandbox mining zone: (-820..-780, 62..84, 780..820)
"""

from __future__ import annotations

import json
import math
import pathlib
import subprocess
import sys
import time
import urllib.request
from typing import Any

from mc_agent.auth import McAuthClient, TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.errors import ApiError
from mc_agent.skills.library import MacroSkills

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runs/verify-mining-dfs-20261116.json"
BOT_NAME = "MineDFS_01"

MIN_X, MAX_X = -820, -780
MIN_Z, MAX_Z = 780, 820
SURFACE_Y = 80
COLLAR_X, COLLAR_Y, COLLAR_Z = -800, 81, 800
ARENA_SEL = "x=-825,y=60,z=775,dx=50,dy=30,dz=50"


def rcon_batch(commands: list[str], *, timeout: int = 35) -> list[dict[str, str]]:
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
        raise RuntimeError(f"RCON SSH failed ({proc.returncode}): {proc.stderr.strip()[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def setup_mining_sandbox() -> None:
    cmds = [
        f"forceload add {MIN_X - 4} {MIN_Z - 4} {MAX_X + 4} {MAX_Z + 4}",
        # Clear any old entities/blocks in sandbox column
        f"kill @e[{ARENA_SEL},type=!player]",
        f"fill {MIN_X} 62 {MIN_Z} {MAX_X} 71 {MAX_Z} stone",
        f"fill {MIN_X} 72 {MIN_Z} {MAX_X} 79 {MAX_Z} stone",
        f"fill {MIN_X} {SURFACE_Y} {MIN_Z} {MAX_X} {SURFACE_Y} {MAX_Z} grass_block",
        f"fill {MIN_X} 81 {MIN_Z} {MAX_X} 83 {MAX_Z} air",
        f"fill {MIN_X} 84 {MIN_Z} {MAX_X} 84 {MAX_Z} sea_lantern",
        # Trap 1: North descent from (-800, 81, 800) hits cave + lava at z=796 after 3 safe steps!
        # Step 1: (-800, 80, 799), Step 2: (-800, 79, 798), Step 3: (-800, 78, 797), next support (-800, 75, 796) is lava/air!
        "setblock -800 78 796 air",
        "setblock -800 77 796 air",
        "setblock -800 76 796 air",
        "setblock -800 75 796 lava",
        # Switchback at (-800, 78, 797) turns west (left of north): steps down 6 levels to (-806, 72, 797) Spine!
        # Along Spine at y=72, first branch_leaf goes south (+Z from 797) or north (-Z from 797),
        # wait: LEFT_RIGHT_DIRS['west'] = ('south', 'north').
        # Place target iron_ore + hanging iron_ore + deferred diamond_ore along BOTH south (+Z) and north (-Z)
        # side walls of (-806, 72, 797) so VeinHook hits them deterministically!
        # Spine node at (-806, 72, 797):
        # 1) Deferred diamond_ore right beside Spine anchor -> exposed immediately, spared by stone_pickaxe!
        "setblock -805 73 797 diamond_ore",
        # 2) South leaf branch (-806, 72, 798..803):
        #    First exposed iron_ore is hanging at (-805, 73, 798) over air at (-805, 72, 798) -> guarantees Catch-Block!
        "setblock -805 73 798 iron_ore",
        "setblock -805 72 798 air",
        "setblock -805 71 798 stone",
        "setblock -807 72 799 iron_ore",
        "setblock -807 73 799 iron_ore",
        "setblock -807 72 800 iron_ore",
        "setblock -807 73 800 iron_ore",
        # Extra fallback along west spine_forward
        "setblock -808 73 796 iron_ore",
        "setblock -809 72 796 iron_ore",
        # Gear & clear starting position for MineDFS_01
        f"clear {BOT_NAME}",
        f"effect clear {BOT_NAME}",
        f"effect give {BOT_NAME} minecraft:instant_health 1 10 true",
        f"effect give {BOT_NAME} minecraft:saturation 1 10 true",
        f"give {BOT_NAME} minecraft:stone_pickaxe 1",
        f"give {BOT_NAME} minecraft:cobblestone 64",
        f"give {BOT_NAME} minecraft:torch 16",
        f"tp {BOT_NAME} {COLLAR_X + 0.5} {COLLAR_Y} {COLLAR_Z + 0.5} 0 0",
    ]
    for i in range(0, len(cmds), 15):
        rcon_batch(cmds[i : i + 15], timeout=40)


def teardown_mining_sandbox() -> None:
    cmds = [
        f"kill @e[{ARENA_SEL},type=!player]",
        f"fill {MIN_X} 62 {MIN_Z} {MAX_X} 71 {MAX_Z} air",
        f"fill {MIN_X} 72 {MIN_Z} {MAX_X} 84 {MAX_Z} air",
        f"forceload remove {MIN_X - 4} {MIN_Z - 4} {MAX_X + 4} {MAX_Z + 4}",
    ]
    rcon_batch(cmds, timeout=40)


def wait_settled(bot: BotSession, x: float, y: float, z: float, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            st = bot.status()
            p = st.get("pos") or [0, 0, 0]
            if st.get("ready") and st.get("on_ground") and abs(p[0] - x) < 2.5 and abs(p[1] - y) < 1.5 and abs(p[2] - z) < 2.5:
                return st
        except ApiError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{BOT_NAME} failed to settle near ({x}, {y}, {z})")


def inv_counts(bot: BotSession) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in bot.inventory().get("items") or []:
        n = row.get("name")
        if n:
            out[n] = out.get(n, 0) + int(row.get("count") or 0)
    return out


def main() -> None:
    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    if not settings.skin_password:
        raise SystemExit("MC_SKIN_PASSWORD missing in .env.beast")
    token_store = TokenStore(settings.state_dir / "minedfs_tokens.json")
    with McAuthClient(base_url=settings.mc_api_base_url, token_store=token_store) as auth:
        outcome = auth.authenticate(BOT_NAME, settings.skin_password, force_refresh=False)

    bot = BotSession(
        outcome.record.token,
        bot_username=BOT_NAME,
        base_url=settings.mc_api_base_url,
        timeout=60.0,
    )
    report: dict[str, Any] = {"bot": BOT_NAME}
    try:
        bot.wait_until_ready(timeout_s=45.0)
        setup_mining_sandbox()
        st0 = wait_settled(bot, COLLAR_X + 0.5, COLLAR_Y, COLLAR_Z + 0.5, timeout_s=20.0)
        for _ in range(10):
            try:
                if bot.call("get_block", {"pos": [-800, 80, 799]}).get("name") == "grass_block":
                    break
            except ApiError:
                pass
            time.sleep(0.25)
        report["start_pos"] = st0.get("pos")

        skills = MacroSkills(bot)
        # Phase 1: mine iron with stone_pickaxe -> trap triggers breach seal + switchback; hanging iron triggers Catch-Block; diamond deferred!
        res_iron = {}
        for slice_idx in range(3):
            res_iron = skills.mine_dfs(target="iron", goal_count=3, target_y=72, max_duration_s=35.0)
            if res_iron.get("stop_reason") != "time_slice_progress":
                break
        cnt_after_iron = inv_counts(bot)
        diamond_remained = bot.call("get_block", {"pos": [-805, 73, 797]}).get("name")
        report["phase1_iron"] = {
            "res": res_iron,
            "raw_iron_count": cnt_after_iron.get("raw_iron", 0),
            "diamond_block_still_intact": diamond_remained == "diamond_ore",
        }

        # Phase 2: Give iron_pickaxe alongside existing stone_pickaxe, intentionally equip iron_pickaxe first,
        # then ask for diamond -> verifies dual-pickaxe rock downgrade + deferred diamond harvest!
        rcon_batch([f"give {BOT_NAME} minecraft:iron_pickaxe 1"])
        time.sleep(0.4)
        bot.call("equip", {"item": "iron_pickaxe", "slot": "hand"})
        res_diamond = {}
        for _ in range(2):
            res_diamond = skills.mine_dfs(target="diamond", goal_count=1, target_y=72, max_duration_s=35.0)
            if res_diamond.get("stop_reason") != "time_slice_progress":
                break
        cnt_after_diamond = inv_counts(bot)
        report["phase2_diamond"] = {
            "res": res_diamond,
            "diamond_count": cnt_after_diamond.get("diamond", 0),
        }

        # Phase 3: Lossless zero-dig backtrack to Collar
        res_ret = skills.mine_dfs(action="return_collar")
        report["phase3_return_collar"] = res_ret
        ret_pos = res_ret.get("pos_after") or [0, 0, 0]
        report["checks"] = {
            "switchback_created": any(n["kind"] == "switchback" for n in res_ret.get("tree", {}).get("nodes", []))
            or res_iron.get("breaches_sealed", 0) > 0,
            "breach_sealed": res_iron.get("breaches_sealed", 0) > 0 or res_ret.get("tree", {}).get("sealed_count", 0) > 0,
            "catch_block_used": res_ret.get("tree", {}).get("catch_blocks_placed", 0) > 0,
            "deferred_diamond_spared": diamond_remained == "diamond_ore",
            "raw_iron_gained": cnt_after_iron.get("raw_iron", 0),
            "diamond_gained": cnt_after_diamond.get("diamond", 0),
            "stone_pick_used_on_rock": res_ret.get("tree", {}).get("stone_pick_rock_equips", 0) >= 1,
            "returned_to_collar": abs(float(ret_pos[0]) - (COLLAR_X + 0.5)) <= 2.0 and abs(float(ret_pos[1]) - COLLAR_Y) <= 1.5 and abs(float(ret_pos[2]) - (COLLAR_Z + 0.5)) <= 2.0,
            "dug_during_return": res_ret.get("dug_during_return", -1),
        }
    finally:
        try:
            bot.call("stop", {}, allow_reauth=False)
        except Exception:
            pass
        try:
            bot.call("auth/logout", {}, allow_reauth=False)
        except Exception:
            pass
        bot.close()
        teardown_mining_sandbox()

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(REPORT_PATH), "checks": report.get("checks")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
