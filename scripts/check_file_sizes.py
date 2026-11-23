#!/usr/bin/env python3
"""File-size ratchet guard for CI: new Python files must stay <= 800 lines;
legacy monoliths may only shrink (ceiling locked to current baseline).
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MAX_LINES = 800

# Ratchet baseline for legacy monoliths (shrink over time as modules are split).
LEGACY_CEILINGS: dict[str, int] = {
    "src/mc_agent/skills/macro/_base.py": 1650,
    "src/mc_agent/skills/macro/staircase.py": 1650,
    "src/mc_agent/skills/macro/inventory_station.py": 1620,
    "src/mc_agent/skills/macro/smelting.py": 1350,
    "src/mc_agent/skills/macro/gather.py": 1320,
    "src/mc_agent/skills/macro/combat_portal.py": 1240,
    "src/mc_agent/skills/macro/escape_unstick.py": 1200,
    "src/mc_agent/skills/macro/mining_container.py": 950,
    "src/mc_agent/skills/mining/runner.py": 1920,
    "src/mc_agent/skills/mining/navigation.py": 1660,
    "src/mc_agent/skills/mining/edges_branches.py": 1380,
    "src/mc_agent/skills/mining/vein_extract.py": 1220,
    "src/mc_agent/skills/mining/collar_radar.py": 1150,
    "src/mc_agent/skills/mining/_base.py": 1100,
    "src/mc_agent/harness/guardian.py": 7600,
    "src/mc_agent/skills/combat_env.py": 6000,
    "src/mc_agent/harness/mc_tools.py": 4150,
    "scripts/l3_wild_endurance_runner.py": 4000,
    "src/mc_agent/harness/curriculum.py": 3250,
    "src/mc_agent/live/state.py": 2500,
    "src/mc_agent/skills/combat_v1.py": 2450,
    "src/mc_agent/harness/agent.py": 2000,
    "src/mc_agent/live/server.py": 1850,
    "scripts/l2_move_live_test.py": 1750,
    "src/mc_agent/skills/movement.py": 1700,
    "src/mc_agent/harness/world_memory.py": 1700,
    "scripts/run_combat_tests.py": 1700,
    "src/mc_agent/skills/ensure.py": 1500,
    "src/mc_agent/console/server.py": 1500,
    "tests/test_l3_phase2.py": 1500,
    "scripts/v2_fleet.py": 1400,
    "src/mc_agent/kb/data_1_21_11.py": 1400,
    "src/mc_agent/skills/inventory_manager.py": 1300,
    "tests/test_live_server.py": 1250,
    "src/mc_agent/harness/model.py": 1100,
    "scripts/probe_micro_combat_v1314.py": 1050,
    "scripts/l2_inv_window_live_test.py": 1000,
    "tests/test_ecology_oracle.py": 950,
    "scripts/l2_mining_live_test.py": 950,
    "src/mc_agent/skills/tunnel.py": 950,
}

SCAN_DIRS = ("src", "tests", "scripts")


def main() -> int:
    violations: list[str] = []
    for sub in SCAN_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for py_file in sorted(base.rglob("*.py")):
            rel = py_file.relative_to(ROOT).as_posix()
            lines = len(py_file.read_text(encoding="utf-8", errors="ignore").splitlines())
            ceiling = LEGACY_CEILINGS.get(rel, DEFAULT_MAX_LINES)
            if lines > ceiling:
                violations.append(f"  {rel}: {lines} lines > ceiling {ceiling}")
    if violations:
        print("File-size ratchet FAILED (split large modules instead of growing monoliths):")
        print("\n".join(violations))
        return 1
    print(f"File-size ratchet OK (new files <= {DEFAULT_MAX_LINES} lines; legacy ceilings enforced).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
