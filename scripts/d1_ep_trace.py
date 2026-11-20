#!/usr/bin/env python3
"""Print a compact timeline for one episode (wave/bot/ep).

Usage:
  python scripts/d1_ep_trace.py --wave overnight_12bot --bot glm_glm_3_village_trade --ep 12 [--tail 40]
  python scripts/d1_ep_trace.py --wave ... --bot ... --ep ... --all   # full timeline
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/runs")


def L(x):
    try:
        return json.loads(x) if isinstance(x, str) else (x or {})
    except Exception:
        return {}


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

    cf = glob.glob(str(WT / args.wave / args.bot / "curriculum-*.json"))
    if not cf:
        print("no curriculum")
        return 1
    d = json.load(open(cf[0]))
    ep = next((e for e in d.get("episodes") or [] if e.get("episode") == args.ep), None)
    if not ep:
        print("episode not found")
        return 1
    print(f"== {args.wave}/{args.bot} EP{args.ep} target={ep.get('target')} status={ep.get('status')} "
          f"req={ep.get('actor_requests')} delta={ep.get('score_delta')}")
    try:
        r = json.load(open(ep["report_file"]))
    except Exception:
        print("no report")
        return 1
    print(f"   error={str(r.get('error'))[:150]}  gate={r.get('gate_reason')}")
    evs = r.get("events") or []
    started = {}
    lines = []
    for e in evs:
        t = e.get("type")
        if t == "tool.started":
            fn = e.get("function") or {}
            started[str(e.get("call_id"))] = (str(fn.get("name") or ""), fn.get("arguments"))
        elif t == "tool.completed":
            nm, a = started.get(str(e.get("call_id")), ("?", None))
            o = L(e.get("result"))
            lines.append(f"[{float(e.get('elapsed') or 0):7.1f}] TOOL {nm} args={str(a)[:70]} -> {fmt_result(o)}")
        elif t == "mc.notable_events":
            txt = str(e.get("text") or "")
            if txt:
                lines.append(f"[{float(e.get('elapsed') or 0):7.1f}] NOTE {txt[:150]}")
        elif t == "guardian.incident":
            lines.append(f"[{float(e.get('elapsed') or 0):7.1f}] GUARD {e.get('reasons')} actions={e.get('actions')}")
        elif t == "run.finished":
            lines.append(f"[{float(e.get('elapsed') or 0):7.1f}] END {e.get('status')} {str(e.get('error') or '')[:110]}")
    out = lines if args.all else lines[-args.tail:]
    for ln in out:
        print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
