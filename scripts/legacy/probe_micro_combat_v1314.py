#!/usr/bin/env python3
"""Live micro-action & combat strategy probe against mc-agent-api v1.3.14 on nju-beast.

Runs in an isolated sky arena (790..830, 119..125, 790..830), protects against death_loop,
reads authoritative server HP/Pos via RCON, and restores blocks, inventory, and bot pos cleanly.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any

from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.errors import ApiError

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runs/probe-micro-combat-20261116.json"
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Isolated high-altitude arena far away from main island (-158, 76, -374)
ARENA_MIN_X = 790
ARENA_MAX_X = 830
ARENA_MIN_Y = 117
ARENA_FLOOR_Y = 119
ARENA_MAX_Y = 125
ARENA_MIN_Z = 790
ARENA_MAX_Z = 830
CENTER_X = 810.5
CENTER_Y = 120.0
CENTER_Z = 810.5
ARENA_SEL = "x=786,y=115,z=786,dx=48,dy=16,dz=48"


def rcon_batch(commands: list[str], *, timeout: int = 30) -> list[dict[str, str]]:
    """Run RCON commands inside nju-beast WSL2 via existing beast_ctl._run_rcon_commands (secret-free)."""
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


def rcon_one(cmd: str) -> str:
    return rcon_batch([cmd])[0]["reply"]


def read_target_health(tag: str = "probe_target") -> float | None:
    rep = rcon_one(f"data get entity @e[tag={tag},limit=1,sort=nearest] Health")
    m = re.search(r"entity data:\s*([0-9.]+)f?", rep)
    if m:
        return float(m.group(1))
    return None


def read_target_pos(tag: str = "probe_target") -> list[float] | None:
    rep = rcon_one(f"data get entity @e[tag={tag},limit=1,sort=nearest] Pos")
    m = re.search(r"\[\s*(-?[0-9.]+)d?\s*,\s*(-?[0-9.]+)d?\s*,\s*(-?[0-9.]+)d?\s*\]", rep)
    if m:
        return [float(m.group(1)), float(m.group(2)), float(m.group(3))]
    return None


def api_call(bot: BotSession, path: str, body: dict[str, Any] | None = None, method: str = "POST") -> dict[str, Any]:
    return bot.call(path, body, method=method, allow_reauth=False)


def safe_stop(bot: BotSession) -> None:
    try:
        api_call(bot, "stop", {})
    except ApiError:
        pass
    try:
        api_call(bot, "control", {"wait": False})
    except ApiError:
        pass
    try:
        api_call(bot, "posture", {})
    except ApiError:
        pass


def reset_bot_vital(bot_name: str = "BeastBot_01", x: float = CENTER_X, y: float = CENTER_Y, z: float = CENTER_Z) -> None:
    rcon_batch([
        f"kill @e[{ARENA_SEL},type=!player]",
        f"effect clear {bot_name}",
        f"effect give {bot_name} minecraft:instant_health 1 10 true",
        f"effect give {bot_name} minecraft:saturation 1 10 true",
        f"tp {bot_name} {x} {y} {z} 0 0",
    ])


def wait_bot_settled(bot: BotSession, target_x: float, target_y: float, target_z: float, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            st = api_call(bot, "status", method="GET")
            if st.get("alive") is False:
                raise RuntimeError("Bot died during probe setup!")
            p = st.get("pos") or [0, 0, 0]
            if (
                st.get("ready")
                and st.get("on_ground")
                and abs(p[0] - target_x) < 2.5
                and abs(p[1] - target_y) < 1.5
                and abs(p[2] - target_z) < 2.5
            ):
                return st
        except ApiError as exc:
            if exc.code not in ("not_ready", "not_connected"):
                raise
        time.sleep(0.25)
    raise RuntimeError(f"Bot did not settle near ({target_x}, {target_y}, {target_z})")


def aim_angles(eye_pos: list[float], target_pos: list[float], height_offset: float = 0.85, *, eye_height: float = 1.62) -> tuple[float, float]:
    dx = target_pos[0] - eye_pos[0]
    dy = (target_pos[1] + height_offset) - (eye_pos[1] + eye_height)
    dz = target_pos[2] - eye_pos[2]
    yaw = math.atan2(-dx, -dz)
    pitch = math.atan2(dy, math.hypot(dx, dz))
    return yaw, pitch


def count_hurt_events(bot: BotSession, since_seq: int) -> tuple[int, int, float]:
    """Drain events since `since_seq`, returning (new_seq, hurt_count, total_damage)."""
    hurts = 0
    total_dmg = 0.0
    deaths = 0
    cur = since_seq
    while True:
        ev_res = bot.events(since=cur, limit=200)
        evs = ev_res.get("events") or []
        cur = int(ev_res.get("last_seq") or cur)
        for ev in evs:
            if ev.get("type") == "hurt":
                hurts += 1
                total_dmg += float(ev.get("damage") or 0.0)
            elif ev.get("type") == "death":
                deaths += 1
        if not ev_res.get("has_more"):
            break
    if deaths > 0:
        raise RuntimeError(f"Aborted: {deaths} unexpected death(s) recorded!")
    return cur, hurts, round(total_dmg, 2)


def latest_seq(bot: BotSession) -> int:
    res = bot.events(since=0, limit=500)
    cur = int(res.get("last_seq") or 0)
    while res.get("has_more"):
        res = bot.events(since=cur, limit=500)
        cur = int(res.get("last_seq") or cur)
    return cur


def setup_arena() -> None:
    rcon_batch([
        f"forceload add {ARENA_MIN_X} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Z}",
        f"fill {ARENA_MIN_X} {ARENA_MIN_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_FLOOR_Y - 1} {ARENA_MAX_Z} polished_andesite",
        f"fill {ARENA_MIN_X} {ARENA_FLOOR_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Y} {ARENA_MAX_Z} polished_andesite hollow",
        f"fill {ARENA_MIN_X + 4} {ARENA_MAX_Y} {ARENA_MIN_Z + 4} {ARENA_MAX_X - 4} {ARENA_MAX_Y} {ARENA_MAX_Z - 4} sea_lantern",
        f"kill @e[{ARENA_SEL},type=!player]",
    ])


def teardown_arena(orig_pos: list[float], orig_items: list[dict[str, Any]], bot_name: str = "BeastBot_01") -> None:
    safe_cmds: list[str] = [
        f"kill @e[{ARENA_SEL},type=!player]",
        f"clear {bot_name}",
        f"effect clear {bot_name}",
        f"effect give {bot_name} minecraft:instant_health 1 10 true",
        f"effect give {bot_name} minecraft:saturation 1 10 true",
    ]
    # Restore original plain items in batches
    agg: dict[str, int] = {}
    for row in orig_items:
        nm = row.get("name")
        cnt = int(row.get("count") or 0)
        if nm and cnt > 0:
            agg[nm] = agg.get(nm, 0) + cnt
    for nm, cnt in sorted(agg.items()):
        rem = cnt
        while rem > 0:
            give_n = min(rem, 6400)
            safe_cmds.append(f"give {bot_name} minecraft:{nm} {give_n}")
            rem -= give_n
    safe_cmds.append(f"tp {bot_name} {orig_pos[0]} {orig_pos[1]} {orig_pos[2]} 0 0")
    safe_cmds.append(f"fill {ARENA_MIN_X} {ARENA_MIN_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Y} {ARENA_MAX_Z} air")
    safe_cmds.append(f"forceload remove {ARENA_MIN_X} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Z}")
    # Execute in chunks of 12 commands to stay well within RCON packet/timeout budgets
    for i in range(0, len(safe_cmds), 12):
        rcon_batch(safe_cmds[i : i + 12], timeout=40)


# ------------------------------------------------------------------ Experiments

def exp1_realtime_baseline(bot: BotSession) -> dict[str, Any]:
    rtts = []
    for _ in range(10):
        t0 = time.perf_counter()
        st = api_call(bot, "status", method="GET")
        rtts.append((time.perf_counter() - t0) * 1000.0)
    assert "held_controls" in st

    # Posture toggle timing (v1.3.12 ~55ms + Tailscale RTT)
    rcon_batch(["clear BeastBot_01", "give BeastBot_01 minecraft:shield 1"])
    time.sleep(0.3)
    t_sh0 = time.perf_counter()
    p_on = api_call(bot, "posture", {"shield": True})
    shield_on_ms = (time.perf_counter() - t_sh0) * 1000.0
    t_sh1 = time.perf_counter()
    p_off = api_call(bot, "posture", {})
    shield_off_ms = (time.perf_counter() - t_sh1) * 1000.0

    # Real-time wait=False sprint + steering takeover + immediate release
    reset_bot_vital()
    s0 = wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
    t_ctrl0 = time.perf_counter()
    c1 = api_call(bot, "control", {"forward": True, "sprint": True, "yaw": 0.0, "pitch": 0.0, "ms": 900, "wait": False})
    ctrl_rtt_ms = (time.perf_counter() - t_ctrl0) * 1000.0
    time.sleep(0.30)
    s_mid = api_call(bot, "status", method="GET")
    moved_forward = math.hypot(s_mid["pos"][0] - s0["pos"][0], s_mid["pos"][2] - s0["pos"][2])
    # Takeover without stop
    c2 = api_call(bot, "control", {"forward": True, "left": True, "ms": 800, "wait": False})
    time.sleep(0.20)
    s_mid2 = api_call(bot, "status", method="GET")
    # Immediate empty release
    c3 = api_call(bot, "control", {"wait": False})
    time.sleep(0.15)
    s_after = api_call(bot, "status", method="GET")

    return {
        "status_rtt_ms": {
            "min": round(min(rtts), 2),
            "median": round(sorted(rtts)[len(rtts) // 2], 2),
            "max": round(max(rtts), 2),
        },
        "posture_shield_on_ms": round(shield_on_ms, 2),
        "posture_blocking_true": p_on.get("blocking"),
        "posture_shield_off_ms": round(shield_off_ms, 2),
        "posture_blocking_false": p_off.get("blocking"),
        "control_wait_false_rtt_ms": round(ctrl_rtt_ms, 2),
        "held_after_start": s_mid.get("held_controls"),
        "speed_during_sprint": s_mid.get("speed"),
        "moved_in_300ms": round(moved_forward, 2),
        "held_after_takeover": s_mid2.get("held_controls"),
        "released_keys": c3.get("released"),
        "held_after_empty_release": s_after.get("held_controls"),
        "c1_held": c1.get("held"),
        "c2_held": c2.get("held"),
    }


def _single_jump_crit_probe(
    bot: BotSession,
    weapon: str,
    delay_s: float | None,
    *,
    single_call_mode: bool = False,
    sprint_jump: bool = False,
    moving_start_dist: float | None = None,
) -> dict[str, Any]:
    reset_bot_vital()
    rcon_batch([
        "clear BeastBot_01",
        f"give BeastBot_01 minecraft:{weapon} 1",
    ])
    time.sleep(0.3)
    api_call(bot, "equip", {"item": weapon, "slot": "hand"})
    if moving_start_dist is not None:
        # Stand south of target by moving_start_dist
        rcon_one(f"tp BeastBot_01 {CENTER_X} {CENTER_Y} {CENTER_Z + moving_start_dist} 0 0")
        wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z + moving_start_dist)
        target_z = CENTER_Z
    else:
        wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        target_z = CENTER_Z - 2.3
    rcon_one(f'summon minecraft:iron_golem {CENTER_X} {CENTER_Y} {target_z} {{NoAI:1b,PersistenceRequired:1b,Tags:["probe_target"]}}')
    time.sleep(0.45)

    ents = api_call(bot, "get_entities", {"radius": 8, "types": ["iron_golem"]}).get("entities") or []
    if not ents:
        raise RuntimeError("iron_golem target not visible")
    gid = ents[0]["id"]
    hp_before = read_target_health("probe_target")
    time.sleep(1.3)  # ensure 100% weapon attack cooldown

    api_call(bot, "look", {"entity_id": gid})
    pre_hit_st = None
    if delay_s is None:
        # Standing baseline hit
        hit_res = api_call(bot, "control", {"hit": 1, "ms": 80, "stop_on": []})
    elif single_call_mode:
        # Same-call jump + hit: leftClick fires at t=0 before rising
        hit_res = api_call(bot, "control", {"jump": True, "hit": 1, "ms": 500, "stop_on": []})
    else:
        jump_payload: dict[str, Any] = {"jump": True, "ms": 550, "wait": False}
        if moving_start_dist is not None:
            jump_payload["forward"] = True
        if sprint_jump:
            jump_payload["sprint"] = True
        api_call(bot, "control", jump_payload)
        time.sleep(delay_s)
        if moving_start_dist is not None:
            api_call(bot, "look", {"entity_id": gid})
        pre_hit_st = api_call(bot, "status", method="GET")
        hit_res = api_call(bot, "control", {"hit": 1, "ms": 60, "wait": False, "stop_on": []})

    time.sleep(0.35)
    hp_after = read_target_health("probe_target")
    safe_stop(bot)
    dmg = round((hp_before or 0.0) - (hp_after or 0.0), 2)
    hits_list = hit_res.get("hits") or []
    hit_ent = hits_list[0].get("entity") if hits_list else None
    return {
        "weapon": weapon,
        "delay_s": delay_s,
        "single_call_mode": single_call_mode,
        "sprint_jump": sprint_jump,
        "moving_start_dist": moving_start_dist,
        "hp_before": hp_before,
        "hp_after": hp_after,
        "damage": dmg,
        "hit_entity": hit_ent,
        "pre_hit_on_ground": pre_hit_st.get("on_ground") if pre_hit_st else None,
        "pre_hit_vy": (pre_hit_st.get("velocity") or [None, None, None])[1] if pre_hit_st else None,
    }


def exp2_jump_crit(bot: BotSession) -> dict[str, Any]:
    # 2A baselines
    base_sword = _single_jump_crit_probe(bot, "iron_sword", None)
    base_axe = _single_jump_crit_probe(bot, "stone_axe", None)
    # 2B naive single-call jump+hit
    naive_single = _single_jump_crit_probe(bot, "iron_sword", 0.0, single_call_mode=True)
    # 2C delay sweep for two-phase wait=False crit
    sweep = []
    for d in (0.28, 0.34, 0.38, 0.42, 0.46):
        sweep.append(_single_jump_crit_probe(bot, "iron_sword", d))
    best_delays = [row["delay_s"] for row in sweep if row["damage"] >= 8.5]
    best_d = best_delays[len(best_delays) // 2] if best_delays else 0.40
    # 2C-axe with best_d
    axe_crit = _single_jump_crit_probe(bot, "stone_axe", best_d)
    # 2C-sprint negative control: sprint cancels crit!
    sprint_jump_ctrl = _single_jump_crit_probe(bot, "iron_sword", best_d, sprint_jump=True)
    # 2D moving approach jump-crit (5 repeats)
    moving_trials = [
        _single_jump_crit_probe(bot, "iron_sword", best_d, moving_start_dist=2.9)
        for _ in range(5)
    ]
    crit_hits = sum(1 for t in moving_trials if t["damage"] >= 8.5)
    return {
        "baseline_iron_sword": base_sword,
        "baseline_stone_axe": base_axe,
        "single_call_jump_hit": naive_single,
        "delay_sweep": sweep,
        "chosen_best_delay_s": best_d,
        "stone_axe_crit": axe_crit,
        "sprint_jump_cancels_crit": sprint_jump_ctrl,
        "moving_approach_trials": moving_trials,
        "moving_crit_success_rate": f"{crit_hits}/5",
    }


def exp3_knockback_and_creeper(bot: BotSession) -> dict[str, Any]:
    # 3A Knockback displacement: normal hit vs sprint hit (zombie with 0 movement speed + 0 follow range)
    kb_results = {}
    for mode in ("normal", "sprint"):
        reset_bot_vital()
        rcon_batch(["clear BeastBot_01", "give BeastBot_01 minecraft:iron_sword 1"])
        time.sleep(0.25)
        api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
        wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        rcon_one(
            f'summon minecraft:zombie {CENTER_X} {CENTER_Y} {CENTER_Z - 2.3} '
            '{Silent:1b,PersistenceRequired:1b,Tags:["probe_target"],'
            'Attributes:[{Name:"minecraft:movement_speed",Base:0.0d},{Name:"minecraft:follow_range",Base:0.0d}]}'
        )
        time.sleep(0.4)
        ents = api_call(bot, "get_entities", {"radius": 8, "types": ["zombie"]}).get("entities") or []
        zid = ents[0]["id"]
        pos_before = read_target_pos("probe_target")
        hp_before = read_target_health("probe_target")
        time.sleep(0.8)
        api_call(bot, "look", {"entity_id": zid})
        if mode == "sprint":
            api_call(bot, "control", {"forward": True, "sprint": True, "hit": 1, "ms": 200, "stop_on": []})
        else:
            api_call(bot, "control", {"hit": 1, "ms": 80, "stop_on": []})
        time.sleep(0.6)
        pos_after = read_target_pos("probe_target")
        hp_after = read_target_health("probe_target")
        kb_dist = math.hypot((pos_after[0] - pos_before[0]), (pos_after[2] - pos_before[2])) if (pos_before and pos_after) else None
        kb_results[mode] = {
            "hp_before": hp_before,
            "hp_after": hp_after,
            "damage": round((hp_before or 0) - (hp_after or 0), 2),
            "pos_before": [round(v, 2) for v in pos_before] if pos_before else None,
            "pos_after": [round(v, 2) for v in pos_after] if pos_after else None,
            "knockback_horiz_blocks": round(kb_dist, 2) if kb_dist is not None else None,
        }

    # 3B Live Creeper kite (two runs: sprint-hit kite vs attack max_hits=1 kite)
    creeper_trials = []
    for strategy in ("sprint_hit_kite", "attack_max1_kite"):
        reset_bot_vital()
        rcon_batch(["clear BeastBot_01", "give BeastBot_01 minecraft:iron_sword 1", "give BeastBot_01 minecraft:shield 1"])
        time.sleep(0.25)
        api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
        api_call(bot, "equip", {"item": "shield", "slot": "off-hand"})
        wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        rcon_one(f'summon minecraft:creeper {CENTER_X} {CENTER_Y} {CENTER_Z - 9.0} {{PersistenceRequired:1b,Tags:["probe_target"]}}')
        time.sleep(0.4)
        seq = latest_seq(bot)
        t0 = time.monotonic()
        strikes = 0
        killed = False
        exploded = False
        while time.monotonic() - t0 < 28.0:
            ents = api_call(bot, "get_entities", {"radius": 24, "types": ["creeper"]}).get("entities") or []
            if not ents:
                # Check if killed or exploded (crater check at floor)
                killed = True
                break
            c = ents[0]
            dist = float(c["dist"])
            if dist > 3.2:
                try:
                    api_call(bot, "move_to", {"pos": c["pos"], "range": 2.4, "timeout": 5000, "stop_on": ["hurt"]})
                except ApiError:
                    pass
                ents = api_call(bot, "get_entities", {"radius": 24, "types": ["creeper"]}).get("entities") or []
                if not ents:
                    killed = True
                    break
                c = ents[0]
                dist = float(c["dist"])
            if dist <= 3.4:
                if strategy == "sprint_hit_kite":
                    api_call(bot, "look", {"entity_id": c["id"]})
                    api_call(bot, "control", {"forward": True, "sprint": True, "hit": 1, "ms": 180, "stop_on": []})
                else:
                    try:
                        api_call(bot, "attack", {"entity_id": c["id"], "max_hits": 1, "timeout": 3000, "stop_on": []})
                    except ApiError:
                        pass
                strikes += 1
                st_now = api_call(bot, "status", method="GET")
                # Back away fast (sprint opposite direction of creeper)
                dx = st_now["pos"][0] - c["pos"][0]
                dz = st_now["pos"][2] - c["pos"][2]
                away_yaw = math.atan2(-dx, -dz)
                api_call(bot, "control", {"forward": True, "sprint": True, "jump": True, "yaw": away_yaw, "pitch": 0.0, "ms": 650, "wait": False})
                time.sleep(0.75)
                api_call(bot, "control", {"wait": False})
            else:
                time.sleep(0.1)
        safe_stop(bot)
        seq, hurts, dmg_taken = count_hurt_events(bot, seq)
        # Check floor integrity in case creeper exploded
        crater_rep = rcon_one(f"fill {ARENA_MIN_X} {ARENA_FLOOR_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_FLOOR_Y} {ARENA_MAX_Z} polished_andesite replace air")
        m_filled = re.search(r"Successfully filled (\d+) block", crater_rep)
        if m_filled and int(m_filled.group(1)) > 0:
            exploded = True
            killed = False
        creeper_trials.append({
            "strategy": strategy,
            "killed": killed,
            "exploded_detected": exploded,
            "strikes": strikes,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "hurt_events": hurts,
            "damage_taken": dmg_taken,
        })
    return {"knockback_compare": kb_results, "creeper_kite": creeper_trials}


def _spawn_skeleton_trial_setup(bot: BotSession, *, give_shield: bool = False, give_bow: bool = False) -> tuple[int, int]:
    reset_bot_vital()
    items = ["clear BeastBot_01", "give BeastBot_01 minecraft:iron_sword 1"]
    if give_shield:
        items.append("give BeastBot_01 minecraft:shield 1")
    if give_bow:
        items.append("give BeastBot_01 minecraft:bow 1")
        items.append("give BeastBot_01 minecraft:arrow 24")
    rcon_batch(items)
    time.sleep(0.3)
    api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
    if give_shield:
        api_call(bot, "equip", {"item": "shield", "slot": "off-hand"})
    rcon_one(f"tp BeastBot_01 {CENTER_X} {CENTER_Y} {CENTER_Z + 6.0} 0 0")
    wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z + 6.0)
    rcon_one(f'summon minecraft:skeleton {CENTER_X} {CENTER_Y} {CENTER_Z - 6.0} {{PersistenceRequired:1b,Tags:["probe_target"]}}')
    time.sleep(0.4)
    ents = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
    if not ents:
        raise RuntimeError("skeleton did not spawn visibly")
    seq = latest_seq(bot)
    return ents[0]["id"], seq


def exp4_skeleton_strategies(bot: BotSession) -> dict[str, Any]:
    out = {}

    # 4A Naive direct attack
    skel_id, seq = _spawn_skeleton_trial_setup(bot, give_shield=False)
    t0 = time.monotonic()
    try:
        atk = api_call(bot, "attack", {"entity_id": skel_id, "timeout": 20000})
    except ApiError as exc:
        atk = {"error": exc.code}
    seq, hurts, dmg = count_hurt_events(bot, seq)
    out["4A_naive_attack"] = {"elapsed_s": round(time.monotonic() - t0, 2), "killed": atk.get("killed"), "hits": atk.get("hits"), "hurt_events": hurts, "damage_taken": dmg}

    # 4B Shield rhythm rush (approach with shield up looking at skeleton -> lower shield at <=3.2m & attack)
    skel_id, seq = _spawn_skeleton_trial_setup(bot, give_shield=True)
    t0 = time.monotonic()
    killed = False
    api_call(bot, "posture", {"shield": True})
    while time.monotonic() - t0 < 22.0:
        ents = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
        if not ents:
            killed = True
            break
        sk = ents[0]
        if sk["dist"] > 3.1:
            api_call(bot, "look", {"entity_id": sk["id"]})
            api_call(bot, "control", {"forward": True, "ms": 380, "wait": False})
            time.sleep(0.28)
        else:
            api_call(bot, "control", {"wait": False})
            api_call(bot, "posture", {})  # drop shield to strike
            try:
                api_call(bot, "attack", {"entity_id": sk["id"], "max_hits": 2, "timeout": 3500, "stop_on": []})
            except ApiError:
                pass
            ents2 = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
            if not ents2:
                killed = True
                break
            api_call(bot, "posture", {"shield": True})
    safe_stop(bot)
    seq, hurts, dmg = count_hurt_events(bot, seq)
    out["4B_shield_rush"] = {"elapsed_s": round(time.monotonic() - t0, 2), "killed": killed, "hurt_events": hurts, "damage_taken": dmg}

    # 4C Shieldless 32-deg zig-zag sprint strafe rush
    skel_id, seq = _spawn_skeleton_trial_setup(bot, give_shield=False)
    t0 = time.monotonic()
    killed = False
    sign = 1
    while time.monotonic() - t0 < 22.0:
        ents = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
        if not ents:
            killed = True
            break
        sk = ents[0]
        if sk["dist"] > 3.2:
            st = api_call(bot, "status", method="GET")
            yaw_direct, _ = aim_angles(st["pos"], sk["pos"])
            strafe_yaw = yaw_direct + sign * 0.56
            sign *= -1
            api_call(bot, "control", {"forward": True, "sprint": True, "yaw": strafe_yaw, "pitch": 0.0, "ms": 420, "wait": False})
            time.sleep(0.36)
        else:
            api_call(bot, "control", {"wait": False})
            try:
                res = api_call(bot, "attack", {"entity_id": sk["id"], "max_hits": 3, "timeout": 5000, "stop_on": []})
                if res.get("killed"):
                    killed = True
                    break
            except ApiError:
                pass
    safe_stop(bot)
    seq, hurts, dmg = count_hurt_events(bot, seq)
    out["4C_zigzag_strafe_rush"] = {"elapsed_s": round(time.monotonic() - t0, 2), "killed": killed, "hurt_events": hurts, "damage_taken": dmg}

    # 4D-1 2-block LoS pillar bait (no shield, lure skeleton into corner/pillar)
    skel_id, seq = _spawn_skeleton_trial_setup(bot, give_shield=False)
    # Bot stands at (810.5, 120, 816.5), skeleton at (810.5, 120, 804.5); place 1x2 pillar directly in front at (810, 120..121, 814)
    rcon_batch([
        f"setblock {int(CENTER_X)} {int(CENTER_Y)} {int(CENTER_Z + 4)} cobblestone",
        f"setblock {int(CENTER_X)} {int(CENTER_Y + 1)} {int(CENTER_Z + 4)} cobblestone",
    ])
    t0 = time.monotonic()
    lured_dist = None
    killed = False
    while time.monotonic() - t0 < 18.0:
        ents = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
        if not ents:
            killed = True
            break
        sk = ents[0]
        lured_dist = sk["dist"]
        if sk["dist"] <= 3.6:
            try:
                res = api_call(bot, "attack", {"entity_id": sk["id"], "timeout": 8000, "stop_on": []})
                if res.get("killed"):
                    killed = True
                    break
            except ApiError:
                pass
        time.sleep(0.35)
    safe_stop(bot)
    rcon_batch([
        f"setblock {int(CENTER_X)} {int(CENTER_Y)} {int(CENTER_Z + 4)} air",
        f"setblock {int(CENTER_X)} {int(CENTER_Y + 1)} {int(CENTER_Z + 4)} air",
    ])
    seq, hurts, dmg = count_hurt_events(bot, seq)
    out["4D_pillar_los_bait"] = {"elapsed_s": round(time.monotonic() - t0, 2), "killed": killed, "min_lured_dist": lured_dist, "hurt_events": hurts, "damage_taken": dmg}

    # 4D-2 Bow /shoot pre-aim duel
    skel_id, seq = _spawn_skeleton_trial_setup(bot, give_shield=False, give_bow=True)
    t0 = time.monotonic()
    shots = 0
    hits_landed = 0
    killed = False
    while time.monotonic() - t0 < 20.0 and shots < 8:
        ents = api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []
        if not ents:
            killed = True
            break
        sk = ents[0]
        shots += 1
        try:
            sres = api_call(bot, "shoot", {"entity_id": sk["id"], "weapon": "bow", "charge_ms": 1000, "stop_on": []})
            hb = sres.get("target_health_before")
            ha = sres.get("target_health_after")
            if hb is not None and (ha is None or ha < hb):
                hits_landed += 1
            if ha is None:
                # Re-check if dead
                time.sleep(0.25)
                if not (api_call(bot, "get_entities", {"radius": 24, "types": ["skeleton"]}).get("entities") or []):
                    killed = True
                    break
        except ApiError:
            pass
    safe_stop(bot)
    seq, hurts, dmg = count_hurt_events(bot, seq)
    out["4D_bow_shoot_duel"] = {"elapsed_s": round(time.monotonic() - t0, 2), "killed": killed, "shots": shots, "hits_landed": hits_landed, "hurt_events": hurts, "damage_taken": dmg}

    return out


def exp5_tunnel_gap_slash(bot: BotSession) -> dict[str, Any]:
    results = {}
    # Build a 1x2 corridor from z=805..815 at x=810 (walls at x=809 and x=811, ceiling y=122)
    bx = int(CENTER_X)
    setup_cmds = [
        f"fill {bx - 1} 120 804 {bx - 1} 122 816 cobblestone",
        f"fill {bx + 1} 120 804 {bx + 1} 122 816 cobblestone",
        f"fill {bx} 122 804 {bx} 122 816 cobblestone",
        f"fill {bx - 1} 120 816 {bx + 1} 122 816 cobblestone",
        f"fill {bx - 1} 120 804 {bx + 1} 122 804 cobblestone",
    ]
    for gap_mode in ("foot_gap_block_head", "head_gap_block_foot"):
        reset_bot_vital(x=bx + 0.5, y=120.0, z=813.0)
        cmds = list(setup_cmds)
        if gap_mode == "foot_gap_block_head":
            cmds += [f"setblock {bx} 120 811 air", f"setblock {bx} 121 811 cobblestone"]
            aim_h = 0.35
        else:
            cmds += [f"setblock {bx} 120 811 cobblestone", f"setblock {bx} 121 811 air"]
            aim_h = 1.45
        cmds += ["clear BeastBot_01", "give BeastBot_01 minecraft:iron_sword 1"]
        rcon_batch(cmds)
        time.sleep(0.3)
        api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
        wait_bot_settled(bot, bx + 0.5, 120.0, 813.0)

        # Spawn zombie + skeleton on the outside (z=807)
        rcon_batch([
            f'summon minecraft:zombie {bx + 0.5} 120 808.0 {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:skeleton {bx + 0.5} 120 806.5 {{PersistenceRequired:1b,Tags:["probe_target"]}}',
        ])
        time.sleep(0.5)
        seq = latest_seq(bot)
        if gap_mode == "foot_gap_block_head":
            # Sneak (eye=1.27) and stand slightly back at z=812.6 so line of sight clears the top block at z=811!
            api_call(bot, "posture", {"sneak": True})
            stand_z = 812.6
            eye_h = 1.27
        else:
            api_call(bot, "posture", {})
            stand_z = 812.2
            eye_h = 1.62
        try:
            api_call(bot, "move_to", {"pos": [bx + 0.5, 120.0, stand_z], "range": 0.4, "timeout": 4000, "stop_on": []})
        except ApiError:
            pass
        t0 = time.monotonic()
        swings = 0
        hits_connected = 0
        killed_all = False
        while time.monotonic() - t0 < 18.0:
            ents = api_call(bot, "get_entities", {"radius": 10, "types": ["hostile"]}).get("entities") or []
            if not ents:
                killed_all = True
                break
            e = ents[0]
            if float(e["dist"]) <= 3.3:
                st = api_call(bot, "status", method="GET")
                yaw, pitch = aim_angles(st["pos"], e["pos"], height_offset=aim_h, eye_height=eye_h)
                hres = api_call(bot, "control", {"hit": 1, "yaw": yaw, "pitch": pitch, "ms": 80, "stop_on": []})
                swings += 1
                if (hres.get("hits") or [{}])[0].get("entity"):
                    hits_connected += 1
                time.sleep(0.60)
            else:
                time.sleep(0.25)
        safe_stop(bot)
        seq, hurts, dmg = count_hurt_events(bot, seq)
        results[gap_mode] = {
            "killed_all": killed_all,
            "swings": swings,
            "hits_connected": hits_connected,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "hurt_events": hurts,
            "damage_taken": dmg,
        }

    # Cleanup corridor walls
    rcon_batch([f"fill {bx - 1} 120 804 {bx + 1} 122 816 air"])
    return results


def exp6_pillar_cheese(bot: BotSession) -> dict[str, Any]:
    out = {}
    bx, bz = int(CENTER_X), int(CENTER_Z)
    for height in (2, 3):
        reset_bot_vital()
        rcon_batch([
            "clear BeastBot_01",
            "give BeastBot_01 minecraft:iron_sword 1",
            "give BeastBot_01 minecraft:cobblestone 16",
        ])
        time.sleep(0.3)
        api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
        wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        seq = latest_seq(bot)
        # Jump-pillar up `height` blocks using real control(jump=True, wait=False) + place
        pillar_ok = True
        for step in range(height):
            target_y = int(CENTER_Y) + step
            api_call(bot, "control", {"jump": True, "ms": 480, "wait": False})
            time.sleep(0.22)
            try:
                api_call(bot, "place", {"item": "cobblestone", "pos": [bx, target_y, bz], "stop_on": []})
            except ApiError:
                pillar_ok = False
            api_call(bot, "control", {"wait": False})
            time.sleep(0.35)
        st_top = api_call(bot, "status", method="GET")
        # Summon 3 zombies at 4.5m radius
        rcon_batch([
            f'summon minecraft:zombie {CENTER_X + 4.0} {CENTER_Y} {CENTER_Z} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:zombie {CENTER_X - 4.0} {CENTER_Y} {CENTER_Z} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:zombie {CENTER_X} {CENTER_Y} {CENTER_Z - 4.0} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
        ])
        time.sleep(0.5)
        api_call(bot, "equip", {"item": "iron_sword", "slot": "hand"})
        t0 = time.monotonic()
        swings = 0
        hits_connect = 0
        killed_all = False
        while time.monotonic() - t0 < 20.0:
            ents = api_call(bot, "get_entities", {"radius": 10, "types": ["zombie"]}).get("entities") or []
            if not ents:
                killed_all = True
                break
            e = ents[0]
            if float(e["dist"]) <= 4.3:
                st = api_call(bot, "status", method="GET")
                yaw, pitch = aim_angles(st["pos"], e["pos"], height_offset=1.55)
                hres = api_call(bot, "control", {"hit": 1, "yaw": yaw, "pitch": pitch, "ms": 70, "stop_on": []})
                swings += 1
                if (hres.get("hits") or [{}])[0].get("entity"):
                    hits_connect += 1
                time.sleep(0.60)
            else:
                time.sleep(0.2)
        safe_stop(bot)
        seq, hurts, dmg = count_hurt_events(bot, seq)
        rcon_batch([f"fill {bx} {int(CENTER_Y)} {bz} {bx} {int(CENTER_Y) + 4} {bz} air"])
        out[f"pillar_{height}_blocks"] = {
            "pillar_ok": pillar_ok,
            "top_pos": st_top.get("pos"),
            "killed_all": killed_all,
            "swings": swings,
            "hits_connected": hits_connect,
            "hurt_events": hurts,
            "damage_taken": dmg,
        }
    return out


def exp7_water_and_stop_responsiveness(bot: BotSession) -> dict[str, Any]:
    bx, bz = int(CENTER_X) + 8, int(CENTER_Z) + 8
    reset_bot_vital()
    # Build 3x3 water tank above floor: walls y=120..122, water interior y=120..121
    rcon_batch([
        f"fill {bx - 2} 120 {bz - 2} {bx + 2} 122 {bz + 2} polished_andesite hollow",
        f"fill {bx - 1} 120 {bz - 1} {bx + 1} 121 {bz + 1} water",
        f"fill {bx - 1} 122 {bz - 1} {bx + 1} 122 {bz + 1} air",
        f"tp BeastBot_01 {bx + 0.5} 120.0 {bz + 0.5} 0 0",
    ])
    time.sleep(0.8)
    s_sink = api_call(bot, "status", method="GET")
    t0 = time.perf_counter()
    p_res = api_call(bot, "posture", {"jump": True})
    posture_ms = (time.perf_counter() - t0) * 1000.0
    time.sleep(1.2)
    s_float = api_call(bot, "status", method="GET")
    api_call(bot, "posture", {})
    # Cleanup pool
    rcon_batch([f"fill {bx - 2} 120 {bz - 2} {bx + 2} 122 {bz + 2} air"])
    reset_bot_vital()
    wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)

    # Test /stop releasing lock cleanly during long move_to -> immediate control works without busy
    import threading
    move_err: dict[str, Any] = {}

    def bg_move() -> None:
        try:
            api_call(bot, "move_to", {"pos": [CENTER_X - 15.0, CENTER_Y, CENTER_Z - 15.0], "range": 0.5})
        except ApiError as exc:
            move_err["code"] = exc.code
            move_err["cause"] = exc.extra.get("cause") if hasattr(exc, "extra") and isinstance(exc.extra, dict) else None

    th = threading.Thread(target=bg_move, daemon=True)
    th.start()
    time.sleep(0.35)
    t_s0 = time.perf_counter()
    stop_res = api_call(bot, "stop", {})
    stop_rtt_ms = (time.perf_counter() - t_s0) * 1000.0
    # Immediately send control right after /stop returns
    ctrl_after_stop = api_call(bot, "control", {"hit": 1, "ms": 60, "wait": False})
    th.join(timeout=5.0)
    safe_stop(bot)

    return {
        "water_oxygen_field_when_submerged": {
            "before_in_water": s_sink.get("in_water"),
            "before_submerged": s_sink.get("submerged"),
            "before_y": s_sink["pos"][1],
            "before_oxygen": s_sink.get("oxygen"),
            "posture_jump_rtt_ms": round(posture_ms, 2),
            "posture_jumping": p_res.get("jumping"),
            "after_float_y": s_float["pos"][1],
            "after_submerged": s_float.get("submerged"),
            "after_oxygen": s_float.get("oxygen"),
        },
        "stop_unlock_check": {
            "stop_rtt_ms": round(stop_rtt_ms, 2),
            "stop_result": stop_res,
            "move_interrupt": move_err,
            "immediate_control_after_stop_ok": ("held" in ctrl_after_stop),
        },
    }


EXPERIMENTS: tuple[tuple[str, str, Any], ...] = (
    ("exp1_realtime_baseline", "v1.3.14 实时操控 RTT、posture 55ms 切盾、control(wait=false) 接管与急停", exp1_realtime_baseline),
    ("exp2_jump_crit", "双阶段 wait=false 跳劈暴击 (1.5x) 与延时扫描 / 疾跑互斥对照 / 移动助跑跳劈", exp2_jump_crit),
    ("exp3_knockback_and_creeper", "疾跑击退斩位移对比 + 活体苦力怕无伤风筝", exp3_knockback_and_creeper),
    ("exp4_skeleton_strategies", "骷髅 4 种策略对比：裸冲 / 举盾推进 / 32° Z字疾跑骗箭 / 立柱卡视野 + /shoot 对狙", exp4_skeleton_strategies),
    ("exp5_tunnel_gap_slash", "1x2 矿道半身封口修脚（封头留脚缝 sneak=true vs 封脚留头缝）", exp5_tunnel_gap_slash),
    ("exp6_pillar_cheese", "原地起跳垫高 2 格 vs 3 格无伤清群怪", exp6_pillar_cheese),
    ("exp7_water_and_stop", "水下 posture(jump=true) 浮面 + /stop 释放锁无缝衔接控制", exp7_water_and_stop_responsiveness),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="On-demand v1.3.14 micro-action & combat probe (zero LLM cost; isolated sky arena).",
    )
    parser.add_argument("cases", nargs="*", help="One or more experiment names to run (omit to list available cases).")
    parser.add_argument("--list", action="store_true", help="List available experiments and exit without connecting.")
    parser.add_argument("--all", action="store_true", help="Run all experiments sequentially.")
    args = parser.parse_args()

    by_name = {name: fn for name, _, fn in EXPERIMENTS}
    if args.list or (not args.all and not args.cases):
        print("Available v1.3.14 micro-action / combat probe cases:")
        for name, desc, _ in EXPERIMENTS:
            print(f"  - {name}: {desc}")
        print("\nUsage:")
        print("  uv run python scripts/probe_micro_combat_v1314.py exp2_jump_crit")
        print("  uv run python scripts/probe_micro_combat_v1314.py exp4_skeleton_strategies exp5_tunnel_gap_slash")
        print("  uv run python scripts/probe_micro_combat_v1314.py --all")
        return

    selected: list[tuple[str, Any]]
    if args.all:
        selected = [(name, fn) for name, _, fn in EXPERIMENTS]
    else:
        unknown = [c for c in args.cases if c not in by_name]
        if unknown:
            raise SystemExit(f"Unknown case(s): {unknown}. Use --list to see valid names.")
        selected = [(c, by_name[c]) for c in args.cases]

    with NO_PROXY.open("http://127.0.0.1:8770/api/state", timeout=8) as resp:
        console = json.load(resp)
    if console.get("running"):
        raise SystemExit("Local mc-console task is active; refusing probe.")

    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    rec = TokenStore(settings.state_dir / "tokens.json").get("BeastBot_01")
    if rec is None:
        raise SystemExit("Missing cached BeastBot_01 token.")

    with BotSession(rec.token, bot_username=rec.bot_username, base_url=settings.mc_api_base_url, timeout=45.0) as bot:
        st0 = api_call(bot, "status", method="GET")
        if not st0.get("ready"):
            raise SystemExit(f"BeastBot_01 not ready: {st0}")
        orig_pos = list(st0["pos"])
        inv0 = api_call(bot, "inventory", method="GET")
        orig_items = list(inv0.get("items") or [])
        for slot_name in ("offhand",):
            if isinstance(inv0.get(slot_name), dict) and inv0[slot_name].get("name"):
                orig_items.append({"name": inv0[slot_name]["name"], "count": int(inv0[slot_name].get("count") or 1)})
        armor0 = inv0.get("armor") or {}
        for _, aitem in armor0.items():
            if isinstance(aitem, dict) and aitem.get("name"):
                orig_items.append({"name": aitem["name"], "count": 1})

        diff_rep = rcon_one("difficulty")
        report: dict[str, Any] = {
            "difficulty": diff_rep,
            "orig_pos": orig_pos,
            "orig_item_count": sum(int(r.get("count") or 0) for r in orig_items),
            "selected": [name for name, _ in selected],
            "experiments": {},
        }
        try:
            setup_arena()
            reset_bot_vital()
            wait_bot_settled(bot, CENTER_X, CENTER_Y, CENTER_Z, timeout_s=20.0)

            for name, fn in selected:
                print(f"[probe] starting {name}...", flush=True)
                report["experiments"][name] = fn(bot)
                print(f"[probe] finished {name}", flush=True)
        finally:
            safe_stop(bot)
            teardown_arena(orig_pos, orig_items)

        st_end = wait_bot_settled(bot, orig_pos[0], orig_pos[1], orig_pos[2], timeout_s=15.0)
        inv_end = api_call(bot, "inventory", method="GET")
        report["restored_pos"] = st_end.get("pos")
        report["restored_health"] = st_end.get("health")
        report["restored_item_count"] = sum(int(r.get("count") or 0) for r in (inv_end.get("items") or []))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(REPORT_PATH), "restored_pos": report["restored_pos"], "restored_item_count": report["restored_item_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
