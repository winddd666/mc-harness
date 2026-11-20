#!/usr/bin/env python3
"""Print a compact timeline for one episode (wave/bot/ep).

Usage:
  python scripts/d1_ep_trace.py --wave overnight_12bot --bot glm_glm_3_village_trade --ep 12 [--tail 40]
  python scripts/d1_ep_trace.py --wave ... --bot ... --ep ... --all   # full timeline
"""
from __future__ import annotations

import argparse
import json
import pathlib

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/runs")


def L(x):
    try:
        value = json.loads(x) if isinstance(x, str) else x
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def fmt_result(o: dict) -> str:
    parts = []
    if "stop_reason" in o:
        parts.append(f"stop={o['stop_reason']}")
    if "steps_done" in o:
        parts.append(f"steps={o.get('steps_done')}")
    if "dug_count" in o:
        parts.append(f"dug={o.get('dug_count')}")
    if "ok" in o:
        parts.append(f"ok={o.get('ok')}")
    er = o.get("error")
    if isinstance(er, dict):
        parts.append(f"code={er.get('code')}")
        m = str(er.get("message") or "")
        if m:
            parts.append(m[:90])
    elif er:
        parts.append(str(er)[:90])
    for k in ("gained", "found", "count", "result"):
        if k in o and o[k] not in (None, [], {}, 0):
            parts.append(f"{k}={str(o[k])[:60]}")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    ap.add_argument("--bot", required=True)
    ap.add_argument("--ep", type=int, required=True)
    ap.add_argument("--tail", type=int, default=40)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    bot_dir = WT / args.wave / args.bot
    summaries = sorted(bot_dir.glob("wild-endurance-*.json"))
    curricula = sorted(bot_dir.glob("curriculum-*.json"))
    if not summaries and not curricula:
        print("no curriculum")
        return 1
    d = json.loads((summaries or curricula)[-1].read_text())
    if summaries:
        d = d.get("curriculum_summary") or {}
    ep = next((e for e in d.get("episodes") or [] if e.get("episode") == args.ep), None)
    if not ep:
        print("episode not found")
        return 1
    print(f"== {args.wave}/{args.bot} EP{args.ep} target={ep.get('target')} status={ep.get('status')} "
          f"req={ep.get('actor_requests')} delta={ep.get('score_delta')}")
    report_name = pathlib.Path(ep.get("report_file") or "").name
    if not report_name:
        print("no report path in summary")
        return 1
    report_path = bot_dir / report_name
    raw_path = report_path.with_suffix(".events.jsonl")
    r = L(report_path.read_text()) if report_path.is_file() else {}
    print(f"   error={str(r.get('error'))[:150]}  gate={r.get('gate_reason')}")
    if raw_path.is_file():
        print(f"   JSONL {raw_path}")
        with raw_path.open(encoding="utf-8") as stream:
            evs = [(line, L(raw)) for line, raw in enumerate(stream, 1)]
    else:
        print(f"   events embedded in {report_path}")
        evs = [(None, event) for event in r.get("events") or []]
    if not evs:
        print("no trace events")
        return 1
    started = {}
    guardian_starts = {}
    lines = []
    for line, e in evs:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        elapsed = float(e.get("elapsed") or 0)
        stamp = f"L{line:<5d} " if line else ""
        stamp += f"[{elapsed:7.1f}]"
        if t == "tool.started":
            fn = e.get("function") or {}
            started[str(e.get("call_id"))] = (str(fn.get("name") or ""), fn.get("arguments"), elapsed)
        elif t == "tool.completed":
            nm, a, start = started.pop(str(e.get("call_id")), ("?", None, None))
            o = L(e.get("result"))
            duration = f" dt={elapsed - start:.1f}s" if start is not None else ""
            lines.append(f"{stamp} TOOL {nm}{duration} args={str(a)[:70]} -> {fmt_result(o)}")
        elif t == "guardian.action.started":
            guardian_starts[(e.get("scope"), e.get("action"))] = elapsed
        elif t == "guardian.action.completed":
            action = e.get("action")
            start = guardian_starts.pop((e.get("scope"), action), None)
            duration = f" dt={elapsed - start:.1f}s" if start is not None else ""
            lines.append(f"{stamp} ACTION {action}{duration} -> {fmt_result(L(e.get('result')))}")
        elif t == "mc.notable_events":
            txt = str(e.get("text") or "")
            if txt:
                lines.append(f"{stamp} NOTE {txt[:150]}")
        elif t == "guardian.incident":
            lines.append(f"{stamp} GUARD {e.get('reasons')} actions={e.get('actions')}")
        elif t == "model.http_retry":
            lines.append(f"{stamp} RETRY HTTP {e.get('status_code')} attempt={e.get('attempt')} wait={e.get('wait_s')}s")
        elif t == "model.failed":
            lines.append(f"{stamp} MODEL FAIL {str(e.get('error') or '')[:110]}")
        elif t == "run.finished":
            lines.append(f"{stamp} END {e.get('status')} {str(e.get('error') or '')[:110]}")
    out = lines if args.all else lines[-args.tail:]
    for ln in out:
        print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
