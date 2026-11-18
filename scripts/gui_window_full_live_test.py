#!/usr/bin/env python3
"""Near-full live regression for Minecraft Agent API window/GUI operations.

Groups:
  A containers + WorldMemory snapshots (chest/barrel/hopper/shulker/dispenser)
  B order-sensitive crafting-table GUI recipes (wrong order -> correct order)
  C enchanting / anvil / smithing / grindstone / loom
  D brewing two-stage / furnace trio / stonecutter
  E villagers: wandering trader multi-trade + custom two-cost librarian
  F beacon payment + effect selection

Run one or more groups:
  uv run --offline python scripts/gui_window_full_live_test.py --groups A,B
The isolated bot is `GuiTest_01` and its token lives in `.state-gui-test`.
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

BOT = "GuiTest_01"
BASE_X, BASE_Y, BASE_Z = 3096, 150, 3096
POS: dict[str, list[int]] = {
    "chest": [3082, BASE_Y, 3096],
    "barrel": [3084, BASE_Y, 3096],
    "hopper": [3086, BASE_Y, 3096],
    "shulker": [3088, BASE_Y, 3096],
    "dispenser": [3090, BASE_Y, 3096],
    "brewing_stand": [3092, BASE_Y, 3096],
    "anvil": [3094, BASE_Y, 3096],
    "smithing_table": [3096, BASE_Y, 3096],
    "stonecutter": [3098, BASE_Y, 3096],
    "crafting_table": [3082, BASE_Y, 3088],
    "furnace": [3084, BASE_Y, 3088],
    "blast_furnace": [3086, BASE_Y, 3088],
    "smoker": [3088, BASE_Y, 3088],
    "grindstone": [3090, BASE_Y, 3088],
    "loom": [3092, BASE_Y, 3088],
    "beacon": [3094, BASE_Y, 3088],
    "enchanting_table": [3098, BASE_Y, 3088],
    "trader": [3104, BASE_Y, 3096],
    "librarian": [3106, BASE_Y, 3096],
}
_ETX, _ETZ = POS["enchanting_table"][0], POS["enchanting_table"][2]
BOOKSHELF_RING = [
    (x, BASE_Y + 1, z)
    for z in (_ETZ - 2, _ETZ + 2)
    for x in range(_ETX - 2, _ETX + 3)
    if not (z == _ETZ - 2 and x == _ETX)
] + [
    (x, BASE_Y + 1, z)
    for x in (_ETX - 2, _ETX + 2)
    for z in range(_ETZ - 1, _ETZ + 2)
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


def setup_world() -> list[dict[str, str]]:
    x0, x1 = 3078, 3112
    z0, z1 = 3080, 3108
    cmds = [
        f"forceload add {x0 - 2} {z0 - 2} {x1 + 2} {z1 + 2}",
        f"fill {x0} {BASE_Y - 1} {z0} {x1} {BASE_Y - 1} {z1} minecraft:stone",
        f"fill {x0} {BASE_Y} {z0} {x1} {BASE_Y + 4} {z1} minecraft:air",
        "time set day",
        "weather clear",
        "kill @e[type=minecraft:wandering_trader,x=3096,y=150,z=3096,distance=..40]",
        "kill @e[type=minecraft:villager,x=3096,y=150,z=3096,distance=..40]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:zombie,distance=..40]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:skeleton,distance=..40]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:creeper,distance=..40]",
        "execute positioned 3096 150 3096 run kill @e[type=minecraft:spider,distance=..40]",
    ]
    for name, pos in POS.items():
        if name in ("trader", "librarian", "beacon"):
            continue
        if name == "enchanting_table":
            block = "enchanting_table"
        elif name == "shulker":
            block = "shulker_box"
        else:
            block = name
        cmds.append(f"setblock {pos[0]} {pos[1]} {pos[2]} minecraft:{block} replace")
    cmds.extend(f"setblock {x} {y} {z} minecraft:bookshelf replace" for x, y, z in BOOKSHELF_RING)
    # 3x3 iron-block beacon base at y=149, beacon at y=150.
    bx, by, bz = POS["beacon"]
    cmds.append(f"fill {bx - 1} {by - 1} {bz - 1} {bx + 1} {by - 1} {bz + 1} minecraft:iron_block")
    cmds.append(f"setblock {bx} {by} {bz} minecraft:beacon replace")
    cmds.append(
        f'summon minecraft:wandering_trader {POS["trader"][0]} {POS["trader"][1]} {POS["trader"][2]} '
        "{PersistenceRequired:1b,NoAI:1b,Invulnerable:1b}"
    )
    cmds.append(
        f'summon minecraft:villager {POS["librarian"][0]} {POS["librarian"][1]} {POS["librarian"][2]} '
        '{VillagerData:{profession:"minecraft:librarian",level:2,type:"minecraft:plains"},'
        'Offers:{Recipes:[{buy:{id:"minecraft:emerald",count:5},'
        'buyB:{id:"minecraft:book",count:1},'
        'sell:{id:"minecraft:enchanted_book",count:1},maxUses:8}]},'
        'PersistenceRequired:1b,NoAI:1b,Invulnerable:1b}'
    )
    return rcon_batch(cmds)


def prepare_player(items: dict[str, int], *, xp_levels: int = 60) -> list[dict[str, str]]:
    cmds = [
        f"tp {BOT} {BASE_X} {BASE_Y} {BASE_Z + 6}",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"xp set {BOT} {xp_levels} levels",
        f"effect give {BOT} minecraft:resistance 1800 4 true",
        f"effect give {BOT} minecraft:regeneration 1800 2 true",
        f"effect give {BOT} minecraft:saturation 30 20 true",
    ]
    for name, count in items.items():
        cmds.append(f"give {BOT} minecraft:{name} {count}")
    return rcon_batch(cmds)


def api_json(toolset: Any, method: str, **kwargs: Any) -> dict[str, Any]:
    raw = getattr(toolset, method)(**kwargs)
    payload = json.loads(raw)
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload


def find_entity(bot: Any, names: tuple[str, ...], pos: list[int], radius: int = 24) -> dict[str, Any] | None:
    res = bot.call("get_entities", {"radius": radius})
    best = None
    best_d = 10**9
    for ent in res.get("entities") or []:
        name = str(ent.get("name") or "")
        if not any(n in name for n in names):
            continue
        epos = ent.get("pos") or [0, 0, 0]
        d = sum((float(epos[i]) - float(pos[i])) ** 2 for i in range(3))
        if d < best_d:
            best_d = d
            best = ent
    return best


def inventory_items(bot: Any, name: str) -> list[dict[str, Any]]:
    inv = bot.call("inventory", {})
    return [
        it for it in (inv.get("items") or [])
        if str(it.get("name") or "").removeprefix("minecraft:") == name
    ]


def inventory_count(bot: Any, name: str) -> int:
    return sum(int(it.get("count") or 0) for it in inventory_items(bot, name))


def error_code(payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return str(err or "")


class Suite:
    def __init__(self, bot: Any, toolset: Any) -> None:
        self.bot = bot
        self.toolset = toolset
        self.checks: dict[str, dict[str, Any]] = {}
        self.ok = True

    def check(self, key: str, value: Any, ok: bool) -> None:
        self.checks[key] = {"ok": bool(ok), "value": value}
        if not ok:
            self.ok = False

    def view(self, pos: list[int]) -> dict[str, Any]:
        return api_json(self.toolset, "mc_window_view", x=pos[0], y=pos[1], z=pos[2])

    def act(self, pos: list[int], ops: list[dict[str, Any]]) -> dict[str, Any]:
        return api_json(self.toolset, "mc_window_act", ops=ops, x=pos[0], y=pos[1], z=pos[2])

    def wait_output(self, pos: list[int], *, timeout_s: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        view: dict[str, Any] = {}
        while time.monotonic() < deadline:
            view = self.view(pos)
            out = (view.get("sections") or {}).get("output") or []
            if out:
                return view
            time.sleep(2.0)
        return view

    def reconnect(self) -> bool:
        """Reset stale mineflayer window/inventory state (upstream repeated-enchant quirk)."""
        try:
            self.bot.disconnect()
        except Exception:
            pass
        time.sleep(2.0)
        try:
            self.bot.connect(respect_reconnect_interval=False)
            st = self.bot.wait_until_ready(timeout_s=30.0)
            return bool(st.get("ready"))
        except Exception:
            return False


def group_a(s: Suite) -> None:
    report = prepare_player({"cobblestone": 64, "oak_log": 16, "iron_ingot": 16, "arrow": 16})
    s.checks["A_setup"] = {"ok": True, "value": report[-6:]}
    chest = POS["chest"]
    dep = json.loads(
        s.toolset.mc_skill_container(
            [
                {"item": "cobblestone", "count": 32, "slot": 0},
                {"item": "oak_log", "count": 16, "slot": 5},
            ],
            action="put",
            x=chest[0], y=chest[1], z=chest[2],
        )
    )
    s.check("A_chest_deposit_slots", dep, bool(dep.get("ok")))
    time.sleep(0.4)
    mem_path = pathlib.Path(".state-gui-test/world_memory_GuiTest_01.json")
    mem = json.loads(mem_path.read_text(encoding="utf-8")).get("containers") or []
    mem_chest = next((c for c in mem if list(c.get("pos") or []) == chest), {})
    s.check(
        "A_chest_memory",
        {"memory": mem_chest, "synced": dep.get("memory_synced")},
        (mem_chest.get("items") or {}).get("cobblestone", 0) >= 32
        and (mem_chest.get("items") or {}).get("oak_log", 0) >= 16,
    )
    take = json.loads(
        s.toolset.mc_skill_container(
            [{"item": "cobblestone", "count": 10}],
            action="take",
            x=chest[0], y=chest[1], z=chest[2],
        )
    )
    s.check("A_chest_withdraw", take, bool(take.get("ok")) and bool(take.get("done")))
    bad = json.loads(
        s.toolset.mc_skill_container(
            [{"item": "diamond", "count": 1}],
            action="take",
            x=chest[0], y=chest[1], z=chest[2],
        )
    )
    s.check("A_chest_missing_negative", bad, bad.get("error") in ("missing_item", "no_output"))

    for key, item in (("A_barrel", "cobblestone"), ("A_hopper", "iron_ingot"), ("A_shulker", "arrow"), ("A_dispenser", "cobblestone")):
        pos = POS[key.split("_", 1)[1]]
        put = json.loads(
            s.toolset.mc_skill_container(
                [{"item": item, "count": 5}], action="put", x=pos[0], y=pos[1], z=pos[2]
            )
        )
        got = json.loads(
            s.toolset.mc_skill_container(
                [{"item": item, "count": 2}], action="take", x=pos[0], y=pos[1], z=pos[2]
            )
        )
        s.check(key, {"put": put, "take": got}, bool(put.get("ok")) and bool(got.get("ok")))

    view = s.view(chest)
    s.check("A_chest_view", view, bool(((view.get("sections") or {}).get("container") or [])))


def group_b(s: Suite) -> None:
    prepare_player({"oak_planks": 32, "stick": 16})
    table = POS["crafting_table"]
    # Wrong order: plank + stick on a diagonal matches no recipe.
    wrong = s.act(
        table,
        [
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 0},
            {"op": "put", "item": "stick", "count": 1, "to": "grid", "slot": 4},
            {"op": "take", "from": "output"},
        ],
    )
    s.check("B_wrong_order_negative", wrong, error_code(wrong) in ("no_output", "missing_item"))

    table_craft = s.act(
        table,
        [
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 0},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 1},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 3},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 4},
            {"op": "take", "from": "output"},
        ],
    )
    s.check(
        "B_crafting_table_2x2",
        table_craft,
        any((d.get("took") or {}).get("crafting_table") for d in (table_craft.get("done") or [])),
    )

    sticks = s.act(
        table,
        [
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 1},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 4},
            {"op": "take", "from": "output"},
        ],
    )
    s.check(
        "B_sticks_vertical",
        sticks,
        any((d.get("took") or {}).get("stick") for d in (sticks.get("done") or [])),
    )

    pickaxe = s.act(
        table,
        [
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 0},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 1},
            {"op": "put", "item": "oak_planks", "count": 1, "to": "grid", "slot": 2},
            {"op": "put", "item": "stick", "count": 1, "to": "grid", "slot": 4},
            {"op": "put", "item": "stick", "count": 1, "to": "grid", "slot": 7},
            {"op": "take", "from": "output"},
        ],
    )
    s.check(
        "B_wooden_pickaxe_3x3",
        pickaxe,
        any((d.get("took") or {}).get("wooden_pickaxe") for d in (pickaxe.get("done") or [])),
    )


def group_c(s: Suite) -> None:
    prepare_player(
        {
            "iron_sword[minecraft:damage=100]": 3,
            "diamond_sword": 2,
            "iron_pickaxe": 1,
            "lapis_lazuli": 12,
            "diamond_chestplate": 1,
            "netherite_ingot": 1,
            "netherite_upgrade_smithing_template": 1,
            "dune_armor_trim_smithing_template": 1,
            "iron_chestplate": 1,
            "gold_ingot": 4,
            "white_banner": 2,
            "red_dye": 4,
        }
    )
    enchant_results = []
    enchant_targets = (("iron_sword", 0), ("diamond_sword", 1), ("iron_pickaxe", 2))
    for item_name, option in enchant_targets:
        last: dict[str, Any] = {}
        for _attempt in range(3):
            last = json.loads(
                s.toolset.mc_skill_enchant(
                    item_name,
                    option,
                    x=POS["enchanting_table"][0],
                    y=POS["enchanting_table"][1],
                    z=POS["enchanting_table"][2],
                )
            )
            if last.get("ok"):
                break
        enchant_results.append(
            {
                "item": item_name,
                "option": option,
                "result": last,
                "verified": last.get("inventory_verified"),
                "auto_reconnected": last.get("reconnected_for_sync"),
                "retried": last.get("retried_after_reconnect"),
            }
        )
        time.sleep(0.5)
    inv = s.bot.call("inventory", {}).get("items") or []
    enchanted_items = [it for it in inv if it.get("enchantments")]
    s.check(
        "C_enchant_0_1_2",
        {"results": enchant_results, "enchanted_items": enchanted_items},
        all(r["result"].get("ok") for r in enchant_results) and len(enchanted_items) >= 3,
    )

    anvil = POS["anvil"]
    combine = s.act(anvil, [
        {"op": "put", "item": "iron_sword", "to": "left", "enchanted": True},
        {"op": "put", "item": "iron_sword", "to": "right"},
        {"op": "take", "from": "output"},
    ])
    s.check(
        "C_anvil_combine",
        combine,
        any(d.get("op") == "take" and (d.get("took") or {}).get("iron_sword") for d in (combine.get("done") or [])),
    )
    rename = s.act(anvil, [
        {"op": "put", "item": "diamond_sword", "to": "left"},
        {"op": "text", "value": "GuiTestBlade"},
        {"op": "take", "from": "output"},
    ])
    s.check(
        "C_anvil_rename",
        rename,
        any(d.get("op") == "text" for d in (rename.get("done") or []))
        and any(d.get("op") == "take" and (d.get("took") or {}).get("diamond_sword") for d in (rename.get("done") or [])),
    )

    smith = POS["smithing_table"]
    netherite = s.act(smith, [
        {"op": "put", "item": "netherite_upgrade_smithing_template", "to": "template"},
        {"op": "put", "item": "diamond_chestplate", "to": "base"},
        {"op": "put", "item": "netherite_ingot", "to": "addition"},
        {"op": "take", "from": "output"},
    ])
    s.check(
        "C_smithing_netherite",
        netherite,
        any((d.get("took") or {}).get("netherite_chestplate") for d in (netherite.get("done") or [])),
    )
    trim = s.act(smith, [
        {"op": "put", "item": "dune_armor_trim_smithing_template", "to": "template"},
        {"op": "put", "item": "iron_chestplate", "to": "base"},
        {"op": "put", "item": "gold_ingot", "to": "addition"},
        {"op": "take", "from": "output"},
    ])
    s.check(
        "C_smithing_trim",
        trim,
        any((d.get("took") or {}).get("iron_chestplate") for d in (trim.get("done") or [])),
    )

    # Grindstone: take the enchanted diamond sword from the enchant step.
    if any(it.get("name") == "diamond_sword" and it.get("enchantments") for it in enchanted_items):
        grind = s.act(POS["grindstone"], [
            {"op": "put", "item": "diamond_sword", "to": "top", "enchanted": True},
            {"op": "take", "from": "output"},
        ])
        s.check(
            "C_grindstone",
            grind,
            any(d.get("op") == "take" and (d.get("took") or {}).get("diamond_sword") for d in (grind.get("done") or [])),
        )
    else:
        s.check("C_grindstone", {"error": "no enchanted diamond sword"}, False)

    loom = s.act(POS["loom"], [
        {"op": "put", "item": "white_banner", "to": "banner"},
        {"op": "put", "item": "red_dye", "to": "dye"},
        {"op": "select", "option": 0},
        {"op": "take", "from": "output"},
    ])
    s.check(
        "C_loom",
        loom,
        any(d.get("op") == "select" for d in (loom.get("done") or []))
        and any((d.get("took") or {}).get("white_banner") for d in (loom.get("done") or [])),
    )


def group_d(s: Suite) -> None:
    prepare_player(
        {
            "potion[minecraft:potion_contents={potion:\"minecraft:water\"}]": 3,
            "nether_wart": 4,
            "sugar": 4,
            "blaze_powder": 8,
            "raw_iron": 4,
            "beef": 4,
            "coal": 16,
            "stone": 16,
        }
    )
    brew_pos = POS["brewing_stand"]
    first = json.loads(
        s.toolset.mc_skill_brew(
            "nether_wart",
            bottles=[
                {"item": "potion", "potion": "water"},
                {"item": "potion", "potion": "water"},
                {"item": "potion", "potion": "water"},
            ],
            fuel_item="blaze_powder",
            wait=True,
            x=brew_pos[0], y=brew_pos[1], z=brew_pos[2],
        )
    )
    bottles1 = ((first.get("sections") or {}).get("bottles") or [])
    s.check(
        "D_brew_water_to_awkward",
        first,
        all(b.get("potion") == "awkward" for b in bottles1) and len(bottles1) == 3,
    )
    second = json.loads(
        s.toolset.mc_skill_brew(
            "sugar",
            fuel_item="blaze_powder",
            wait=True,
            x=brew_pos[0], y=brew_pos[1], z=brew_pos[2],
        )
    )
    bottles2 = ((second.get("sections") or {}).get("bottles") or [])
    s.check(
        "D_brew_awkward_to_swiftness",
        second,
        all(b.get("potion") == "swiftness" for b in bottles2) and len(bottles2) == 3,
    )

    for key, item, output, wait_s in (
        ("D_furnace", "raw_iron", "iron_ingot", 13.0),
        ("D_blast_furnace", "raw_iron", "iron_ingot", 8.0),
        ("D_smoker", "beef", "cooked_beef", 8.0),
    ):
        pos = POS[key.split("_", 1)[1]]
        put = s.act(pos, [
            {"op": "put", "item": item, "count": 1, "to": "input"},
            {"op": "put", "item": "coal", "count": 1, "to": "fuel"},
        ])
        time.sleep(wait_s)
        ready = s.wait_output(pos, timeout_s=20.0)
        outputs = (ready.get("sections") or {}).get("output") or []
        take = s.act(pos, [{"op": "take", "from": "output"}]) if outputs else {}
        s.check(
            key,
            {"put": put, "view": ready, "take": take},
            any((d.get("took") or {}).get(output) for d in (take.get("done") or [])),
        )

    stone = POS["stonecutter"]
    bricks = s.act(stone, [
        {"op": "put", "item": "stone", "count": 1, "to": "input"},
        {"op": "select", "option": 4},
        {"op": "take", "from": "output"},
    ])
    slabs = s.act(stone, [
        {"op": "put", "item": "stone", "count": 1, "to": "input"},
        {"op": "select", "option": 5},
        {"op": "take", "from": "output"},
    ])
    bad = s.act(stone, [
        {"op": "put", "item": "stone", "count": 1, "to": "input"},
        {"op": "select", "option": 99},
    ])
    s.check(
        "D_stonecutter_options",
        {"bricks": bricks, "slabs": slabs, "bad": bad},
        any((d.get("took") or {}).get("stone_bricks") for d in (bricks.get("done") or []))
        and any((d.get("took") or {}).get("stone_slab") for d in (slabs.get("done") or []))
        and error_code(bad) == "bad_option",
    )


def group_e(s: Suite) -> None:
    prepare_player({"emerald": 64, "book": 4, "wheat": 64})
    trader = find_entity(s.bot, ("wandering_trader", "trader"), POS["trader"])
    if trader is None:
        s.check("E_trader_found", {"error": "no trader"}, False)
        return
    view = s.bot.call("window/view", {"target": {"entity_id": int(trader["id"])}})
    options = [o for o in (view.get("options") or []) if isinstance(o, dict)]
    s.check("E_trader_view", {"trader": trader, "options": options}, bool(options))

    unavailable = next((o for o in options if o.get("available") is False), None)
    if unavailable:
        neg = json.loads(
            s.toolset.mc_skill_trade(int(trader["id"]), option=int(unavailable["index"]), times=1)
        )
        s.check("E_trader_unavailable_negative", neg, neg.get("error") == "option_unavailable")
    else:
        s.check("E_trader_unavailable_negative", {"note": "all options available"}, True)

    usable = next(
        (
            o for o in options
            if o.get("available") is not False
            and int(o.get("max_uses") or 0) - int(o.get("uses") or 0) >= 3
        ),
        None,
    )
    if usable is None:
        usable = next((o for o in options if o.get("available") is not False), None)
    if usable is None:
        s.check("E_trader_multi_trade", {"error": "no available trade"}, False)
    else:
        get_name = str((usable.get("get") or {}).get("name") or "")
        multi = json.loads(
            s.toolset.mc_skill_trade(int(trader["id"]), get_item=get_name, times=3)
        )
        done = multi.get("done") or []
        n_results = sum(len(d.get("results") or []) for d in done)
        s.check(
            "E_trader_multi_trade",
            {"picked": usable, "result": multi, "n_results": n_results},
            bool(multi.get("ok")) and n_results >= 1,
        )

    librarian = find_entity(s.bot, ("villager",), POS["librarian"])
    if librarian is None:
        s.check("E_librarian_found", {"error": "no librarian"}, False)
        return
    lview = s.bot.call("window/view", {"target": {"entity_id": int(librarian["id"])}})
    loptions = [o for o in (lview.get("options") or []) if isinstance(o, dict)]
    two_cost = next((o for o in loptions if len(o.get("give") or []) >= 2), None)
    s.check(
        "E_librarian_view",
        {"librarian": librarian, "options": loptions, "two_cost": two_cost},
        two_cost is not None,
    )
    if two_cost:
        trade = json.loads(
            s.toolset.mc_skill_trade(
                int(librarian["id"]), option=int(two_cost["index"]), times=1
            )
        )
        s.check(
            "E_librarian_two_cost",
            trade,
            bool(trade.get("ok")) and bool((trade.get("done") or [{}])[0].get("results")),
        )


def group_f(s: Suite) -> None:
    prepare_player({"iron_ingot": 8})
    beacon = POS["beacon"]
    action = s.act(beacon, [
        {"op": "put", "item": "iron_ingot", "count": 1, "to": "payment"},
        {"op": "select", "option": 0},
    ])
    view = s.view(beacon)
    s.check(
        "F_beacon_payment_select",
        {"act": action, "view": view},
        any(d.get("op") == "select" for d in (action.get("done") or []))
        and bool((view.get("info") or {}).get("primary")),
    )


GROUPS = {"A": group_a, "B": group_b, "C": group_c, "D": group_d, "E": group_e, "F": group_f}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", default="ABCDEF", help="e.g. A,B,C or ABCDEF")
    args = parser.parse_args()
    selected = [g for g in args.groups.replace(",", "").upper() if g in GROUPS]
    if not selected:
        raise SystemExit("no valid groups")

    from mc_agent.auth import TokenStore
    from mc_agent.bot import BotSession
    from mc_agent.config import Settings
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset

    os.environ.setdefault("MC_STATE_DIR", ".state-gui-test")
    os.environ.setdefault("MC_WORLD_MEMORY_PATH", ".state-gui-test/world_memory_GuiTest_01.json")

    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    record = TokenStore(pathlib.Path(settings.state_dir) / "tokens.json").get(BOT)
    if record is None:
        raise SystemExit(f"no cached token for {BOT}")
    bot = BotSession(record.token, bot_username=record.bot_username, base_url=settings.mc_api_base_url)
    toolset = McToolset(bot)
    report: dict[str, Any] = {"bot": BOT, "groups": selected, "ok": True, "checks": {}}
    try:
        report["world_setup"] = setup_world()
        try:
            st = bot.status()
        except ApiError:
            st = {}
        if not st.get("ready"):
            last = ""
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                try:
                    bot.connect(respect_reconnect_interval=False)
                    st = bot.wait_until_ready(timeout_s=30)
                    if st.get("ready"):
                        break
                except ApiError as exc:
                    last = f"{exc.code}: {exc.message}"
                time.sleep(8)
            else:
                raise RuntimeError(f"connect failed: {last}")
        report["status_ready"] = {k: st.get(k) for k in ("ready", "pos", "health", "food", "alive")}
        for key in selected:
            suite = Suite(bot, toolset)
            GROUPS[key](suite)
            report["checks"].update({f"{key}:{name}": value for name, value in suite.checks.items()})
            if not suite.ok:
                report["ok"] = False
            print(json.dumps({"group": key, "ok": suite.ok, "checks": {k: v["ok"] for k, v in suite.checks.items()}}, ensure_ascii=False))
        report["final_status"] = {k: bot.status().get(k) for k in ("ready", "pos", "health", "food", "alive")}
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            report["cleanup"] = rcon_batch(
                [
                    f"tp {BOT} {BASE_X} {BASE_Y} {BASE_Z + 6}",
                    f"effect give {BOT} minecraft:resistance 1800 4 true",
                    "forceload remove 3076 3078 3114 3110",
                ]
            )
            report["cleanup"].append(
                {"cmd": "forceload", "reply": "kept resistance; bot left online for next group"}
            )
        except Exception as exc:  # noqa: BLE001
            report["cleanup_error"] = str(exc)
        bot.close()

    out = ROOT / "runs" / f"gui-window-full-{'-'.join(selected)}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "groups": selected, "report": str(out)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
