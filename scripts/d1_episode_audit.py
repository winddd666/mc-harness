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
import glob
import json
import pathlib

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/runs")


def L(x):
    try:
        return json.loads(x) if isinstance(x, str) else (x or {})
    except Exception:
        return {}


def episode_events(report_file: str) -> list[dict]:
    try:
        r = json.load(open(report_file))
    except Exception:
        return []
    evs = r.get("events")
    if isinstance(evs, list):
        return evs
    return []


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


def audit_episode(wave: str, bot: str, ep: dict) -> dict:
    report = ep.get("report_file") or ""
    evs = episode_events(report)
    err = ""
    try:
        err = str(json.load(open(report)).get("error") or "")
    except Exception:
        pass
    gate = ""
    try:
        gate = str(json.load(open(report)).get("gate_reason") or "")
    except Exception:
        pass

    calls: list[tuple[str, str, str]] = []  # (tool, stop_reason, err_code)
    started: dict[str, str] = {}
    deaths = 0
    geofence_blocks = 0
    for e in evs:
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
        elif t == "mc.notable_events":
            txt = str(e.get("text") or "")
            deaths += txt.count("死亡")

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
        "error_sig": sig(err),
        "gate": gate,
        "last_tool": last[0],
        "last_stop": last[1],
        "last_code": last[2],
        "tool_calls": len(calls),
        "longest_repeat": longest,
        "geofence_blocks": geofence_blocks,
        "deaths": deaths,
        "error_raw": err[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", action="append", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rows: list[dict] = []
    for wave in args.wave:
        for cf in sorted(glob.glob(str(WT / wave / "*/curriculum-*.json"))):
            bot = pathlib.Path(cf).parent.name
            try:
                d = json.load(open(cf))
            except Exception:
                continue
            for ep in d.get("episodes") or []:
                rows.append(audit_episode(wave, bot, ep))

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
        print(f"\n===== {wave}  episodes={len(wr)}  {dict(st)}")
        sigs = collections.Counter(
            (r["status"], r["error_sig"], r["gate"]) for r in wr if r["status"] not in ("answered", "goal_satisfied")
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
            bad = sum(n for s, n in st.items() if s not in ("answered", "goal_satisfied"))
            print(
                f"  {wave[:22]:22s} {b:28s} eps={len(br):3d} bad={bad:3d} "
                f"{dict(st)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
