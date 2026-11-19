#!/usr/bin/env python3
"""D1 wave watcher: notify when ANY wave (rNN_*) settles, including future ones.

Why this exists: earlier watchers were pinned to one wave name (r17_22bot), so a
new wave launched as `r19_short` was never watched. This one scans the whole
`runs/` directory every tick, so new/renamed waves are picked up automatically.

Settle rule (robust to different fleet sizes and crashed waves):
  - summaries >= 1 and no event-file write for --idle seconds   -> settled
  - summaries == 0 and no event write for --idle*1.5 seconds    -> settled (crashed)

Usage (background):
  python scripts/d1_watch_waves.py --exclude r18_22bot --idle 600 --max-hours 3
It prints any newly settled wave and exits 0 so the caller gets notified.
Waves already settled at startup are marked seen and do NOT trigger an exit.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import time

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")


def wave_state(d: pathlib.Path) -> dict:
    summaries = len(list(d.glob("*/wild-endurance-*.json")))
    bots = len([p for p in d.iterdir() if p.is_dir()])
    files = list(d.glob("*/*.events.jsonl"))
    newest = max((f.stat().st_mtime for f in files), default=d.stat().st_mtime)
    tracebacks = 0
    for log in d.glob("*/stdout.log"):
        try:
            if "Traceback" in log.read_text(errors="ignore"):
                tracebacks += 1
        except OSError:
            pass
    return {
        "wave": d.name,
        "summaries": summaries,
        "bots": bots,
        "idle": max(0.0, time.time() - newest),
        "tracebacks": tracebacks,
    }


def is_settled(st: dict, idle: float) -> bool:
    if st["summaries"] >= 1:
        return st["idle"] >= idle
    return st["idle"] >= idle * 1.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--idle", type=float, default=600.0)
    ap.add_argument("--max-hours", type=float, default=3.0)
    args = ap.parse_args()

    deadline = time.monotonic() + args.max_hours * 3600
    seen: set[str] = set()
    # Pre-seed waves that are already settled so we only report new settles.
    for d in sorted(WT.joinpath("runs").iterdir()):
        if not d.is_dir() or d.name in args.exclude:
            continue
        st = wave_state(d)
        if st["idle"] >= args.idle * 1.5:
            seen.add(d.name)

    last_beat = 0.0
    print(f"[watcher] start {time.strftime('%T')} exclude={args.exclude} preseen={sorted(seen)}", flush=True)
    while time.monotonic() < deadline:
        for d in sorted(WT.joinpath("runs").iterdir()):
            if not d.is_dir() or d.name in args.exclude or d.name in seen:
                continue
            st = wave_state(d)
            if is_settled(st, args.idle):
                print(
                    f"WAVE SETTLED {st['wave']} summaries={st['summaries']}/{st['bots']} "
                    f"idle={st['idle']:.0f}s tracebacks={st['tracebacks']} at {time.strftime('%T')}",
                    flush=True,
                )
                return 0
        now = time.monotonic()
        if now - last_beat >= 300:
            last_beat = now
            live = [d.name for d in WT.joinpath("runs").iterdir() if d.is_dir() and d.name not in seen]
            print(f"[watcher] {time.strftime('%T')} alive, watching {live}", flush=True)
        time.sleep(15)
    print(f"[watcher] timeout after {args.max_hours}h at {time.strftime('%T')}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
