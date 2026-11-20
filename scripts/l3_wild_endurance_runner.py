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
  village_trade                                lit landing pad in a natural village + staged trader
  brewing_alchemy                              cobblestone pad + brewing stand + 2x2 water source
  cave_combat                                  roofed 11x11 arena with one zombie + one skeleton
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


def select_village_wild_site(
    bot: Any,
    bot_name: str,
    start_idx: int = 0,
) -> tuple[float, float, float, str]:
    """Locate a natural village and use the surface selected by spreadplayers."""
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


def _pin_villager_near(bot: Any, bot_name: str, pos: list[float]) -> bool:
    """Place a persistent trader at the specified position on the village_trade pad.

    r18 glm_glm_3_village_trade kept chasing villagers 27m away (`out_of_reach`) and hit
    `path_not_found` on the village hillside. A NoAI villager at arm's length makes the trade slice
    deterministic without touching the harness.
    """
    tx, ty, tz = float(pos[0]), float(pos[1]), float(pos[2])

    def _verified(radius: int = 12) -> bool:
        try:
            res = bot.call("get_entities", {"radius": radius}) or {}
            ents = res.get("entities") if isinstance(res, dict) else None
            return isinstance(ents, list) and any(
                "villager" in f"{e.get('name') or ''} {e.get('type') or ''}".lower()
                for e in ents
                if isinstance(e, dict)
            )
        except Exception:
            return False

    try:
        # Preferred fixture: SUMMON a persistent, invulnerable, NoAI farmer with a paper->emerald
        # trade. Teleporting an existing villager kept losing it
        # (r22: entities=[] + 20m chase; r23 NJU: stale id 1330 -> target_not_visible).
        summon = (
            f"summon minecraft:villager {tx:.1f} {ty:.1f} {tz:.1f} "
            "{NoAI:1b,Invulnerable:1b,Persistent:1b,Silent:1b,"
            'VillagerData:{profession:"minecraft:farmer",level:2,type:"minecraft:plains"},'
            "Offers:{Recipes:[{buy:{id:\"minecraft:paper\",count:24},"
            'sell:{id:"minecraft:emerald",count:1},maxUses:999,uses:0,xp:1}]}}'
        )
        try:
            replies = rcon_batch([summon], timeout=30)
            reply = str((replies[0] if replies else {}).get("reply") or "")
        except Exception:
            reply = ""
        if reply.lower().startswith("summoned"):
            time.sleep(1.2)
            if _verified():
                print(
                    f"[Scene] summoned a persistent trader villager for {bot_name} at "
                    f"({tx:.1f}, {ty:.1f}, {tz:.1f})",
                    flush=True,
                )
                return True
            print(f"[Scene] trader summon replied '{reply[:80]}' but verify failed; falling back", flush=True)
        else:
            print(f"[Scene] trader summon not accepted ({reply[:80]!r}); falling back to tp+NoAI", flush=True)

        # Fallback: teleport a loaded villager onto the same prepared block and freeze it.
        pinned = False
        for attempt in (1, 2):
            rcon_batch(
                [
                    f"execute positioned {tx:.1f} {ty:.1f} {tz:.1f} run tp @e[type=minecraft:villager,sort=nearest,limit=1] {tx:.1f} {ty:.1f} {tz:.1f}",
                ],
                timeout=30,
            )
            time.sleep(0.6 if attempt == 1 else 1.2)
            rcon_batch(
                [
                    f"execute positioned {tx:.1f} {ty:.1f} {tz:.1f} run data merge entity @e[type=minecraft:villager,distance=..6,limit=1] {{NoAI:1b,Invulnerable:1b}}",
                ],
                timeout=30,
            )
            time.sleep(1.5)
            pinned = _verified()
            if pinned:
                break
        print(
            f"[Scene] pinned a NoAI villager next to {bot_name} at ({tx:.1f}, {ty:.1f}, {tz:.1f}) "
            f"verified={pinned}",
            flush=True,
        )
        return pinned
    except Exception as exc:
        print(f"[Scene] villager pin failed for {bot_name}: {exc}", flush=True)
        return False


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
    elif abs(float(pos[1]) - float(ny)) > 6.0:
        # The bot fell into the structure during the settle window (r16 HY_6 anchored at y=46.9
        # inside a bastion and then died to fall/lava on every return). Re-carve the ledge and pin
        # the anchor to the carved height instead of the fall-through position.
        print(
            f"[Scene] nether settle y={pos[1]:.1f} differs from ledge y={ny}; re-carving ledge",
            flush=True,
        )
        rcon_batch(
            prepare_scene_ground(bot_name, "nether_fortress", float(nx), float(ny), float(nz), dim="the_nether"),
            timeout=30,
        )
        time.sleep(1.0)
        pos = [float(nx) + 0.5, float(ny), float(nz) + 0.5]
    return float(pos[0]), float(pos[1]), float(pos[2]), f"nether:{structure_tag.split(':')[-1]}"


def _deep_water_pad_cmds(bot_name: str, ix: int, iz: int, surface_y: int) -> list[str]:
    """7x7 dry ocean pad: floor, 2-high parapet with an east gate, and a drowned/trident sweep."""
    return [
        f"forceload add {ix - 16} {iz - 16} {ix + 16} {iz + 16}",
        f"fill {ix - 3} {surface_y - 1} {iz - 3} {ix + 3} {surface_y - 1} {iz + 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz - 3} {ix + 3} {surface_y + 3} {iz + 3} minecraft:air",
        # 1-high rim (easy to climb back onto from the water) with an east gate; trident safety comes
        # from the drowned sweep + resistance, since a 2-high wall also blocked the bot's escape.
        f"fill {ix - 3} {surface_y} {iz - 3} {ix + 3} {surface_y} {iz - 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz + 3} {ix + 3} {surface_y} {iz + 3} minecraft:cobblestone",
        f"fill {ix - 3} {surface_y} {iz - 2} {ix - 3} {surface_y} {iz + 2} minecraft:cobblestone",
        f"fill {ix + 3} {surface_y} {iz - 3} {ix + 3} {surface_y} {iz - 1} minecraft:cobblestone",
        f"fill {ix + 3} {surface_y} {iz + 1} {ix + 3} {surface_y} {iz + 3} minecraft:cobblestone",
        f"tp {bot_name} {ix + 0.5} {surface_y} {iz + 0.5}",
        f"spawnpoint {bot_name} {ix} {surface_y} {iz}",
        # Clear the drowned/trident camp that otherwise farms the bot on respawn (r13/r14: 100% of
        # pad damage was `by trident`).
        f"execute positioned {ix} {surface_y} {iz} run kill @e[type=minecraft:drowned,distance=..48]",
        f"execute positioned {ix} {surface_y} {iz} run kill @e[type=minecraft:trident,distance=..48]",
    ]


def select_deep_water_site(bot: Any, bot_name: str, start_idx: int = 0) -> tuple[float, float, float, str]:
    """Locate a real deep ocean and stage the bot on the closest dry shore/pad.

    BUG-023 (D2 scene side): r12-r14 died repeatedly to drowned tridents because the bot spawned on
    a tiny pad in open deep water with no reachable dry shelf. We now prefer the nearest
    `minecraft:beach` (dry land + shallow water within a few blocks) and only fall back to a
    parapet-protected pad when the beach is too far away.
    """
    cx, cz = CANDIDATE_SITES[start_idx % len(CANDIDATE_SITES)]
    res = rcon_batch(
        [f"execute positioned {cx} 100 {cz} run locate biome minecraft:deep_ocean"],
        timeout=45,
    )
    coords = _locate_coords(str((res[0] if res else {}).get("reply") or ""))
    wx, wz = coords if coords is not None else (cx, cz)

    beach: tuple[int, int] | None = None
    try:
        bres = rcon_batch(
            [f"execute positioned {wx} 100 {wz} run locate biome minecraft:beach"],
            timeout=45,
        )
        beach = _locate_coords(str((bres[0] if bres else {}).get("reply") or ""))
    except Exception:
        beach = None
    beach_dist = math.hypot(beach[0] - wx, beach[1] - wz) if beach else float("inf")
    if beach is not None and beach_dist <= 200.0:
        staging_x, staging_z = beach
        spread_radius = 8
        stage_kind = f"beach({beach_dist:.0f}m from deep_ocean)"
    else:
        staging_x, staging_z = wx, wz
        spread_radius = 40
        stage_kind = "deep_ocean_pad" if beach is None else f"deep_ocean_pad(beach {beach_dist:.0f}m away)"

    rcon_batch(
        [
            f"tp {bot_name} {staging_x} 120 {staging_z}",
            f"spreadplayers {staging_x} {staging_z} 0 {spread_radius} false {bot_name}",
        ],
        timeout=45,
    )
    # `spreadplayers` drops the bot from y=120; polling too early reads a mid-fall y and would build
    # a floating platform tens of blocks above the ocean surface. Wait until y stabilises first.
    pos = _wait_settled(bot, timeout_s=15.0) or [float(staging_x), 70.0, float(staging_z)]
    st = bot.status()
    ix, iz = math.floor(pos[0]), math.floor(pos[2])
    biome = str(st.get("biome") or "deep_ocean").lower()
    # Trust the live `in_water` flag; only treat the ocean/river biome as wet when the bot is below
    # sea level, otherwise a dry sandbar/beach on a deep_ocean column would get a needless pad.
    frozen_y = int(round(pos[1]))
    wet = bool(st.get("in_water")) or (("ocean" in biome or "river" in biome) and frozen_y <= 62)
    print(
        f"[Scene] deep_water staging={stage_kind} pos=({ix}, {int(round(pos[1]))}, {iz}) "
        f"biome={biome or '?'} in_water={bool(st.get('in_water'))}",
        flush=True,
    )
    if wet:
        # Deep ocean sea level is 63; clamp so a stale reading can never produce a sky platform.
        surface_y = min(max(int(round(pos[1])) + 1, 64), 70)
        prep = _deep_water_pad_cmds(bot_name, ix, iz, surface_y)
    else:
        # Natural dry shore: keep it untouched, just pin the spawn and sweep the water edge.
        surface_y = int(round(pos[1]))
        prep = [
            f"forceload add {ix - 16} {iz - 16} {ix + 16} {iz + 16}",
            f"tp {bot_name} {ix + 0.5} {surface_y} {iz + 0.5}",
            f"spawnpoint {bot_name} {ix} {surface_y} {iz}",
            f"execute positioned {ix} {surface_y} {iz} run kill @e[type=minecraft:drowned,distance=..32]",
            f"execute positioned {ix} {surface_y} {iz} run kill @e[type=minecraft:trident,distance=..32]",
        ]
    rcon_batch(prep, timeout=45)
    time.sleep(1.0)
    kind = "beach" if not wet else "pad"
    return float(ix) + 0.5, float(surface_y), float(iz) + 0.5, f"deep_water[{kind}]:{biome}"


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
    ix, iy, iz = math.floor(sx), int(round(sy)), math.floor(sz)
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
                # 4-high obsidian parapet on three sides with a 1x2 gate on the east edge: the ledge
                # is a respawn point inside a hostile structure, so open sightlines just feed the
                # death loop (r17 hy4_hy_6_nether_bastion: 24 deaths).
                f"{pre} fill {ix - 3} {iy} {iz - 3} {ix + 3} {iy + 3} {iz - 3} minecraft:obsidian",
                f"{pre} fill {ix - 3} {iy} {iz + 3} {ix + 3} {iy + 3} {iz + 3} minecraft:obsidian",
                f"{pre} fill {ix - 3} {iy} {iz - 2} {ix - 3} {iy + 3} {iz + 2} minecraft:obsidian",
                f"{pre} fill {ix + 3} {iy} {iz - 3} {ix + 3} {iy + 3} {iz - 1} minecraft:obsidian",
                f"{pre} fill {ix + 3} {iy} {iz + 1} {ix + 3} {iy + 3} {iz + 3} minecraft:obsidian",
                f"{pre} tp {bot_name} {ix + 0.5} {iy} {iz + 0.5}",
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
                f"setblock {ix - 2} {iy} {iz - 2} minecraft:torch",
                f"setblock {ix + 4} {iy} {iz + 2} minecraft:torch",
                f"tp {bot_name} {ix + 0.5} {iy} {iz + 0.5}",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "brewing_alchemy":
        # The pad encloses the basin on four sides. A solid bottom keeps it from spilling on slopes.
        cmds.extend(
            [
                f"fill {ix - 3} {iy - 1} {iz - 3} {ix + 5} {iy - 1} {iz + 3} minecraft:cobblestone",
                f"fill {ix - 3} {iy} {iz - 3} {ix + 5} {iy + 3} {iz + 3} minecraft:air",
                f"setblock {ix + 2} {iy} {iz} minecraft:brewing_stand",
                f"setblock {ix + 2} {iy} {iz + 2} minecraft:crafting_table",
                f"fill {ix - 2} {iy - 2} {iz - 1} {ix - 1} {iy - 2} {iz} minecraft:cobblestone",
                f"fill {ix - 2} {iy - 1} {iz - 1} {ix - 1} {iy - 1} {iz} minecraft:water[level=0]",
                # BUG-026 scene side: light the pad so the brewing/trade slices are not a night-time
                # mob farm while the model stands still at the stand.
                f"setblock {ix - 2} {iy} {iz - 2} minecraft:torch",
                f"setblock {ix + 4} {iy} {iz - 2} minecraft:torch",
                f"setblock {ix - 2} {iy} {iz + 2} minecraft:torch",
                f"setblock {ix + 4} {iy} {iz + 2} minecraft:torch",
                f"tp {bot_name} {ix + 0.5} {iy} {iz + 0.5}",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "village_trade":
        # r31 NJU_3 suffocated at a fixed offset from a villager inside a building.
        return [
            f"fill {ix - 2} {iy - 1} {iz - 2} {ix + 2} {iy - 1} {iz + 2} minecraft:cobblestone",
            f"fill {ix - 2} {iy} {iz - 2} {ix + 2} {iy + 2} {iz + 2} minecraft:air",
            f"setblock {ix - 2} {iy} {iz - 2} minecraft:torch",
            f"setblock {ix + 2} {iy} {iz + 2} minecraft:torch",
        ]
    if stage == "cave_combat":
        # Enclosed 11x11x4 cobblestone combat arena with roof (prevents sunburn) and torches at corners.
        interior = f"x={ix - 4},y={iy},z={iz - 4},dx=8,dy=3,dz=8"
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
                f"setblock {ix + 4} {iy} {iz - 4} minecraft:torch",
                f"setblock {ix - 4} {iy} {iz + 4} minecraft:torch",
                f"setblock {ix} {iy} {iz - 4} minecraft:torch",
                f"setblock {ix} {iy} {iz + 4} minecraft:torch",
                f"setblock {ix - 4} {iy} {iz} minecraft:torch",
                f"setblock {ix + 4} {iy} {iz} minecraft:torch",
                # Scope the duplicate check to the arena so nearby wild mobs do not suppress the fixture.
                f"execute unless entity @e[type=minecraft:zombie,{interior}] run summon minecraft:zombie {ix + 3.5} {iy} {iz + 0.5}",
                f"execute unless entity @e[type=minecraft:skeleton,{interior}] run summon minecraft:skeleton {ix - 2.5} {iy} {iz + 3.5}",
                f"tp {bot_name} {ix + 0.5} {iy} {iz + 0.5}",
                f"spawnpoint {bot_name} {ix} {iy} {iz}",
            ]
        )
        return cmds
    if stage == "deep_water":
        # Re-assert the dry staging pad on death return (idempotent, only when spawned at/below sea level).
        cmds.append(
            f"execute positioned {ix} {iy} {iz} run kill @e[type=minecraft:drowned,distance=..40]"
        )
        cmds.append(
            f"execute positioned {ix} {iy} {iz} run kill @e[type=minecraft:trident,distance=..40]"
        )
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
                f"give {bot_name} minecraft:blaze_powder 8",
                f"give {bot_name} minecraft:nether_wart 8",
                f"give {bot_name} minecraft:glass_bottle 9",
                f"give {bot_name} minecraft:magma_cream 4",
                f"give {bot_name} minecraft:sugar 4",
                f"give {bot_name} minecraft:redstone 4",
                f"give {bot_name} minecraft:iron_sword 1",
                f"give {bot_name} minecraft:shield 1",
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
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
                f"give {bot_name} minecraft:iron_helmet 1",
                f"give {bot_name} minecraft:iron_chestplate 1",
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
LOCAL_FIXTURE_STAGES = frozenset({"brewing_alchemy", "cave_combat", "enchant_anvil", "village_trade"})

# Surface slices where a night start is pure mob-farm noise (fleet passes --keep-time to every bot
# except the first, so without this the village/brewing scenes would open at night).
SURFACE_DAYTIME_STAGES = frozenset({"village", "village_trade", "brewing_alchemy"})


def scene_dimension(stage: str) -> str:
    return "the_nether" if stage in NETHER_STAGES else "overworld"


def compute_stage_baseline(stage: str) -> dict[str, Any]:
    """Score the advancements a slice loadout pre-grants (P2-32 baseline vs earned points).

    `final_score` counts every completed advancement, including the ~95 points that `midgame`
    pre-grants in `stage_loadout_cmds`. Reporting this baseline separately makes cross-scene scores
    comparable: earned = final_score - baseline_points.
    """
    ids: list[str] = []
    for cmd in stage_loadout_cmds("__baseline__", stage, 0.0, 64.0, 0.0):
        if cmd.startswith("advancement grant "):
            ids.append(cmd.split()[-1])
    roots = [
        "minecraft:story/root",
        "minecraft:adventure/root",
        "minecraft:husbandry/root",
        "minecraft:nether/root",
    ]
    if stage not in NETHER_STAGES:
        roots.append("minecraft:end/root")
    ids.extend(roots)
    points = 0
    breakdown: dict[str, int] = {}
    try:
        from mc_agent.harness.curriculum import score_completed_advancements

        points, breakdown = score_completed_advancements(set(ids))
    except Exception:
        pass
    return {
        "points": int(points),
        "advancements": sorted(set(ids)),
        "breakdown": {k: int(v) for k, v in sorted(breakdown.items())},
    }


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


def _scene_geofence_cls(base_cls: Any) -> Any:
    """Build a `VoidGeofence` subclass that adds the `contains(x, y, z)` predicate.

    r16 crash: `AttributeError: 'VoidGeofence' object has no attribute 'contains'`
    (ds_ds_1_bare / glm_glm_5_midgame / hy4_hy_6_nether_bastion call it via skills/combat_env.py).
    `VoidGeofence` is a frozen slots dataclass, so we subclass it (keeping dataclass semantics for
    `dataclasses.replace`) rather than patching Slice B's file. Remove once upstream adds `contains`.
    """

    class SceneGeofence(base_cls):  # type: ignore[misc, valid-type]
        def contains(self, x: float, y: float | None = None, z: float | None = None) -> bool:
            if z is None and y is not None:
                x, z = x, y  # tolerate the 2-arg (x, z) form
            if z is None:
                return True
            if not self.enabled:
                return True
            return self.distance_from_center(float(x), float(z)) <= self.safe_radius

    return SceneGeofence


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
    ix, iy, iz = math.floor(sx), int(round(sy)), math.floor(sz)
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
    if (set_day or stage in SURFACE_DAYTIME_STAGES) and dim != "the_nether":
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
    ix, iy, iz = math.floor(scene.x), int(round(scene.y)), math.floor(scene.z)
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
        fence_repair: Any = None,
    ) -> None:
        super().__init__(name=f"scene-watchdog-{bot_name}", daemon=True)
        self.bot = bot
        self.bot_name = bot_name
        self.scene = scene
        self.interval_s = interval_s
        self.near_container = near_container
        self.fence_repair = fence_repair
        self._stop = threading.Event()
        self._last_return = 0.0
        self._last_total = -1
        self._inv_tick = 0
        self._ticks = 0
        self._preempt_skips = 0

    def stop(self) -> None:
        self._stop.set()

    def _owned_total(self) -> int | None:
        """Total item count from the real `/inventory` shape (items[] + offhand + armor)."""
        try:
            inv = self.bot.inventory()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # include ReflexPreempted(BaseException) raised by guardian._wrapped_call
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
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # guardian._wrapped_call raises ReflexPreempted(BaseException) during a reflex takeover;
            # letting it escape would kill the watchdog thread for the rest of the run (r18 ds_ds_4).
            self._preempt_skips += 1
            return
        if not st:
            return
        ready = bool(st.get("ready"))
        health = float(st.get("health") or 0.0)
        alive = st.get("alive")
        # The API reports ready=false for the whole death/respawn window; do not skip it, otherwise
        # the death itself is never observed and the bot comes back with an empty inventory.
        dead_signal = health <= 0.0 or alive is False
        if self.fence_repair is not None and ready and not dead_signal:
            try:
                self.fence_repair(st.get("dimension"), st.get("pos"))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
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
        self._ticks += 1
        if self._ticks % 15 == 0:  # ~2 min at the default 8s interval
            print(
                f"[Scene Watchdog] {self.bot_name} heartbeat tick={self._ticks} dim={dim} "
                f"pos={pos if pos else '?'} hp={health:.0f} ready={ready} "
                f"preempt_skips={self._preempt_skips}",
                flush=True,
            )
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
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                print(f"[Scene Watchdog] {self.bot_name} tick error: {exc!r}", flush=True)


def _reassert_scene_geofence(
    toolset: Any,
    scene: SceneAnchor,
    *,
    dimension: str | None,
    pos: Any = None,
    supervisor: Any = None,
) -> bool:
    """Keep the home fence at the scene and repair stale fences after same-episode portals."""
    from mc_agent.kb import normalize_dimension

    if not dimension:
        return False
    dim = normalize_dimension(dimension)
    gf = getattr(toolset, "geofence", None)
    if gf is None:
        return False
    try:
        if dim == scene.dim:
            x, z = scene.x, scene.z
            limit = 50.0
        elif (
            scene.dim == "overworld"
            and dim in {"the_nether", "the_end"}
            and isinstance(pos, (list, tuple))
            and len(pos) >= 3
        ):
            # Curriculum recenters between episodes. A bot can die and re-enter a portal
            # within one episode, leaving the home fence thousands of blocks away (r39 GuiTest).
            x, z = float(pos[0]), float(pos[2])
            limit = scene.drift_limit
        else:
            return False
        drift = math.hypot(float(gf.center_x) - x, float(gf.center_z) - z)
    except Exception:
        return False
    if drift <= limit:
        return False
    try:
        if hasattr(gf, "contains"):
            rebuilt = dataclasses.replace(gf, center_x=x, center_z=z)
        else:
            rebuilt = _scene_geofence_cls(type(gf))(center_x=x, center_z=z, radius=gf.radius, margin=gf.margin)
    except Exception as exc:
        print(f"[Scene] geofence rebuild failed: {exc}", flush=True)
        return False
    toolset.geofence = rebuilt
    if supervisor is not None:
        try:
            supervisor.geofence = rebuilt
        except Exception:
            pass
    print(
        f"[Scene] geofence drift {drift:.0f}m -> rebuilt in {dim} at ({x:.1f}, {z:.1f})",
        flush=True,
    )
    return True


def verify_scene_entry(bot: Any, bot_name: str, scene: SceneAnchor, *, attempts: int = 2) -> bool:
    """Confirm two entry samples are in the scene dimension and near the appropriate anchor."""
    from mc_agent.kb import normalize_dimension

    local_fixture = scene.stage in LOCAL_FIXTURE_STAGES
    radius = 2.0 if local_fixture else 64.0
    station = {"brewing_alchemy": "brewing_stand", "enchant_anvil": "enchanting_table"}.get(scene.stage)
    station_pos = [math.floor(scene.x) + 2, round(scene.y), math.floor(scene.z)]
    for i in range(attempts):
        for sample in range(2):
            try:
                st = bot.status()
            except Exception:
                st = {}
            dim, pos = normalize_dimension(st.get("dimension")), st.get("pos")
            if dim != scene.dim or not isinstance(pos, (list, tuple)) or len(pos) < 3:
                break
            dx = float(pos[0]) - scene.x
            dy = float(pos[1]) - scene.y
            dz = float(pos[2]) - scene.z
            if scene.stage == "cave_combat":
                # Knockback can carry the bot across the 9x9 interior before verification.
                inside = max(abs(dx), abs(dz)) < 4.5 and 0.0 <= dy <= 2.0
            else:
                inside = math.hypot(dx, dz) <= radius and (not local_fixture or abs(dy) <= 1.0)
            if not inside:
                break
            if sample == 0:
                time.sleep(1.2)
        else:
            try:
                block = bot.call("get_block", {"pos": station_pos}, timeout_s=5.0) if station else {}
                actual = str((block or {}).get("name") or "").removeprefix("minecraft:")
            except Exception:
                actual = ""
            if not station or actual == station:
                print(
                    f"[Scene] entry verified bot={bot_name} dim={dim} "
                    f"pos=({float(pos[0]):.1f}, {float(pos[1]):.1f}, {float(pos[2]):.1f})",
                    flush=True,
                )
                return True
            print(f"[Scene] expected {station} at {station_pos}, got {actual or '?'}", flush=True)
        print(
            f"[Scene] entry verify failed (attempt {i + 1}/{attempts}) bot={bot_name} "
            f"dim={dim} pos={pos} expected {scene.dim}@({scene.x:.1f},{scene.y:.1f},{scene.z:.1f})",
            flush=True,
        )
        if local_fixture:
            try:
                bot.call("stop", {}, timeout_s=5.0)
            except Exception:
                pass
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
    max_cooldown_wait_s: float = 600.0,
    attempts: int = 8,
) -> dict[str, Any]:
    """Bring the bot to `ready: True`, sleeping through the API's death-loop cooldown instead of dying.

    BUG-021: a bot that died at the end of the previous wave enters a ~130s `death_loop_cooldown`;
    `bot.connect()` raises `DeathLoopCooldownError` and the old runner aborted the whole run (an
    entire fleet slot was lost with 0 events). We now wait out the reported ETA and retry.
    """
    from mc_agent.errors import ApiError, BotNotReadyError, DeathLoopCooldownError

    waited = 0.0
    attempt = 0
    last_status: dict[str, Any] = {}
    # BUG-039: the cooldown budget is the real loop bound; `attempts` is only the soft cap for
    # non-cooldown not-ready retries (2s backoff). A long death-loop must never abort in 186s.
    while waited <= max_cooldown_wait_s:
        attempt += 1
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
            remaining_budget = max_cooldown_wait_s - waited
            if cooldown + 2.0 > remaining_budget:
                print(
                    f"[Cooldown] death-loop cooldown {cooldown:.0f}s exceeds remaining budget "
                    f"{remaining_budget:.0f}s (waited {waited:.0f}s/{max_cooldown_wait_s:.0f}s); giving up",
                    flush=True,
                )
                raise DeathLoopCooldownError(
                    code="death_loop_cooldown",
                    message=(
                        f"cooldown {cooldown:.0f}s exceeds remaining startup budget "
                        f"{remaining_budget:.0f}s"
                    ),
                    cooldown_remaining_seconds=cooldown,
                )
            wait_s = max(1.0, cooldown + 2.0)
            print(
                f"[Cooldown] bot in death-loop cooldown {cooldown:.0f}s "
                f"(attempt {attempt}, waited {waited:.0f}s/{max_cooldown_wait_s:.0f}s, "
                f"ETA {wait_s:.0f}s); retrying",
                flush=True,
            )
            time.sleep(wait_s)
            waited += wait_s
        elif attempt < attempts:
            time.sleep(2.0)
        else:
            break
    raise RuntimeError(
        f"bot did not become ready after {attempt} attempts (cooldown waited {waited:.0f}s, "
        f"budget {max_cooldown_wait_s:.0f}s); last_status={last_status}"
    )


SCENE_FOCUS: dict[str, dict[str, Any]] = {
    # Prioritize the local fixture while its opening supplies are within reach.
    "village_trade": {
        "key": "minecraft:adventure/trade",
        "title": "15分：与村民完成一次交易",
        "points": 15,
        "tier": 2,
        "budget_class": "short",
        "task": (
            "你在村庄的照明台面，旁边有一名 NoAI 交易村民，背包备有 emerald 32 / wheat 32 / "
            "stick 64 / paper 32 / coal 32。先 mc_get_entities(radius=12)（**不要**传 types 过滤，"
            "直接看返回列表里的 villager），若距离 >4m 先 mc_move_to(x=..., z=..., range=1.5) 贴近"
            "（不要隔着 20m 直接开窗，会 out_of_reach），然后 mc_window_view(entity_id=...) 读取报价；"
            "只要有一项 available=true 且材料够，马上 mc_skill_trade(entity_id=..., option=...) 成交，"
            "优先 paper/wheat 换绿宝石。返回 no_window 就换下一个村民，禁止对同一村民反复重试，"
            "也不要离开村庄爬山。"
        ),
        "extra_tools": ("mc_skill_trade", "mc_window_view", "mc_interact"),
        "advancement_id": "minecraft:adventure/trade",
    },
    "brewing_alchemy": {
        "key": "minecraft:nether/brew_potion",
        "title": "15分：酿出任意一瓶药水",
        "points": 15,
        "tier": 2,
        "budget_class": "short",
        "episodes": 3,
        "task": (
            "出生点西侧是低一格的 2x2 水池，东侧是酿造台。用 mc_find_blocks(radius=6, types=['water','brewing_stand']) "
            "找到 properties.level=0 的水源，站在干燥台面用 mc_interact(x=..., y=..., z=..., item='glass_bottle') 装水。"
            "每次核对 used.glass_bottle=1、gained.potion=1 或背包增量。没有增量就换一个可见的 level=0 水格。"
            "拿到一瓶即可调用 mc_skill_brew(bottles=[{'item':'potion','potion':'water','count':1}], "
            "ingredient_item='nether_wart', fuel_item='blaze_powder', wait=True)。待 brew_time_left=0 后用 "
            "mc_window_act(block='brewing_stand', ops=[{'op':'take','from':'bottles','count':1}]) 取回药水以触发成就。"
        ),
        "extra_tools": ("mc_skill_brew", "mc_window_act", "mc_window_view", "mc_interact"),
        "advancement_id": "minecraft:nether/brew_potion",
    },
    "enchant_anvil": {
        "key": "minecraft:story/enchant_item",
        "title": "30分：在出生点完成一次附魔",
        "points": 30,
        "tier": 2,
        "budget_class": "short",
        "task": (
            "出生台面东侧已有附魔台，背包有钻石剑、青金石，已有 30 级经验。留在台面，"
            "立刻用 mc_skill_enchant(item='diamond_sword', option=0, block='enchanting_table', take=True) "
            "完成附魔并确认钻石剑已带附魔。需要坐标时用 mc_find_blocks(radius=6, types=['enchanting_table'])。"
        ),
        "extra_tools": ("mc_skill_enchant",),
        "advancement_id": "minecraft:story/enchant_item",
    },
    "cave_combat": {
        "key": "minecraft:adventure/kill_a_mob",
        "title": "5分：击杀竞技场内的敌对生物并存活",
        "points": 5,
        "tier": 2,
        "budget_class": "short",
        "task": (
            "你被封闭在 11x11 战斗竞技场，场内已有一只僵尸与一只骷髅，背包有铁剑/盾/弓/铁甲。"
            "先 mc_posture(shield=True) 举盾贴近僵尸，用铁剑击杀即可解锁 adventure/kill_a_mob；"
            "骷髅在远处时用盾挡箭或弓箭反制，低血量退到角落回血，不要在开阔处站桩。"
        ),
        "extra_tools": ("mc_posture",),
        "advancement_id": "minecraft:adventure/kill_a_mob",
    },
}


def install_scene_focus(stage: str, episodes: int = 2, *, scene: SceneAnchor | None = None) -> bool:
    """Force the first `episodes` curriculum picks to a stage-specific target (SCENE-001).

    `CurriculumSupervisor.run()` resolves `select_next_target` from the module namespace at call
    time, so wrapping that module function is enough — no core `harness/` edits. After the focus
    budget is exhausted the original selector takes over again.
    """
    spec = SCENE_FOCUS.get(stage)
    if not spec or episodes <= 0:
        return False
    from mc_agent.harness import curriculum as cur

    target_kwargs = {k: v for k, v in spec.items() if k != "episodes"}
    if stage == "brewing_alchemy" and scene is not None:
        x, y, z = math.floor(scene.x), round(scene.y), math.floor(scene.z)
        target_kwargs["task"] = (
            f"场景台面中心在 ({scene.x:.1f}, {y}, {scene.z:.1f})。若已走远，先回到台面中心。"
            f"优先用玻璃瓶点布置好的水源 ({x - 1}, {y - 1}, {z})，备用水源在 ({x - 2}, {y - 1}, {z - 1})。"
            f"酿造台在 ({x + 2}, {y}, {z})。查询水源时看这两个坐标和 level=0。"
            + target_kwargs["task"]
        )
    target = cur.CurriculumTarget(**target_kwargs)
    remaining = int(spec.get("episodes", episodes))
    original = cur.select_next_target

    def _wrapped(bot: Any, **kwargs: Any):
        nonlocal remaining
        if remaining > 0:
            frontier = cur.compute_advancement_frontier(bot, limit=50)
            if target.advancement_id not in (frontier.get("completed_ids") or []):
                remaining -= 1
                return target, frontier
            remaining = 0
        return original(bot, **kwargs)

    cur.select_next_target = _wrapped
    print(f"[Scene Focus] stage={stage} up to {remaining} opening episode(s) -> {target.key}", flush=True)
    return True


class TraderKeeper(threading.Thread):
    """Keep a villager at the bot's feet for the village_trade slice.

    r22 proved that a one-shot `tp` + `{NoAI}` pin can drift/lose the villager by the first episode
    (the model saw `entities: []` then a villager 20m+ away behind walls). This daemon re-anchors the
    nearest loaded villager to the bot's current position every `interval_s`, so the trade target is
    always within a couple of blocks without touching the harness.
    """

    def __init__(self, bot: Any, bot_name: str, *, interval_s: float = 15.0) -> None:
        super().__init__(name=f"trader-keeper-{bot_name}", daemon=True)
        self.bot = bot
        self.bot_name = bot_name
        self.interval_s = interval_s
        self._stop = threading.Event()
        self.moves = 0

    def stop(self) -> None:
        self._stop.set()

    def _tick(self) -> None:
        try:
            st = self.bot.status()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return
        pos = st.get("pos") if isinstance(st, dict) else None
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
        try:
            rcon_batch(
                [
                    f"execute positioned {x:.1f} {y:.1f} {z:.1f} run tp @e[type=minecraft:villager,sort=nearest,limit=1] {x:.1f} {y:.1f} {z:.1f}",
                ],
                timeout=20,
            )
            self.moves += 1
        except Exception:
            return

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._tick()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                print(f"[Trader Keeper] {self.bot_name} tick error: {exc!r}", flush=True)


def _write_not_ready_summary(
    out_file: pathlib.Path,
    *,
    bot_name: str,
    tag: str,
    rpm_budget: int | None,
    stamp: str,
    stage: str,
    error: str,
) -> None:
    """Persist a `status=not_ready` summary so a startup-cooldown slot still shows up in triage."""
    baseline = compute_stage_baseline(stage)
    payload = {
        "bot": bot_name,
        "tag": tag,
        "rpm_budget": rpm_budget,
        "started_at": stamp,
        "spawn": None,
        "end_status": None,
        "owned_end": {},
        "scene": {
            "stage": stage,
            "status": "not_ready",
            "entry_verified": False,
            "error": error,
            "baseline_points": baseline["points"],
            "baseline_breakdown": baseline["breakdown"],
        },
        "curriculum_summary": {
            "status": "not_ready",
            "final_score": 0,
            "elapsed_s": 0.0,
            "episodes": [],
            "error": error,
        },
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_token_refresher(bot_name: str, token_file: pathlib.Path, settings: Any) -> Any:
    """Return a `BotSession` reauth callback: re-login with .env.beast credentials and persist the token.

    BUG-036: a long run lost 38 minutes when the cached token was revoked mid-wave
    (`[invalid_token] token not found or expired`) because the session had no reauth path.
    """
    from mc_agent.auth import BotTokenRecord, TokenStore
    from mc_agent.config import parse_dotenv

    store = TokenStore(token_file)
    api_base = str(settings.mc_api_base_url).rstrip("/")

    def _refresh() -> Any:
        import httpx

        env = parse_dotenv(ENV_FILE)
        password = env.get("MC_SKIN_PASSWORD") or env.get("MC_BOT_PASSWORD") or ""
        if not password:
            raise RuntimeError(
                f"cannot refresh token for {bot_name}: MC_SKIN_PASSWORD missing in {ENV_FILE.name}"
            )
        try:
            resp = httpx.post(
                f"{api_base}/auth/login",
                json={"username": bot_name, "password": password},
                timeout=15.0,
            )
        except Exception as exc:
            raise RuntimeError(f"token refresh request failed for {bot_name}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"token refresh failed for {bot_name}: HTTP {resp.status_code}")
        payload = resp.json()
        res = payload.get("result") or payload
        token = res.get("token")
        if not token:
            raise RuntimeError(f"token refresh for {bot_name} returned no token")
        record = BotTokenRecord(
            bot_username=res.get("bot_username") or bot_name,
            token=str(token),
            expires_at=res.get("expires_at"),
            login_username=bot_name,
        )
        store.put(record)
        print(f"[Token] refreshed cached token for {bot_name}", flush=True)
        return record

    return _refresh


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", type=str, default=BOT)
    parser.add_argument("--token-file", type=str, default=str(TOKEN_FILE))
    parser.add_argument("--rpm-budget", type=int, default=None, help="Per-process RPM cap (e.g. 12 for 2x concurrency)")
    parser.add_argument("--provider", type=str, default=None, help="LLM provider override (nju/opencode/deepseek/dashscope)")
    parser.add_argument("--model", type=str, default=None, help="LLM model override (e.g. glm-5.3)")
    parser.add_argument("--runs-subdir", type=str, default="", help="Optional subdirectory under runs/ for multi-bot isolation")
    parser.add_argument("--duration-s", type=float, default=1800.0, help="Wall-clock budget for this wild run")
    parser.add_argument("--max-episodes", type=int, default=12, help="Episode cap (0 runs to the wall-clock deadline)")
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
        "--focus-episodes",
        type=int,
        default=2,
        help="For scene stages, force this many opening episodes onto the stage-specific target (0 disables)",
    )
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
    if args.max_episodes < 0:
        parser.error("--max-episodes must be zero or positive")

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
            "baseline": compute_stage_baseline(args.stage),
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

    with BotSession(
        rec.token,
        bot_username=rec.bot_username,
        base_url=settings.mc_api_base_url,
        reauth_callback=build_token_refresher(bot_name, token_file, settings),
    ) as bot:
        try:
            st = ensure_bot_ready(bot, timeout_s=45.0)
        except Exception as exc:  # BUG-039: never kill the process for a cooldown-bound slot
            msg = str(exc) or exc.__class__.__name__
            print(f"[Startup] {bot_name} not ready: {msg}", flush=True)
            _write_not_ready_summary(
                out_file,
                bot_name=bot_name,
                tag=args.tag,
                rpm_budget=args.rpm_budget,
                stamp=stamp,
                stage=args.stage,
                error=msg,
            )
            return 0
        if args.stage in ("village", "village_trade"):
            sx, sy, sz, biome = select_village_wild_site(
                bot,
                bot_name,
                start_idx=args.site_index,
            )
        elif args.stage == "nether_fortress":
            sx, sy, sz, biome = select_nether_structure_site(bot, bot_name, "minecraft:fortress", start_idx=args.site_index)
        elif args.stage == "nether_bastion":
            sx, sy, sz, biome = select_nether_structure_site(bot, bot_name, "minecraft:bastion_remnant", start_idx=args.site_index)
        elif args.stage == "deep_water":
            sx, sy, sz, biome = select_deep_water_site(bot, bot_name, start_idx=args.site_index)
        else:
            sx, sy, sz, biome = select_dry_isolated_wild_site(bot, bot_name, start_idx=args.site_index)
        if args.stage in LOCAL_FIXTURE_STAGES:
            sx, sy, sz = math.floor(sx) + 0.5, float(round(sy)), math.floor(sz) + 0.5
        dim = scene_dimension(args.stage)
        scene = SceneAnchor(stage=args.stage, dim=dim, x=sx, y=sy, z=sz)
        baseline = compute_stage_baseline(args.stage)
        prep = prepare_scene_ground(bot_name, args.stage, sx, sy, sz, dim=dim)
        if prep:
            rcon_batch(prep, timeout=60)
        reset_wild_bot(bot_name, sx, sy, sz, set_day=not args.keep_time, stage=args.stage, dim=dim)
        time.sleep(1.5)
        entry_ok = verify_scene_entry(bot, bot_name, scene)
        if args.stage == "village_trade":
            _pin_villager_near(bot, bot_name, [sx + 2.0, sy, sz])

        geofence = _scene_geofence_cls(VoidGeofence)(
            center_x=sx, center_z=sz, radius=220.0, margin=8.0
        )
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
                _reassert_scene_geofence(
                    toolset, scene, dimension=st_cur.get("dimension"), pos=p_cur, supervisor=sup,
                )
                if wrong_dim or (scene.dim in dim_cur and dist > scene.drift_limit):
                    restock_scene(bot_name, scene, reason="episode_anchor_pullback")
                    print(
                        f"[Scene Pullback] {bot_name} dim={dim_cur} dist={dist:.0f}m -> reset to "
                        f"({sx:.1f}, {sy:.1f}, {sz:.1f})",
                        flush=True,
                    )
            except Exception:
                pass

        scene_state: dict[str, Any] = {"sup": None}

        def _repair_fence(dimension: str | None, pos: Any) -> None:
            _reassert_scene_geofence(toolset, scene, dimension=dimension, pos=pos, supervisor=scene_state["sup"])

        watchdog = SceneWatchdog(
            bot, bot_name, scene, near_container=_near_known_container, fence_repair=_repair_fence
        )
        watchdog.start()
        trader_keeper = TraderKeeper(bot, bot_name) if args.stage == "village_trade" else None
        if trader_keeper is not None:
            trader_keeper.start()
            print(f"[Scene] trader keeper started for {bot_name} (15s re-anchor)", flush=True)
        install_scene_focus(args.stage, args.focus_episodes, scene=scene)
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
            scene_state["sup"] = sup
            summary = sup.run(max_episodes=args.max_episodes or None)
        finally:
            if trader_keeper is not None:
                trader_keeper.stop()
                print(f"[Scene] trader keeper stopped for {bot_name} moves={trader_keeper.moves}", flush=True)
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
                "watchdog_ticks": watchdog._ticks,
                "watchdog_preempt_skips": watchdog._preempt_skips,
                "trader_keeper_moves": trader_keeper.moves if trader_keeper is not None else 0,
                "baseline_points": baseline["points"],
                "baseline_breakdown": baseline["breakdown"],
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
