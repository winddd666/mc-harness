#!/usr/bin/env python3
"""Live natural-chunk MiningDFS diamond expedition on nju-beast using isolated bot MineDFS_01.

Strictly isolated from:
- BeastBot_01 / mc-live / mc-console
- FightBot_01 & teammate combat arena around (810, 120, 810)

Untouched natural Overworld biome target: minecraft:plains around (2288, ~, -2224).
Initial gear: strictly 2 x stone_pickaxe + 2 x iron_pickaxe (0 initial cobblestone, 0 fake ores).
"""

from __future__ import annotations

import json
import math
import pathlib
import subprocess
import time
from typing import Any

from mc_agent.auth import McAuthClient, TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.errors import ApiError
from mc_agent.skills.library import MacroSkills, SHAFT_SEAL_BLOCKS, _inventory_counts_from_raw
from mc_agent.skills.mining_dfs import _pickaxe_inventory_summary

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runs/natural-diamond-dfs-20261116.json"
BOT_NAME = "MineDFS_01"
TARGET_X = 2288
TARGET_Z = -2224
MAX_SLICES = 24
SLICE_DURATION_S = 45.0


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


def setup_natural_expedition() -> list[dict[str, str]]:
    cmds = [
        f"spreadplayers {TARGET_X} {TARGET_Z} 0 8 false {BOT_NAME}",
        f"gamemode survival {BOT_NAME}",
        f"clear {BOT_NAME}",
        f"effect clear {BOT_NAME}",
        f"effect give {BOT_NAME} minecraft:instant_health 1 10 true",
        f"effect give {BOT_NAME} minecraft:saturation 1 10 true",
        f"give {BOT_NAME} minecraft:stone_pickaxe 1",
        f"give {BOT_NAME} minecraft:stone_pickaxe 1",
        f"give {BOT_NAME} minecraft:iron_pickaxe 1",
        f"give {BOT_NAME} minecraft:iron_pickaxe 1",
    ]
    return rcon_batch(cmds, timeout=40)


def wait_landed_and_loaded(bot: BotSession, *, timeout_s: float = 35.0) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            st = bot.status()
            p = st.get("pos") or [0.0, 64.0, 0.0]
            if (
                st.get("ready")
                and st.get("alive") is not False
                and st.get("on_ground")
                and abs(float(p[0]) - TARGET_X) <= 32.0
                and abs(float(p[2]) - TARGET_Z) <= 32.0
            ):
                below = [math.floor(float(p[0])), math.floor(float(p[1])) - 1, math.floor(float(p[2]))]
                blk = bot.call("get_block", {"pos": below})
                bname = str(blk.get("name") or blk.get("block") or "").removeprefix("minecraft:")
                if bname:
                    return st, bname
        except ApiError:
            pass
        time.sleep(0.35)
    raise RuntimeError(f"{BOT_NAME} failed to settle and load natural terrain near ({TARGET_X}, {TARGET_Z})")


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
        timeout=70.0,
    )
    t_start = time.monotonic()
    report: dict[str, Any] = {
        "bot": BOT_NAME,
        "target_biome_center": [TARGET_X, TARGET_Z],
        "max_slices": MAX_SLICES,
        "slice_duration_s": SLICE_DURATION_S,
        "slices": [],
    }
    try:
        bot.wait_until_ready(timeout_s=45.0)
        setup_natural_expedition()
        st0, surface_block = wait_landed_and_loaded(bot, timeout_s=35.0)
        inv0 = bot.inventory()
        counts0 = _inventory_counts_from_raw(inv0)
        picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        report["start_pos"] = st0.get("pos")
        report["start_surface_block"] = surface_block
        report["start_biome"] = st0.get("biome")
        report["start_counts"] = counts0
        report["start_pickaxes"] = picks0
        report["start_total_durability"] = total_rem0

        print(
            json.dumps(
                {
                    "event": "expedition_start",
                    "pos": st0.get("pos"),
                    "biome": st0.get("biome"),
                    "surface_block": surface_block,
                    "stone_pickaxes": counts0.get("stone_pickaxe", 0),
                    "iron_pickaxes": counts0.get("iron_pickaxe", 0),
                    "total_durability": total_rem0,
                    "max_tier": max_tier0,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        skills = MacroSkills(bot)
        skills.mine_dfs(action="reset")
        consecutive_hurts = 0
        final_res: dict[str, Any] = {}

        for slice_idx in range(1, MAX_SLICES + 1):
            t_slice = time.monotonic()
            res = skills.mine_dfs(
                target="diamond",
                goal_count=1,
                target_y=-54,
                max_duration_s=SLICE_DURATION_S,
            )
            final_res = res
            inv_now = bot.inventory()
            counts_now = _inventory_counts_from_raw(inv_now)
            picks_now, _, total_rem_now = _pickaxe_inventory_summary(inv_now)
            st_now = bot.status()
            pos_now = st_now.get("pos") or res.get("pos_after") or [0.0, 64.0, 0.0]
            seal_blocks = sum(counts_now.get(name, 0) for name in SHAFT_SEAL_BLOCKS)
            diamonds = counts_now.get("diamond", 0) + counts_now.get("diamond_ore", 0) + counts_now.get("deepslate_diamond_ore", 0)
            stop_reason = str(res.get("stop_reason") or "unknown")

            slice_entry = {
                "slice": slice_idx,
                "elapsed_s": round(time.monotonic() - t_slice, 2),
                "total_elapsed_s": round(time.monotonic() - t_start, 2),
                "stop_reason": stop_reason,
                "pos": pos_now,
                "y": math.floor(float(pos_now[1])),
                "health": st_now.get("health"),
                "food": st_now.get("food"),
                "diamonds": diamonds,
                "seal_blocks": seal_blocks,
                "breaches_sealed_slice": res.get("breaches_sealed", 0),
                "total_breaches_sealed": (res.get("tree") or {}).get("sealed_count", 0),
                "node_count": (res.get("tree") or {}).get("node_count", 0),
                "pickaxes_remaining": [{"name": p["name"], "rem": p["remaining"], "offhand": bool(p.get("in_offhand"))} for p in picks_now],
                "total_remaining_durability": total_rem_now,
                "mined_ores": res.get("mined_ores", []),
            }
            report["slices"].append(slice_entry)
            print(json.dumps(slice_entry, ensure_ascii=False), flush=True)

            if diamonds >= 1 or stop_reason == "quota_reached":
                break
            if stop_reason in {"dead", "pickaxe_low", "insufficient_pickaxe_tier", "shaft_exhausted"}:
                break
            if st_now.get("alive") is False or total_rem_now <= 6:
                break
            if stop_reason == "hurt":
                consecutive_hurts += 1
                if consecutive_hurts >= 2 or float(st_now.get("health") or 0.0) <= 6.0:
                    break
            else:
                consecutive_hurts = 0

        inv_end = bot.inventory()
        counts_end = _inventory_counts_from_raw(inv_end)
        picks_end, _, total_rem_end = _pickaxe_inventory_summary(inv_end)
        st_end = bot.status()
        diamonds_end = counts_end.get("diamond", 0) + counts_end.get("diamond_ore", 0) + counts_end.get("deepslate_diamond_ore", 0)
        report["summary"] = {
            "success": diamonds_end >= 1,
            "elapsed_s": round(time.monotonic() - t_start, 2),
            "slices_used": len(report["slices"]),
            "final_stop_reason": final_res.get("stop_reason"),
            "final_pos": st_end.get("pos"),
            "lowest_y_reached": min([s["y"] for s in report["slices"]], default=math.floor(float((st0.get("pos") or [0, 64, 0])[1]))),
            "diamonds_mined": diamonds_end,
            "final_counts": counts_end,
            "pickaxes_remaining": picks_end,
            "total_remaining_durability": total_rem_end,
            "tree": final_res.get("tree"),
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

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(REPORT_PATH), "summary": report.get("summary")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
