#!/usr/bin/env python3
"""Watch fleet artifacts and print progress without contacting any services.

Modes:
  - Watches waves whose directory name starts with any --prefix.
  - If a watched wave settles -> print SETTLED + snapshot, exit 0.
  - Otherwise after --every-min minutes -> print SPOTCHECK snapshot, exit 0.
Exit is deliberate: the caller (agent) is notified and re-arms, giving the
requested "completion event or 45-minute spot check" cadence without polling.
With --follow --since, discover every wave with recent fleet artifacts, announce
existing and new waves, and keep watching across notices. A fresh Fleet report
can also settle missing summaries accounted for as process crashes.

Usage:
  python scripts/d1_overnight_watch.py --every-min 45 \
      --prefix overnight_12bot --prefix hy4_audit_ --prefix r24_long
"""
from __future__ import annotations

import argparse
import collections
from datetime import datetime
import json
import pathlib
import re
import time

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")
HTTP_FAILURE = re.compile(r"\bHTTP(?:\s+(?:status|error))?[\s:]+(429|502)\b|\[(429|502)\]", re.I)


def snapshot(wave: pathlib.Path, *, final_only: bool = False) -> dict:
    bots = [p for p in wave.iterdir() if p.is_dir()]
    summaries = 0
    files = list(wave.glob("*/*.events.jsonl"))
    newest = max((f.stat().st_mtime for f in files), default=wave.stat().st_mtime)
    episodes = collections.Counter()
    for bot in bots:
        finals = sorted(bot.glob("wild-endurance-*.json"))
        candidates = finals or ([] if final_only else sorted(bot.glob("curriculum-*.json")))
        if not candidates:
            continue
        try:
            d = json.loads(candidates[-1].read_text())
        except (OSError, ValueError):
            continue
        if finals:
            d = d.get("curriculum_summary") or {}
            summaries += bool(d)
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
                    match = HTTP_FAILURE.search(str(ev.get("error") or ""))
                    status = next((item for item in match.groups() if item), None) if match else None
                    if status == "429":
                        failed429 += 1
                    elif status == "502":
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


def fp(s: dict) -> tuple:
    """Stable fingerprint of a wave snapshot (excludes volatile `idle`)."""
    return (
        s["wave"],
        s["summaries"],
        s["bots"],
        tuple(sorted((s["episodes"] or {}).items())),
        s["retries"],
        s["failed429"],
        s["failed502"],
        s["tracebacks"],
    )


def line(s: dict) -> str:
    return (
        f"{s['wave']}: summaries={s['summaries']}/{s['bots']} idle={s['idle']:.0f}s "
        f"episodes={s['episodes']} retries={s['retries']} 429={s['failed429']} "
        f"502={s['failed502']} tracebacks={s['tracebacks']}"
    )


def fleet_metadata(wave: pathlib.Path) -> tuple[int, int, float]:
    """Read file metadata only; event contents are read on notices."""
    bots = [path for path in wave.iterdir() if path.is_dir()]
    finals = 0
    newest = 0.0
    for bot in bots:
        has_final = False
        for pattern in ("wild-endurance-*.json", "curriculum-*.json", "*.events.jsonl", "stdout.log"):
            for path in bot.glob(pattern):
                try:
                    newest = max(newest, path.stat().st_mtime)
                except FileNotFoundError:
                    continue
                has_final |= pattern == "wild-endurance-*.json"
        finals += has_final
    return len(bots), finals, newest


def fleet_report_crashes(wave: pathlib.Path, bots: int, finals: int, newest: float) -> int:
    """Trust a Fleet report only when it covers the current slots and artifacts."""
    report = wave / f"triage-{wave.name}.md"
    try:
        if report.stat().st_mtime < newest:
            return 0
        with report.open(encoding="utf-8") as handle:
            if handle.readline().rstrip() != f"# FLEET TRIAGE — `{wave.name}`":
                return 0
            handle.readline()
            match = re.search(r"bot 目录 `(\d+)` · completed `\d+` · crash `(\d+)`", handle.readline())
    except (OSError, ValueError):
        return 0
    if not match:
        return 0
    listed_bots, crashes = map(int, match.groups())
    return crashes if listed_bots == bots and finals + crashes == bots else 0


def follow(root: pathlib.Path, prefixes: list[str], since: float, every_min: float, idle: float) -> None:
    discovered: set[str] = set()
    settled: dict[str, tuple[int, int]] = {}
    stalled: set[tuple[str, float]] = set()
    deadline = time.monotonic() + every_min * 60
    print(f"[overnight] follow prefixes={prefixes or 'all fleet waves'} every={every_min}m", flush=True)
    while True:
        spotcheck = time.monotonic() >= deadline
        if spotcheck:
            print(f"SPOTCHECK {time.strftime('%T')}", flush=True)
            deadline = time.monotonic() + every_min * 60
        for wave in sorted(root.iterdir()):
            if not wave.is_dir() or (prefixes and not wave.name.startswith(tuple(prefixes))):
                continue
            directory_changed = wave.stat().st_mtime_ns
            if settled.get(wave.name) == (directory_changed, 0) and not spotcheck:
                continue
            bots, finals, newest = fleet_metadata(wave)
            if not bots or not newest or newest < since:
                continue
            if wave.name not in discovered:
                discovered.add(wave.name)
                print(f"WAVE DISCOVERED {wave.name}: summaries={finals}/{bots}", flush=True)
            crashes = fleet_report_crashes(wave, bots, finals, newest) if finals < bots else 0
            terminal = finals + crashes == bots
            if wave.name in settled and (not terminal or settled[wave.name][1] != crashes):
                del settled[wave.name]
                print(f"WAVE REOPENED {wave.name}: summaries={finals}/{bots}", flush=True)
            if terminal and wave.name not in settled:
                state = snapshot(wave, final_only=True)
                if state["summaries"] + crashes == bots:
                    settled[wave.name] = (directory_changed, crashes)
                    outcome = f" WITH CRASHES ({crashes})" if crashes else ""
                    print(f"WAVE SETTLED{outcome} {line(state)} at {time.strftime('%T')}", flush=True)
                    continue
            if wave.name in settled:
                settled[wave.name] = (directory_changed, crashes)
            if not terminal and time.time() - newest >= idle and (wave.name, newest) not in stalled:
                stalled.add((wave.name, newest))
                print(f"WAVE STALLED {wave.name}: summaries={finals}/{bots}", flush=True)
            if spotcheck:
                print(f"  {wave.name}: summaries={finals}/{bots}", flush=True)
        time.sleep(15)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every-min", type=float, default=45.0)
    ap.add_argument("--prefix", action="append", default=[])
    ap.add_argument("--idle", type=float, default=600.0)
    ap.add_argument("--follow", action="store_true")
    ap.add_argument(
        "--quiet-idle",
        action="store_true",
        help="keep waiting (up to --max-hours) while every spotcheck is identical; avoids idle spam",
    )
    ap.add_argument("--max-hours", type=float, default=4.0)
    ap.add_argument("--since", help="ISO-8601 cutoff with timezone, applied to fleet artifact mtime")
    ap.add_argument("--runs-root", type=pathlib.Path, default=WT / "runs")
    args = ap.parse_args()
    if args.follow:
        cutoff = 0.0
        if args.since:
            try:
                value = datetime.fromisoformat(args.since)
                if value.tzinfo is None:
                    raise ValueError("timezone required")
                cutoff = value.timestamp()
            except ValueError:
                ap.error("--since must include an ISO-8601 date, time and timezone")
        follow(args.runs_root, args.prefix, cutoff, args.every_min, args.idle)
        return 0
    if args.since:
        ap.error("--since requires --follow")
    prefixes = args.prefix or ["overnight_12bot", "hy4_audit_", "r24_long"]

    def waves() -> list[pathlib.Path]:
        out = []
        for p in args.runs_root.iterdir():
            if p.is_dir() and any(p.name.startswith(x) for x in prefixes):
                out.append(p)
        return sorted(out)

    # Pre-seen: a wave that is already settled must not trigger an immediate exit
    # (hy4_audit_* finishes in ~20 min, often before the watcher starts).
    seen: dict[str, float] = {}
    for w in waves():
        s = snapshot(w)
        if s["summaries"] >= max(1, s["bots"]):
            newest = max(
                (f.stat().st_mtime for f in w.glob("*/*.events.jsonl")), default=0.0
            )
            seen[w.name] = newest

    deadline = time.monotonic() + args.every_min * 60.0
    hard_deadline = time.monotonic() + max(args.max_hours, args.every_min / 60.0) * 3600.0
    last_fps: list[tuple] | None = [fp(snapshot(w)) for w in waves()]
    print(f"[overnight] start {time.strftime('%T')} prefixes={prefixes} every={args.every_min}m", flush=True)
    while True:
        now = time.monotonic()
        if now >= deadline:
            snaps = [snapshot(w) for w in waves()]
            fps = [fp(s) for s in snaps]
            if args.quiet_idle and fps == last_fps and now < hard_deadline:
                deadline = now + args.every_min * 60.0
                continue
            print(f"SPOTCHECK {time.strftime('%T')}", flush=True)
            for s in snaps:
                print("  " + line(s), flush=True)
            return 0
        for w in waves():
            s = snapshot(w)
            settled = (s["summaries"] >= max(1, s["bots"])) or (
                s["summaries"] >= 1 and s["idle"] >= args.idle
            )
            newest = max(
                (f.stat().st_mtime for f in w.glob("*/*.events.jsonl")), default=0.0
            )
            if settled and (w.name not in seen or newest > seen[w.name] + 120):
                print(f"WAVE SETTLED {line(s)} at {time.strftime('%T')}", flush=True)
                return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
