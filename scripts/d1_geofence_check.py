#!/usr/bin/env python3
"""D1 BUG-028 guard: combat_env must only call the real VoidGeofence API.

`combat_env.escape_lava/_find_walkable_shore_target` called
`self.geofence.contains(...)`, but VoidGeofence exposes only
check_point / check_ray / distance_from_center / margin_remaining.
The AttributeError kills the whole bot process mid-reflex (r16 hy4_hy_6).

Run:
  /home/xzq/projects/mc/.venv/bin/python scripts/d1_geofence_check.py
Exit 0 = clean.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")
SRC = ROOT / "src/mc_agent/harness/geofence.py"
COMBAT = ROOT / "src/mc_agent/skills/combat_env.py"

ALLOWED = {"check_point", "check_ray", "distance_from_center", "margin_remaining", "enabled", "safe_radius"}


def main() -> int:
    geofence_src = SRC.read_text()
    public = set(re.findall(r"^\s{4}def (\w+)\(", geofence_src, re.M))
    combat_src = COMBAT.read_text()
    used = set(re.findall(r"self\.geofence\.(\w+)\(", combat_src))
    unknown = sorted(used - public)
    print(f"VoidGeofence public methods : {sorted(public)}")
    print(f"combat_env uses             : {sorted(used)}")
    if unknown:
        print(f"\nFAIL: combat_env calls non-existent geofence API: {unknown}")
        print("These raise AttributeError at runtime and kill the bot (BUG-028).")
        for line_no, line in enumerate(combat_src.splitlines(), 1):
            if any(f".{name}(" in line for name in unknown):
                print(f"  combat_env.py:{line_no}: {line.strip()}")
        print("\nFix: use self.geofence.check_point(x, z, action=...) is not None")
        return 1
    print("\nOK: only real geofence APIs are used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
