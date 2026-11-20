#!/usr/bin/env python3
"""D1 precise triage: cross-model P0/P1 leaderboard from r11 run logs.

Reads .worktrees/v2-integrated/runs/<wave>/<bot>/*.events.jsonl and
wild-endurance-*.json. Produces:
  - per-bot score/deaths/guardians/429
  - tool failure leaderboard keyed by (tool, error_code), ranked by bots affected
  - model.failed / transient retry leaderboard
  - slow tool calls

Usage:
  python scripts/d1_triage.py --wave r11_22bot [--format md] [--top 20]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import pathlib

if __package__:
    from .fleet_lifecycle import LifecycleCounter
else:
    from fleet_lifecycle import LifecycleCounter

WT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")


def parse_result(raw) -> tuple[bool | None, str | None, str | None]:
    """Return (ok, error_code, message) from a tool.completed result payload."""
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except Exception:
            return None, None, raw[:200]
    elif isinstance(raw, dict):
        obj = raw
    else:
        return None, None, None
    ok = obj.get("ok")
    err = obj.get("error")
    msg = obj.get("message") or obj.get("reason") or obj.get("stop_reason")
    if isinstance(err, dict):
        code = err.get("code") or err.get("error") or str(err)[:80]
        msg = err.get("message") or msg
    else:
        code = err
    return ok, (str(code) if code else None), msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    ap.add_argument("--format", choices=["md", "text"], default="text")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    wave_dir = WT / "runs" / args.wave
    bot_dirs = sorted(p for p in wave_dir.iterdir() if p.is_dir())

    err_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    err_cnt: collections.Counter[str] = collections.Counter()
    err_samples: dict[str, tuple[str, dict]] = {}
    err_models: collections.defaultdict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    err_msgs: collections.defaultdict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    # Failures caused by the 0-LLM reflex preempting an in-flight tool are NOT harness bugs.
    preempt_cnt: collections.Counter[str] = collections.Counter()
    preempt_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)

    model_fail_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    model_fail_cnt: collections.Counter[str] = collections.Counter()
    retry_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    retry_cnt: collections.Counter[str] = collections.Counter()
    slow: list[tuple[float, str, str]] = []
    tool_dur: collections.defaultdict[str, list[float]] = collections.defaultdict(list)

    rows = []
    for bd in bot_dirs:
        name = bd.name
        squad = name.split("_")[0]
        cur = {"squad": squad, "bot": name, "model": "?", "score": 0,
               "guard": 0, "c429": 0, "eps": 0, "gained": 0, "status": "running"}
        sums = sorted(bd.glob("wild-endurance-*.json"))
        if sums:
            d = json.loads(sums[-1].read_text())
            cs = d.get("curriculum_summary") or {}
            gt = d.get("guardian_totals") or {}
            cur["score"] = int(cs.get("final_score") or 0)
            cur["eps"] = int(cs.get("episodes_completed") or 0)
            cur["gained"] = len(cs.get("score_breakdown") or {})
            cur["guard"] = int(gt.get("incidents") or 0)
            cur["status"] = "done"
            for ep in cs.get("episodes") or []:
                pass

        starts: dict[str, str] = {}
        lifecycle = LifecycleCounter(name)
        for evf in sorted(bd.glob("*.events.jsonl")):
            for ln in evf.read_text(errors="ignore").splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                lifecycle.observe(ev)
                t = ev.get("type")
                if t == "run.started":
                    m = str(ev.get("model") or "")
                    if m and cur["model"] == "?":
                        cur["model"] = m.split("@")[0]
                elif t == "tool.started":
                    fn = ev.get("function") or {}
                    starts[str(ev.get("call_id"))] = (
                        str(fn.get("name") or "?"),
                        float(ev.get("elapsed") or 0),
                    )
                elif t == "tool.completed":
                    cid = str(ev.get("call_id"))
                    tool, s0 = starts.pop(cid, ("?", 0.0))
                    ok, code, msg = parse_result(ev.get("result"))
                    if ok is False:
                        key = f"{tool}:{code or 'unknown'}"
                        if code is None and "小脑本能抢占接管" in str(msg or ""):
                            preempt_cnt[tool] += 1
                            preempt_bots[tool].add(name)
                            continue
                        err_cnt[key] += 1
                        err_bots[key].add(name)
                        err_models[key][cur["model"]] += 1
                        if msg:
                            err_msgs[key][str(msg)[:220]] += 1
                        err_samples.setdefault(key, (str(evf), ev))
                    dt = float(ev.get("elapsed") or 0) - s0
                    tool_dur[tool].append(max(0.0, dt))
                    if dt >= 60.0:
                        slow.append((dt, name, tool))
                elif t == "model.failed":
                    err_s = str(ev.get("error") or ev.get("reason") or "")
                    if ev.get("cancelled") or "已取消本次模型请求" in err_s:
                        key = "model_cancelled:new_input_preempt"
                    elif "max_tokens" in err_s:
                        key = "model_truncated:finish_reason_length"
                    elif "ReadTimeout" in err_s or "ReadError" in err_s:
                        key = f"model_net:{'ReadTimeout' if 'ReadTimeout' in err_s else 'ReadError'}"
                    elif "运行总时限已到" in err_s:
                        key = "model_run_timeout:deadline"
                    else:
                        key = "model_failed:" + err_s[:80]
                    model_fail_cnt[key] += 1
                    model_fail_bots[key].add(name)
                elif t in ("model.http_retry", "model.transient_retry"):
                    key = f"{t}:{ev.get('error')}"
                    retry_cnt[key] += 1
                    retry_bots[key].add(name)
                if '"429"' in ln or "rate_limit" in ln.lower():
                    cur["c429"] += 1
        cur["deaths"] = lifecycle.deaths
        cur["death_receipts"] = lifecycle.death_receipts
        rows.append(cur)

    if args.format == "text":
        print(f"\n=============== D1 TRIAGE: {args.wave} ({len(rows)} bots) ===============")
        for r in rows:
            print(f"  [{r['squad']:3s}] {r['bot']:28s} {r['status']:7s} model={r['model']:24s} "
                  f"score={r['score']:3d} eps={r['eps']:2d} gained={r['gained']:2d} "
                  f"deaths={r['deaths']:2d} (idless={r['death_receipts']}) "
                  f"guard={r['guard']:3d} 429={r['c429']}")

        print("\n--- TOOL FAILURE LEADERBOARD (by bots affected, then count) ---")
        for key, cnt in sorted(err_cnt.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))[:args.top]:
            bots = sorted(err_bots[key])
            mods = ",".join(f"{m}x{c}" for m, c in err_models[key].most_common())
            print(f"  {len(bots):2d} bots | {cnt:4d}x | {key:52s} | models={mods}")
            print(f"      bots={bots[:6]}")
            for msg, mc in err_msgs[key].most_common(2):
                print(f"      msg[{mc}x] {msg}")

        print("\n--- REFLEX PREEMPTIONS (excluded from bug leaderboard) ---")
        for tool, cnt in preempt_cnt.most_common():
            print(f"  {len(preempt_bots[tool]):2d} bots | {cnt:4d}x | {tool}")

        print("\n--- PER-MODEL FAILURE MATRIX (top tools) ---")
        models = sorted({m for c in err_models.values() for m in c})
        print("  " + f"{'error':44s} " + " ".join(f"{m[:14]:>14s}" for m in models))
        for key, cnt in sorted(err_cnt.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))[:12]:
            row = " ".join(f"{err_models[key].get(m, 0):14d}" for m in models)
            print(f"  {key:44s} " + row)

        print("\n--- MODEL FAILURE / RETRY LEADERBOARD ---")
        for key, cnt in sorted(model_fail_cnt.items(), key=lambda kv: (-len(model_fail_bots[kv[0]]), -kv[1])):
            print(f"  {len(model_fail_bots[key]):2d} bots | {cnt:4d}x | {key}")
        for key, cnt in sorted(retry_cnt.items(), key=lambda kv: (-len(retry_bots[kv[0]]), -kv[1])):
            print(f"  {len(retry_bots[key]):2d} bots | {cnt:4d}x | {key}")

        if slow:
            print("\n--- SLOW TOOL CALLS (>=60s) ---")
            for dt, nm, tool in sorted(slow, reverse=True)[:15]:
                print(f"  {dt:6.1f}s | {nm:28s} | {tool}")

        print("\n--- TOOL DURATION STATS (start/finish delta) ---")
        for tool, vals in sorted(tool_dur.items(), key=lambda kv: -max(kv[1]))[:12]:
            v = sorted(vals)
            print(f"  {tool:26s} n={len(v):4d} med={v[len(v)//2]:6.1f}s "
                  f"p90={v[min(len(v)-1, int(0.9*len(v)))]:6.1f}s max={v[-1]:6.1f}s")
        return 0

    print(f"# D1 TRIAGE {args.wave}\n")
    for key, cnt in sorted(err_cnt.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))[:args.top]:
        bots = sorted(err_bots[key])
        print(f"- **{key}** — {len(bots)} bots / {cnt}x — {', '.join(bots[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
