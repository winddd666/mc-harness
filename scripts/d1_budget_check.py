#!/usr/bin/env python3
"""D1 BUG-018 repro: r12 mining budget regression in `_BudgetSession`.

`src/mc_agent/skills/mining_dfs.py:344` caps every nested API call at
`(remaining_slice - 0.15) / 8` seconds. With the default `max_duration_s=32`
that is ~3.9s per call, shorter than a normal `move_to` (r12 median 5.1s,
p90 25.7s), so `mine_dfs` aborts with `time_slice` before doing any work.

Run:
  PYTHONPATH=/home/xzq/projects/mc/.worktrees/v2-integrated/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_budget_check.py

Exit code 0 means the budget is sane (>= 8s for a default slice).
"""
from __future__ import annotations

import pathlib
import sys
import time

V2_SRC = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/src")
if str(V2_SRC) not in sys.path:
    sys.path.insert(0, str(V2_SRC))

import mc_agent.harness.mc_tools  # noqa: E402,F401  (establishes the repo import order)
from mc_agent.skills.mining_dfs import _BudgetSession  # noqa: E402

FLOOR_S = 8.0


class _FakeBot:
    def __init__(self) -> None:
        self.timeouts: list[tuple[str, float | None]] = []

    def call(self, path, body=None, **kwargs):  # noqa: ANN001, ANN201
        self.timeouts.append((str(path), kwargs.get("timeout_s")))
        return {}

    def status(self):
        return {"pos": [0.0, 64.0, 0.0], "health": 20, "food": 20, "ready": True, "alive": True}

    def inventory(self):
        return {"items": []}


def main() -> int:
    bad = 0
    print("slice_budget  move_to_timeout  dig_timeout  verdict")
    for budget in (31.5, 59.5, 89.5):
        bot = _FakeBot()
        sess = _BudgetSession(bot, time.monotonic() + budget)
        sess.call("move_to", {"x": 1, "z": 1, "range": 2})
        mv = sess.session.timeouts[-1][1]
        bot2 = _FakeBot()
        sess2 = _BudgetSession(bot2, time.monotonic() + budget)
        sess2.call("dig", {"pos": [0, 64, 0]})
        dig = sess2.session.timeouts[-1][1]
        ok = mv is not None and mv >= FLOOR_S
        bad += 0 if ok else 1
        print(f"{budget:11.1f}s  {mv:14.2f}s  {dig:11.2f}s  {'OK' if ok else 'STARVED'}")
    if bad:
        print(
            f"\nFAIL: {bad}/{3} slices starve nested calls below {FLOOR_S:.0f}s.\n"
            "Fix: do not divide the remaining slice by 8; the deadline is already\n"
            "checked before/after each call. Use e.g.\n"
            "  request_timeout = min(timeout_s or 45.0, max(6.0, remaining - 0.5))"
        )
        return 1
    print("\nOK: nested calls keep a usable floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
