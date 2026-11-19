#!/usr/bin/env python3
"""Real-world competition-fidelity single-concurrency CurriculumSupervisor endurance runner.

- Uses dedicated `MapTest_01` (reusing `.state-maptest/tokens.json` without re-login).
- Picks a dry land biome >= 2000m away from all other online bots on `nju-beast`.
- Zero artificial survival buffs (real normal-difficulty mobs, real hunger, real Y=-54 caves).
  Exception: scene slices grant short *scene-entry* safety effects only (nether fire_resistance,
  deep_water water_breathing) so a mountainside/ocean spawn cannot be a guaranteed instant death.
- Enforces `RULES.md` 220m `VoidGeofence`, `enable_pre_ensure=True`, `enable_island_recon=True`,
  and `enable_async_world_jobs=True`.

Scene slices (`--stage`, one unique site per fleet slot):
  bare / iron / midgame / nether / village     overworld checkpoints, `/spawnpoint` death return
  nether_fortress / nether_bastion             locate real structure, obsidian ledge + charged
                                               respawn anchor, `execute in the_nether` everywhere
  enchant_anvil                                flattened 9x7 pad with table/anvil/bookshelves
  deep_water                                   real `minecraft:deep_ocean` + dry staging pad

Every slice runs a background `SceneWatchdog`: wrong dimension / >400m drift / death (health<=0 or
inventory collapse at the anchor away from known chests) triggers `restock_scene` (teleport back +
re-apply the slice loadout) within one poll (~8s).

`--print-plan` prints the exact ground-prep + reset RCON batch for a stage without touching the
server, so scene requests can be reviewed offline before #1 triggers a live run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pathlib
import re
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
RUNS_DIR = WT_ROOT / "runs"

CANDIDATE_SITES: list[tuple[int, int]] = [
    # 24 anchors, each >= 2500m apart on both axes, so a 22-bot fleet can give every slot a
    # unique site and avoid the friendly-fire / player_explosion deaths seen on shared islands.
    (-6400, -6400),
    (6400, -6400),
    (-6400, 6400),
    (6400, 6400),
    (-10000, -3000),
    (10000, -3000),
    (-10000, 3000),
    (10000, 3000),
    (-3000, -10000),
    (3000, -10000),
    (-3000, 10000),
    (3000, 10000),
    (-11000, -11000),
    (11000, -11000),
    (-11000, 11000),
    (11000, 11000),
    (-7800, -2800),
    (7800, -2800),
    (-7800, 2800),
    (7800, 2800),
    (-5200, -7600),
    (5200, -7600),
    (-5200, 7600),
    (5200, 7600),
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


def _wait_settled(bot: Any, *, timeout_s: float = 12.0, settle_delta: float = 0.6) -> list[float] | None:
    """Poll status until the bot stops falling after a teleport/spreadplayers (returns last pos)."""
    pos: list[float] | None = None
    last_y: float | None = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.8)
        try:
            st = bot.status()
        except Exception:
            continue
        p = st.get("pos")
        if not isinstance(p, (list, tuple)) or len(p) < 3:
            continue
        pos = [float(p[0]), float(p[1]), float(p[2])]
        if last_y is not None and abs(pos[1] - last_y) < settle_delta:
            return pos
        last_y = pos[1]
    return pos


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
        pos = _wait_settled(bot) or [cx, 64, cz]
        st = bot.status()
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


def select_village_wild_site(bot: Any, bot_name: str, start_idx: int = 0) -> tuple[float, float, float, str]:
    """Locate a real natural village near a far-away candidate anchor via RCON `locate structure`."""
    cx, cz = CANDIDATE_SITES[start_idx % len(CANDIDATE_SITES)]
    res = rcon_batch(
        [f"execute positioned {cx} 100 {cz} run locate structure #minecraft:village"],
        timeout=45,
    )
    reply = str((res[0] if res else {}).get("reply") or "")
    m = re.search(r"\[\s*(-?\d+)\s*,\s*[^,\]]+,\s*(-?\d+)\s*\]", reply)
    if m:
        vx, vz = int(m.group(1)), int(m.group(2))
        rcon_batch(
            [
                f"tp {bot_name} {vx} 120 {vz}",
                f"spreadplayers {vx} {vz} 0 24 false {bot_name}",
            ],
            timeout=45,
        )
        pos = _wait_settled(bot) or [vx, 64, vz]
        st = bot.status()
        pos = st.get("pos") or pos
        biome = str(st.get("biome") or "village").lower()
        return float(pos[0]), float(pos[1]), float(pos[2]), f"village:{biome}"
    return select_dry_isolated_wild_site(bot, bot_name, start_idx=start_idx)


def _locate_coords(reply: str) -> tuple[int, int] | None:
    """Parse vanilla `locate` chat replies such as `[123, ~, -45]` (dimension-agnostic)."""
    m = re.search(r"\[\s*(-?\d+)\s*,\s*[^,\]]+,\s*(-?\d+)\s*\]", str(reply or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _locate_structure(dim: str, cx: int, cz: int, structure_id: str) -> tuple[int, int] | None:
    """`locate structure` inside `dim`, returning None when the server says it cannot find one."""
    out = rcon_batch(
        [f"execute in {dim} positioned {cx} 64 {cz} run locate structure {structure_id}"],
        timeout=30,
    )
    reply = str((out[0] if out else {}).get("reply") or "")
    if "could not" in reply.lower() or "cannot" in reply.lower():
        return None
    return _locate_coords(reply)


def select_nether_structure_site(
    bot: Any,
    bot_name: str,
    structure_tag: str,
    start_idx: int = 0,
) -> tuple[float, float, float, str]:
    """Locate a Nether structure and carve a safe spawn ledge.

    `structure_tag` accepts the vanilla structure *tag* (`#minecraft:fortress`, used by callers for
    readability) but `locate` needs the concrete registry id (`minecraft:fortress`). The r12 fleet
    silently fell back to the anchor because `#minecraft:fortress` is not a valid structure tag.
    """
    structure_id = structure_tag.lstrip("#")
    candidates = (structure_id,)
    search_anchors = [(0, 0), (800, 0), (-800, 0), (0, 800), (0, -800), (800, 800)]
    nx: int | None = None
    nz: int | None = None
    for offset in range(len(search_anchors)):
        sx, sz = search_anchors[(start_idx + offset) % len(search_anchors)]
        for cand in candidates:
            coords = _locate_structure("minecraft:the_nether", sx, sz, cand)
            if coords is not None:
                nx, nz = coords
                break
        if nx is not None:
            break
    if nx is None:
        nx, nz = search_anchors[start_idx % len(search_anchors)]
        print(
            f"[Scene] locate {structure_id} failed across {len(search_anchors)} anchors; "
            f"falling back to ({nx}, {nz})",
            flush=True,
        )
    ny = 64
    prep = prepare_scene_ground(
        bot_name, "nether_fortress", float(nx), float(ny), float(nz), dim="the_nether"
    )
    # Fire resistance + resistance shield soften the first ~30s of fortress/bastion aggro while the bot boots up.
    prep.extend(
        [
            f"effect give {bot_name} minecraft:fire_resistance 30 0 true",
            f"effect give {bot_name} minecraft:resistance 15 4 true",
        ]
    )
    rcon_batch(prep, timeout=45)
    pos = _wait_settled(bot, timeout_s=8.0) or [float(nx) + 0.5, float(ny), float(nz) + 0.5]
    st = bot.status()
    if "nether" not in str(st.get("dimension") or "").lower():
        # `tp` may not have settled yet; never anchor the scene to a stale overworld position.
        pos = [float(nx) + 0.5, float(ny), float(nz) + 0.5]
    return float(pos[0]), float(pos[1]), float(pos[2]), f"nether:{structure_tag.split(':')[-1]}"


def select_deep_water_site(bot: Any, bot_name: str, start_idx: int = 0) -> tuple[float, float, float, str]:
    """Locate a real `minecraft:deep_ocean` biome and build a safe surface staging platform above the water.

    The bot must be able to *dive* into deep water for the slice, but must never spawn inside it
    (drowning on episode 1 would poison every downstream metric). A 7x7 cobblestone platform with a
    3-block air pocket keeps the spawn dry while leaving open water within a few blocks.
    """
    cx, cz = CANDIDATE_SITES[start_idx % len(CANDIDATE_SITES)]
    res = rcon_batch(
        [f"execute positioned {cx} 100 {cz} run locate biome minecraft:deep_ocean"],
        timeout=45,
    )
    coords = _locate_coords(str((res[0] if res else {}).get("reply") or ""))
    if coords is not None:
        wx, wz = coords
    else:
        wx, wz = cx, cz
    rcon_batch(
        [
            f"tp {bot_name} {wx} 120 {wz}",
            f"spreadplayers {wx} {wz} 0 40 false {bot_name}",
        ],
        timeout=45,
    )
    # `spreadplayers` drops the bot from y=120; polling too early reads a mid-fall y and would build
    # a floating platform tens of blocks above the ocean surface. Wait until y stabilises first.
    pos = _wait_settled(bot, timeout_s=15.0) or [float(wx), 70.0, float(wz)]
    st = bot.status()
    ix, iz = int(pos[0]), int(pos[2])
    # Deep ocean sea level is 63; clamp so a stale reading can never produce a sky platform.
    surface_y = min(max(int(round(pos[1])) + 1, 64), 70)
    prep = [
        f"forceload add {ix - 16} {iz - 16} {ix + 16} {iz + 16}",
        f"fill {ix - 3} {surface_y - 1} {iz - 3} {ix + 3} {surface_y - 1} {iz + 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz - 3} {ix + 3} {surface_y + 3} {iz + 3} minecraft:air",
        # 2-high cobblestone parapet blocks drowned trident line-of-sight (r13/r14 showed all pad
        # damage came from tridents); a 1x2 gate on the east side keeps the dive route open.
        f"fill {ix - 3} {surface_y} {iz - 3} {ix + 3} {surface_y + 1} {iz - 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz + 3} {ix + 3} {surface_y + 1} {iz + 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz - 2} {ix - 3} {surface_y + 1} {iz + 2} minecraft:cobblestone",
        f"fill {ix + 3} {surface_y} {iz - 3} {ix + 3} {surface_y + 1} {iz - 1} minecraft:cobblestone",
        f"fill {ix + 3} {surface_y} {iz + 1} {ix + 3} {surface_y + 1} {iz + 3} minecraft:cobblestone",
        f"tp {bot_name} {ix}.5 {surface_y} {iz}.5",
        f"spawnpoint {bot_name} {ix} {surface_y} {iz}",
    ]
    rcon_batch(prep, timeout=45)
    time.sleep(1.0)
    biome = str(st.get("biome") or "deep_ocean").lower()
    return float(ix) + 0.5, float(surface_y), float(iz) + 0.5, f"deep_water:{biome}"


def prepare_scene_ground(
    bot_name: str,
    stage: str,
    sx: float,
    sy: float,
    sz: float,
    *,
    dim: str = "overworld",
) -> list[str]:
    """Build / re-assert an idempotent safe standing surface for the active slice.

    Returns the RCON command list (callers may append scene-specific effects before batching).
    All fills are absolute and idempotent so this can be re-run after a death without drifting.
    """
    ix, iy, iz = int(round(sx)), int(round(sy)), int(round(sz))
    cmds: list[str] = []
    if dim == "the_nether":
        # Obsidian ledge (lava-proof), 3-block headroom, charged respawn anchor for in-Nether respawns.
        # Every command MUST be prefixed with `execute in minecraft:the_nether run` — bare RCON
        # fill/setblock/tp would otherwise act on the overworld coordinates.
        pre = "execute in minecraft:the_nether run"
        cmds.extend(
            [
                f"{pre} forceload add {ix - 16} {iz - 16} {ix + 16} {iz + 16}",
                f"{pre} fill {ix - 3} {iy - 1} {iz - 3} {ix + 3} {iy - 1} {iz + 3} minecraft:obsidian",
                f"{pre} fill {ix - 3} {iy} {iz - 3} {ix + 3} {iy + 3} {iz + 3} minecraft:air",
                f"{pre} setblock {ix + 3} {iy - 1} {iz} minecraft:respawn_anchor[charges=4]",
                f"{pre} tp {bot_name} {ix}.5 {iy} {iz}.5",
                f"{pre} spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "enchant_anvil":
        # Flatten a 9x7 pad so table/anvil/bookshelves never float or clip into a hillside.
        cmds.extend(
            [
                f"fill {ix - 3} {iy - 1} {iz - 3} {ix + 5} {iy - 1} {iz + 3} minecraft:cobblestone",
                f"fill {ix - 3} {iy} {iz - 3} {ix + 5} {iy + 3} {iz + 3} minecraft:air",
                f"setblock {ix + 2} {iy} {iz} minecraft:enchanting_table",
                f"setblock {ix + 2} {iy} {iz + 2} minecraft:anvil",
                f"setblock {ix + 4} {iy} {iz} minecraft:bookshelf",
                f"setblock {ix + 4} {iy} {iz - 1} minecraft:bookshelf",
                f"setblock {ix + 4} {iy} {iz + 1} minecraft:bookshelf",
                f"tp {bot_name} {ix}.5 {iy} {iz}.5",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "brewing_alchemy":
        # 9x7 cobblestone pad with brewing stand, 2x2 infinite water source, and crafting table.
        cmds.extend(
            [
                f"fill {ix - 3} {iy - 1} {iz - 3} {ix + 5} {iy - 1} {iz + 3} minecraft:cobblestone",
                f"fill {ix - 3} {iy} {iz - 3} {ix + 5} {iy + 3} {iz + 3} minecraft:air",
                f"setblock {ix + 2} {iy} {iz} minecraft:brewing_stand",
                f"setblock {ix + 2} {iy} {iz + 2} minecraft:crafting_table",
                f"fill {ix + 3} {iy - 1} {iz - 2} {ix + 4} {iy - 1} {iz - 1} minecraft:water",
                f"tp {bot_name} {ix}.5 {iy} {iz}.5",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "cave_combat":
        # Enclosed 11x11x4 cobblestone combat arena with roof (prevents sunburn) and torches at corners.
        cmds.extend(
            [
                f"fill {ix - 5} {iy - 1} {iz - 5} {ix + 5} {iy - 1} {iz + 5} minecraft:cobblestone",
                f"fill {ix - 5} {iy} {iz - 5} {ix + 5} {iy + 3} {iz + 5} minecraft:air",
                f"fill {ix - 5} {iy + 4} {iz - 5} {ix + 5} {iy + 4} {iz + 5} minecraft:cobblestone",
                f"fill {ix - 5} {iy} {iz - 5} {ix + 5} {iy + 3} {iz - 5} minecraft:cobblestone",
                f"fill {ix - 5} {iy} {iz + 5} {ix + 5} {iy + 3} {iz + 5} minecraft:cobblestone",
                f"fill {ix - 5} {iy} {iz - 4} {ix - 5} {iy + 3} {iz + 4} minecraft:cobblestone",
                f"fill {ix + 5} {iy} {iz - 4} {ix + 5} {iy + 3} {iz + 4} minecraft:cobblestone",
                f"setblock {ix - 4} {iy} {iz - 4} minecraft:torch",
                f"setblock {ix + 4} {iy} {iz + 4} minecraft:torch",
                f"summon minecraft:zombie {ix + 3}.5 {iy} {iz}.5",
                f"summon minecraft:skeleton {ix - 3}.5 {iy} {iz + 3}.5",
                f"tp {bot_name} {ix}.5 {iy} {iz}.5",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "deep_water":
        # Re-assert the dry staging pad on death return (idempotent, only when spawned at/below sea level).
        if iy <= 63:
            cmds.extend(
                [
                    f"fill {ix - 3} {iy - 1} {iz - 3} {ix + 3} {iy - 1} {iz + 3} minecraft:cobblestone",
                    f"fill {ix - 3} {iy} {iz - 3} {ix + 3} {iy + 3} {iz + 3} minecraft:air",
                ]
            )
        return cmds
    return cmds


def stage_loadout_cmds(bot_name: str, stage: str, sx: float, sy: float, sz: float) -> list[str]:
    """Stage-specific advancements/inventory only (no teleport, no clear, no platform)."""
    cmds: list[str] = []
    if stage == "iron":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"give {bot_name} minecraft:iron_pickaxe 1",
                f"give {bot_name} minecraft:stone_pickaxe 2",
                f"give {bot_name} minecraft:stone_sword 1",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:coal 16",
                f"give {bot_name} minecraft:cooked_beef 16",
                f"give {bot_name} minecraft:crafting_table 2",
                f"give {bot_name} minecraft:furnace 1",
                f"give {bot_name} minecraft:cobblestone 32",
            ]
        )
    elif stage == "midgame":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/mine_diamond",
                f"advancement grant {bot_name} only minecraft:story/shiny_gear",
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:diamond_boots 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:bucket 1",
                f"give {bot_name} minecraft:shears 1",
                f"give {bot_name} minecraft:iron_ingot 16",
                f"give {bot_name} minecraft:redstone 8",
                f"give {bot_name} minecraft:lapis_lazuli 8",
                f"give {bot_name} minecraft:emerald 8",
                f"give {bot_name} minecraft:obsidian 4",
                f"give {bot_name} minecraft:book 2",
                f"give {bot_name} minecraft:diamond 4",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:coal 16",
                f"give {bot_name} minecraft:cooked_beef 16",
                f"give {bot_name} minecraft:cobblestone 32",
            ]
        )
    elif stage == "village":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/mine_diamond",
                f"advancement grant {bot_name} only minecraft:story/shiny_gear",
                f"xp set {bot_name} 15 levels",
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:diamond_boots 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:emerald 16",
                f"give {bot_name} minecraft:iron_block 4",
                f"give {bot_name} minecraft:shears 1",
                f"give {bot_name} minecraft:obsidian 4",
                f"give {bot_name} minecraft:book 2",
                f"give {bot_name} minecraft:diamond 4",
                f"give {bot_name} minecraft:lapis_lazuli 12",
                f"give {bot_name} minecraft:wheat 24",
                f"give {bot_name} minecraft:paper 24",
                f"give {bot_name} minecraft:coal 20",
                f"give {bot_name} minecraft:bucket 1",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:cooked_beef 16",
            ]
        )
    elif stage == "nether":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/mine_diamond",
                f"advancement grant {bot_name} only minecraft:story/shiny_gear",
                f"advancement grant {bot_name} only minecraft:story/lava_bucket",
                f"advancement grant {bot_name} only minecraft:story/form_obsidian",
                f"give {bot_name} minecraft:obsidian 14",
                f"give {bot_name} minecraft:flint_and_steel 1",
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:golden_boots 1",
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:iron_leggings 1",
                f"give {bot_name} minecraft:bow 1",
                f"give {bot_name} minecraft:arrow 32",
                f"give {bot_name} minecraft:gold_ingot 16",
                f"give {bot_name} minecraft:cobblestone 64",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:cooked_beef 24",
            ]
        )
    elif stage in {"nether_fortress", "nether_bastion"}:
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/mine_diamond",
                f"advancement grant {bot_name} only minecraft:story/shiny_gear",
                f"advancement grant {bot_name} only minecraft:story/lava_bucket",
                f"advancement grant {bot_name} only minecraft:story/form_obsidian",
                f"advancement grant {bot_name} only minecraft:story/enter_the_nether",
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:golden_boots 1",
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:iron_leggings 1",
                f"give {bot_name} minecraft:bow 1",
                f"give {bot_name} minecraft:arrow 32",
                f"give {bot_name} minecraft:gold_ingot 24",
                f"give {bot_name} minecraft:cobblestone 64",
                f"give {bot_name} minecraft:obsidian 8",
                f"give {bot_name} minecraft:flint_and_steel 1",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:glass_bottle 3",
                f"give {bot_name} minecraft:cooked_beef 24",
            ]
        )
    elif stage == "enchant_anvil":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/mine_diamond",
                f"advancement grant {bot_name} only minecraft:story/shiny_gear",
                f"xp set {bot_name} 30 levels",
                f"give {bot_name} minecraft:lapis_lazuli 32",
                f"give {bot_name} minecraft:book 6",
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:diamond_sword 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:cooked_beef 16",
            ]
        )
    elif stage == "brewing_alchemy":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"advancement grant {bot_name} only minecraft:story/enter_the_nether",
                f"give {bot_name} minecraft:blaze_powder 8",
                f"give {bot_name} minecraft:nether_wart 8",
                f"give {bot_name} minecraft:glass_bottle 9",
                f"give {bot_name} minecraft:magma_cream 4",
                f"give {bot_name} minecraft:sugar 4",
                f"give {bot_name} minecraft:redstone 4",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:cooked_beef 16",
            ]
        )
    elif stage == "village_trade":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"give {bot_name} minecraft:emerald 32",
                f"give {bot_name} minecraft:wheat 32",
                f"give {bot_name} minecraft:stick 64",
                f"give {bot_name} minecraft:coal 32",
                f"give {bot_name} minecraft:paper 32",
                f"give {bot_name} minecraft:iron_ingot 16",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:cooked_beef 16",
            ]
        )
    elif stage == "cave_combat":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:iron_pickaxe 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:bow 1",
                f"give {bot_name} minecraft:arrow 24",
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:iron_leggings 1",
                f"give {bot_name} minecraft:iron_boots 1",
                f"give {bot_name} minecraft:torch 16",
                f"give {bot_name} minecraft:cooked_beef 24",
            ]
        )
    elif stage == "deep_water":
        cmds.extend(
            [
                f"advancement grant {bot_name} only minecraft:story/mine_stone",
                f"advancement grant {bot_name} only minecraft:story/upgrade_tools",
                f"advancement grant {bot_name} only minecraft:story/smelt_iron",
                f"advancement grant {bot_name} only minecraft:story/iron_tools",
                f"give {bot_name} minecraft:iron_pickaxe 1",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:bucket 2",
                f"give {bot_name} minecraft:oak_boat 1",
                f'give {bot_name} minecraft:potion[minecraft:potion_contents={{potion:"minecraft:water_breathing"}}] 2',
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
                f"give {bot_name} minecraft:iron_leggings 1",
                f"give {bot_name} minecraft:iron_boots 1",
                f"give {bot_name} minecraft:oak_log 16",
                f"give {bot_name} minecraft:cooked_beef 24",
            ]
        )
    return cmds


NETHER_STAGES = frozenset({"nether_fortress", "nether_bastion"})


def scene_dimension(stage: str) -> str:
    return "the_nether" if stage in NETHER_STAGES else "overworld"


@dataclasses.dataclass(frozen=True)
class SceneAnchor:
    """Immutable description of the slice's safe scene anchor (used by guard + watchdog)."""

    stage: str
    dim: str
    x: float
    y: float
    z: float
    radius: float = 220.0
    drift_limit: float = 400.0


def build_reset_cmds(
    bot_name: str,
    sx: float,
    sy: float,
    sz: float,
    *,
    set_day: bool = True,
    stage: str = "bare",
    dim: str | None = None,
) -> list[str]:
    """Build the checkpoint reset RCON batch (dimension-aware; shared by live reset and --print-plan)."""
    dim = dim or scene_dimension(stage)
    ix, iy, iz = int(round(sx)), int(round(sy)), int(round(sz))
    cmds: list[str] = ["difficulty normal", f"gamemode survival {bot_name}", f"clear {bot_name}"]
    if dim == "the_nether":
        cmds.extend(
            [
                f"execute in minecraft:the_nether run tp {bot_name} {sx:.1f} {sy:.1f} {sz:.1f}",
                f"execute in minecraft:the_nether run spawnpoint {bot_name} {ix} {iy} {iz}",
                f"advancement revoke {bot_name} everything",
                f"advancement grant {bot_name} only minecraft:story/root",
                f"advancement grant {bot_name} only minecraft:adventure/root",
                f"advancement grant {bot_name} only minecraft:husbandry/root",
                f"advancement grant {bot_name} only minecraft:nether/root",
            ]
        )
    else:
        cmds.extend(
            [
                f"tp {bot_name} {sx:.1f} {sy:.1f} {sz:.1f}",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
                f"advancement revoke {bot_name} everything",
                f"advancement grant {bot_name} only minecraft:story/root",
                f"advancement grant {bot_name} only minecraft:adventure/root",
                f"advancement grant {bot_name} only minecraft:husbandry/root",
                f"advancement grant {bot_name} only minecraft:nether/root",
                f"advancement grant {bot_name} only minecraft:end/root",
            ]
        )
    cmds.extend(
        [
            f"effect clear {bot_name}",
            f"effect give {bot_name} minecraft:instant_health 1 10 true",
            f"effect give {bot_name} minecraft:saturation 2 10 true",
        ]
    )
    if dim == "the_nether":
        cmds.append(f"effect give {bot_name} minecraft:fire_resistance 45 0 true")
    if stage == "deep_water":
        # Scene-safety window (analogous to nether fire_resistance): r13 showed a deep_ocean spawn
        # dying to drowned tridents/drowning before orienting. 60s is enough to walk to a dive point.
        cmds.extend(
            [
                f"effect give {bot_name} minecraft:water_breathing 60 0 true",
                f"effect give {bot_name} minecraft:resistance 60 1 true",
            ]
        )
    cmds.extend(stage_loadout_cmds(bot_name, stage, sx, sy, sz))
    if set_day and dim != "the_nether":
        cmds.insert(0, "time set 1000")
    return cmds


def reset_wild_bot(
    bot_name: str,
    sx: float,
    sy: float,
    sz: float,
    *,
    set_day: bool = True,
    stage: str = "bare",
    dim: str | None = None,
) -> None:
    """Reset the bot to the slice checkpoint. Dimension-aware: Nether slices never `tp` to overworld."""
    rcon_batch(
        build_reset_cmds(bot_name, sx, sy, sz, set_day=set_day, stage=stage, dim=dim),
        timeout=60,
    )


def restock_scene(bot_name: str, scene: SceneAnchor, *, reason: str, clear_inventory: bool = False) -> None:
    """Death-return / drift-return: teleport back to the scene anchor, and re-issue loadout on death."""
    ix, iy, iz = int(round(scene.x)), int(round(scene.y)), int(round(scene.z))
    cmds = [
        f"gamemode survival {bot_name}",
        f"effect clear {bot_name}",
        f"effect give {bot_name} minecraft:instant_health 1 10 true",
        f"effect give {bot_name} minecraft:saturation 1 20 true",
        f"effect give {bot_name} minecraft:resistance 20 4 true",
    ]
    if clear_inventory:
        cmds.insert(1, f"clear {bot_name}")
    if scene.dim == "the_nether":
        cmds.extend(
            [
                f"execute in minecraft:the_nether run tp {bot_name} {scene.x:.1f} {scene.y:.1f} {scene.z:.1f}",
                f"execute in minecraft:the_nether run spawnpoint {bot_name} {ix} {iy} {iz}",
                f"execute in minecraft:the_nether run setblock {ix + 3} {iy - 1} {iz} minecraft:respawn_anchor[charges=4]",
                f"effect give {bot_name} minecraft:fire_resistance 60 0 true",
            ]
        )
    else:
        cmds.extend(
            [
                f"tp {bot_name} {scene.x:.1f} {scene.y:.1f} {scene.z:.1f}",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
    cmds.extend(prepare_scene_ground(bot_name, scene.stage, scene.x, scene.y, scene.z, dim=scene.dim))
    if scene.stage == "deep_water":
        cmds.extend(
            [
                f"effect give {bot_name} minecraft:water_breathing 45 0 true",
                f"effect give {bot_name} minecraft:resistance 45 1 true",
            ]
        )
    if clear_inventory:
        cmds.extend(stage_loadout_cmds(bot_name, scene.stage, scene.x, scene.y, scene.z))
    rcon_batch(cmds, timeout=60)
    print(
        f"[Scene Restock] {bot_name} stage={scene.stage} reason={reason} restock={clear_inventory} "
        f"-> ({scene.x:.1f}, {scene.y:.1f}, {scene.z:.1f})",
        flush=True,
    )


class SceneWatchdog(threading.Thread):
    """Background death-return + drift-return guard for a single-bot scene slice.

    The LLM episode hook only fires at `curriculum.episode.started`, so a death mid-episode would
    otherwise leave the bot at the world spawn with an empty inventory until the episode ends.
    This watchdog polls status every `interval_s` and returns/restocks within one cycle.
    """

    def __init__(
        self,
        bot: Any,
        bot_name: str,
        scene: SceneAnchor,
        *,
        interval_s: float = 8.0,
        near_container: Any = None,
    ) -> None:
        super().__init__(name=f"scene-watchdog-{bot_name}", daemon=True)
        self.bot = bot
        self.bot_name = bot_name
        self.scene = scene
        self.interval_s = interval_s
        self.near_container = near_container
        self._stop = threading.Event()
        self._last_return = 0.0
        self._last_total = -1
        self._inv_tick = 0

    def stop(self) -> None:
        self._stop.set()

    def _owned_total(self) -> int | None:
        """Total item count from the real `/inventory` shape (items[] + offhand + armor)."""
        try:
            inv = self.bot.inventory()
        except Exception:
            return None
        if not isinstance(inv, dict):
            return None
        total = 0
        items = inv.get("items")
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, dict):
                    continue
                try:
                    total += int(row.get("count") or 0)
                except (TypeError, ValueError):
                    continue
        else:
            # Tolerate a legacy flat {item: count} mapping used by older API builds/tests.
            for k, v in inv.items():
                if str(k).startswith("_") or str(k) in ("offhand", "armor"):
                    continue
                try:
                    total += int(v)
                except (TypeError, ValueError):
                    continue
        off = inv.get("offhand")
        if isinstance(off, dict):
            onm = str(off.get("name") or "").removeprefix("minecraft:")
            if onm and onm != "air":
                try:
                    total += max(1, int(off.get("count") or 1))
                except (TypeError, ValueError):
                    total += 1
        armor = inv.get("armor")
        if isinstance(armor, dict):
            for slot in ("head", "chest", "torso", "legs", "feet"):
                piece = armor.get(slot)
                if isinstance(piece, dict):
                    anm = str(piece.get("name") or "").removeprefix("minecraft:")
                    if anm and anm != "air":
                        total += 1
        return total

    def _tick(self) -> None:
        try:
            st = self.bot.status()
        except Exception:
            return
        if not st:
            return
        ready = bool(st.get("ready"))
        health = float(st.get("health") or 0.0)
        alive = st.get("alive")
        # The API reports ready=false for the whole death/respawn window; do not skip it, otherwise
        # the death itself is never observed and the bot comes back with an empty inventory.
        dead_signal = health <= 0.0 or alive is False
        if not ready and not dead_signal:
            return
        pos = st.get("pos") or []
        dim = str(st.get("dimension") or "overworld").lower()
        in_nether = "nether" in dim
        in_end = "end" in dim
        # Overworld slices may legitimately portal into the Nether (stage=`nether` builds and enters a
        # portal); only the opposite case — a Nether slice bot sitting in the overworld — is a fault.
        dim_matches = in_nether if self.scene.dim == "the_nether" else not (in_nether or in_end)
        wrong_dim = self.scene.dim == "the_nether" and not dim_matches
        dist = (
            math.hypot(float(pos[0]) - self.scene.x, float(pos[2]) - self.scene.z)
            if isinstance(pos, (list, tuple)) and len(pos) >= 3
            else float("inf")
        )
        drifted = dim_matches and dist > self.scene.drift_limit and ready
        suspected_death = dead_signal
        if not suspected_death:
            self._inv_tick += 1
            if self._inv_tick % 3 == 1:  # sample on the very first tick, then every 3rd (~24s)
                total = self._owned_total()
                if total is not None:
                    # Only infer death from an inventory collapse when the bot is back at the scene
                    # anchor and not standing next to a known chest (otherwise chest deposits and
                    # deliberate stashing would trigger free-gear restocks).
                    at_anchor = dim_matches and dist <= 48.0
                    stashing = False
                    if self.near_container is not None:
                        try:
                            stashing = bool(self.near_container(dim, pos))
                        except Exception:
                            stashing = False
                    if (
                        at_anchor
                        and not stashing
                        and self._last_total >= 6
                        and total <= max(1, self._last_total // 4)
                        and self._last_total - total >= 6
                    ):
                        suspected_death = True
                    self._last_total = total
        if not (wrong_dim or drifted or suspected_death):
            return
        now = time.monotonic()
        if now - self._last_return < 20.0:
            return
        if suspected_death:
            reason = "death_return"
        elif wrong_dim:
            reason = f"wrong_dimension({dim})"
        else:
            reason = f"drift_{dist:.0f}m"
        near_empty = self._last_total >= 0 and self._last_total <= 2
        try:
            restock_scene(
                self.bot_name,
                self.scene,
                reason=reason,
                clear_inventory=suspected_death or (wrong_dim and near_empty),
            )
        except Exception as exc:  # never kill the watchdog thread on a transient RCON hiccup
            print(f"[Scene Restock] {self.bot_name} failed reason={reason}: {exc}", flush=True)
        finally:
            self._last_return = time.monotonic()
            self._last_total = -1

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._tick()
            except Exception as exc:
                print(f"[Scene Watchdog] {self.bot_name} tick error: {exc}", flush=True)


def verify_scene_entry(bot: Any, bot_name: str, scene: SceneAnchor, *, attempts: int = 2) -> bool:
    """Confirm the bot is in the scene dimension and within 64m of the anchor; retry via restock."""
    for i in range(attempts):
        try:
            st = bot.status()
        except Exception:
            st = {}
        dim = str(st.get("dimension") or "overworld").lower()
        pos = st.get("pos")
        ok_dim = scene.dim != "the_nether" or "nether" in dim
        ok_pos = isinstance(pos, (list, tuple)) and len(pos) >= 3 and (
            math.hypot(float(pos[0]) - scene.x, float(pos[2]) - scene.z) <= 64.0
        )
        if ok_dim and ok_pos:
            return True
        print(
            f"[Scene] entry verify failed (attempt {i + 1}/{attempts}) bot={bot_name} "
            f"dim={dim} pos={pos} expected {scene.dim}@({scene.x:.1f},{scene.y:.1f},{scene.z:.1f})",
            flush=True,
        )
        try:
            restock_scene(bot_name, scene, reason="scene_entry_verify")
        except Exception as exc:
            print(f"[Scene] entry verify restock error: {exc}", flush=True)
        time.sleep(2.0)
    return False


def ensure_bot_ready(
    bot: Any,
    *,
    timeout_s: float = 45.0,
    max_cooldown_wait_s: float = 420.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """Bring the bot to `ready: True`, sleeping through the API's death-loop cooldown instead of dying.

    BUG-021: a bot that died at the end of the previous wave enters a ~130s `death_loop_cooldown`;
    `bot.connect()` raises `DeathLoopCooldownError` and the old runner aborted the whole run (an
    entire fleet slot was lost with 0 events). We now wait out the reported ETA and retry.
    """
    from mc_agent.errors import ApiError, BotNotReadyError, DeathLoopCooldownError

    waited = 0.0
    last_status: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        try:
            last_status = bot.status() or {}
        except DeathLoopCooldownError as exc:
            last_status = {
                "ready": False,
                "code": "death_loop_cooldown",
                "cooldown_remaining_seconds": exc.cooldown_remaining_seconds,
            }
        except ApiError as exc:
            if exc.code in ("not_connected", "not_ready"):
                last_status = {"ready": False, "code": exc.code}
            else:
                raise
        if last_status.get("ready"):
            return last_status
        cooldown = float(last_status.get("cooldown_remaining_seconds") or 0.0)
        try:
            bot.connect(respect_reconnect_interval=False)
            return bot.wait_until_ready(timeout_s=timeout_s)
        except DeathLoopCooldownError as exc:
            cooldown = float(exc.cooldown_remaining_seconds or cooldown or 60.0)
        except BotNotReadyError:
            cooldown = 0.0
        if cooldown > 0:
            if waited + cooldown > max_cooldown_wait_s:
                print(
                    f"[Cooldown] death-loop cooldown {cooldown:.0f}s exceeds budget "
                    f"{max_cooldown_wait_s:.0f}s (attempt {attempt}/{attempts}); giving up",
                    flush=True,
                )
                raise DeathLoopCooldownError(
                    code="death_loop_cooldown",
                    message=f"cooldown {cooldown:.0f}s exceeds startup budget",
                    cooldown_remaining_seconds=cooldown,
                )
            wait_s = max(1.0, cooldown + 2.0)
            print(
                f"[Cooldown] bot in death-loop cooldown {cooldown:.0f}s "
                f"(attempt {attempt}/{attempts}); sleeping {wait_s:.0f}s then retrying",
                flush=True,
            )
            time.sleep(wait_s)
            waited += wait_s
        elif attempt < attempts:
            time.sleep(2.0)
    raise RuntimeError(f"bot did not become ready after {attempts} attempts (cooldown waited {waited:.0f}s)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", type=str, default=BOT)
    parser.add_argument("--token-file", type=str, default=str(TOKEN_FILE))
    parser.add_argument("--rpm-budget", type=int, default=None, help="Per-process RPM cap (e.g. 12 for 2x concurrency)")
    parser.add_argument("--provider", type=str, default=None, help="LLM provider override (nju/opencode/deepseek/dashscope)")
    parser.add_argument("--model", type=str, default=None, help="LLM model override (e.g. glm-5.3)")
    parser.add_argument("--runs-subdir", type=str, default="", help="Optional subdirectory under runs/ for multi-bot isolation")
    parser.add_argument("--duration-s", type=float, default=1800.0, help="Wall-clock budget for this wild run")
    parser.add_argument("--max-episodes", type=int, default=12)
    parser.add_argument("--site-index", type=int, default=0)
    parser.add_argument(
        "--stage",
        choices=[
            "bare",
            "iron",
            "midgame",
            "village",
            "village_trade",
            "nether",
            "nether_fortress",
            "nether_bastion",
            "enchant_anvil",
            "brewing_alchemy",
            "cave_combat",
            "deep_water",
        ],
        default="bare",
        help="Starting checkpoint stage for sliced testing",
    )
    parser.add_argument("--tag", type=str, default="wild")
    parser.add_argument("--keep-time", action="store_true", help="Do not reset world time to morning")
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="Print the exact ground-prep + reset RCON batch for this stage and exit (no network)",
    )
    parser.add_argument(
        "--plan-pos",
        type=str,
        default=None,
        help="Anchor x,y,z used by --print-plan (default: first CANDIDATE_SITES entry)",
    )
    args = parser.parse_args()

    bot_name = args.bot
    if args.print_plan:
        if args.plan_pos:
            try:
                px, py, pz = (float(v) for v in args.plan_pos.split(","))
            except ValueError:
                raise SystemExit("--plan-pos must be 'x,y,z'")
        else:
            ax, az = CANDIDATE_SITES[args.site_index % len(CANDIDATE_SITES)]
            px, py, pz = float(ax) + 0.5, 64.0, float(az) + 0.5
        dim = scene_dimension(args.stage)
        plan = {
            "bot": bot_name,
            "stage": args.stage,
            "dimension": dim,
            "anchor": [px, py, pz],
            "ground": prepare_scene_ground(bot_name, args.stage, px, py, pz, dim=dim),
            "reset": build_reset_cmds(
                bot_name, px, py, pz, set_day=not args.keep_time, stage=args.stage, dim=dim
            ),
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

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
        st = ensure_bot_ready(bot, timeout_s=45.0)

        if args.stage in ("village", "village_trade"):
            sx, sy, sz, biome = select_village_wild_site(bot, bot_name, start_idx=args.site_index)
        elif args.stage == "nether_fortress":
            sx, sy, sz, biome = select_nether_structure_site(bot, bot_name, "minecraft:fortress", start_idx=args.site_index)
        elif args.stage == "nether_bastion":
            sx, sy, sz, biome = select_nether_structure_site(bot, bot_name, "minecraft:bastion_remnant", start_idx=args.site_index)
        elif args.stage == "deep_water":
            sx, sy, sz, biome = select_deep_water_site(bot, bot_name, start_idx=args.site_index)
        else:
            sx, sy, sz, biome = select_dry_isolated_wild_site(bot, bot_name, start_idx=args.site_index)
        dim = scene_dimension(args.stage)
        scene = SceneAnchor(stage=args.stage, dim=dim, x=sx, y=sy, z=sz)
        prep = prepare_scene_ground(bot_name, args.stage, sx, sy, sz, dim=dim)
        if prep:
            rcon_batch(prep, timeout=60)
        reset_wild_bot(bot_name, sx, sy, sz, set_day=not args.keep_time, stage=args.stage, dim=dim)
        time.sleep(1.5)
        entry_ok = verify_scene_entry(bot, bot_name, scene)

        geofence = VoidGeofence(center_x=sx, center_z=sz, radius=220.0, margin=8.0)
        toolset = McToolset(bot, geofence=geofence, wait_for_cooldown=True)

        print(
            f"[Wild Run Start] bot={bot_name} stage={args.stage} provider={args.provider or 'default'} "
            f"tag={args.tag} rpm={args.rpm_budget or 'default'} "
            f"spawn=({sx:.1f}, {sy:.1f}, {sz:.1f}) biome={biome} "
            f"duration_s={args.duration_s} max_episodes={args.max_episodes}",
            flush=True,
        )

        qwen = QwenModel(
            QwenSpec(
                dotenv_path=str(ENV_FILE),
                fallback_dotenv_path=str(MAIN_ROOT / ".env"),
                provider=args.provider,
                model_override=args.model,
                rpm_budget_override=args.rpm_budget,
            )
        )
        def _near_known_container(dim_cur: str, pos_cur: Any, radius: float = 8.0) -> bool:
            """True when the bot stands next to a remembered chest/container (so item loss is a stash, not a death)."""
            try:
                if not isinstance(pos_cur, (list, tuple)) or len(pos_cur) < 3:
                    return False
                dim_key = "nether" if "nether" in dim_cur else dim_cur
                for key in toolset.world_memory.containers:
                    cdim = str(key[0]).lower()
                    cdim_norm = "nether" if "nether" in cdim else cdim
                    if cdim_norm != dim_key:
                        continue
                    _, cx, cy, cz = key
                    if math.hypot(float(pos_cur[0]) - cx, float(pos_cur[2]) - cz) <= radius and abs(float(pos_cur[1]) - cy) <= 4:
                        return True
            except Exception:
                return False
            return False

        def _guard_scene_anchor(ev: dict[str, Any]) -> None:
            if ev.get("type") != "curriculum.episode.started":
                return
            try:
                st_cur = bot.status()
                dim_cur = str(st_cur.get("dimension") or "overworld").lower()
                p_cur = st_cur.get("pos")
                if not isinstance(p_cur, (list, tuple)) or len(p_cur) < 3:
                    return
                wrong_dim = scene.dim == "the_nether" and "nether" not in dim_cur
                dist = math.hypot(float(p_cur[0]) - sx, float(p_cur[2]) - sz)
                if wrong_dim or (scene.dim in dim_cur and dist > scene.drift_limit):
                    restock_scene(bot_name, scene, reason="episode_anchor_pullback")
                    print(
                        f"[Scene Pullback] {bot_name} dim={dim_cur} dist={dist:.0f}m -> reset to "
                        f"({sx:.1f}, {sy:.1f}, {sz:.1f})",
                        flush=True,
                    )
            except Exception:
                pass

        watchdog = SceneWatchdog(bot, bot_name, scene, near_container=_near_known_container)
        watchdog.start()
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
                on_episode_event=_guard_scene_anchor,
            )
            summary = sup.run(max_episodes=args.max_episodes)
        finally:
            watchdog.stop()
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
                st_end = ensure_bot_ready(bot, timeout_s=30.0, max_cooldown_wait_s=120.0, attempts=2)
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
            "scene": {
                "stage": scene.stage,
                "dimension": scene.dim,
                "anchor": {"x": round(scene.x, 1), "y": round(scene.y, 1), "z": round(scene.z, 1)},
                "radius": scene.radius,
                "drift_limit": scene.drift_limit,
                "entry_verified": entry_ok,
            },
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
