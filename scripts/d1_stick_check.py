#!/usr/bin/env python3
"""D1 BUG-025 repro: `_stone_pickaxe_headroom` over-counts sticks.

r15 evidence (`ds_ds_1_bare`): cobblestone 169 + cobbled_deepslate 223,
acacia_planks 1, stick 0, no logs. The hint claims "可立刻合 1 把石镐
(木棍折算 2)", but the real recipe needs 2 planks -> 4 sticks; a single
plank yields 0 sticks, so the craft can never succeed.

Correct conversion: sticks = stick + 4 * (planks // 2) + 8 * logs
(1 log -> 4 planks -> 8 sticks).

Run:
  PYTHONPATH=/home/xzq/projects/mc/.worktrees/v2-integrated/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_stick_check.py
Exit 0 = formula sane.
"""
from __future__ import annotations

import pathlib
import sys

V2_SRC = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/src")
if str(V2_SRC) not in sys.path:
    sys.path.insert(0, str(V2_SRC))

import mc_agent.harness.mc_tools  # noqa: E402,F401  (repo import order)
from mc_agent.skills.mining_dfs import _stone_pickaxe_headroom  # noqa: E402

CASES = [
    # (name, counts, expected craftable stone pickaxes)
    ("1 plank / 0 stick / 391 stone (r15 ds_ds_1_bare)", {"cobblestone": 169, "cobbled_deepslate": 222, "acacia_planks": 1}, 0),
    ("2 planks -> 4 sticks (enough for 1)", {"cobblestone": 3, "oak_planks": 2}, 1),
    ("4 planks -> 8 sticks (enough for 2)", {"cobblestone": 6, "oak_planks": 4}, 2),
    ("1 log -> 4 planks -> 8 sticks", {"cobblestone": 6, "oak_log": 1}, 2),
    ("3 sticks + 3 stone", {"cobblestone": 3, "stick": 3}, 1),
]


def main() -> int:
    bad = 0
    print(f"{'case':52s} {'got':>4s} {'want':>5s}  sticks_available")
    for name, counts, want in CASES:
        n, stone, sticks = _stone_pickaxe_headroom(counts)
        ok = n == want
        bad += 0 if ok else 1
        print(f"{name:52s} {n:4d} {want:5d}  {sticks}  {'OK' if ok else 'WRONG'}")
    if bad:
        print(
            f"\nFAIL: {bad}/{len(CASES)} cases wrong.\n"
            "Fix in src/mc_agent/skills/mining_dfs.py::_stone_pickaxe_headroom:\n"
            "  sticks_available = sticks + 4 * (planks // 2) + 8 * logs\n"
            "and add a 'harvest 1 log first' branch when n == 0 and stone >= 3."
        )
        return 1
    print("\nOK: stick conversion matches vanilla recipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
