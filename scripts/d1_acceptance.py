#!/usr/bin/env python3
"""D1 acceptance gate: check a wave against every open ticket's threshold.

Usage:
  python scripts/d1_acceptance.py --wave r16_22bot

Exit 0 = all gates pass; 1 = at least one FAIL (details printed).
Thresholds are copied from docs/BUG_BOARD.md acceptance criteria.
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


def collect(wave: str) -> dict:
    root = WT / "runs" / wave
    bots = sorted(p.name for p in root.iterdir() if p.is_dir())
    out = {
        "bots": bots,
        "summaries": {},
        "tool_calls": 0,
        "errs": collections.Counter(),
        "no_support": 0,
        "stick_fails": 0,
        "deaths": collections.Counter(),
        "cancel": 0,
        "cancel_wait": 0.0,
        "mine_dur": [],
        "mine_zero": 0,
        "mine_total": 0,
        "trade": 0,
        "brew": 0,
        "runtime_error": 0,
        "dw_trident": 0,
        "startup_crash": [],
        "thread_errors": [],
        "deep_water": None,
        "iron_scores": [],
    }
    for bot in bots:
        sd = root / bot
        sums = sorted(sd.glob("wild-endurance-*.json"))
        if sums:
            d = json.loads(sums[-1].read_text())
            cs = d.get("curriculum_summary") or {}
            out["summaries"][bot] = int(cs.get("final_score") or 0)
            for ep in cs.get("episodes") or []:
                if ep.get("status") == "runtime_error":
                    out["runtime_error"] += 1
        log = sd / "stdout.log"
        txt = log.read_text(errors="ignore") if log.exists() else ""
        if "Traceback" in txt:
            if "Exception in thread" in txt:
                # A dead background thread (e.g. ReflexPreempted in a watchdog) is
                # not a process crash; report it separately (BUG-031).
                out["thread_errors"].append(bot)
            elif not sums:
                out["startup_crash"].append(bot)
        starts: dict[str, tuple[str, float, dict]] = {}
        for evf in sorted(sd.glob("*.events.jsonl")):
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
                        str(fn.get("name") or "?"),
                        float(ev.get("elapsed") or 0),
                        L(fn.get("arguments")),
                    )
                elif t == "tool.completed":
                    st = starts.pop(str(ev.get("call_id")), None)
                    if not st:
                        continue
                    tool, t0, args = st
                    out["tool_calls"] += 1
                    dur = max(0.0, float(ev.get("elapsed") or 0) - t0)
                    o = L(ev.get("result"))
                    err = o.get("error") if isinstance(o.get("error"), dict) else None
                    code = err.get("code") if err else o.get("error")
                    if code == "repeated_identical_failure_blocked":
                        code = o.get("error", {}).get("code") if isinstance(o.get("error"), dict) else code
                    if tool == "mc_skill_mine_dfs":
                        out["mine_dur"].append(dur)
                        out["mine_total"] += 1
                        if o.get("ok") is False and o.get("stop_reason") == "time_slice":
                            out["mine_zero"] += 1
                    if o.get("ok") is False:
                        out["errs"][f"{tool}:{code or o.get('stop_reason') or 'unknown'}"] += 1
                        if o.get("stop_reason") == "missing_step_support":
                            le = o.get("last_error") or {}
                            if isinstance(le, dict) and le.get("code") == "no_support":
                                out["no_support"] += 1
                        if tool == "mc_skill_craft_gear" and code == "missing_ingredients":
                            # BUG-025 pattern: stone tool blocked *only* by sticks
                            miss = o.get("missing_materials") or {}
                            if isinstance(miss, dict) and miss and set(miss) == {"stick"}:
                                out["stick_fails"] += 1
                    if tool == "mc_skill_trade":
                        out["trade"] += 1
                    if tool == "mc_skill_brew":
                        out["brew"] += 1
                elif t == "guardian.incident":
                    if "death_or_connection_changed" in (ev.get("reasons") or []):
                        out["deaths"][bot] += 1
                elif t == "mc.notable_events":
                    txt = str(ev.get("text") or "")
                    if "死亡" in txt and "trident" in txt and "deep_water" in bot:
                        out["dw_trident"] += 1
                elif t == "model.failed" and ev.get("cancelled"):
                    out["cancel"] += 1
                elif t == "model.cancel_cooldown_wait":
                    out["cancel_wait"] += float(ev.get("wait_s") or 0)
        if "deep_water" in bot:
            out["deep_water"] = bot
        if "_iron" in bot:
            out["iron_scores"].append(out["summaries"].get(bot, 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    args = ap.parse_args()
    o = collect(args.wave)
    med = statistics.median(o["mine_dur"]) if o["mine_dur"] else 0.0
    zero_pct = 100.0 * o["mine_zero"] / max(1, o["mine_total"])
    deaths = sum(o["deaths"].values())
    deep_score = o["summaries"].get(o["deep_water"] or "", None)

    gates = [
        ("BUG-021 startup crash", len(o["startup_crash"]), "== 0"),
        ("BUG-025 stick craft fails", o["stick_fails"], "== 0"),
        ("BUG-024 no_support", o["no_support"], "<= 5"),
        ("BUG-026 deaths (incidents)", deaths, "<= 25"),
        ("BUG-026 cancel cooldown s", round(o["cancel_wait"], 1), "<= 200"),
        ("BUG-018 mine_dfs median s", round(med, 1), "25~40"),
        ("BUG-018 mine zero-slice %", round(zero_pct, 1), "< 10"),
        ("BUG-020 runtime_error", o["runtime_error"], "<= 2"),
        ("BUG-033 thread exceptions", len(o["thread_errors"]), "== 0"),
        ("BUG-023 deep_water trident deaths", o["dw_trident"], "<= 2"),
        ("SCENE-001 trade calls", o["trade"], ">= 1"),
        ("SCENE-001 brew calls", o["brew"], ">= 1"),
    ]
    width = max(len(g[0]) for g in gates) + 2
    print(f"\n===== D1 ACCEPTANCE {args.wave} ({len(o['summaries'])}/{len(o['bots'])} summaries, {o['tool_calls']} tool calls) =====")
    failed = 0
    for name, val, rule in gates:
        if rule.startswith("=="):
            ok = val == float(rule[2:]) if "." in rule else val == int(rule[2:])
        elif rule.startswith("<="):
            ok = float(val) <= float(rule[2:])
        elif rule.startswith("<"):
            ok = float(val) < float(rule[2:])
        elif rule.startswith(">="):
            ok = float(val) >= float(rule[2:])
        else:  # range a~b
            lo, hi = rule.split("~")
            ok = float(lo) <= float(val) <= float(hi)
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} actual={val!s:>8s}  target {rule}")
    print(f"\n  deep_water bot: {o['deep_water']} score={deep_score}")
    print(f"  iron slots: {o['iron_scores']}")
    print(f"  top failures: {o['errs'].most_common(5)}")
    if o["startup_crash"]:
        print(f"  startup crashes: {o['startup_crash']}")
    if o["thread_errors"]:
        print(f"  dead background threads: {o['thread_errors']}")
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} GATE(S) FAILED'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
