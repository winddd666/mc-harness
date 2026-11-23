#!/usr/bin/env python3
"""Natural-terrain soak test: 10+ minutes of `navigate_segmented` over real world chunks.

Runs waypoint loops around a real plains/forest area (no built course), records deaths,
damage, recoveries and stalls, and prints/writes a JSON summary.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import l2_pack_live_test as common  # noqa: E402
import l2_move_live_test as move  # noqa: E402
from mc_agent.errors import ApiError  # noqa: E402
from mc_agent.skills.library import MacroSkills  # noqa: E402
from mc_agent.skills.movement import TerrainNavigator  # noqa: E402

BOT = common.BOT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--radius", type=float, default=70.0)
    ap.add_argument("--biome", default="plains")
    ap.add_argument("--mode", default="circle", choices=("circle", "banks"))
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    s = common.Session()
    report: dict[str, Any] = {"bot": BOT, "minutes": args.minutes, "biome": args.biome, "stops": [], "ok": True}
    try:
        report["connect"] = common.status_brief(s.connect())
        coords = move.locate_biome(args.biome) or [5172, 150, 4884]
        st = move._drop_into_world(s.bot, coords[0], coords[2])
        common.rcon_batch(
            [
                f"effect give {BOT} minecraft:saturation 100000 4 true",
                f"effect give {BOT} minecraft:resistance 100000 4 true",
            ]
        )
        common.prepare_player({"cobblestone": 64, "oak_boat": 2, "cooked_beef": 32})
        time.sleep(0.8)
        start = list(st.get("pos") or [0, 0, 0])
        report["start"] = start

        nav = TerrainNavigator(s.bot, skills=s.skills)
        skills = MacroSkills(s.bot)
        skills._terrain_nav = nav
        rng = random.Random(int(time.time()))
        report["seed"] = rng.randrange(10_000, 99_999)

        waypoints: list[tuple[float, float]] = []
        if args.mode == "banks":
            center = move.locate_biome("river") or [4884, 150, 4884]
            center_y = int(start[1])
            dry = move._find_dry_column(s.bot, int(center[0]), center_y, int(center[2]), max_r=70, coarse=3)
            if dry is not None:
                bx, bz, _, _ = dry
                mirror = (center[0] + (center[0] - bx), center[2] + (center[2] - bz))
                dry2 = move._find_dry_column(
                    s.bot, int(mirror[0]), center_y, int(mirror[1]), max_r=40, coarse=3
                )
                if dry2 is not None:
                    waypoints = [(float(bx), float(bz)), (float(dry2[0]), float(dry2[1]))]
            report["waypoints"] = waypoints
        if not waypoints:
            # fall back to the circle schedule
            for i in range(12):
                ang = 2 * math.pi * i / 12
                waypoints.append(
                    (
                        start[0] + math.cos(ang) * args.radius,
                        start[2] + math.sin(ang) * args.radius,
                    )
                )
        wp_idx = 0

        deadline = time.monotonic() + args.minutes * 60
        reached = 0
        attempts = 0
        deaths = 0
        recoveries_total = 0
        distance = 0.0
        last_pos = start
        angle = 0.0
        while time.monotonic() < deadline:
            if args.mode == "banks":
                tx, tz = waypoints[wp_idx % len(waypoints)]
                wp_idx += 1
                if math.hypot(tx - float(last_pos[0]), tz - float(last_pos[2])) < 6.0:
                    wp_idx += 1
                    tx, tz = waypoints[wp_idx % len(waypoints)]
                    wp_idx += 1
            else:
                angle += math.radians(360.0 / 10) + rng.uniform(-0.15, 0.15)
                r = args.radius * rng.uniform(0.7, 1.15)
                tx = start[0] + math.cos(angle) * r
                tz = start[2] + math.sin(angle) * r
            attempts += 1
            t0 = time.monotonic()
            out: dict[str, Any]
            try:
                out = skills.navigate_segmented(tx, tz, range_m=2.5)
            except ApiError as err:
                out = {
                    "ok": False,
                    "error": err.code,
                    "message": err.message,
                    "recoveries": (err.context or {}).get("recoveries"),
                    "blocked_reason": (err.context or {}).get("blocked_reason"),
                }
            except Exception as err:  # noqa: BLE001 - a hung HTTP call must not kill the soak
                out = {"ok": False, "error": f"{type(err).__name__}", "message": str(err)[:160]}
            elapsed = time.monotonic() - t0
            st = s.bot.status()
            pos = list(st.get("pos") or last_pos)
            step = math.hypot(float(pos[0]) - float(last_pos[0]), float(pos[2]) - float(last_pos[2]))
            distance += step
            last_pos = pos
            recs = out.get("recoveries") or []
            recoveries_total += len(recs)
            alive = st.get("alive", True)
            if not alive or float(st.get("health") or 0) <= 0:
                deaths += 1
                common.rcon_batch([f"effect give {BOT} minecraft:instant_health 1 10 true"])
                move._drop_into_world(s.bot, start[0], start[2])
                last_pos = list((s.bot.status().get("pos") or start))
            elif out.get("ok"):
                reached += 1
            report["stops"].append(
                {
                    "target": [round(tx, 1), round(tz, 1)],
                    "ok": bool(out.get("ok")),
                    "error": out.get("error"),
                    "elapsed_s": round(elapsed, 1),
                    "moved_m": round(step, 1),
                    "recoveries": [r.get("op") for r in recs],
                    "diagnosis": [r.get("diagnosis") for r in recs],
                    "pos": [round(float(v), 1) for v in pos],
                    "health": st.get("health"),
                    "food": st.get("food"),
                }
            )
            print(
                json.dumps(
                    {
                        "attempt": attempts,
                        "ok": out.get("ok"),
                        "err": out.get("error"),
                        "moved": round(step, 1),
                        "elapsed": round(elapsed, 1),
                        "rec": [r.get("op") for r in recs],
                        "hp": st.get("health"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if time.monotonic() >= deadline:
                break
            s.skills.note_status() if hasattr(s.skills, "note_status") else None

        report["summary"] = {
            "attempts": attempts,
            "reached": reached,
            "deaths": deaths,
            "recoveries": recoveries_total,
            "distance_m": round(distance, 1),
            "minutes": round(args.minutes, 1),
        }
        report["ok"] = deaths == 0 and reached >= max(1, int(attempts * 0.6))
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            common.arena_cleanup()
            p_last = (s.bot.status().get("pos") or [0, 0, 0])
            common.rcon_batch(
                [
                    *(
                        f"execute positioned {p_last[0]:.0f} 120 {p_last[2]:.0f} run kill @e[type=minecraft:{b},distance=..120]"
                        for b in common.BOAT_TYPES
                    ),
                    f"execute positioned {p_last[0]:.0f} 120 {p_last[2]:.0f} run kill @e[type=minecraft:item,distance=..120]",
                ]
            )
        except Exception:  # noqa: BLE001
            pass
        s.close()

    out = pathlib.Path(args.report) if args.report else ROOT / "runs" / f"l2-soak-{time.strftime('%Y%m%d-%H%M%S')}.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(out), "summary": report.get("summary")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
