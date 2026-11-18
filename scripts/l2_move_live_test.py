#!/usr/bin/env python3
"""`nju-beast` live acceptance for the L2 movement primitives (M1-M8, real physics, no teleport cheating).

Usage:
  PYTHONPATH=.../src python scripts/l2_move_live_test.py --only M1,M2
  ... --only all --report runs/move-live-<ts>.json
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import l2_pack_live_test as common  # noqa: E402
from mc_agent.errors import ApiError  # noqa: E402
from mc_agent.skills.movement import TerrainNavigator  # noqa: E402

BOT = common.BOT
Y = common.ARENA["y"]


def _x0() -> int:
    return common.ARENA["x0"]


def _z0() -> int:
    return common.ARENA["z0"]


def _status(bot: Any) -> dict[str, Any]:
    return bot.status()


def _pos(bot: Any) -> list[float]:
    return list((bot.status().get("pos") or [0.0, 0.0, 0.0]))


def _health(bot: Any) -> float:
    return float(bot.status().get("health") or 0.0)


def _tp(x: float, y: float, z: float) -> None:
    common.tp(x, y, z)


def _walk(bot: Any, x: float, z: float, *, range_m: float = 1.4, timeout: float = 30.0) -> dict[str, Any]:
    try:
        return bot.call(
            "move_to",
            {"x": float(x), "z": float(z), "range": float(range_m), "stop_on": ["death"], "timeout": int(timeout * 1000)},
            timeout_s=timeout + 10.0,
        )
    except ApiError as err:
        return {"ok": False, "error": err.code, "message": err.message}
    except Exception as err:  # noqa: BLE001 - httpx timeouts must not kill a level
        return {"ok": False, "error": type(err).__name__, "message": str(err)[:200]}


def _walk_straight(bot: Any, tx: float, tz: float, *, max_steps: int = 24, step_ms: int = 300, tol: float = 0.45) -> dict[str, Any]:
    """Deterministic short walk (no pathfinder): face the target and press forward in small bursts."""
    moved = 0.0
    stalled = 0
    for _ in range(max_steps):
        st = bot.status()
        p = st.get("pos") or [0, 0, 0]
        d = math.hypot(tx - float(p[0]), tz - float(p[2]))
        if d <= tol:
            return {"ok": True, "dist": round(d, 2), "pos": p, "moved": round(moved, 2)}
        bot.call("look", {"x": float(tx), "y": float(p[1]) + 1.5, "z": float(tz)})
        try:
            bot.call("control", {"forward": True, "ms": int(step_ms), "stop_on": ["death"]}, timeout_s=8.0)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": type(err).__name__, "dist": round(d, 2)}
        p2 = (bot.status().get("pos") or p)
        step = math.hypot(float(p2[0]) - float(p[0]), float(p2[2]) - float(p[2]))
        moved += step
        stalled = stalled + 1 if step < 0.05 else 0
        if stalled >= 3:
            break
    p = bot.status().get("pos") or [0, 0, 0]
    return {"ok": False, "dist": round(math.hypot(tx - float(p[0]), tz - float(p[2])), 2), "pos": p, "moved": round(moved, 2)}


def _park_if_fell(bot: Any, x: float, y: float, z: float) -> bool:
    st = bot.status()
    p = st.get("pos") or [0, 0, 0]
    if float(p[1]) < y - 2.0:
        _tp(x, y, z)
        time.sleep(0.8)
        return True
    return False


# ---------------------------------------------------------------------------
# M1 soft-trap corridor: sweet_berry_bush + cobweb + bamboo
# ---------------------------------------------------------------------------


def build_m1() -> None:
    x0, z = _x0() + 2, _z0() + 8
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 16} {Y - 1} {z + 2} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 16} {Y + 4} {z + 2} minecraft:air",
        # 1-wide corridor walls
        f"fill {x0 + 1} {Y} {z - 1} {x0 + 15} {Y + 2} {z - 1} minecraft:stone",
        f"fill {x0 + 1} {Y} {z + 1} {x0 + 15} {Y + 2} {z + 1} minecraft:stone",
        # start / goal pads
        f"fill {x0 - 2} {Y} {z - 1} {x0} {Y} {z + 1} minecraft:stone",
        f"fill {x0 + 16} {Y} {z - 1} {x0 + 18} {Y} {z + 1} minecraft:stone",
        f"setblock {x0 - 2} {Y} {z} minecraft:air",
    ]
    # 10 obstructed cells: alternating berry bush / cobweb / bamboo, 2 high
    for i in range(10):
        cx = x0 + 2 + i
        block = ("sweet_berry_bush", "cobweb", "bamboo")[i % 3]
        cmds.append(f"setblock {cx} {Y} {z} minecraft:{block} replace")
        if block == "bamboo":
            cmds.append(f"setblock {cx} {Y + 1} {z} minecraft:bamboo replace")
        elif block == "sweet_berry_bush":
            cmds.append(f"setblock {cx} {Y + 1} {z} minecraft:air replace")
    common.rcon_batch(cmds)


def run_m1(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 2, _z0() + 8
    goal = (x0 + 17 + 0.5, z + 0.5)
    _tp(x0 - 0.5, Y, z + 0.5)
    common.prepare_player({"cooked_beef": 16})
    time.sleep(0.6)
    hp0 = _health(bot)
    t0 = time.monotonic()
    obstacles: list[dict[str, Any]] = []
    reached = False
    while time.monotonic() - t0 < 45.0:
        st = _status(bot)
        p = st.get("pos") or [0, 0, 0]
        if math.hypot(goal[0] - float(p[0]), goal[1] - float(p[2])) <= 1.8:
            reached = True
            break
        before = [float(p[0]), float(p[2])]
        res = nav.clear_traps_and_doors(dx=1, dz=0, clear_radius=1, max_ops=4)
        obstacles.append({"cleared": res.get("cleared"), "ops": res.get("ops")})
        walk = _walk(bot, goal[0], goal[1], range_m=1.2, timeout=6.0)
        after = _pos(bot)
        if math.hypot(float(after[0]) - before[0], float(after[2]) - before[1]) < 0.2 and not walk.get("ok"):
            # fully blocked and nothing left to clear
            if not res.get("cleared"):
                break
    elapsed = time.monotonic() - t0
    hp1 = _health(bot)
    return {
        "reached": reached,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": hp1,
        "damage": round(max(0.0, hp0 - hp1), 1),
        "pos": _pos(bot),
        "goal": list(goal),
        "obstacles": obstacles[:8],
        "ok": reached and hp1 >= 18.0 and elapsed <= 35.0,
    }


# ---------------------------------------------------------------------------
# M2 powder snow pit (no leather boots)
# ---------------------------------------------------------------------------


def build_m2() -> None:
    x0, z = _x0() + 2, _z0() + 20
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 8} {Y - 1} {z + 6} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 8} {Y + 5} {z + 6} minecraft:air",
        # 3x3x2 powder snow pit, walls 2 high around
        f"fill {x0 + 2} {Y} {z + 2} {x0 + 4} {Y + 1} {z + 4} minecraft:powder_snow",
        f"fill {x0 + 1} {Y} {z + 1} {x0 + 5} {Y + 2} {z + 1} minecraft:stone",
        f"fill {x0 + 1} {Y} {z + 5} {x0 + 5} {Y + 2} {z + 5} minecraft:stone",
        f"fill {x0 + 1} {Y} {z + 1} {x0 + 1} {Y + 2} {z + 5} minecraft:stone",
        f"fill {x0 + 5} {Y} {z + 1} {x0 + 5} {Y + 2} {z + 5} minecraft:stone",
    ]
    common.rcon_batch(cmds)


def run_m2(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 2, _z0() + 20
    common.prepare_player({"bucket": 1, "cooked_beef": 8})
    _tp(x0 + 3.5, Y + 2, z + 3.5)
    time.sleep(2.0)
    st = _status(bot)
    start = {
        "pos": st.get("pos"),
        "in_water": st.get("in_water"),
        "submerged": st.get("submerged"),
        "health": st.get("health"),
    }
    hp0 = _health(bot)
    t0 = time.monotonic()
    res = nav.clear_traps_and_doors(dx=0, dz=-1, clear_radius=2, max_ops=8)
    # try to walk out upwards/north
    for _ in range(4):
        if not _status(bot).get("in_water") and _pos(bot)[1] >= Y:
            break
        nav.climb_or_descend_cliff(target_y=Y + 2, dz=-1, allow_dig=True)
        _walk(bot, x0 + 3.5, z - 1.0, range_m=1.5, timeout=8.0)
    elapsed = time.monotonic() - t0
    st2 = _status(bot)
    p2 = st2.get("pos") or [0, 0, 0]
    escaped = float(p2[1]) >= Y and not st2.get("submerged")
    return {
        "start": start,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st2.get("health"),
        "pos": st2.get("pos"),
        "scooped": res.get("scooped"),
        "cleared": res.get("cleared"),
        "escaped": escaped,
        "ok": escaped and float(st2.get("health") or 0) > 0,
    }


# ---------------------------------------------------------------------------
# M3 village door + 1.5-high fence yard with gate
# ---------------------------------------------------------------------------


def build_m3() -> None:
    x0, z = _x0() + 22, _z0() + 4
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 18} {Y - 1} {z + 16} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 18} {Y + 6} {z + 16} minecraft:air",
        # fence yard 12x10 with gate on the south wall
        f"fill {x0} {Y} {z} {x0 + 11} {Y + 1} {z} minecraft:oak_fence",
        f"fill {x0} {Y} {z + 9} {x0 + 11} {Y + 1} {z + 9} minecraft:oak_fence",
        f"fill {x0} {Y} {z} {x0} {Y + 1} {z + 9} minecraft:oak_fence",
        f"fill {x0 + 11} {Y} {z} {x0 + 11} {Y + 1} {z + 9} minecraft:oak_fence",
        f"setblock {x0 + 5} {Y} {z + 9} minecraft:oak_fence_gate[open=false,facing=south]",
        # a real gate opening is 1 block high: clear the fence block above it
        f"setblock {x0 + 5} {Y + 1} {z + 9} minecraft:air",
        # 5x5 house inside the yard with a closed wooden door on its south side
        f"fill {x0 + 3} {Y} {z + 2} {x0 + 7} {Y + 2} {z + 6} minecraft:oak_planks",
        f"fill {x0 + 4} {Y} {z + 3} {x0 + 6} {Y + 1} {z + 5} minecraft:air",
        f"setblock {x0 + 5} {Y} {z + 6} minecraft:oak_door[half=lower,facing=south,open=false]",
        f"setblock {x0 + 5} {Y + 1} {z + 6} minecraft:oak_door[half=upper,facing=south,open=false]",
        # ceiling so it is a real house
        f"fill {x0 + 3} {Y + 3} {z + 2} {x0 + 7} {Y + 3} {z + 6} minecraft:oak_planks",
    ]
    common.rcon_batch(cmds)


def run_m3(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 22, _z0() + 4
    _tp(x0 + 5.5, Y, z + 13.5)
    common.prepare_player({"cooked_beef": 8})
    time.sleep(0.6)
    door_state = bot.call("get_block", {"pos": [x0 + 5, Y, z + 6]})
    gate_state = bot.call("get_block", {"pos": [x0 + 5, Y, z + 9]})
    events: list[dict[str, Any]] = []
    fell = False
    hp0 = _health(bot)
    t0 = time.monotonic()
    ok_path = False
    for _round in range(2):
        w1 = _walk_straight(bot, x0 + 5.5, z + 10.6, max_steps=26)
        res1 = nav.clear_traps_and_doors(dx=0, dz=-1, clear_radius=1, max_ops=4, door_radius=6)
        if res1.get("opened"):
            events.append({"stage": "gate", "opened": res1["opened"], "errors": res1.get("errors")})
        w2 = _walk_straight(bot, x0 + 5.5, z + 8.2, max_steps=20)
        res2 = nav.clear_traps_and_doors(dx=0, dz=-1, clear_radius=1, max_ops=4, door_radius=6)
        if res2.get("opened"):
            events.append({"stage": "door", "opened": res2["opened"], "errors": res2.get("errors")})
        w3 = _walk_straight(bot, x0 + 5.5, z + 4.5, max_steps=24)
        p = _pos(bot)
        if math.hypot(p[0] - (x0 + 5.5), p[2] - (z + 4.5)) <= 1.4 and abs(p[1] - Y) <= 2:
            ok_path = True
            break
        if _park_if_fell(bot, x0 + 5.5, Y, z + 13.5):
            fell = True
    st = _status(bot)
    elapsed = time.monotonic() - t0
    gate_after = bot.call("get_block", {"pos": [x0 + 5, Y, z + 9]})
    door_after = bot.call("get_block", {"pos": [x0 + 5, Y, z + 6]})
    return {
        "reached": ok_path,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "door_before": (door_state.get("properties") or {}).get("open"),
        "gate_before": (gate_state.get("properties") or {}).get("open"),
        "door_after": (door_after.get("properties") or {}).get("open"),
        "gate_after": (gate_after.get("properties") or {}).get("open"),
        "fell_out": fell,
        "events": events[:10],
        "ok": ok_path and float(st.get("health") or 0) > 0,
    }


# ---------------------------------------------------------------------------
# M4 8-wide chasm + floating sand shore
# ---------------------------------------------------------------------------


def build_m4() -> None:
    x0, z = _x0() + 40, _z0() + 4
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 20} {Y - 1} {z + 8} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 20} {Y + 6} {z + 8} minecraft:air",
        # carve the chasm all the way down (8 wide)
        f"fill {x0 + 4} {Y - 2} {z} {x0 + 11} {Y - 1} {z + 4} minecraft:air",
        f"fill {x0 + 4} {Y - 40} {z} {x0 + 11} {Y - 3} {z + 4} minecraft:air",
        # sand lip at the very edge, but properly supported (no false-positive refusal expected)
        f"fill {x0 + 2} {Y - 1} {z} {x0 + 3} {Y - 1} {z + 4} minecraft:sand",
        f"fill {x0 + 2} {Y - 2} {z} {x0 + 3} {Y - 2} {z + 4} minecraft:dirt",
        # far shore pad
        f"fill {x0 + 12} {Y - 1} {z} {x0 + 16} {Y - 1} {z + 4} minecraft:stone",
    ]
    common.rcon_batch(cmds)


def run_m4(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 40, _z0() + 4
    _tp(x0 + 0.5, Y, z + 2.5)
    common.prepare_player({"cobblestone": 32, "cooked_beef": 8})
    time.sleep(0.6)
    hp0 = _health(bot)
    t0 = time.monotonic()
    zc = z + 2.5
    res = nav.sneak_bridge(x0 + 13.0, zc, max_steps=20)
    # finish walking onto the far pad
    walk = _walk(bot, x0 + 15.0, zc, range_m=1.2, timeout=10.0)
    elapsed = time.monotonic() - t0
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    safe_pos = abs(float(p[1]) - Y) <= 2.0
    return {
        "bridge": res,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "sneaking": st.get("sneaking"),
        "support_below_start": res.get("reinforced_gravity"),
        "walk": walk,
        "ok": bool(res.get("ok")) and safe_pos and not st.get("sneaking") and float(st.get("health") or 0) > 0,
    }


# ---------------------------------------------------------------------------
# M5 7-block stone cliff up + 8-block safe descent
# ---------------------------------------------------------------------------


def build_m5() -> None:
    x0, z = _x0() + 62, _z0() + 4
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 20} {Y - 1} {z + 10} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 20} {Y + 12} {z + 10} minecraft:air",
        # solid 8x8 column cliff, 7 high
        f"fill {x0 + 4} {Y} {z} {x0 + 11} {Y + 6} {z + 7} minecraft:stone",
    ]
    common.rcon_batch(cmds)


def run_m5(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 62, _z0() + 4
    _tp(x0 + 1.5, Y, z + 3.5)
    common.prepare_player({"stone_pickaxe": 1, "cobblestone": 32, "cooked_beef": 8})
    time.sleep(0.6)
    hp0 = _health(bot)
    t0 = time.monotonic()
    # walk up to the cliff face first (the primitive handles the vertical part)
    _walk(bot, x0 + 3.2, z + 3.5, range_m=0.8, timeout=15.0)
    up = nav.climb_or_descend_cliff(target_y=Y + 7, dx=1, dz=0)
    st_mid = _status(bot)
    y_mid = float((st_mid.get("pos") or [0, 0, 0])[1])
    # walk onto the top plateau edge, then descend inside the rock
    _walk(bot, x0 + 9.0, z + 3.5, range_m=1.0, timeout=20.0)
    down = nav.climb_or_descend_cliff(target_y=Y + 0, dx=-1, dz=0)
    elapsed = time.monotonic() - t0
    st = _status(bot)
    y_end = float((st.get("pos") or [0, 0, 0])[1])
    return {
        "up": up,
        "down": down,
        "y_after_up": y_mid,
        "y_end": y_end,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "ok": y_mid >= Y + 6 and y_end <= Y + 1 and float(st.get("health") or 0) >= hp0 - 0.5,
    }


# ---------------------------------------------------------------------------
# M6 three-layer leaf wall
# ---------------------------------------------------------------------------


def build_m6() -> None:
    x0, z = _x0() + 24, _z0() + 26
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 18} {Y - 1} {z + 4} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 18} {Y + 5} {z + 4} minecraft:air",
        f"fill {x0 + 5} {Y} {z} {x0 + 7} {Y + 4} {z + 2} minecraft:oak_leaves[persistent=true]",
    ]
    common.rcon_batch(cmds)


def run_m6(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 24, _z0() + 26
    _tp(x0 + 1.5, Y, z + 1.5)
    common.prepare_player({"cooked_beef": 8})
    time.sleep(0.5)
    goal = (x0 + 14.0, z + 1.5)
    hp0 = _health(bot)
    t0 = time.monotonic()
    cleared: list[dict[str, Any]] = []
    reached = False
    while time.monotonic() - t0 < 45.0:
        p = _pos(bot)
        if math.hypot(goal[0] - float(p[0]), goal[1] - float(p[2])) <= 1.6:
            reached = True
            break
        res = nav.clear_traps_and_doors(dx=1, dz=0, clear_radius=1, max_ops=6)
        cleared.append({"cleared": res.get("cleared"), "ops": res.get("ops")})
        _walk(bot, goal[0], goal[1], range_m=1.2, timeout=6.0)
    elapsed = time.monotonic() - t0
    st = _status(bot)
    return {
        "reached": reached,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "cleared_rounds": len([c for c in cleared if c["cleared"]]),
        "ok": reached and float(st.get("health") or 0) > 0,
    }


# ---------------------------------------------------------------------------
# M7 40 m water crossing by boat
# ---------------------------------------------------------------------------


def build_m7() -> None:
    x0, z = _x0() + 2, _z0() + 40
    cmds = [
        f"fill {x0 - 2} {Y - 2} {z - 2} {x0 + 48} {Y - 2} {z + 24} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 48} {Y + 6} {z + 24} minecraft:air",
        # 40x20 water basin (surface at Y-1 = 149)
        f"fill {x0 + 4} {Y - 18} {z + 2} {x0 + 43} {Y - 1} {z + 21} minecraft:water",
        # raised stone banks
        f"fill {x0} {Y - 1} {z + 2} {x0 + 3} {Y} {z + 21} minecraft:stone",
        f"fill {x0 + 44} {Y - 1} {z + 2} {x0 + 47} {Y - 1} {z + 21} minecraft:stone",
        # keep the water from spilling over the pads
        f"fill {x0} {Y + 1} {z + 2} {x0 + 47} {Y + 1} {z + 1} minecraft:air",
    ]
    common.rcon_batch(cmds)


def run_m7(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 2, _z0() + 40
    _tp(x0 + 2.5, Y + 1, z + 12.0)
    common.prepare_player({"oak_boat": 1, "cooked_beef": 8})
    time.sleep(0.6)
    hp0 = _health(bot)
    t0 = time.monotonic()
    res = nav.cross_water_with_boat(x0 + 46.0, z + 12.0)
    elapsed = time.monotonic() - t0
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    on_far_bank = float(p[0]) >= x0 + 44.0 and not st.get("in_water") and not st.get("in_vehicle")
    return {
        "crossing": res,
        "elapsed_s": round(elapsed, 1),
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "on_far_bank": on_far_bank,
        "ok": bool(res.get("ok")) and on_far_bank and float(st.get("health") or 0) > 0,
    }


# ---------------------------------------------------------------------------
# M8 L-shaped corridor into a 2-layer dead end, then breadcrumb retreat
# ---------------------------------------------------------------------------


def build_m8() -> None:
    x0, z = _x0() + 40, _z0() + 40
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 30} {Y - 1} {z + 20} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 30} {Y + 8} {z + 20} minecraft:stone",
        # entrance platform + L corridor (2 high, 2 wide)
        f"fill {x0} {Y} {z + 10} {x0 + 3} {Y + 2} {z + 13} minecraft:air",
        f"fill {x0 + 3} {Y} {z + 10} {x0 + 18} {Y + 2} {z + 12} minecraft:air",
        f"fill {x0 + 16} {Y} {z + 2} {x0 + 18} {Y + 2} {z + 13} minecraft:air",
        # blind lower chamber
        f"fill {x0 + 12} {Y} {z + 2} {x0 + 18} {Y + 2} {z + 8} minecraft:air",
        # upper blind shelf reachable only by a hole (dead end above)
        f"fill {x0 + 14} {Y + 3} {z + 4} {x0 + 18} {Y + 4} {z + 8} minecraft:air",
    ]
    common.rcon_batch(cmds)


def run_m8(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 40, _z0() + 40
    _tp(x0 + 1.5, Y, z + 11.5)
    common.prepare_player({"cooked_beef": 8})
    time.sleep(0.6)
    trail = nav.trail
    trail.clear()
    trail.note_status()
    path = [
        (x0 + 9.5, z + 11.5),
        (x0 + 17.0, z + 11.0),
        (x0 + 17.0, z + 4.0),
        (x0 + 15.0, z + 3.5),
    ]
    for hx, hz in path:
        _walk(bot, hx, hz, range_m=1.0, timeout=12.0)
        trail.note_status()
    total_before = len(trail.points())
    t0 = time.monotonic()
    retreat = trail.backtrack(steps=3, max_dist=40.0)
    elapsed = time.monotonic() - t0
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    back_at_entrance = float(p[0]) <= x0 + 10.5 and abs(float(p[1]) - Y) <= 2
    return {
        "breadcrumbs_before": total_before,
        "retreat": retreat,
        "elapsed_s": round(elapsed, 1),
        "pos": st.get("pos"),
        "hp": st.get("health"),
        "back_at_entrance": back_at_entrance,
        "ok": total_before >= 3 and bool(retreat.get("ok")) and back_at_entrance,
    }


# ---------------------------------------------------------------------------
# Integration: navigate_segmented with a blocked middle (2 recoveries max)
# ---------------------------------------------------------------------------


def build_m9_integration() -> None:
    """A 66 m 1-wide bedrock corridor with a 6-wide bottomless gap (forces segmented recovery)."""
    x0, z = _x0() + 90, _z0() + 4
    cmds = [
        f"fill {x0 - 4} {Y - 1} {z - 5} {x0 + 72} {Y + 6} {z + 5} minecraft:air",
        f"fill {x0 - 2} {Y - 1} {z} {x0 + 70} {Y - 1} {z} minecraft:stone",
        f"fill {x0} {Y} {z - 1} {x0 + 66} {Y + 4} {z - 1} minecraft:bedrock",
        f"fill {x0} {Y} {z + 1} {x0 + 66} {Y + 4} {z + 1} minecraft:bedrock",
        # 6-wide gap: pathfinder cannot jump 6 blocks
        f"fill {x0 + 30} {Y - 30} {z} {x0 + 35} {Y - 1} {z} minecraft:air",
    ]
    common.rcon_batch(cmds)


def run_m9_integration(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 90, _z0() + 4
    _tp(x0 + 0.5, Y, z + 0.5)
    common.prepare_player({"cooked_beef": 8, "cobblestone": 32})
    time.sleep(0.5)
    t0 = time.monotonic()
    from mc_agent.skills.library import MacroSkills

    skills = MacroSkills(bot)
    skills.world_memory = getattr(nav, "_wm", None)
    skills._terrain_nav = nav
    try:
        out = skills.navigate_segmented(x0 + 66.0, z + 0.5, range_m=2.0)
    except ApiError as err:
        out = {"ok": False, "error": err.code, "message": err.message, "context": err.context}
    elapsed = time.monotonic() - t0
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    return {
        "result": out,
        "elapsed_s": round(elapsed, 1),
        "pos": st.get("pos"),
        "reached": float(p[0]) >= x0 + 60.0 and abs(float(p[1]) - Y) <= 2.0,
        "recoveries": out.get("recoveries") if isinstance(out, dict) else None,
        "ok": bool(out.get("ok")) and float(p[0]) >= x0 + 60.0 and abs(float(p[1]) - Y) <= 2.0 and bool(out.get("recoveries")),
    }


BUILDERS = {
    "M1": build_m1,
    "M2": build_m2,
    "M3": build_m3,
    "M4": build_m4,
    "M5": build_m5,
    "M6": build_m6,
    "M7": build_m7,
    "M8": build_m8,
    "M9": build_m9_integration,
}
RUNNERS = {
    "M1": run_m1,
    "M2": run_m2,
    "M3": run_m3,
    "M4": run_m4,
    "M5": run_m5,
    "M6": run_m6,
    "M7": run_m7,
    "M8": run_m8,
    "M9": run_m9_integration,
}



# ---------------------------------------------------------------------------
# M10 Step-Climb: a staircase of 1-block steps (no digging, no MLG)
# ---------------------------------------------------------------------------


def build_m10() -> None:
    x0, z = _x0() + 62, _z0() + 18
    cmds = [
        f"fill {x0 - 6} {Y - 1} {z - 6} {x0 + 18} {Y - 1} {z + 8} minecraft:stone",
        f"fill {x0 - 6} {Y} {z - 6} {x0 + 18} {Y + 12} {z + 8} minecraft:air",
    ]
    # 5 ascending 1-block steps (1 deep each), 3 wide, then a plateau at the top
    for i in range(5):
        cmds.append(
            f"fill {x0 + 2 + i} {Y - 1} {z} {x0 + 2 + i} {Y - 1 + i} {z + 2} minecraft:stone"
        )
    cmds.append(f"fill {x0 + 7} {Y - 1} {z} {x0 + 13} {Y + 3} {z + 2} minecraft:stone")
    common.rcon_batch(cmds)


def run_m10(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 62, _z0() + 18
    _tp(x0 + 0.5, Y, z)
    common.prepare_player({"cobblestone": 32, "cooked_beef": 8})
    time.sleep(0.6)
    hp0 = _health(bot)
    up = nav.climb_or_descend_cliff(target_y=Y + 4, dx=1, dz=0, allow_dig=False)
    st = _status(bot)
    y_end = float((st.get("pos") or [0, 0, 0])[1])
    return {
        "up": up,
        "y_end": y_end,
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "ok": y_end >= Y + 3 and float(st.get("health") or 0) >= hp0 - 0.5,
    }


# ---------------------------------------------------------------------------
# M11 water-bucket descent from a 6-block ledge
# ---------------------------------------------------------------------------


def build_m11() -> None:
    x0, z = _x0() + 80, _z0() + 18
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 10} {Y - 1} {z + 6} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 10} {Y + 10} {z + 6} minecraft:air",
        # 6-high platform with the drop on the west side
        f"fill {x0 + 2} {Y - 1} {z} {x0 + 8} {Y + 5} {z + 4} minecraft:stone",
        f"fill {x0 + 2} {Y} {z} {x0 + 8} {Y + 5} {z + 4} minecraft:stone",
    ]
    common.rcon_batch(cmds)


def run_m11(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 80, _z0() + 18
    _tp(x0 + 2.5, Y + 6, z + 2.0)
    common.prepare_player({"water_bucket": 1, "bucket": 1, "cooked_beef": 8})
    time.sleep(0.8)
    hp0 = _health(bot)
    down = nav.climb_or_descend_cliff(target_y=Y, dx=-1, dz=0, allow_dig=False, allow_water_bucket=True)
    st = _status(bot)
    y_end = float((st.get("pos") or [0, 0, 0])[1])
    return {
        "down": down,
        "y_end": y_end,
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "ok": y_end <= Y + 1 and float(st.get("health") or 0) >= hp0 - 2.0,
    }


# ---------------------------------------------------------------------------
# M12 insufficient filler: bridge must refuse and the bot must stay on the lip
# ---------------------------------------------------------------------------


def build_m12() -> None:
    x0, z = _x0() + 100, _z0() + 18
    cmds = [
        f"fill {x0 - 2} {Y - 1} {z - 2} {x0 + 16} {Y - 1} {z + 3} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 16} {Y + 6} {z + 3} minecraft:air",
        f"fill {x0 + 3} {Y - 30} {z - 1} {x0 + 10} {Y - 1} {z} minecraft:air",
    ]
    common.rcon_batch(cmds)


def run_m12(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 100, _z0() + 18
    _tp(x0 + 1.5, Y, z - 0.5)
    common.prepare_player({"cobblestone": 2, "cooked_beef": 8})
    time.sleep(0.6)
    res = nav.sneak_bridge(x0 + 12.0, z - 0.5, max_steps=12)
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    return {
        "bridge": res,
        "pos": st.get("pos"),
        "sneaking": st.get("sneaking"),
        "ok": res.get("ok") is False
        and abs(float(p[1]) - Y) <= 2
        and not st.get("sneaking"),
    }


BUILDERS.update({"M10": build_m10, "M11": build_m11, "M12": build_m12})
RUNNERS.update({"M10": run_m10, "M11": run_m11, "M12": run_m12})



# ---------------------------------------------------------------------------
# M13 2-high stone bank: place a water step, then climb ashore
# ---------------------------------------------------------------------------


def build_m13() -> None:
    x0, z = _x0() + 2, _z0() + 70
    cmds = [
        f"fill {x0 - 2} {Y - 3} {z - 2} {x0 + 48} {Y - 3} {z + 22} minecraft:stone",
        f"fill {x0 - 2} {Y} {z - 2} {x0 + 48} {Y + 6} {z + 22} minecraft:air",
        f"fill {x0 + 4} {Y - 18} {z + 2} {x0 + 43} {Y - 1} {z + 19} minecraft:water",
        f"fill {x0} {Y - 1} {z + 2} {x0 + 3} {Y} {z + 19} minecraft:stone",
        # far bank is TWO blocks above the water surface
        f"fill {x0 + 44} {Y - 1} {z + 2} {x0 + 47} {Y} {z + 19} minecraft:stone",
    ]
    common.rcon_batch(cmds)


def run_m13(bot: Any, nav: TerrainNavigator) -> dict[str, Any]:
    x0, z = _x0() + 2, _z0() + 70
    _tp(x0 + 2.5, Y + 1, z + 10.0)
    common.prepare_player({"oak_boat": 1, "cobblestone": 8, "cooked_beef": 8})
    time.sleep(0.6)
    hp0 = _health(bot)
    res = nav.cross_water_with_boat(x0 + 46.0, z + 10.0)
    st = _status(bot)
    p = st.get("pos") or [0, 0, 0]
    return {
        "crossing": res,
        "hp_before": hp0,
        "hp_after": st.get("health"),
        "pos": st.get("pos"),
        "ok": bool(res.get("ok")) and float(p[0]) >= x0 + 44 and not st.get("in_water"),
    }


BUILDERS.update({"M13": build_m13})
RUNNERS.update({"M13": run_m13})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    ap.add_argument("--report", default="")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()
    selected = list(BUILDERS) if args.only == "all" else [x.strip().upper() for x in args.only.split(",") if x.strip()]
    bad = [x for x in selected if x not in BUILDERS]
    if bad:
        raise SystemExit(f"unknown levels: {bad}")

    s = common.Session()
    report: dict[str, Any] = {"bot": BOT, "levels": selected, "checks": {}, "ok": True}
    try:
        report["connect"] = common.status_brief(s.connect())
        report["arena_reset"] = common.arena_reset()[-4:]
        nav = TerrainNavigator(s.bot, skills=s.skills)
        for key in selected:
            print(f"=== {key} build ===", flush=True)
            try:
                if not args.skip_build:
                    BUILDERS[key]()
                print(f"=== {key} run ===", flush=True)
                res = RUNNERS[key](s.bot, nav)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "exception": f"{type(exc).__name__}: {exc}"}
            report["checks"][key] = res
            if not res.get("ok"):
                report["ok"] = False
            print(json.dumps({"level": key, "ok": res.get("ok"), "detail": {k: v for k, v in res.items() if k in ("reached", "elapsed_s", "damage", "error", "exception", "pos", "hp_after")}}, ensure_ascii=False), flush=True)
            # keep the arena tidy between levels
            common.rcon_batch([f"effect give {BOT} minecraft:instant_health 1 10 true", f"clear {BOT}"])
        report["final_status"] = common.status_brief(s.bot.status())
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            report["cleanup"] = common.arena_cleanup()[-3:]
        except Exception as exc:  # noqa: BLE001
            report["cleanup_error"] = str(exc)
        s.close()

    out = pathlib.Path(args.report) if args.report else ROOT / "runs" / f"l2-move-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(out), "levels": {k: v.get("ok") for k, v in report["checks"].items()}}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
