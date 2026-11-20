#!/usr/bin/env python3
"""D1 acceptance gate: check a wave against every open ticket's threshold.

Usage:
  python scripts/d1_acceptance.py --wave r16_22bot

Exit 0 = no observed failure; 1 = at least one FAIL. PENDING is reported separately.
Thresholds are copied from docs/BUG_BOARD.md acceptance criteria.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics

if __package__:
    from .d1_ticket_gates import ticket_gates
    from .fleet_lifecycle import LifecycleCounter
else:
    from d1_ticket_gates import ticket_gates
    from fleet_lifecycle import LifecycleCounter

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
        "owned_end": {},
        "tool_calls": 0,
        "errs": collections.Counter(),
        "no_support": 0,
        "stick_fails": 0,
        "deaths": collections.Counter(),
        "death_receipts": 0,
        "death_or_connection_incidents": 0,
        "cancel": 0,
        "cancel_wait": 0.0,
        "mine_dur": [],
        "mine_pairs": [],
        "mine_zero": 0,
        "mine_total": 0,
        "trade": 0,
        "brew": 0,
        "runtime_error": 0,
        "dw_trident": 0,
        "startup_crash": [],
        "runner_abort": [],
        "thread_errors": [],
        "deep_water": None,
        "iron_scores": [],
    }
    for bot in bots:
        sd = root / bot
        sums = sorted(sd.glob("wild-endurance-*.json"))
        cs = {}
        if sums:
            d = json.loads(sums[-1].read_text())
            cs = d.get("curriculum_summary") or {}
            out["summaries"][bot] = int(cs.get("final_score") or 0)
            out["owned_end"][bot] = d.get("owned_end") or {}
        else:
            # Mid-wave: episodes live in curriculum-*.json before the final summary.
            for cf in sorted(sd.glob("curriculum-*.json")):
                try:
                    cs = json.loads(cf.read_text())
                except Exception:
                    cs = {}
        for ep in cs.get("episodes") or []:
            if ep.get("status") == "runtime_error":
                out["runtime_error"] += 1
        log = sd / "stdout.log"
        txt = log.read_text(errors="ignore") if log.exists() else ""
        has_thread_error = "Traceback" in txt and "Exception in thread" in txt
        if has_thread_error:
            out["thread_errors"].append(bot)
        has_runner_traceback = "Traceback" in txt and (
            not has_thread_error or any(
                "l3_wild_endurance_runner.py" in line and "in <module>" in line
                for line in txt.splitlines()
            )
        )
        actor_started = False
        starts: dict[str, tuple[str, float, dict]] = {}
        lifecycle = LifecycleCounter(bot)
        for evf in sorted(sd.glob("*.events.jsonl")):
            for ln in evf.read_text(errors="ignore").splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                lifecycle.observe(ev)
                t = ev.get("type")
                if t == "run.started" and ev.get("scope") == "actor":
                    actor_started = True
                elif t == "tool.started":
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
                        try:
                            b = float((args or {}).get("max_duration_s") or 0)
                        except (TypeError, ValueError):
                            b = 0.0
                        if b > 0:
                            out["mine_pairs"].append((dur, b))
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
                            # BUG-025: flag only when the inventory *could* yield the
                            # missing sticks but the formula under-counted them.
                            miss = o.get("missing_materials") or {}
                            inv = o.get("inventory") or {}
                            if isinstance(miss, dict) and set(miss) == {"stick"} and isinstance(inv, dict):
                                planks = sum(v for k, v in inv.items() if k.endswith("_planks"))
                                logs = sum(v for k, v in inv.items() if k.endswith(("_log", "_stem")))
                                sticks = int(inv.get("stick", 0) or 0)
                                avail = sticks + 4 * (planks // 2) + 8 * logs
                                if avail >= int(miss.get("stick", 1)):
                                    out["stick_fails"] += 1
                    if tool == "mc_skill_trade":
                        out["trade"] += 1
                    if tool == "mc_skill_brew":
                        out["brew"] += 1
                elif t == "model.failed" and ev.get("cancelled"):
                    out["cancel"] += 1
                elif t == "model.cancel_cooldown_wait":
                    out["cancel_wait"] += float(ev.get("wait_s") or 0)
        if has_runner_traceback and not sums:
            out["runner_abort" if actor_started else "startup_crash"].append(bot)
        out["deaths"][bot] = lifecycle.deaths
        out["death_receipts"] += lifecycle.death_receipts
        out["death_or_connection_incidents"] += lifecycle.mixed_guardian_incidents
        if "deep_water" in bot:
            out["deep_water"] = bot
            out["dw_trident"] += sum(n for cause, n in lifecycle.death_causes.items() if "trident" in cause)
        if "_iron" in bot:
            out["iron_scores"].append(out["summaries"].get(bot, 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    args = ap.parse_args()
    wave_dir = WT / "runs" / args.wave
    if not wave_dir.is_dir():
        ap.error(f"wave directory not found: {wave_dir}")
    o = collect(args.wave)
    med = statistics.median(o["mine_dur"]) if o["mine_dur"] else 0.0
    ratios = sorted(d / b for d, b in o["mine_pairs"]) if o["mine_pairs"] else []
    ratio_med = ratios[len(ratios) // 2] if ratios else 0.0
    zero_pct = 100.0 * o["mine_zero"] / max(1, o["mine_total"])
    deaths = sum(o["deaths"].values())
    deep_score = o["summaries"].get(o["deep_water"] or "", None)

    caps = {"cobblestone": 192, "cobbled_deepslate": 192, "netherrack": 128, "dirt": 64}
    # Soft cap: mine_dfs cleanup drops in whole stacks, so allow 15% overshoot and
    # only flag gross hoarding (the user-visible "inventory clogged" problem).
    over_cap = sum(
        1 for owned in o["owned_end"].values()
        if any(int(owned.get(k, 0) or 0) > cap * 1.15 for k, cap in caps.items())
    )
    has_trade = any("village_trade" in b for b in o["bots"])
    has_brew = any("brewing_alchemy" in b for b in o["bots"])
    has_dw = o["deep_water"] is not None
    gates = [
        ("BUG-021 startup crash", len(o["startup_crash"]), "== 0"),
        ("BUG-025 stick craft fails", o["stick_fails"], "== 0"),
        ("BUG-024 no_support", o["no_support"], "<= 5"),
        ("BUG-026 death/connection incidents", o["death_or_connection_incidents"], "<= 25"),
        ("BUG-026 cancel cooldown s", round(o["cancel_wait"], 1), "<= 200"),
        ("BUG-018 mine dur/budget med", round(ratio_med, 2), "<= 1.2"),
        ("BUG-018 mine zero-slice %", round(zero_pct, 1), "< 10"),
        ("BUG-020 runtime_error in summaries", o["runtime_error"], "<= 2"),
        ("BUG-020 runner abort without final", len(o["runner_abort"]), "== 0"),
        ("BUG-029 over-cap bots", over_cap, "== 0", bool(o["summaries"])),
        ("BUG-033 thread exceptions", len(o["thread_errors"]), "== 0"),
        ("BUG-023 deep_water trident deaths", o["dw_trident"], "<= 2", has_dw),
        ("SCENE-001 trade calls", o["trade"], ">= 1", has_trade),
        ("SCENE-001 brew calls", o["brew"], ">= 1", has_brew),
    ]
    print(f"\n===== D1 ACCEPTANCE {args.wave} ({len(o['summaries'])}/{len(o['bots'])} summaries, {o['tool_calls']} tool calls) =====")
    failed = 0
    for gate in gates:
        name, val, rule = gate[0], gate[1], gate[2]
        applicable = gate[3] if len(gate) > 3 else True
        if not applicable:
            print(f"  N/A   {name:34s} actual={val!s:>8s}  (stage not in this wave)")
            continue
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
    print(f"  observed deaths: {deaths} (ID-less receipts: {o['death_receipts']})")
    print(f"  iron slots: {o['iron_scores']}")
    print(f"  top failures: {o['errs'].most_common(5)}")
    if o["startup_crash"]:
        print(f"  startup crashes: {o['startup_crash']}")
    if o["runner_abort"]:
        print(f"  runner aborted after actor start without final summary: {o['runner_abort']}")
    if o["thread_errors"]:
        print(f"  dead background threads: {o['thread_errors']}")
    print("\n===== ASSIGNED D1 TICKETS =====")
    pending = 0
    for gate in ticket_gates(wave_dir):
        failed += gate.status == "FAIL"
        pending += gate.status == "PENDING"
        evidence = f"  [{gate.evidence}]" if gate.evidence else ""
        print(f"  {gate.status:7s} {gate.ticket} {gate.metric}  actual={gate.actual}  target {gate.threshold}{evidence}")
    print(f"\n{failed} GATE(S) FAILED, {pending} ASSIGNED GATE(S) PENDING")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
