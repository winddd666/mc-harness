#!/usr/bin/env python3
"""Live acceptance test for the no-strategy GUI atomic wrappers on nju-beast.

Uses the isolated bot `GuiTest_01` and its own `.state-gui-test` token store, so
BeastBot_01 / mc-live / mc-console are not touched. Test fixtures are created with
RCON on a far-away sky platform; no competition world state near the base is used.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("MC_STATE_DIR", ".state-gui-test")
os.environ.setdefault("MC_WORLD_MEMORY_PATH", ".state-gui-test/world_memory_GuiTest_01.json")

from mc_agent.auth import TokenStore  # noqa: E402
from mc_agent.bot import BotSession  # noqa: E402
from mc_agent.config import Settings  # noqa: E402
from mc_agent.errors import ApiError  # noqa: E402
from mc_agent.harness.mc_tools import McToolset  # noqa: E402

BOT = "GuiTest_01"
BASE_X, BASE_Y, BASE_Z = 3096, 150, 3096
POS = {
    "chest": [3092, BASE_Y, 3096],
    "barrel": [3094, BASE_Y, 3096],
    "enchanting_table": [3096, BASE_Y, 3096],
    "brewing_stand": [3098, BASE_Y, 3096],
    "anvil": [3100, BASE_Y, 3096],
    "smithing_table": [3092, BASE_Y, 3098],
    "stonecutter": [3094, BASE_Y, 3098],
    "trader": [3100, BASE_Y, 3098],
}


def rcon_batch(commands: list[str], *, timeout: int = 45) -> list[dict[str, str]]:
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
        raise RuntimeError(f"RCON SSH failed ({proc.returncode}): {proc.stderr.strip()[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def setup_fixtures() -> list[dict[str, str]]:
    """Build the sky-platform world fixtures. Safe to run before the bot connects."""
    x0, z0 = BASE_X - 6, BASE_Z - 6
    x1, z1 = BASE_X + 6, BASE_Z + 6
    cmds = [
        f"forceload add {x0 - 2} {z0 - 2} {x1 + 2} {z1 + 2}",
        f"fill {x0} {BASE_Y - 1} {z0} {x1} {BASE_Y - 1} {z1} minecraft:stone",
        f"fill {x0} {BASE_Y} {z0} {x1} {BASE_Y + 3} {z1} minecraft:air",
        "time set day",
        "weather clear",
        f"setblock {POS['chest'][0]} {BASE_Y} {POS['chest'][2]} minecraft:chest replace",
        f"setblock {POS['barrel'][0]} {BASE_Y} {POS['barrel'][2]} minecraft:barrel replace",
        f"setblock {POS['enchanting_table'][0]} {BASE_Y} {POS['enchanting_table'][2]} minecraft:enchanting_table replace",
        f"setblock {POS['brewing_stand'][0]} {BASE_Y} {POS['brewing_stand'][2]} minecraft:brewing_stand replace",
        f"setblock {POS['anvil'][0]} {BASE_Y} {POS['anvil'][2]} minecraft:anvil replace",
        f"setblock {POS['smithing_table'][0]} {BASE_Y} {POS['smithing_table'][2]} minecraft:smithing_table replace",
        f"setblock {POS['stonecutter'][0]} {BASE_Y} {POS['stonecutter'][2]} minecraft:stonecutter replace",
        # 15+ bookshelves one block above the table ring: enables all three enchant options.
        *[
            f"setblock {x} {BASE_Y + 1} {z} minecraft:bookshelf replace"
            for z in (BASE_Z - 2, BASE_Z + 2)
            for x in range(BASE_X - 2, BASE_X + 3)
            if not (z == BASE_Z - 2 and x == BASE_X)
        ],
        *[
            f"setblock {x} {BASE_Y + 1} {z} minecraft:bookshelf replace"
            for x in (BASE_X - 2, BASE_X + 2)
            for z in range(BASE_Z - 1, BASE_Z + 2)
        ],
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:zombie,distance=..24]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:skeleton,distance=..24]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:creeper,distance=..24]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:spider,distance=..24]",
    ]
    return rcon_batch(cmds)


def prepare_player() -> list[dict[str, str]]:
    """Player-only setup, after the bot is connected and teleported to the platform."""
    cmds = [
        f"tp {BOT} {BASE_X} {BASE_Y} {BASE_Z - 4}",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"xp set {BOT} 60 levels",
        f"effect give {BOT} minecraft:resistance 900 4 true",
        f"effect give {BOT} minecraft:regeneration 900 2 true",
        f"effect give {BOT} minecraft:saturation 10 20 true",
        f"summon minecraft:wandering_trader {POS['trader'][0]} {BASE_Y} {POS['trader'][2]}",
    ]
    gives = {
        "cobblestone": 64,
        "oak_log": 32,
        "stone": 32,
        "iron_ingot": 16,
        "lapis_lazuli": 16,
        "emerald": 64,
        "coal": 16,
        "nether_wart": 8,
        "blaze_powder": 8,
        "iron_sword": 2,
        "diamond_chestplate": 1,
        "netherite_ingot": 1,
        "netherite_upgrade_smithing_template": 1,
        "wheat": 64,
        "bread": 16,
    }
    cmds.extend(f"give {BOT} minecraft:{item} {count}" for item, count in gives.items())
    cmds.append(
        f'give {BOT} minecraft:potion[minecraft:potion_contents={{potion:"minecraft:water"}}] 3'
    )
    return rcon_batch(cmds)


def connect_until_ready(bot: BotSession, *, timeout_s: float = 420.0) -> dict[str, Any]:
    """Retry `/connect` across death-loop cooldowns, then wait for ready."""
    deadline = time.monotonic() + timeout_s
    last_error = ""
    try:
        st0 = bot.status()
        if st0.get("ready"):
            return st0
    except ApiError as exc:
        last_error = f"{exc.code}: {exc.message}"
    while time.monotonic() < deadline:
        try:
            bot.connect(respect_reconnect_interval=False)
            st = bot.wait_until_ready(timeout_s=30.0)
            if st.get("ready"):
                return st
            last_error = f"not ready: {st.get('pos')}"
        except ApiError as exc:
            last_error = f"{exc.code}: {exc.message}"
        time.sleep(8.0)
    raise RuntimeError(f"connect_until_ready failed: {last_error}")



def wait_ready(bot: BotSession, *, timeout_s: float = 35.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    st: dict[str, Any] = {}
    while time.monotonic() < deadline:
        st = bot.status()
        p = st.get("pos") or [0, 0, 0]
        if (
            st.get("ready")
            and st.get("alive") is not False
            and abs(float(p[0]) - BASE_X) <= 24
            and abs(float(p[2]) - BASE_Z) <= 24
        ):
            return st
        time.sleep(1.0)
    return st


def find_entity(bot: BotSession, names: tuple[str, ...], radius: int = 24) -> dict[str, Any] | None:
    res = bot.call("get_entities", {"radius": radius})
    for ent in res.get("entities") or []:
        name = str(ent.get("name") or "")
        if any(n in name for n in names):
            return ent
    return None


def inventory_item(bot: BotSession, name: str) -> list[dict[str, Any]]:
    inv = bot.call("inventory", {})
    return [
        item
        for item in (inv.get("items") or [])
        if str(item.get("name") or "").removeprefix("minecraft:") == name
    ]


def main() -> int:
    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    record = TokenStore(pathlib.Path(settings.state_dir) / "tokens.json").get(BOT)
    if record is None:
        raise SystemExit(f"no cached token for {BOT}; run mc-bot login first")
    bot = BotSession(record.token, bot_username=record.bot_username, base_url=settings.mc_api_base_url)
    toolset = McToolset(bot)
    report: dict[str, Any] = {"bot": BOT, "setup": [], "checks": {}, "ok": True}
    try:
        report["setup"] = setup_fixtures()
        st = connect_until_ready(bot)
        report["player_setup"] = prepare_player()
        time.sleep(2.0)
        st = wait_ready(bot)
        report["status_after_setup"] = {
            k: st.get(k) for k in ("ready", "pos", "health", "food", "dimension", "is_day", "alive")
        }

        def check(key: str, value: Any, *, ok: bool) -> None:
            report["checks"][key] = {"ok": ok, "value": value}
            if not ok:
                report["ok"] = False

        # 1) Container deposit + WorldMemory snapshot
        dep = json.loads(
            toolset.mc_skill_container(
                [{"item": "cobblestone", "count": 32}, {"item": "oak_log", "count": 16}],
                action="put",
                x=POS["chest"][0],
                y=POS["chest"][1],
                z=POS["chest"][2],
            )
        )
        check("container_deposit", dep, ok=bool(dep.get("ok")))
        dep_slots = ((dep.get("window") or {}).get("sections") or {}).get("container") or []
        dep_counts: dict[str, int] = {}
        for row in dep_slots:
            dep_counts[str(row.get("name"))] = dep_counts.get(str(row.get("name")), 0) + int(row.get("count") or 0)
        time.sleep(0.5)
        mem_path = pathlib.Path(os.environ["MC_WORLD_MEMORY_PATH"])
        mem_containers = json.loads(mem_path.read_text(encoding="utf-8")).get("containers") or []
        mem_chest = next(
            (c for c in mem_containers if list(c.get("pos") or []) == POS["chest"]), {}
        )
        check(
            "container_memory_snapshot",
            {
                "deposit_window": dep_counts,
                "memory_chest": mem_chest,
                "memory_synced": dep.get("memory_synced"),
            },
            ok=dep_counts.get("cobblestone", 0) >= 32
            and dep_counts.get("oak_log", 0) >= 16
            and (mem_chest.get("items") or {}).get("cobblestone", 0) >= 32,
        )
        take = json.loads(
            toolset.mc_skill_container(
                [{"item": "cobblestone", "count": 10}],
                action="take",
                x=POS["chest"][0],
                y=POS["chest"][1],
                z=POS["chest"][2],
            )
        )
        check("container_withdraw", take, ok=bool(take.get("ok")) and bool(take.get("done")))
        view = {}
        for _ in range(3):
            time.sleep(0.8)
            payload = json.loads(
                toolset.mc_window_view(x=POS["chest"][0], y=POS["chest"][1], z=POS["chest"][2])
            )
            view = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            if ((view.get("sections") or {}).get("container") or []):
                break
        report["checks"]["container_view_after_withdraw"] = {
            "ok": bool(((view.get("sections") or {}).get("container") or [])),
            "value": view,
        }
        if not report["checks"]["container_view_after_withdraw"]["ok"]:
            report["ok"] = False

        # 2) Enchanting table single combo (option 2 with 3 lapis; fallback to option 0)
        enchant = json.loads(
            toolset.mc_skill_enchant(
                "iron_sword",
                2,
                x=POS["enchanting_table"][0],
                y=POS["enchanting_table"][1],
                z=POS["enchanting_table"][2],
            )
        )
        if not enchant.get("ok") and enchant.get("error") == "option_unavailable":
            enchant = json.loads(
                toolset.mc_skill_enchant(
                    "iron_sword",
                    0,
                    x=POS["enchanting_table"][0],
                    y=POS["enchanting_table"][1],
                    z=POS["enchanting_table"][2],
                )
            )
        swords = inventory_item(bot, "iron_sword")
        enchanted = [s for s in swords if s.get("enchantments")]
        check(
            "enchant_item_once",
            {"result": enchant, "swords": swords},
            ok=bool(enchant.get("ok")) and bool(enchanted),
        )

        # 3) Merchant trade by matching an available get_item
        trader = find_entity(bot, ("trader", "villager"))
        if trader is None:
            check("merchant_trade", {"error": "no trader found"}, ok=False)
        else:
            trader_view = bot.call("window/view", {"target": {"entity_id": int(trader["id"])}})
            options = [o for o in (trader_view.get("options") or []) if isinstance(o, dict)]
            available = [o for o in options if o.get("available") is not False]
            picked = (available or options or [{}])[0]
            get_name = str((picked.get("get") or {}).get("name") or "")
            trade = json.loads(
                toolset.mc_skill_trade(int(trader["id"]), get_item=get_name, times=1)
            ) if get_name else {"ok": False, "error": "no available trade"}
            check(
                "merchant_trade",
                {"entity": trader, "picked": picked, "result": trade},
                ok=bool(trade.get("ok")),
            )

        # 4) Brewing stand with bottles/ingredient/fuel + brew_time_left polling
        brew = json.loads(
            toolset.mc_skill_brew(
                "nether_wart",
                bottles=[
                    {"item": "potion", "potion": "water"},
                    {"item": "potion", "potion": "water"},
                    {"item": "potion", "potion": "water"},
                ],
                fuel_item="blaze_powder",
                wait=True,
                x=POS["brewing_stand"][0],
                y=POS["brewing_stand"][1],
                z=POS["brewing_stand"][2],
            )
        )
        check(
            "brew_once",
            brew,
            ok=bool(brew.get("ok")) and isinstance(brew.get("sections"), dict),
        )

        # 5) Anvil rename combo (server accepted text => xp_spent on take)
        anvil = json.loads(
            toolset.mc_skill_anvil(
                "iron_sword",
                text="GuiTestBlade",
                x=POS["anvil"][0],
                y=POS["anvil"][1],
                z=POS["anvil"][2],
            )
        )
        done = anvil.get("done") or []
        check(
            "anvil_operate",
            {"result": anvil, "swords": inventory_item(bot, "iron_sword")},
            ok=bool(anvil.get("ok"))
            and any(d.get("op") == "text" for d in done)
            and any(d.get("op") == "take" and int(d.get("xp_spent") or 0) >= 1 for d in done),
        )

        # 6) Smithing table upgrade combo
        smithing = json.loads(
            toolset.mc_skill_smithing(
                "netherite_upgrade_smithing_template",
                "diamond_chestplate",
                "netherite_ingot",
                x=POS["smithing_table"][0],
                y=POS["smithing_table"][1],
                z=POS["smithing_table"][2],
            )
        )
        netherite = inventory_item(bot, "netherite_chestplate")
        check(
            "smithing_apply",
            {"result": smithing, "netherite_chestplate": netherite},
            ok=bool(smithing.get("ok")) and bool(netherite),
        )

        # 7) Stonecutter: read options, then cut option 0
        sc_view = json.loads(
            toolset.mc_window_view(
                x=POS["stonecutter"][0], y=POS["stonecutter"][1], z=POS["stonecutter"][2]
            )
        )
        stonecutter = json.loads(
            toolset.mc_skill_stonecutter(
                "stone",
                0,
                count=8,
                x=POS["stonecutter"][0],
                y=POS["stonecutter"][1],
                z=POS["stonecutter"][2],
            )
        )
        check(
            "stonecutter_cut",
            {"view": sc_view, "result": stonecutter},
            ok=bool(stonecutter.get("ok")) and bool(stonecutter.get("done")),
        )

        report["final_status"] = {
            k: bot.status().get(k) for k in ("ready", "pos", "health", "food", "alive")
        }
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            report["cleanup"] = rcon_batch(
                [
                    f"tp {BOT} {BASE_X} {BASE_Y} {BASE_Z - 4}",
                    f"effect clear {BOT}",
                    "forceload remove 3088 3088 3104 3104",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            report["cleanup_error"] = str(exc)
        bot.close()

    out = ROOT / "runs" / f"gui-atomic-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "checks": {k: v["ok"] for k, v in report["checks"].items()}, "report": str(out)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
