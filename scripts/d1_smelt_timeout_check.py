#!/usr/bin/env python3
"""D1 BUG-035 repro: a furnace-collect ReadTimeout must not escape the run.

r20_short `hy4_hy_3_midgame`: `curriculum.run -> WorldJobRegistry.collect_job ->
collect_smelt_batch -> skills.smelt_collect -> bot.call("window/view")` raised
`httpx.ReadTimeout`, which propagated to `main` and killed the whole bot.

Expected contract after the fix: `collect_job` returns a deferred result
(`ok=False, deferred=True, still_burning=True`) and keeps the job, so the next
episode retries. The furnace contents do not disappear.

Run:
  PYTHONPATH=/home/xzq/projects/mc/.worktrees/v2-integrated/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_smelt_timeout_check.py
Exit 0 = contract holds.
"""
from __future__ import annotations

import pathlib
import sys

import httpx

V2_SRC = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/src")
if str(V2_SRC) not in sys.path:
    sys.path.insert(0, str(V2_SRC))

import mc_agent.harness.mc_tools  # noqa: E402,F401  (repo import order)
from mc_agent.harness.curriculum import WorldJob, WorldJobRegistry  # noqa: E402


class _FakeSkills:
    def smelt_collect(self, furnaces=None, *, wait_remaining_s: float = 15.0, dim=None):
        raise httpx.ReadTimeout("timed out")


def main() -> int:
    reg = WorldJobRegistry()
    reg.jobs["j1"] = WorldJob(
        job_id="j1",
        kind="smelt",
        dim="overworld",
        anchor_pos=[0, 64, 0],
        furnaces=[{"pos": [0, 64, 0]}],
        input_item="raw_iron",
        output_item="iron_ingot",
        total_count=1,
        started_at=0.0,
        ready_at=0.0,
    )
    try:
        res = reg.collect_job(_FakeSkills(), "j1", wait_remaining_s=0.1)
    except httpx.HTTPError as exc:
        print(f"FAIL: ReadTimeout escaped collect_job -> would kill the run: {exc!r}")
        print("Fix: catch httpx.HTTPError in smelt_collect/collect_smelt_batch/collect_job")
        print("     and return {'ok': False, 'deferred': True, 'still_burning': True}.")
        return 1
    print(f"collect_job returned: {res}")
    if not isinstance(res, dict) or not (res.get("deferred") or res.get("still_burning")):
        print("FAIL: expected deferred/still_burning result")
        return 1
    if "j1" not in reg.jobs:
        print("FAIL: job was dropped despite an uncertain network result")
        return 1
    print("OK: timeout is deferred and the job is preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
