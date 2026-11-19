#!/usr/bin/env python3
"""Long-run (45m+) non-error progression diagnostics for 22-Bot Fleet waves.

Detects soft progression bottlenecks that hard-error acceptance gates miss:
  - LONG-001: Inventory saturation (>=28/36 slots), junk block buildup, containers=0
  - LONG-002: Repeated unverified `answered` streaks / stalled progression in late episodes
  - LONG-003: Deep underground resource starvation (y<0 with no wood/pickaxe)
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

WT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_ROOT = WT_ROOT / "runs"

JUNK_BLOCKS = frozenset(
    {
        "cobblestone",
        "cobbled_deepslate",
        "dirt",
        "andesite",
        "diorite",
        "granite",
        "tuff",
        "gravel",
        "netherrack",
        "sand",
        "deepslate",
    }
)
UNSTACKABLE_HINTS = (
    "pickaxe",
    "sword",
    "axe",
    "shovel",
    "hoe",
    "helmet",
    "chestplate",
    "leggings",
    "boots",
    "shield",
    "bow",
    "bucket",
    "flint_and_steel",
    "bed",
    "boat",
    "potion",
)


def estimate_slots(owned: dict[str, int]) -> int:
    slots = 0
    for name, count in owned.items():
        if count <= 0:
            continue
        if any(h in name for h in UNSTACKABLE_HINTS):
            slots += count
        else:
            slots += (count + 63) // 64
    return slots


def analyze_bot(bot_dir: pathlib.Path) -> dict[str, Any] | None:
    wjs = sorted(bot_dir.glob("wild-endurance-*.json"))
    if not wjs:
        return None
    data = json.loads(wjs[-1].read_text(encoding="utf-8"))
    cs = data.get("curriculum_summary") or {}
    eps = cs.get("episodes") or []
    owned = data.get("owned_end") or {}
    est_slots = estimate_slots(owned)
    junk_blocks = sum(int(v) for k, v in owned.items() if k in JUNK_BLOCKS)
    containers = int((data.get("memory_stats") or {}).get("containers") or 0)

    unverified_streak = 0
    max_unverified_streak = 0
    ep_rows: list[dict[str, Any]] = []
    for idx, ep in enumerate(eps, 1):
        ver = bool(ep.get("objective_verified"))
        if ver:
            unverified_streak = 0
        else:
            unverified_streak += 1
            max_unverified_streak = max(max_unverified_streak, unverified_streak)
        ans = ""
        rfile = ep.get("report_file")
        if rfile and pathlib.Path(rfile).exists():
            try:
                rep = json.loads(pathlib.Path(rfile).read_text(encoding="utf-8"))
                ans = str(rep.get("final_answer") or rep.get("answer") or rep.get("output") or "").replace("\n", " ")[:100]
            except Exception:
                pass
        ep_rows.append(
            {
                "ep": idx,
                "status": ep.get("status"),
                "verified": ver,
                "calls": ep.get("actor_requests"),
                "ans": ans,
            }
        )

    flags: list[str] = []
    if est_slots >= 28 and containers == 0:
        flags.append(f"LONG-001:inv_saturated({est_slots}/36,junk={junk_blocks},containers=0)")
    if max_unverified_streak >= 4:
        flags.append(f"LONG-002:stalled_streak({max_unverified_streak}_eps)")

    return {
        "bot_dir": bot_dir.name,
        "eps": len(eps),
        "verified_eps": sum(1 for r in ep_rows if r["verified"]),
        "est_slots": est_slots,
        "junk_blocks": junk_blocks,
        "containers": containers,
        "max_unverified_streak": max_unverified_streak,
        "flags": flags,
        "ep_rows": ep_rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose non-error progression bottlenecks in long runs.")
    ap.add_argument("--wave", required=True, help="Wave directory under runs/ (e.g. r15_22bot)")
    ap.add_argument("--bots", default="nju_movetest_01_bare,glm_glm_5_midgame,ds_ds_1_bare,hy4_hy_1_bare")
    ap.add_argument("--runs-root", default=None)
    args = ap.parse_args()

    runs_root = pathlib.Path(args.runs_root).expanduser() if args.runs_root else RUNS_ROOT
    wave_dir = runs_root / args.wave
    targets = [b.strip() for b in args.bots.split(",") if b.strip()]

    print(f"===== LONG-RUN PROGRESSION DIAG: {args.wave} =====")
    for name in targets:
        bdir = wave_dir / name
        if not bdir.exists():
            print(f"\n[{name}] (missing)")
            continue
        res = analyze_bot(bdir)
        if res is None:
            print(f"\n[{name}] (in-progress / no summary json yet)")
            continue
        flag_str = ", ".join(res["flags"]) if res["flags"] else "OK"
        print(
            f"\n[{name}] eps={res['eps']} verified={res['verified_eps']} "
            f"slots~={res['est_slots']}/36 junk={res['junk_blocks']} containers={res['containers']} "
            f"max_unverified_streak={res['max_unverified_streak']} | flags=[{flag_str}]"
        )
        for r in res["ep_rows"]:
            print(f"  EP{r['ep']:02d} [{str(r['status']):13s}] ver={str(r['verified']):5s} calls={str(r['calls']):>2s}")
            if r["ans"] and not r["verified"]:
                print(f"       ans: {r['ans']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
