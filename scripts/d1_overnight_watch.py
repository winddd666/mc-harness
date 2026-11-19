#!/usr/bin/env python3
"""Overnight low-frequency auditor: notify on wave completion OR every --every-min.

Modes:
  - Watches waves whose directory name starts with any --prefix.
  - If a watched wave settles -> print SETTLED + snapshot, exit 0.
  - Otherwise after --every-min minutes -> print SPOTCHECK snapshot, exit 0.
Exit is deliberate: the caller (agent) is notified and re-arms, giving the
requested "completion event or 45-minute spot check" cadence without polling.

Usage:
  python scripts/d1_overnight_watch.py --every-min 45 \
      --prefix overnight_12bot --prefix hy4_audit_ --prefix r24_long
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import pathlib
import time

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")


def snapshot(wave: pathlib.Path) -> dict:
    bots = [p for p in wave.iterdir() if p.is_dir()]
    summaries = len(list(wave.glob("*/wild-endurance-*.json")))
    files = list(wave.glob("*/*.events.jsonl"))
    newest = max((f.stat().st_mtime for f in files), default=wave.stat().st_mtime)
    episodes = collections.Counter()
    for cf in wave.glob("*/curriculum-*.json"):
        try:
            d = json.loads(cf.read_text())
        except Exception:
            continue
        for ep in d.get("episodes") or []:
            episodes[str(ep.get("status"))] += 1
    retries = failed429 = failed502 = 0
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for ln in text.splitlines():
            if "429" in ln or "502" in ln or "http_retry" in ln:
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                if ev.get("type") == "model.http_retry":
                    retries += 1
                elif ev.get("type") == "model.failed":
                    s = str(ev.get("error") or "")
                    if "429" in s:
                        failed429 += 1
                    elif "502" in s:
                        failed502 += 1
    tracebacks = 0
    for log in wave.glob("*/stdout.log"):
        try:
            if "Traceback" in log.read_text(errors="ignore"):
                tracebacks += 1
        except OSError:
            pass
    return {
        "wave": wave.name,
        "bots": len(bots),
        "summaries": summaries,
        "idle": max(0.0, time.time() - newest),
        "episodes": dict(episodes),
        "runtime_error": episodes.get("runtime_error", 0),
        "retries": retries,
        "failed429": failed429,
        "failed502": failed502,
        "tracebacks": tracebacks,
    }


def line(s: dict) -> str:
    return (
        f"{s['wave']}: summaries={s['summaries']}/{s['bots']} idle={s['idle']:.0f}s "
        f"episodes={s['episodes']} retries={s['retries']} 429={s['failed429']} "
        f"502={s['failed502']} tracebacks={s['tracebacks']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every-min", type=float, default=45.0)
    ap.add_argument("--prefix", action="append", default=[])
    ap.add_argument("--idle", type=float, default=600.0)
    args = ap.parse_args()
    prefixes = args.prefix or ["overnight_12bot", "hy4_audit_", "r24_long"]

    def waves() -> list[pathlib.Path]:
        out = []
        for p in WT.joinpath("runs").iterdir():
            if p.is_dir() and any(p.name.startswith(x) for x in prefixes):
                out.append(p)
        return sorted(out)

    deadline = time.monotonic() + args.every_min * 60.0
    print(f"[overnight] start {time.strftime('%T')} prefixes={prefixes} every={args.every_min}m", flush=True)
    while time.monotonic() < deadline:
        for w in waves():
            s = snapshot(w)
            settled = (s["summaries"] >= max(1, s["bots"])) or (
                s["summaries"] >= 1 and s["idle"] >= args.idle
            )
            if settled:
                print(f"WAVE SETTLED {line(s)} at {time.strftime('%T')}", flush=True)
                return 0
        time.sleep(60)
    print(f"SPOTCHECK {time.strftime('%T')}", flush=True)
    for w in waves():
        print("  " + line(snapshot(w)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
