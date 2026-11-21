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
  day2_village_near                            iron checkpoint 120–180m outside a real village
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
DAY2_VILLAGE_STAGE = "day2_village_near"
VILLAGE_APPROACH_RANGE = (120.0, 180.0)
STRONGHOLD_STAGE = "village_to_stronghold_end"
STRONGHOLD_DISTANCE_RANGE = (120.0, 600.0)
STRONGHOLD_TRAVEL_RADIUS = 720.0
STRONGHOLD_LOCATE_TIMEOUT_S = 60
FOLLOW_ENDER_EYE = "minecraft:story/follow_ender_eye"
ENTER_END = "minecraft:story/enter_the_end"
ENTER_NETHER = "minecraft:story/enter_the_nether"

CANDIDATE_SITES: list[tuple[int, int]] = [
    # 24 spaced anchors give each slot its own site and keep bots off shared islands.
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

NETHER_SITE_RADIUS = 600.0
# Centers are 1800m apart, leaving at least 600m between accepted structure locations.
NETHER_CANDIDATE_SITES = sorted(
    ((1800 * x, 1800 * z) for x in range(-2, 3) for z in range(-2, 3)),
    key=lambda site: math.hypot(*site),
)
NETHER_STRUCTURES = {"nether_fortress": "minecraft:fortress", "nether_bastion": "minecraft:bastion_remnant"}


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


def _spread_overworld(
    bot: Any, bot_name: str, x: int, z: int, radius: int,
) -> tuple[list[float] | None, dict[str, Any]]:
    rcon_batch(
        [
            f"execute in minecraft:overworld run tp {bot_name} {x} 120 {z}",
            f"execute in minecraft:overworld run spreadplayers {x} {z} 0 {radius} false {bot_name}",
        ],
        timeout=45,
    )
    pos = _wait_settled(bot)
    return pos, bot.status()


def _portal_free_landing(bot: Any) -> bool:
    result = bot.call("find_blocks", {"types": ["nether_portal"], "radius": 64, "limit": 1}) or {}
    return result.get("blocks") == []


def select_dry_isolated_wild_site(
    bot: Any, bot_name: str, start_idx: int = 0, *, day1: bool = False,
) -> tuple[float, float, float, str]:
    """Find a dry, wooded Overworld landing for the configured slot."""
    from mc_agent.kb import normalize_dimension

    start_idx %= len(CANDIDATE_SITES)
    if day1:
        # r41 global fallbacks sent two bots to a shared portal area. Translate the
        # whole slot layout for backups, keeping the same separation in each region.
        x, z = CANDIDATE_SITES[start_idx]
        offsets = ((0, 0), (160, 0), (-160, 0), (0, 160), (0, -160),
                   (360, 0), (-360, 0), (0, 360), (0, -360))
        candidates = [
            (x + region_dx + dx, z + dz)
            for region_dx in (0, 30_000, -30_000)
            for dx, dz in offsets
        ]
    else:
        candidates = CANDIDATE_SITES[start_idx:] + CANDIDATE_SITES[:start_idx]
    for cx, cz in candidates:
        settled, st = _spread_overworld(bot, bot_name, cx, cz, 32)
        pos = settled or [cx, 64, cz]
        if day1:
            pos = st.get("pos")
            if (
                not st.get("dimension")
                or normalize_dimension(st["dimension"]) != "overworld"
                or not isinstance(pos, (list, tuple))
                or len(pos) < 3
                or math.hypot(float(pos[0]) - cx, float(pos[2]) - cz) > 48.0
            ):
                continue
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
        if day1 and not _portal_free_landing(bot):
            print(f"[Day 1] {bot_name} skipped portal-unsafe landing near ({cx}, {cz})", flush=True)
            continue
        return float(pos[0]), float(pos[1]), float(pos[2]), biome
    if day1:
        raise RuntimeError("Could not find a wooded, portal-free Day 1 landing in the assigned regions")
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


def _locate_structure(
    dim: str, cx: int, cz: int, structure_id: str, *, timeout_s: int = 30,
) -> tuple[int, int] | None:
    """`locate structure` inside `dim`, returning None when the server says it cannot find one."""
    out = rcon_batch(
        [f"execute in {dim} positioned {cx} 64 {cz} run locate structure {structure_id}"],
        timeout=timeout_s,
    )
    reply = str((out[0] if out else {}).get("reply") or "")
    if "could not" in reply.lower() or "cannot" in reply.lower():
        return None
    return _locate_coords(reply)


def select_day2_village_site(
    bot: Any, bot_name: str, start_idx: int,
) -> tuple[tuple[float, float, float, str], tuple[int, int]]:
    """Land on natural dry ground at the requested distance from the located village."""
    from mc_agent.kb import normalize_dimension

    cx, cz = CANDIDATE_SITES[start_idx % len(CANDIDATE_SITES)]
    village = _locate_structure("minecraft:overworld", cx, cz, "#minecraft:village")
    if village is None:
        raise RuntimeError("Day 2 could not locate a natural village for this slot")
    vx, vz = village
    inner, outer = VILLAGE_APPROACH_RANGE
    mid = (inner + outer) / 2
    for offset in range(8):
        bearing = ((start_idx + offset) % 8) * math.tau / 8
        x = vx + round(mid * math.cos(bearing))
        z = vz + round(mid * math.sin(bearing))
        _, st = _spread_overworld(bot, bot_name, x, z, 14)
        pos = st.get("pos")
        if (
            not st.get("dimension")
            or normalize_dimension(st["dimension"]) != "overworld"
            or not isinstance(pos, (list, tuple))
            or len(pos) < 3
            or st.get("on_ground") is False
            or st.get("in_water")
            or st.get("in_lava")
        ):
            continue
        sx, sy, sz = map(float, pos[:3])
        if inner <= math.hypot(sx - vx, sz - vz) <= outer and _portal_free_landing(bot):
            return (sx, sy, sz, str(st.get("biome") or "")), village
    raise RuntimeError("Day 2 could not find a dry, portal-free landing 120–180m from the village")


def _dry_overworld_position(status: dict[str, Any]) -> tuple[float, float, float] | None:
    from mc_agent.kb import normalize_dimension

    dim, pos = status.get("dimension"), status.get("pos")
    if (
        not dim
        or normalize_dimension(dim) != "overworld"
        or not isinstance(pos, (list, tuple))
        or len(pos) < 3
        or status.get("on_ground") is not True
        or status.get("in_water")
        or status.get("in_lava")
    ):
        return None
    return float(pos[0]), float(pos[1]), float(pos[2])


def select_stronghold_village_site(
    bot: Any, bot_name: str, start_idx: int,
) -> tuple[tuple[float, float, float, str], tuple[int, int], tuple[int, int]]:
    """Find a dry village start and the nearest reachable stronghold for this slot."""
    dim = "minecraft:overworld"

    def locate(x: int, z: int, structure_id: str) -> tuple[int, int] | None:
        return _locate_structure(dim, x, z, structure_id, timeout_s=STRONGHOLD_LOCATE_TIMEOUT_S)

    anchor = CANDIDATE_SITES[start_idx % len(CANDIDATE_SITES)]
    nearest = locate(*anchor, "minecraft:stronghold")
    probes = [anchor]
    if nearest is not None:
        hx, hz = nearest
        for radius in (320, 520):
            for offset in range(8):
                bearing = ((start_idx + offset) % 8) * math.tau / 8
                probes.append((hx + round(radius * math.cos(bearing)), hz + round(radius * math.sin(bearing))))
    inner, outer = STRONGHOLD_DISTANCE_RANGE
    seen_villages = set()
    nearby_pairs = 0
    for probe in probes:
        village = locate(*probe, "#minecraft:village")
        if village is None or village in seen_villages:
            continue
        seen_villages.add(village)
        if min(CANDIDATE_SITES, key=lambda site: math.dist(site, village)) != anchor:
            continue
        stronghold = locate(*village, "minecraft:stronghold")
        if (
            stronghold is None
            or min(CANDIDATE_SITES, key=lambda site: math.dist(site, stronghold)) != anchor
            or not inner - 32 <= math.dist(village, stronghold) <= outer + 32
        ):
            continue
        nearby_pairs += 1
        for _ in range(2):
            _, status = _spread_overworld(bot, bot_name, *village, 16)
            pos = _dry_overworld_position(status)
            if pos is None:
                continue
            xz = pos[0], pos[2]
            if (
                math.dist(xz, village) <= 32
                and inner <= math.dist(xz, stronghold) <= outer
                and _portal_free_landing(bot)
                and locate(math.floor(xz[0]), math.floor(xz[1]), "minecraft:stronghold") == stronghold
            ):
                return (*pos, str(status.get("biome") or "")), village, stronghold
    raise RuntimeError(
        "Could not find a safe village with a stronghold 120–600m away in the assigned region "
        f"({len(seen_villages)} distinct villages located, {nearby_pairs} pairs sent to landing checks)"
    )


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
    region = NETHER_CANDIDATE_SITES[start_idx % len(NETHER_CANDIDATE_SITES)]
    cx, cz = region
    for dx, dz in ((0, 0), (360, 0), (-360, 0), (0, 360), (0, -360)):
        coords = _locate_structure("minecraft:the_nether", cx + dx, cz + dz, structure_id)
        if coords is not None and math.dist(region, coords) <= NETHER_SITE_RADIUS:
            nx, nz = coords
            break
    else:
        raise RuntimeError(f"Could not locate {structure_id} within {NETHER_SITE_RADIUS:g}m of the assigned Nether region")
    ny = 64
    anchor = (nx + 0.5, float(ny), nz + 0.5)
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
    pos = _wait_settled(bot, timeout_s=8.0) or anchor
    st = bot.status()
    if "nether" in str(st.get("dimension") or "").lower() and (
        abs(float(pos[1]) - ny) > 6.0 or math.hypot(float(pos[0]) - anchor[0], float(pos[2]) - anchor[2]) > 4.0
    ):
        # A stale position or a fall through the ledge must not move the slot's anchor.
        print(
            f"[Scene] nether settle at {pos} missed assigned ledge {anchor}. Re-carving ledge.",
            flush=True,
        )
        rcon_batch(
            prepare_scene_ground(bot_name, "nether_fortress", float(nx), float(ny), float(nz), dim="the_nether"),
            timeout=30,
        )
        time.sleep(1.0)
    return (*anchor, f"nether:{structure_id.split(':')[-1]}")


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
    if stage in {"iron", DAY2_VILLAGE_STAGE}:
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
        if stage == DAY2_VILLAGE_STAGE:
            cmds.extend(
                [
                    f"give {bot_name} minecraft:iron_axe 1",
                    f"give {bot_name} minecraft:shield 1",
                    f"give {bot_name} minecraft:torch 16",
                    f"give {bot_name} minecraft:bread 16",
                ]
            )
    elif stage == STRONGHOLD_STAGE:
        cmds.extend(
            f"advancement grant {bot_name} only minecraft:story/{advancement}"
            for advancement in ("mine_stone", "upgrade_tools", "smelt_iron", "iron_tools", "mine_diamond", "shiny_gear")
        )
        cmds.extend(
            [
                f"give {bot_name} minecraft:diamond_pickaxe 1",
                f"give {bot_name} minecraft:diamond_sword 1",
                f"item replace entity {bot_name} armor.head with minecraft:iron_helmet",
                f"item replace entity {bot_name} armor.chest with minecraft:iron_chestplate",
                f"item replace entity {bot_name} armor.legs with minecraft:iron_leggings",
                f"item replace entity {bot_name} armor.feet with minecraft:diamond_boots",
                f"item replace entity {bot_name} weapon.offhand with minecraft:shield",
                f"give {bot_name} minecraft:cooked_beef 32",
                f"give {bot_name} minecraft:cobblestone 64",
                f"give {bot_name} minecraft:torch 48",
                f"give {bot_name} minecraft:ender_eye 14",
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


NETHER_STAGES = frozenset(NETHER_STRUCTURES)
LOCAL_FIXTURE_STAGES = frozenset({"brewing_alchemy", "cave_combat", "enchant_anvil", "village_trade"})

# Surface slices where a night start is pure mob-farm noise (fleet passes --keep-time to every bot
# except the first, so without this the village/brewing scenes would open at night).
SURFACE_DAYTIME_STAGES = frozenset({"village", "village_trade", "brewing_alchemy", DAY2_VILLAGE_STAGE, STRONGHOLD_STAGE})


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
    village_xz: tuple[int, int] | None = None


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
        prefix = "execute in minecraft:overworld run " if stage == STRONGHOLD_STAGE else ""
        cmds.extend(
            [
                f"{prefix}tp {bot_name} {sx:.1f} {sy:.1f} {sz:.1f}",
                f"{prefix}spawnpoint {bot_name} {ix} {iy} {iz}",
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


def restock_scene(
    bot_name: str, scene: SceneAnchor, *, reason: str, clear_inventory: bool = False,
    revoke_advancement: str | None = None,
) -> None:
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
                f"execute in minecraft:overworld run tp {bot_name} {scene.x:.1f} {scene.y:.1f} {scene.z:.1f}",
                f"execute in minecraft:overworld run spawnpoint {bot_name} {ix} {iy} {iz}",
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
    if revoke_advancement is not None:
        cmds.append(f"advancement revoke {bot_name} only {revoke_advancement}")
    rcon_batch(cmds, timeout=60)
    print(
        f"[Scene Restock] {bot_name} stage={scene.stage} reason={reason} restock={clear_inventory} "
        f"-> ({scene.x:.1f}, {scene.y:.1f}, {scene.z:.1f})",
        flush=True,
    )


class OverworldProgressGuard:
    """Return accidental portal travelers while allowing the slice's intended progression."""

    def __init__(self, bot: Any, bot_name: str, scene: SceneAnchor) -> None:
        self.bot = bot
        self.bot_name = bot_name
        self.scene = scene
        self.stronghold_run = scene.stage == STRONGHOLD_STAGE
        self.label = "Stronghold" if self.stronghold_run else ("Day 1" if scene.stage == "bare" else "Day 2")
        self.portal_dimension = "the_end" if self.stronghold_run else "the_nether"
        self.portal_advancement = ENTER_END if self.stronghold_run else ENTER_NETHER
        self._lock = threading.Lock()
        self._target: str | None = None
        self.portal_entered = False

    def note_target(self, key: str) -> None:
        with self._lock:
            self._target = key

    def _end_after_stronghold(self) -> bool:
        if not self.stronghold_run or self._target != FOLLOW_ENDER_EYE:
            return False
        resp = self.bot.call("advancements", {"status": "completed", "ids": [FOLLOW_ENDER_EYE]})
        return any(row.get("id") == FOLLOW_ENDER_EYE for row in resp.get("advancements", []))

    def keep_home(self, observed_dim: str | None = None) -> bool:
        from mc_agent.kb import normalize_dimension

        if observed_dim and normalize_dimension(observed_dim) == "overworld":
            return False
        with self._lock:
            if self.portal_entered and not self.stronghold_run:
                return False
            # Re-read alternate-dimension observations under the lock to avoid racing another
            # caller's return or authorizing a trip based on a stale watchdog snapshot.
            reported_dim = self.bot.status().get("dimension")
            if not reported_dim:
                raise RuntimeError(f"{self.bot_name} {self.label} dimension unavailable")
            dim = normalize_dimension(reported_dim)
            if dim == "overworld":
                return False
            if dim == self.portal_dimension and (
                self.portal_entered or self._target == self.portal_advancement or self._end_after_stronghold()
            ):
                if not self.portal_entered:
                    print(f"[{self.label}] {self.bot_name} verified progression to {dim}", flush=True)
                self.portal_entered = True
                return False
            revoke = ENTER_END if self.stronghold_run and dim == "the_end" else ENTER_NETHER
            restock_scene(
                self.bot_name, self.scene,
                reason=f"{self.label.lower().replace(' ', '')}_unplanned_dimension({dim})",
                revoke_advancement=revoke,
            )
            for _ in range(4):
                time.sleep(0.5)
                reported_dim = self.bot.status().get("dimension")
                if reported_dim and normalize_dimension(reported_dim) == "overworld":
                    return True
            raise RuntimeError(f"{self.bot_name} {self.label} Overworld return could not be verified")


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
    stronghold_start = scene.stage == STRONGHOLD_STAGE
    sample_limit = 5 if stronghold_start else 2
    station = {"brewing_alchemy": "brewing_stand", "enchant_anvil": "enchanting_table"}.get(scene.stage)
    station_pos = [math.floor(scene.x) + 2, round(scene.y), math.floor(scene.z)]
    for i in range(attempts):
        consecutive = 0
        for sample in range(sample_limit):
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
            if inside and scene.stage == DAY2_VILLAGE_STAGE and scene.village_xz is not None:
                vx, vz = scene.village_xz
                inner, outer = VILLAGE_APPROACH_RANGE
                distance = math.hypot(float(pos[0]) - vx, float(pos[2]) - vz)
                inside = (
                    inner <= distance <= outer
                    and st.get("on_ground") is not False
                    and not (st.get("in_water") or st.get("in_lava"))
                )
            wait_for_ground = False
            if inside and stronghold_start:
                near_anchor = math.hypot(dx, dz) <= 6.0 and abs(dy) <= 2.0
                inside = near_anchor and _dry_overworld_position(st) is not None
                wait_for_ground = near_anchor and not (st.get("in_water") or st.get("in_lava"))
            consecutive = consecutive + 1 if inside else 0
            if consecutive == 2:
                break
            if not inside and not wait_for_ground:
                break
            if sample + 1 < sample_limit:
                time.sleep(1.2)
        if consecutive == 2:
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
        footing = (
            f" on_ground={st.get('on_ground')} in_water={st.get('in_water')} in_lava={st.get('in_lava')}"
            if stronghold_start else ""
        )
        print(
            f"[Scene] entry verify failed (attempt {i + 1}/{attempts}) bot={bot_name} "
            f"dim={dim} pos={pos}{footing} expected {scene.dim}@({scene.x:.1f},{scene.y:.1f},{scene.z:.1f})",
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

STRONGHOLD_TOOLS = ("mc_use_item", "mc_skill_scout_surface", "mc_skill_collect_drops", "mc_status", "mc_inventory")
STRONGHOLD_FOCUS = (
    {
        "key": FOLLOW_ENDER_EYE,
        "title": "60分：从村庄跟随末影之眼进入要塞",
        "task": (
            "从村庄出发寻找并进入天然主世界要塞。背包有 14 颗末影之眼。先在开阔处调用 "
            "mc_use_item(item='ender_eye')。确认 projectile 是 eye_of_ender 后，把回执的 scout_direction 原样传给斥候工具。"
            "回执缺少有效方向或 projectile 不符时，立即用 mc_get_entities(types=['eye_of_ender'], radius=32)，"
            "把眼的位置减去你的位置。正 X 向东、负 X 向西、正 Z 向南、负 Z 向北。"
            "[0,0] 表示方向未测出。等眼飞开再查一次，仍无方向且眼多于 12 颗时才补投一次。"
            "工具给出 suggested_second_throw_xz 时，可先走到围栏内的干地观测点再投第二颗。"
            "有 triangulated_stronghold_xz 时，把它当作估算坐标，留住剩余的眼并分段接近，再用方块扫描确认。"
            "沿眼指向用 mc_skill_scout_surface(direction=方向, target_types=['end_portal_frame'], "
            "step_dist=30, max_duration_s=45) 分段推进。确认附近有掉落的眼后，再用 mc_skill_collect_drops 回收。"
            "没有第二观测点建议时，至少离上次投掷点 100 格再复测。若两次有效方向相反，用 mc_move_to 向上一观测点回走一半距离，"
            "缩小搜索范围。只剩 12 颗时优先用已有方向和方块线索定位。"
            "eye_went_down 为 true、到达估算坐标附近或出现方向反转时，分开扫描 end_portal_frame、stone_bricks 与 mossy_stone_bricks。"
            "看到门框或成片要塞砖后，记下回执的 X/Y/Z，结束长距离斥候。"
            "若只有靠近黑曜石的零散地表砖和箱子，继续核对地下结构。"
            "用 mc_skill_staircase(vertical='down', steps=8, stop_on_ore=False) 分段下挖，留火把和回程阶梯。"
            "下到与砖群同层后，沿安全实心地面用 mc_skill_tunnel 分段朝砖群 X/Z 接近。"
            "遇岩浆或连续挖掘失败，沿既有阶梯撤回干地，从另一侧下挖。"
            "进入实际要塞结构才触发 follow_ender_eye。"
        ),
        "extra_tools": STRONGHOLD_TOOLS,
    },
    {
        "key": ENTER_END,
        "title": "60分：找到要塞传送门房并进入末地",
        "task": (
            "沿之前的地下入口继续调查要塞。用 mc_find_blocks(types=['end_portal_frame'], radius=64) "
            "找传送门框并记住观测到的 X/Y/Z。用阶梯先上到或下到门框附近的高度，勿隔层直传 y 寻路。"
            "确认落脚点和岩浆后再水平接近。用门框坐标减当前坐标，负 Z 向北、正 Z 向南，负 X 向西、正 X 向东。"
            "优先走现有通道，需挖通时每次掘进 4–8 格。核对回执位置确实更接近门框，距离增大就改向。"
            "对每一个没有眼的门框，在可触及范围内调用 mc_interact(x=框的X, y=框的Y, z=框的Z, item='ender_eye')。"
            "用 mc_get_block 核对门框的 eye 状态。全部激活后扫描 end_portal，走进传送门并用 mc_status 确认 the_end。"
            "若死亡回村，拿补给沿记住的路线返回，必要时再用末影之眼与地表斥候重新定位。"
        ),
        "extra_tools": (*STRONGHOLD_TOOLS, "mc_interact", "mc_get_block"),
    },
)


def install_scene_focus(stage: str, episodes: int = 2, *, scene: SceneAnchor | None = None) -> bool:
    """Force the first `episodes` curriculum picks to a stage-specific target (SCENE-001).

    `CurriculumSupervisor.run()` resolves `select_next_target` from the module namespace at call
    time, so wrapping that module function is enough — no core `harness/` edits. After the focus
    budget is exhausted the original selector takes over again.
    """
    spec = SCENE_FOCUS.get(stage)
    if episodes <= 0 or (not spec and stage not in {DAY2_VILLAGE_STAGE, STRONGHOLD_STAGE}):
        return False
    from mc_agent.harness import curriculum as cur

    if stage == STRONGHOLD_STAGE:
        targets = tuple(
            cur.CurriculumTarget(
                **step,
                points=cur.RULES_ADVANCEMENT_POINTS[step["key"]],
                tier=3,
                budget_class="expedition",
                advancement_id=step["key"],
            )
            for step in STRONGHOLD_FOCUS
        )
        remaining = None
    elif stage == DAY2_VILLAGE_STAGE:
        origin = (scene.x, scene.y, scene.z) if scene is not None else None
        target = cur._build_day2_village_search_target(origin, ())
        target = dataclasses.replace(
            target,
            task=(
                "开局在真实村庄外围 120–180 格。沿地表分段搜索，用 bell、dirt_path、hay_block 或村民确认村庄。"
                "若当前方向没有村庄信号，回到出发区域换方向再查。"
                + target.task
            ),
        )
        targets = (target,)
        remaining = 1
    else:
        target_kwargs = {k: v for k, v in spec.items() if k != "episodes"}
        if stage == "brewing_alchemy" and scene is not None:
            x, y, z = math.floor(scene.x), round(scene.y), math.floor(scene.z)
            target_kwargs["task"] = (
                f"场景台面中心在 ({scene.x:.1f}, {y}, {scene.z:.1f})。若已走远，先回到台面中心。"
                f"优先用玻璃瓶点布置好的水源 ({x - 1}, {y - 1}, {z})，备用水源在 ({x - 2}, {y - 1}, {z - 1})。"
                f"酿造台在 ({x + 2}, {y}, {z})。查询水源时看这两个坐标和 level=0。"
                + target_kwargs["task"]
            )
        targets = (cur.CurriculumTarget(**target_kwargs),)
        remaining = int(spec.get("episodes", episodes))
    original = cur.select_next_target

    def _wrapped(bot: Any, **kwargs: Any):
        nonlocal remaining
        if remaining is None or remaining > 0:
            frontier = cur.compute_advancement_frontier(bot, limit=50)
            completed = frontier.get("completed_ids") or []
            for target in targets:
                if target.advancement_id not in completed:
                    if remaining is not None:
                        remaining -= 1
                    return target, frontier
            remaining = 0
        return original(bot, **kwargs)

    cur.select_next_target = _wrapped
    focus = " -> ".join(target.key for target in targets)
    limit = "until verified" if remaining is None else f"up to {remaining} opening episode(s)"
    print(f"[Scene Focus] stage={stage} {limit} -> {focus}", flush=True)
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
            DAY2_VILLAGE_STAGE,
            STRONGHOLD_STAGE,
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
    parser.add_argument("--day1", action="store_true", help="Require a portal-free bare landing and guard unplanned dimension changes")
    parser.add_argument(
        "--focus-episodes",
        type=int,
        default=2,
        help="Scene opening episode count (0 disables). The stronghold chain continues until both goals are verified.",
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
        help="Anchor x,y,z used by --print-plan (defaults to the slot's search origin)",
    )
    args = parser.parse_args()
    if args.max_episodes < 0:
        parser.error("--max-episodes must be zero or positive")
    if args.day1 and args.stage != "bare":
        parser.error("--day1 requires --stage bare")

    bot_name = args.bot
    if args.print_plan:
        if args.plan_pos:
            try:
                px, py, pz = (float(v) for v in args.plan_pos.split(","))
            except ValueError:
                raise SystemExit("--plan-pos must be 'x,y,z'")
        else:
            sites = NETHER_CANDIDATE_SITES if args.stage in NETHER_STAGES else CANDIDATE_SITES
            ax, az = sites[args.site_index % len(sites)]
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
        if args.stage == DAY2_VILLAGE_STAGE:
            plan["site_search"] = {"structure": "#minecraft:village", "distance_xz_m": list(VILLAGE_APPROACH_RANGE)}
        elif args.stage == STRONGHOLD_STAGE:
            plan["site_search"] = {
                "village": "#minecraft:village",
                "stronghold": "minecraft:stronghold",
                "stronghold_distance_xz_m": list(STRONGHOLD_DISTANCE_RANGE),
            }
            plan["geofence_radius"] = STRONGHOLD_TRAVEL_RADIUS
            plan["focus"] = [FOLLOW_ENDER_EYE, ENTER_END] if args.focus_episodes > 0 else []
        elif args.stage in NETHER_STAGES:
            plan["site_search"] = {
                "structure": NETHER_STRUCTURES[args.stage],
                "origin_xz": NETHER_CANDIDATE_SITES[args.site_index % len(NETHER_CANDIDATE_SITES)],
                "max_distance_xz_m": NETHER_SITE_RADIUS,
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
        starting_site = None
        village_xz = None
        stronghold_xz = None
        try:
            st = ensure_bot_ready(bot, timeout_s=45.0)
            if args.day1:
                starting_site = select_dry_isolated_wild_site(bot, bot_name, start_idx=args.site_index, day1=True)
            elif args.stage == DAY2_VILLAGE_STAGE:
                starting_site, village_xz = select_day2_village_site(bot, bot_name, args.site_index)
            elif args.stage == STRONGHOLD_STAGE:
                starting_site, village_xz, stronghold_xz = select_stronghold_village_site(bot, bot_name, args.site_index)
            elif args.stage in NETHER_STAGES:
                starting_site = select_nether_structure_site(
                    bot, bot_name, NETHER_STRUCTURES[args.stage], start_idx=args.site_index,
                )
        except Exception as exc:
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
        if starting_site is not None:
            sx, sy, sz, biome = starting_site
        elif args.stage in ("village", "village_trade"):
            sx, sy, sz, biome = select_village_wild_site(
                bot,
                bot_name,
                start_idx=args.site_index,
            )
        elif args.stage == "deep_water":
            sx, sy, sz, biome = select_deep_water_site(bot, bot_name, start_idx=args.site_index)
        else:
            sx, sy, sz, biome = select_dry_isolated_wild_site(bot, bot_name, start_idx=args.site_index)
        if args.stage in LOCAL_FIXTURE_STAGES:
            sx, sy, sz = math.floor(sx) + 0.5, float(round(sy)), math.floor(sz) + 0.5
        dim = scene_dimension(args.stage)
        scene = SceneAnchor(stage=args.stage, dim=dim, x=sx, y=sy, z=sz, village_xz=village_xz)
        if args.stage == STRONGHOLD_STAGE:
            scene = dataclasses.replace(scene, radius=STRONGHOLD_TRAVEL_RADIUS, drift_limit=STRONGHOLD_TRAVEL_RADIUS + 100)
        baseline = compute_stage_baseline(args.stage)
        prep = prepare_scene_ground(bot_name, args.stage, sx, sy, sz, dim=dim)
        if prep:
            rcon_batch(prep, timeout=60)
        reset_wild_bot(bot_name, sx, sy, sz, set_day=not args.keep_time, stage=args.stage, dim=dim)
        time.sleep(1.5)
        entry_ok = verify_scene_entry(bot, bot_name, scene)
        if not entry_ok and args.stage in (NETHER_STAGES | {DAY2_VILLAGE_STAGE, STRONGHOLD_STAGE}):
            msg = f"Scene {args.stage} landing could not be verified"
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
        if args.stage == "village_trade":
            _pin_villager_near(bot, bot_name, [sx + 2.0, sy, sz])

        geofence = _scene_geofence_cls(VoidGeofence)(
            center_x=sx, center_z=sz, radius=scene.radius, margin=8.0
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

        scene_state: dict[str, Any] = {"sup": None}
        portal_guard = (
            OverworldProgressGuard(bot, bot_name, scene)
            if args.day1 or args.stage in {DAY2_VILLAGE_STAGE, STRONGHOLD_STAGE}
            else None
        )

        def _recover_overworld(dimension: str | None = None) -> bool:
            if portal_guard is None or not portal_guard.keep_home(dimension):
                return False
            _reassert_scene_geofence(toolset, scene, dimension="overworld", supervisor=scene_state["sup"])
            return True

        install_scene_focus(args.stage, args.focus_episodes, scene=scene)
        if portal_guard is not None:
            from mc_agent.harness import curriculum as cur

            select_target = cur.select_next_target

            def _select_guarded_target(session: Any, **kwargs: Any):
                _recover_overworld()
                target, frontier = select_target(session, **kwargs)
                portal_guard.note_target(target.key)
                return target, frontier

            cur.select_next_target = _select_guarded_target

        def _guard_scene_anchor(ev: dict[str, Any]) -> None:
            kind = ev.get("type")
            if portal_guard is not None and kind in (
                "curriculum.pre_ensure.started", "curriculum.pre_ensure.completed", "run.started", "run.finished",
            ):
                if kind == "curriculum.pre_ensure.started":
                    portal_guard.note_target(str(ev["target"]))
                try:
                    _recover_overworld()
                except Exception as exc:
                    print(f"[{portal_guard.label}] {bot_name} dimension check at {kind} pending: {exc}", flush=True)
            if kind != "curriculum.episode.started":
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

        def _repair_fence(dimension: str | None, pos: Any) -> None:
            if not _recover_overworld(dimension):
                _reassert_scene_geofence(toolset, scene, dimension=dimension, pos=pos, supervisor=scene_state["sup"])

        watchdog = SceneWatchdog(
            bot, bot_name, scene, near_container=_near_known_container, fence_repair=_repair_fence
        )
        watchdog.start()
        trader_keeper = TraderKeeper(bot, bot_name) if args.stage == "village_trade" else None
        if trader_keeper is not None:
            trader_keeper.start()
            print(f"[Scene] trader keeper started for {bot_name} (15s re-anchor)", flush=True)

        class StrongholdSupervisor(CurriculumSupervisor):
            def _detect_surface_preempt_opportunity(self, *, current_key: str, **kwargs: Any) -> str | None:
                if current_key in (FOLLOW_ENDER_EYE, ENTER_END):
                    return None
                return super()._detect_surface_preempt_opportunity(current_key=current_key, **kwargs)

        supervisor_class = (
            StrongholdSupervisor if args.stage == STRONGHOLD_STAGE and args.focus_episodes > 0 else CurriculumSupervisor
        )
        try:
            sup = supervisor_class(
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
                "day1": args.day1,
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
                "dimension": st_end.get("dimension"),
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
        if village_xz is not None:
            vx, vz = village_xz
            payload["scene"]["village"] = {"x": vx, "z": vz, "spawn_distance_m": round(math.hypot(sx - vx, sz - vz), 2)}
        if stronghold_xz is not None:
            hx, hz = stronghold_xz
            payload["scene"]["stronghold"] = {"x": hx, "z": hz, "spawn_distance_m": round(math.hypot(sx - hx, sz - hz), 2)}
            payload["scene"]["end_entry_verified"] = portal_guard is not None and portal_guard.portal_entered
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"\n[Wild Run Complete] {out_file} | final_score={summary.get('final_score')} "
            f"| completed_advs={summary.get('completed_count')} | guardian={guardian_totals}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
