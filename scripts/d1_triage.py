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

    model_fail_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    model_fail_cnt: collections.Counter[str] = collections.Counter()
    retry_bots: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    retry_cnt: collections.Counter[str] = collections.Counter()
    slow: list[tuple[float, str, str]] = []

    rows = []
    for bd in bot_dirs:
        name = bd.name
        squad = name.split("_")[0]
        cur = {"squad": squad, "bot": name, "model": "?", "score": 0, "deaths": 0,
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
        for evf in sorted(bd.glob("*.events.jsonl")):
            for ln in evf.read_text(errors="ignore").splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "run.started":
                    m = str(ev.get("model") or "")
                    if m and cur["model"] == "?":
                        cur["model"] = m.split("@")[0]
                elif t == "tool.started":
                    fn = ev.get("function") or {}
                    starts[str(ev.get("call_id"))] = str(fn.get("name") or "?")
                elif t == "tool.completed":
                    cid = str(ev.get("call_id"))
                    tool = starts.pop(cid, "?")
                    ok, code, msg = parse_result(ev.get("result"))
                    if ok is False:
                        key = f"{tool}:{code or 'unknown'}"
                        err_cnt[key] += 1
                        err_bots[key].add(name)
                        err_models[key][cur["model"]] += 1
                        if msg:
                            err_msgs[key][str(msg)[:220]] += 1
                        err_samples.setdefault(key, (str(evf), ev))
                    dt = float(ev.get("elapsed") or 0)
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
                elif t == "guardian.incident":
                    if "death_or_connection_changed" in (ev.get("reasons") or []):
                        cur["deaths"] += 1
                if '"429"' in ln or "rate_limit" in ln.lower():
                    cur["c429"] += 1
        rows.append(cur)

    if args.format == "text":
        print(f"\n=============== D1 TRIAGE: {args.wave} ({len(rows)} bots) ===============")
        for r in rows:
            print(f"  [{r['squad']:3s}] {r['bot']:28s} {r['status']:7s} model={r['model']:24s} "
                  f"score={r['score']:3d} eps={r['eps']:2d} gained={r['gained']:2d} "
                  f"deaths={r['deaths']:2d} guard={r['guard']:3d} 429={r['c429']}")

        print("\n--- TOOL FAILURE LEADERBOARD (by bots affected, then count) ---")
        for key, cnt in sorted(err_cnt.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))[:args.top]:
            bots = sorted(err_bots[key])
            mods = ",".join(f"{m}x{c}" for m, c in err_models[key].most_common())
            print(f"  {len(bots):2d} bots | {cnt:4d}x | {key:52s} | models={mods}")
            print(f"      bots={bots[:6]}")
            for msg, mc in err_msgs[key].most_common(2):
                print(f"      msg[{mc}x] {msg}")

        print("\n--- MODEL FAILURE / RETRY LEADERBOARD ---")
        for key, cnt in sorted(model_fail_cnt.items(), key=lambda kv: (-len(model_fail_bots[kv[0]]), -kv[1])):
            print(f"  {len(model_fail_bots[key]):2d} bots | {cnt:4d}x | {key}")
        for key, cnt in sorted(retry_cnt.items(), key=lambda kv: (-len(retry_bots[kv[0]]), -kv[1])):
            print(f"  {len(retry_bots[key]):2d} bots | {cnt:4d}x | {key}")

        if slow:
            print("\n--- SLOW TOOL CALLS (>=60s) ---")
            for dt, nm, tool in sorted(slow, reverse=True)[:15]:
                print(f"  {dt:6.1f}s | {nm:28s} | {tool}")
        return 0

    print(f"# D1 TRIAGE {args.wave}\n")
    for key, cnt in sorted(err_cnt.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))[:args.top]:
        bots = sorted(err_bots[key])
        print(f"- **{key}** — {len(bots)} bots / {cnt}x — {', '.join(bots[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
