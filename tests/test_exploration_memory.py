from __future__ import annotations

import pathlib

from mc_agent.harness.world_memory import WorldMemory
from mc_agent.skills.library import MacroSkills, _SkillBudgetSession


class _FakeSession(_SkillBudgetSession):
    def __init__(self, blocks: dict[tuple[int, int, int], str]) -> None:
        self._blocks = blocks

    def call(self, route: str, payload=None, *, method: str = "POST", timeout_s: float = 2.0, allow_reauth: bool = False):
        if route == "get_block":
            pos = tuple(int(v) for v in payload["pos"])
            return {"name": self._blocks.get(pos, "grass_block")}
        return {}


def test_exploration_memory_two_tier_persistence_and_quadrant_summary(tmp_path: pathlib.Path) -> None:
    mem_path = tmp_path / "world_memory.json"
    mem = WorldMemory(mem_path)

    # Spawn at (0, 64, 0) in birch_forest (non-village)
    mem.observe_status({"pos": [0.0, 64.0, 0.0], "dimension": "overworld", "biome": "birch_forest", "on_ground": True})
    # Walk east and hit water edge
    mem.record_exploration_edge([64.0, 64.0, 0.0], "east", "liquid_ahead", dim="overworld")
    # Walk north and hit void edge
    mem.record_exploration_edge([0.0, 64.0, -64.0], "north", "void_geofence_blocked", dim="overworld")
    # Observe plains in SW via blocks even if server biome is empty
    mem.observe_blocks([{"name": "sandstone", "pos": [-90, 64, 90]}], dim="overworld")
    mem.mark_village_scanned([0.0, 64.0, 0.0], dim="overworld")
    mem.save(force=True)

    # Reload into fresh instance
    mem2 = WorldMemory(mem_path)
    assert len(mem2.exploration_cells) >= 4
    summary = mem2.summarize_exploration([0.0, 64.0, 0.0], dim="overworld", intent="village", half_size=1024)
    assert summary["total_cells"] >= 4
    assert set(summary["quadrants"].keys()) == {"NE", "NW", "SE", "SW"}
    # Recommended directions should prefer unexplored/village-eligible over blocked east/north
    assert summary["recommended_directions"][0] not in {"east", "north"}
    assert "southwest" in summary["recommended_directions"] or "south" in summary["recommended_directions"]
    assert len(summary["brief"]) <= 160
    assert "[周边探索(village,以当前位置为原点)]" in summary["brief"]
    assert "%" not in summary["brief"]


def test_direction_novelty_deprioritizes_water_and_void_without_hard_ban(tmp_path: pathlib.Path) -> None:
    mem = WorldMemory(tmp_path / "wm.json")
    mem.observe_status({"pos": [0.0, 64.0, 0.0], "dimension": "overworld", "on_ground": True})
    mem.record_exploration_edge([0.0, 64.0, 0.0], "east", "liquid_ahead", dim="overworld")
    mem.record_exploration_edge([0.0, 64.0, 0.0], "west", "void_geofence_blocked", dim="overworld")

    ranked = mem.unexplored_directions_8([0.0, 64.0, 0.0], dim="overworld", intent="village")
    # Both blocked directions still exist in the list (no dead-end hard ban), but ranked at the end
    assert len(ranked) == 8
    assert ranked.index("east") >= 6
    assert ranked.index("west") >= 6


def test_scout_route_has_liquid_blocks_water_and_lava() -> None:
    water_sess = _FakeSession({(15, 63, 0): "water", (30, 63, 0): "grass_block"})
    assert MacroSkills._scout_route_has_liquid(water_sess, 0.0, 64.0, 0.0, 30.0, 0.0) is True

    lava_sess = _FakeSession({(15, 63, 0): "flowing_lava", (30, 63, 0): "grass_block"})
    assert MacroSkills._scout_route_has_liquid(lava_sess, 0.0, 64.0, 0.0, 30.0, 0.0) is True

    dry_sess = _FakeSession({(15, 63, 0): "grass_block", (30, 63, 0): "grass_block"})
    assert MacroSkills._scout_route_has_liquid(dry_sess, 0.0, 64.0, 0.0, 30.0, 0.0) is False
