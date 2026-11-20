#!/usr/bin/env python3
"""Fast multi-run trajectory triage tool for V2 concurrent endurance tests.

Usage:
    python scripts/v2_triage.py runs/v2_r2_*
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
import sys
from typing import Any

if __package__:
    from .fleet_lifecycle import LifecycleCounter
else:
    from fleet_lifecycle import LifecycleCounter

_HTTP_FAILURE = re.compile(r"\b(?:HTTP(?:\s+(?:status|error))?[\s:]+|failed:\s*\[)([1-5]\d{2})\b", re.I)


def is_http_429_failure(event: dict[str, Any]) -> bool:
    """Read the HTTP status from current or historical model.failed events."""
    status = event.get("status_code")
    if status is not None:
        return status in (429, "429")
    match = _HTTP_FAILURE.search(str(event.get("error") or ""))
    return match is not None and match[1] == "429"


def _extract_error_code(output_text: str) -> str:
    try:
        obj = json.loads(output_text)
    except Exception:
        return "non_json"
    if not isinstance(obj, dict):
        return "non_dict"
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("reason") or "err_dict")
    if isinstance(err, str) and err:
        return err
    if obj.get("stop_reason"):
        return str(obj["stop_reason"])
    pe = obj.get("partial_error")
    if isinstance(pe, dict) and pe.get("code"):
        return str(pe["code"])
    return "unknown_fail"


def triage_run_dir(run_dir: pathlib.Path) -> dict[str, Any]:
    cur_files = sorted(run_dir.glob("curriculum-*.json"))
    summary = json.loads(cur_files[-1].read_text(encoding="utf-8")) if cur_files else {}

    tool_calls = collections.Counter()
    tool_fails = collections.Counter()
    error_codes = collections.Counter()
    slow_calls: list[tuple[float, str, str]] = []
    call_start: dict[str, tuple[float, str]] = {}
    rate_waits: list[float] = []
    http_429 = 0
    http_429_retries = 0
    lifecycle = LifecycleCounter(run_dir.name)
    guardian_incidents = 0
    tool_preemptions = 0
    llm_requests = 0

    for ef in sorted(run_dir.glob("*.events.jsonl")):
        call_start.clear()
        for raw in ef.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except Exception:
                continue
            lifecycle.observe(r)
            t = str(r.get("type") or "")
            el = float(r.get("elapsed") or 0.0)
            if t == "model.request":
                llm_requests += 1
            elif t == "model.rate_wait":
                w = float(r.get("wait_s") or 0.0)
                if w > 0:
                    rate_waits.append(w)
            elif t == "model.http_retry" and r.get("status_code") in (429, "429"):
                http_429_retries += 1
            elif t == "model.failed" and is_http_429_failure(r):
                http_429 += 1
            elif t in ("tool.started", "tool.ready"):
                cid = str(r.get("call_id") or "")
                tname = str(r.get("tool") or r.get("name") or (r.get("function") or {}).get("name") or "unknown")
                if cid and tname != "unknown":
                    call_start[cid] = (el, tname)
            elif t == "tool.completed":
                cid = str(r.get("call_id") or "")
                st_info = call_start.pop(cid, None)
                tname = str(r.get("tool") or r.get("name") or (st_info[1] if st_info else "unknown"))
                dur = float(r.get("duration") or (max(0.0, el - st_info[0]) if st_info else 0.0))
                out = str(r.get("output") or r.get("result") or "")
                tool_calls[tname] += 1
                ok_flag = r.get("ok")
                is_fail = (ok_flag is False) or ('"ok": false' in out) or ('"ok":false' in out)
                if is_fail:
                    tool_fails[tname] += 1
                    error_codes[f"{tname}:{_extract_error_code(out)}"] += 1
                if '"preempted_by_reflex": true' in out:
                    tool_preemptions += 1
                if dur >= 35.0:
                    slow_calls.append((dur, tname, ef.name))
            elif t == "guardian.incident":
                guardian_incidents += 1

    slow_calls.sort(reverse=True)
    score = summary.get("final_score", summary.get("current_score", 0))
    elapsed_s = float(summary.get("elapsed_s") or 0.0)
    eps = summary.get("episodes") or []
    return {
        "dir": run_dir.name,
        "status": summary.get("status", "running"),
        "elapsed_s": elapsed_s,
        "score": score,
        "pts_per_10m": round(score / (elapsed_s / 600.0), 1) if elapsed_s > 60 else 0.0,
        "episodes_done": len(eps),
        "llm_requests": llm_requests,
        "deaths": lifecycle.deaths,
        "death_receipts": lifecycle.death_receipts,
        "http_429": http_429,
        "http_429_retries": http_429_retries,
        "rate_waits": len(rate_waits),
        "max_rate_wait_s": round(max(rate_waits, default=0.0), 2),
        "guardian_incidents": guardian_incidents,
        "tool_preemptions": tool_preemptions,
        "top_tools": [(k, tool_calls[k], tool_fails[k]) for k, _ in tool_calls.most_common(6)],
        "top_errors": error_codes.most_common(5),
        "slow_calls": slow_calls[:4],
        "episodes": [
            (e.get("episode"), e.get("target"), e.get("status"), e.get("actor_requests"), e.get("score_after"), e.get("gained_advancements"))
            for e in eps
        ],
    }


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python scripts/v2_triage.py <run_dir> [<run_dir> ...]")
        return 2
    for arg in argv:
        p = pathlib.Path(arg)
        if not p.is_dir():
            print(f"[skip] not a directory: {p}")
            continue
        rep = triage_run_dir(p)
        print("=" * 72)
        print(
            f"[{rep['dir']}] status={rep['status']} elapsed={rep['elapsed_s']:.0f}s "
            f"score={rep['score']} ({rep['pts_per_10m']} pts/10m) eps={rep['episodes_done']} "
            f"llm_reqs={rep['llm_requests']} deaths={rep['deaths']} "
            f"(idless={rep['death_receipts']}) 429_failed={rep['http_429']} 429_retries={rep['http_429_retries']} "
            f"rate_waits={rep['rate_waits']}(max={rep['max_rate_wait_s']}s) "
            f"guardian={rep['guardian_incidents']}/{rep['tool_preemptions']}preempt"
        )
        for ep_no, tgt, st, calls, sc, gained in rep["episodes"]:
            print(f"  EP{int(ep_no or 0):02d} {str(tgt or '-'):<36} {str(st or '-'):<15} calls={str(calls):<3} score={sc} gained={gained}")
        if rep["top_tools"]:
            t_str = ", ".join(f"{k}:{c}({f}F)" for k, c, f in rep["top_tools"])
            print(f"  Tools: {t_str}")
        if rep["top_errors"]:
            e_str = ", ".join(f"{k}×{v}" for k, v in rep["top_errors"])
            print(f"  TopErr: {e_str}")
        if rep["slow_calls"]:
            s_str = ", ".join(f"{tn}({d:.0f}s)" for d, tn, _ in rep["slow_calls"])
            print(f"  Slow>=35s: {s_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
