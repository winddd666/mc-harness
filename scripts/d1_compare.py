#!/usr/bin/env python3
"""D1 before/after verification: compare two waves (e.g. r11_22bot vs r12_22bot).

Prints only the deltas that matter for the filed tickets:
  - tool failure code: count, bots affected
  - tool single-call duration: median / p90 / max (start->finish delta)
  - model cancelled count and cancel cooldown seconds
  - episode status counts + objective_verified

Usage:
  python scripts/d1_compare.py r11_22bot r12_22bot [--keys mc_skill_build_portal:unknown]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import pathlib
import statistics

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")


def L(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw or {}


def analyze(wave: str) -> dict:
    root = WT / "runs" / wave
    err_cnt: collections.Counter[str] = collections.Counter()
    err_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    preempt: collections.Counter[str] = collections.Counter()
    dur: collections.defaultdict[str, list[float]] = collections.defaultdict(list)
    model_cancel = 0
    cancel_wait = 0.0
    ep_status: collections.Counter[str] = collections.Counter()
    ep_verified: collections.Counter[bool] = collections.Counter()
    scores: dict[str, int] = {}
    deaths = collections.Counter()

    for bd in sorted(p for p in root.iterdir() if p.is_dir()):
        bot = bd.name
        sums = sorted(bd.glob("wild-endurance-*.json"))
        if sums:
            d = json.loads(sums[-1].read_text())
            cs = d.get("curriculum_summary") or {}
            scores[bot] = int(cs.get("final_score") or 0)
            for ep in cs.get("episodes") or []:
                ep_status[str(ep.get("status"))] += 1
                ep_verified[bool(ep.get("objective_verified"))] += 1
        starts: dict[str, tuple[str, float]] = {}
        for evf in sorted(bd.glob("*.events.jsonl")):
            for ln in evf.read_text(errors="ignore").splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "tool.started":
                    fn = ev.get("function") or {}
                    starts[str(ev.get("call_id"))] = (
                        str(fn.get("name") or "?"), float(ev.get("elapsed") or 0))
                elif t == "tool.completed":
                    st = starts.pop(str(ev.get("call_id")), None)
                    if not st:
                        continue
                    tool, t0 = st
                    dur[tool].append(max(0.0, float(ev.get("elapsed") or 0) - t0))
                    o = L(ev.get("result"))
                    if o.get("ok") is False:
                        err = o.get("error")
                        code = err.get("code") if isinstance(err, dict) else err
                        msg = o.get("message") or (err or {}).get("message") if isinstance(err, dict) else o.get("message")
                        if code is None and "小脑本能抢占接管" in str(msg or ""):
                            preempt[tool] += 1
                        else:
                            key = f"{tool}:{code or 'unknown'}"
                            err_cnt[key] += 1
                            err_bots[key].add(bot)
                elif t == "model.failed" and ev.get("cancelled"):
                    model_cancel += 1
                elif t == "model.cancel_cooldown_wait":
                    cancel_wait += float(ev.get("wait_s") or 0)
                elif t == "guardian.incident" and "death_or_connection_changed" in (ev.get("reasons") or []):
                    deaths[bot] += 1
    return {
        "err_cnt": err_cnt, "err_bots": err_bots, "preempt": preempt, "dur": dur,
        "model_cancel": model_cancel, "cancel_wait": cancel_wait,
        "ep_status": ep_status, "ep_verified": ep_verified, "scores": scores, "deaths": deaths,
    }


def dstats(vals: list[float]) -> tuple[int, float, float, float]:
    if not vals:
        return 0, 0.0, 0.0, 0.0
    v = sorted(vals)
    return len(v), statistics.median(v), v[min(len(v) - 1, int(0.9 * len(v)))], v[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--keys", nargs="*", default=[])
    args = ap.parse_args()

    a = analyze(args.before)
    b = analyze(args.after)
    print(f"\n===== D1 COMPARE {args.before} -> {args.after} =====")

    print("\n-- failure keys (count / bots) --")
    keys = sorted(set(a["err_cnt"]) | set(b["err_cnt"]),
                  key=lambda k: -(len(a["err_bots"][k]) + len(b["err_bots"][k])))
    for k in keys[:25]:
        ca, cb = a["err_cnt"][k], b["err_cnt"][k]
        ba, bb = len(a["err_bots"][k]), len(b["err_bots"][k])
        flag = "  " if cb == 0 else ("<<<" if cb < ca else (">>>" if cb > ca else " = "))
        print(f"  {flag} {k:52s} {ca:4d}/{ba:2d} -> {cb:4d}/{bb:2d}")

    print("\n-- tool duration med/p90/max --")
    for tool in sorted(set(a["dur"]) | set(b["dur"]),
                       key=lambda t: -max(a["dur"][t] or [0])):
        na, ma, pa, xa = dstats(a["dur"][tool])
        nb, mb, pb, xb = dstats(b["dur"][tool])
        if not na and not nb:
            continue
        print(f"  {tool:26s} n {na:4d}->{nb:<4d} med {ma:6.1f}->{mb:6.1f} "
              f"p90 {pa:6.1f}->{pb:6.1f} max {xa:6.1f}->{xb:6.1f}")

    print("\n-- reflex preemptions --")
    for tool in sorted(set(a["preempt"]) | set(b["preempt"])):
        print(f"  {tool:26s} {a['preempt'][tool]:4d} -> {b['preempt'][tool]:4d}")

    print("\n-- model cancel / cooldown --")
    print(f"  cancelled generations : {a['model_cancel']:4d} -> {b['model_cancel']:4d}")
    print(f"  cancel cooldown total : {a['cancel_wait']:7.1f}s -> {b['cancel_wait']:7.1f}s")

    print("\n-- episodes / score --")
    print(f"  episode status  : {dict(a['ep_status'])} -> {dict(b['ep_status'])}")
    print(f"  objective_verified: {dict(a['ep_verified'])} -> {dict(b['ep_verified'])}")
    print(f"  score sum       : {sum(a['scores'].values())} -> {sum(b['scores'].values())} "
          f"({len(a['scores'])} vs {len(b['scores'])} bots reported)")
    print(f"  deaths total    : {sum(a['deaths'].values())} -> {sum(b['deaths'].values())}")

    if args.keys:
        print("\n-- ticket spot-check --")
        for k in args.keys:
            print(f"  {k:52s} {a['err_cnt'][k]:4d} -> {b['err_cnt'][k]:4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
