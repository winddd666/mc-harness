"""D1 minimal failing repros for r11_22bot P0/P1 tickets.

Run against the shared source (do NOT install):
  PYTHONPATH=/home/xzq/projects/mc/.worktrees/v2-integrated/src \
  /home/xzq/projects/mc/.venv/bin/pytest \
  /home/xzq/projects/mc/.worktrees/s-triage/tests/test_d1_repro.py -q

These tests describe the EXPECTED contract after Slice C fixes; they fail on
current `v2-integrated`. They are hand-off artifacts, not part of the suite.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

V2_SRC = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated/src")
if str(V2_SRC) not in sys.path:
    sys.path.insert(0, str(V2_SRC))

from mc_agent.harness.mc_tools import _normalize_find_block_types  # noqa: E402
from mc_agent.skills.crafting import material_shortage  # noqa: E402


def test_find_blocks_never_emits_unknown_colloquial_names():
    """BUG-006: a single bad/alias name must not reach the backend as-is."""
    out = _normalize_find_block_types(["log", "shield", "oak_log"])
    assert "log" not in out, f"'log' must be expanded or dropped, got {out}"
    assert "shield" not in out, f"'shield' is not a block, got {out}"
    # a valid concrete name must survive the normalization
    assert "oak_log" in out


def test_find_blocks_alias_log_expands_to_real_log_blocks():
    """BUG-006: `log` is the highest-frequency alias in r11 (22 failures)."""
    out = _normalize_find_block_types(["log"])
    assert out, "alias 'log' should expand to concrete *_log block ids"
    assert all(name.endswith("_log") or name.endswith("_stem") for name in out), out


def test_craft_precheck_covers_stone_tools():
    """BUG-007: stone tools were not in the hand-written precheck table."""
    plan = material_shortage("stone_pickaxe", 1, {"cobblestone": 1, "stick": 2})
    assert plan is not None, "1 cobblestone cannot craft a stone_pickaxe (needs 3)"
    assert plan["missing_materials"]["cobblestone"] == 2


def test_triage_summary_uses_real_field_paths():
    """BUG-001: the aggregator must read curriculum_summary, not top-level keys."""
    run = pathlib.Path(
        "/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot"
        "/hy4_hy_2_bare/wild-endurance-r11_22bot_hy_2_bare-20261119-023653.json"
    )
    if not run.exists():
        pytest.skip("r11 run artifact not present")
    d = json.loads(run.read_text())
    assert "final_score" not in d, "sanity: score lives under curriculum_summary"
    assert d["curriculum_summary"]["final_score"] == 10
    assert d["curriculum_summary"]["episodes"], "episodes live under curriculum_summary"
