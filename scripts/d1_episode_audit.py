#!/usr/bin/env python3
"""Per-episode stall audit for overnight waves.

For every episode of every bot in the given waves, extract:
  bot, ep, target, status, requests, delta, error, gate_reason,
  last tool + its stop_reason/error code, repeat-call stats, deaths.

Outputs a JSONL dump plus per-bot / per-signature summaries.

Usage:
  python scripts/d1_episode_audit.py --wave overnight_12bot --wave overnight_12bot_part2 \
      --json /tmp/d1_eps.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

if __package__:
    from .fleet_lifecycle import LifecycleCounter
else:
    from fleet_lifecycle import LifecycleCounter

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/runs")


def L(x):
    try:
        return json.loads(x) if isinstance(x, str) else (x or {})
    except Exception:
        return {}


def read_report(report_file: pathlib.Path) -> dict:
    try:
        return json.loads(report_file.read_text())
    except (OSError, ValueError):
        return {}


def sig(err: str) -> str:
    e = (err or "").strip()
    if not e:
        return "(none)"
    for marker, name in (
        ("429", "http_429"),
        ("ReadTimeout", "mc_api_readtimeout"),
        ("not_connected", "not_connected"),
        ("not_loaded", "chunk_not_loaded"),
        ("502", "http_502"),
        ("max_tokens", "max_tokens_trunc"),
        ("模型响应未正常完成", "finish_reason_bad"),
        ("运行总时限已到", "episode_timeout"),
        ("动作锁", "action_lock_busy"),
        ("void_geofence_blocked", "void_geofence"),
    ):
        if marker in e:
            return name
    return e[:48]


def audit_episode(
    wave: str, bot: str, ep: dict, seen_ids: set[tuple[str, str]] | None = None
) -> dict:
    report_name = pathlib.Path(ep.get("report_file") or "").name
    report_path = WT / wave / bot / report_name
    report = read_report(report_path) if report_name else {}
    err = str(report.get("error") or "")
    gate = str(report.get("gate_reason") or "")
    raw_path = report_path.with_suffix(".events.jsonl") if report_name else None
    evs = report.get("events") or []
    if raw_path and raw_path.is_file():
        with raw_path.open(encoding="utf-8") as stream:
            evs = [event for line in stream if isinstance(event := L(line), dict) and event]

    calls: list[tuple[str, str, str]] = []  # (tool, stop_reason, err_code)
    started: dict[str, str] = {}
    lifecycle = LifecycleCounter(bot, seen_ids if seen_ids is not None else set())
    geofence_blocks = 0
    for e in evs:
        lifecycle.observe(e)
        t = e.get("type")
        if t == "tool.started":
            fn = e.get("function") or {}
            started[str(e.get("call_id"))] = str(fn.get("name") or "")
        elif t == "tool.completed":
            nm = started.get(str(e.get("call_id")), "?")
            o = L(e.get("result"))
            er = o.get("error")
            code = str(er.get("code") or "") if isinstance(er, dict) else str(er or "")
            if code == "void_geofence_blocked":
                geofence_blocks += 1
            calls.append((nm, str(o.get("stop_reason") or ""), code))

    last = calls[-1] if calls else ("", "", "")
    # longest run of identical (tool, stop_reason, code)
    longest, run, prev = 0, 0, None
    for c in calls:
        if c == prev:
            run += 1
        else:
            run, prev = 1, c
        longest = max(longest, run)

    return {
        "wave": wave,
        "bot": bot,
        "ep": ep.get("episode"),
        "target": ep.get("target"),
        "status": ep.get("status"),
        "requests": ep.get("actor_requests"),
        "delta": ep.get("score_delta"),
        "verified": ep.get("objective_verified"),
        "succeeded": bool(ep.get("objective_verified") or ep.get("status") == "goal_satisfied"),
        "event_file": str(raw_path) if raw_path and raw_path.is_file() else "",
        "error_sig": sig(err),
        "gate": gate,
        "last_tool": last[0],
        "last_stop": last[1],
        "last_code": last[2],
        "tool_calls": len(calls),
        "longest_repeat": longest,
        "geofence_blocks": geofence_blocks,
        "deaths": lifecycle.deaths,
        "death_receipts": lifecycle.death_receipts,
        "respawns": lifecycle.respawns,
        "connection_changes": lifecycle.connection_changes,
        "mixed_guardian_incidents": lifecycle.mixed_guardian_incidents,
        "error_raw": err[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", action="append", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rows: list[dict] = []
    final_bots: collections.Counter[str] = collections.Counter()
    wave_bots: collections.Counter[str] = collections.Counter()
    for wave in args.wave:
        seen_ids: set[tuple[str, str]] = set()
        for bot_dir in sorted(p for p in (WT / wave).glob("*/") if p.is_dir()):
            wave_bots[wave] += 1
            final = sorted(bot_dir.glob("wild-endurance-*.json"))
            if final:
                summary = read_report(final[-1]).get("curriculum_summary") or {}
                final_bots[wave] += bool(summary)
            else:
                snapshots = sorted(bot_dir.glob("curriculum-*.json"))
                summary = read_report(snapshots[-1]) if snapshots else {}
            for ep in summary.get("episodes") or []:
                rows.append(audit_episode(wave, bot_dir.name, ep, seen_ids))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote {len(rows)} rows -> {args.json}")

    # per-wave status table
    for wave in args.wave:
        wr = [r for r in rows if r["wave"] == wave]
        if not wr:
            continue
        st = collections.Counter(r["status"] for r in wr)
        provisional = " PROVISIONAL" if final_bots[wave] < wave_bots[wave] else ""
        print(f"\n===== {wave}{provisional}  episodes={len(wr)}  "
              f"verified={sum(r['succeeded'] for r in wr)}  {dict(st)}")
        sigs = collections.Counter(
            (r["status"], r["error_sig"], r["gate"]) for r in wr if not r["succeeded"]
        )
        for (s, e, g), n in sigs.most_common(14):
            print(f"  {n:4d}  {s:16s} sig={e:22s} gate={g}")

    # per-bot table
    print("\n===== per bot")
    for wave in args.wave:
        wr = [r for r in rows if r["wave"] == wave]
        bots = sorted({r["bot"] for r in wr})
        for b in bots:
            br = [r for r in wr if r["bot"] == b]
            st = collections.Counter(r["status"] for r in br)
            print(
                f"  {wave[:22]:22s} {b:28s} eps={len(br):3d} verified={sum(r['succeeded'] for r in br):3d} "
                f"{dict(st)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
