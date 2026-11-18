#!/usr/bin/env python3
"""Shared live-acceptance plumbing for the L2 pack (movement + inventory + window/smelt).

0-LLM: talks straight to the `nju-beast` MC Agent API + RCON. The isolated bot is
`MoveTest_01` and its token lives in `.state-move-test` (never printed).
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BOT = "MoveTest_01"
BOAT_TYPES = (
    "oak_boat", "spruce_boat", "birch_boat", "jungle_boat", "acacia_boat", "dark_oak_boat",
    "mangrove_boat", "cherry_boat", "pale_oak_boat", "oak_chest_boat", "spruce_chest_boat",
    "birch_chest_boat", "jungle_chest_boat", "acacia_chest_boat", "dark_oak_chest_boat",
    "mangrove_chest_boat", "cherry_chest_boat", "pale_oak_chest_boat",
)
ENV_PATH = pathlib.Path("/home/xzq/projects/mc/.env.beast")
SKIN_ENV_PATH = pathlib.Path("/home/xzq/projects/mc/.env")
STATE_DIR = pathlib.Path("/home/xzq/projects/mc/.state-move-test")
# Dedicated high-altitude arena, far from every other live test site.
ARENA = {"x0": 5160, "x1": 5340, "z0": 5200, "z1": 5340, "y": 150}


def rcon_batch(commands: list[str], *, timeout: int = 90) -> list[dict[str, str]]:
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
        ["ssh", "-T", "-F", "/home/xzq/.ssh/config", "nju-beast", "wsl", "-d", "Ubuntu-22.04", "-u", "xia", "--", "python3", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"RCON SSH failed ({proc.returncode}): {proc.stderr.strip()[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def ensure_token() -> tuple[str, str]:
    """Return (token, bot_username) from the isolated state dir, creating the profile once."""
    os.environ["MC_STATE_DIR"] = str(STATE_DIR)
    from mc_agent.auth import TokenStore
    from mc_agent.config import Settings, parse_dotenv
    from mc_agent.errors import ApiError

    settings = Settings.load(dotenv_path=ENV_PATH)
    settings.state_dir = STATE_DIR
    store = TokenStore(STATE_DIR / "tokens.json")
    rec = store.get(BOT)
    if rec is not None and not rec.is_expired():
        return rec.token, rec.bot_username
    # Create profile idempotently with the shared skin account, then log in via the MC API.
    from mc_agent.skin_station import SkinStationClient
    from mc_agent.auth import McAuthClient

    skin_vars = parse_dotenv(SKIN_ENV_PATH)
    ident = skin_vars.get("MC_SKIN_EMAIL") or ""
    pwd = skin_vars.get("MC_SKIN_PASSWORD") or ""
    api_pwd = parse_dotenv(ENV_PATH).get("MC_SKIN_PASSWORD") or pwd
    if not ident or not pwd:
        raise RuntimeError("缺少皮肤站账号凭据（MC_SKIN_EMAIL / MC_SKIN_PASSWORD）")
    with SkinStationClient(base_url=settings.skin_base_url) as skin:
        skin.login(ident, pwd)
        skin.create_player(BOT, auto_sign=True, idempotent=True)
    with McAuthClient(base_url=settings.mc_api_base_url, token_store=store) as auth:
        outcome = auth.authenticate(BOT, api_pwd, force_refresh=False)
    return outcome.record.token, outcome.record.bot_username


class Session:
    def __init__(self) -> None:
        ensure_token_dir()
        from mc_agent.bot import BotSession
        from mc_agent.config import Settings
        from mc_agent.harness.mc_tools import McToolset
        from mc_agent.harness.world_memory import WorldMemory

        self.settings = Settings.load(dotenv_path=ENV_PATH)
        self.token, self.username = ensure_token()
        self.bot = BotSession(self.token, bot_username=self.username, base_url=self.settings.mc_api_base_url)
        self.world_memory = WorldMemory(STATE_DIR / f"world_memory_{BOT}.json")
        self.toolset = McToolset(self.bot, world_memory=self.world_memory)
        self.skills = self.toolset.skills
        self.skills.world_memory = self.world_memory
        self.checks: dict[str, dict[str, Any]] = {}

    def connect(self, *, timeout_s: float = 300.0) -> dict[str, Any]:
        from mc_agent.errors import ApiError

        try:
            st = self.bot.status()
        except ApiError:
            st = {}
        if st.get("ready"):
            return st
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            try:
                self.bot.connect(respect_reconnect_interval=False)
                st = self.bot.wait_until_ready(timeout_s=45)
                if st.get("ready"):
                    return st
            except ApiError as exc:
                last = f"{exc.code}: {exc.message}"
            time.sleep(8)
        raise RuntimeError(f"connect failed: {last}")

    def check(self, name: str, detail: Any, ok: bool) -> bool:
        self.checks[name] = {"ok": bool(ok), "detail": detail}
        print(json.dumps({"check": name, "ok": bool(ok)}, ensure_ascii=False), flush=True)
        return ok

    def close(self) -> None:
        try:
            self.bot.close()
        except Exception:
            pass


def ensure_token_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass


def status_brief(st: dict[str, Any]) -> dict[str, Any]:
    keys = ("ready", "pos", "health", "food", "dimension", "on_ground", "in_water", "sneaking", "alive")
    return {k: st.get(k) for k in keys}


def prepare_player(items: dict[str, int] | None = None, *, clear: bool = True, effects: bool = True) -> list[dict[str, str]]:
    """Give the test bot an exact loadout via RCON (never touches other roles)."""
    cmds = [f"gamemode survival {BOT}"]
    if clear:
        cmds.append(f"clear {BOT}")
    if effects:
        cmds.append(f"effect give {BOT} minecraft:resistance 3600 4 true")
        cmds.append(f"effect give {BOT} minecraft:water_breathing 3600 0 true")
        cmds.append(f"effect give {BOT} minecraft:saturation 30 4 true")
    for name, count in (items or {}).items():
        cmds.append(f"give {BOT} minecraft:{name} {int(count)}")
    return rcon_batch(cmds)


def tp(x: float, y: float, z: float, *, yaw: float | None = None) -> list[dict[str, str]]:
    cmd = f"tp {BOT} {x} {y} {z}"
    if yaw is not None:
        cmd += f" {yaw} 0"
    return rcon_batch([cmd])


def arena_reset() -> list[dict[str, str]]:
    x0, x1, z0, z1, y = ARENA["x0"], ARENA["x1"], ARENA["z0"], ARENA["z1"], ARENA["y"]
    cmds = [
        f"forceload add {x0 - 16} {z0 - 16} {x1 + 16} {z1 + 16}",
        f"fill {x0} {y - 20} {z0} {x1} {y + 40} {z1} minecraft:air",
        f"fill {x0} {y - 21} {z0} {x1} {y - 21} {z1} minecraft:stone",
        f"fill {x0 + 3} {y - 1} {z0 + 3} {x0 + 7} {y - 1} {z0 + 7} minecraft:stone",
        "time set day",
        "weather clear",
        "gamerule doMobSpawning false",
        "gamerule doWeatherCycle false",
        "gamerule randomTickSpeed 0",
        "gamerule fallDamage true",
        "gamerule keepInventory false",
        *(
            f"execute positioned {x0 + 90} {y} {z0 + 90} run kill @e[type=minecraft:{b},distance=..260]"
            for b in BOAT_TYPES
        ),
        f"kill @e[type=minecraft:item,x={x0},y={y},z={z0},distance=..200]",
    ]
    return rcon_batch(cmds)


def arena_cleanup() -> list[dict[str, str]]:
    x0, x1, z0, z1, y = ARENA["x0"], ARENA["x1"], ARENA["z0"], ARENA["z1"], ARENA["y"]
    cmds = [
        *(
            f"execute positioned {x0 + 90} {y} {z0 + 90} run kill @e[type=minecraft:{b},distance=..260]"
            for b in BOAT_TYPES
        ),
        f"kill @e[type=minecraft:item,x={x0},y={y},z={z0},distance=..200]",
        f"fill {x0} {y - 22} {z0} {x1} {y + 42} {z1} minecraft:air",
        # permanent 5x5 parking pad so the bot never free-falls when the arena is wiped
        f"fill {x0 + 3} {y - 1} {z0 + 3} {x0 + 7} {y - 1} {z0 + 7} minecraft:stone",
        f"tp {BOT} {x0 + 5} {y} {z0 + 5}",
        f"forceload remove {x0 - 16} {z0 - 16} {x1 + 16} {z1 + 16}",
    ]
    return rcon_batch(cmds)


def fill(x0: int, y0: int, z0: int, x1: int, y1: int, z1: int, block: str) -> None:
    rcon_batch([f"fill {x0} {y0} {z0} {x1} {y1} {z1} minecraft:{block}"])


def setblock(x: int, y: int, z: int, block: str, *, replace: bool = True) -> None:
    rcon_batch([f"setblock {x} {y} {z} minecraft:{block} replace" if replace else f"setblock {x} {y} {z} minecraft:{block}"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.status:
        s = Session()
        try:
            print(json.dumps(status_brief(s.connect()), ensure_ascii=False))
        finally:
            s.close()
        return 0
    if args.reset:
        print(json.dumps(arena_reset()[-3:], ensure_ascii=False))
        return 0
    if args.cleanup:
        print(json.dumps(arena_cleanup(), ensure_ascii=False))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
