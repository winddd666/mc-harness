"""Offline tests for L2 terrain primitives and `navigate_segmented` limited recovery."""

from __future__ import annotations

import math
from typing import Any

import pytest

from mc_agent.errors import ApiError
from mc_agent.skills.movement import BreadcrumbTrail, TerrainNavigator
from mc_agent.skills.library import MacroSkills


class FakeBot:
    """Minimal physical simulator: blocks, one-block forward steps, scripted failures."""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 65.0, 0.5),
        *,
        blocks: dict[tuple[int, int, int], str] | None = None,
        items: list[dict[str, Any]] | None = None,
        dimension: str = "overworld",
    ) -> None:
        self.pos = list(pos)
        self.blocks = dict(blocks or {})
        self.items = [dict(r) for r in (items or [])]
        self.dimension = dimension
        self.sneaking = False
        self.in_water = False
        self.submerged = False
        self.on_ground = True
        self.in_vehicle: dict[str, Any] | None = None
        self.look_target: tuple[float, float] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.frozen = False
        self.move_ok = True
        self.move_fail_times = 0
        self.place_fail_at: set[int] = set()
        self.place_count = 0
        self.dig_fail = False
        self.health = 20.0

    # -- observation ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "pos": list(self.pos),
            "yaw": 0.0,
            "dimension": self.dimension,
            "on_ground": self.on_ground,
            "in_water": self.in_water,
            "submerged": self.submerged,
            "in_vehicle": self.in_vehicle,
            "sneaking": self.sneaking,
            "health": self.health,
            "ready": True,
        }

    def inventory(self) -> dict[str, Any]:
        return {"items": [dict(r) for r in self.items], "empty_slots": 30}

    def _block(self, pos: tuple[int, int, int]) -> str:
        if pos in self.blocks:
            return self.blocks[pos]
        return "air" if pos[1] >= 65 else "stone"

    def _cell(self, body: dict[str, Any]) -> tuple[int, int, int]:
        p = body.get("pos") or [0, 0, 0]
        return (int(p[0]), int(p[1]), int(p[2]))

    # -- actions --------------------------------------------------------
    def call(self, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        body = body or {}
        self.calls.append((path, dict(body)))
        if path == "get_block":
            pos = self._cell(body)
            name = self._block(pos)
            props: dict[str, Any] = {}
            if name.endswith(("_door", "_fence_gate")):
                props = {"open": bool(self.blocks.get(("open",) + pos, False))}
            return {"name": name, "pos": list(pos), "properties": props, "diggable": True}
        if path == "dig":
            pos = self._cell(body)
            if self.dig_fail:
                raise ApiError("dig_unconfirmed", "cannot dig")
            name = self._block(pos)
            self.blocks[pos] = "air"
            return {"dug": name, "gained": {name: 1}}
        if path == "place":
            self.place_count += 1
            if self.place_count in self.place_fail_at:
                raise ApiError("place_failed", "no support")
            pos = self._cell(body)
            self.blocks[pos] = str(body.get("item") or "cobblestone")
            return {"placed": body.get("item"), "pos": list(pos), "sneaked": True}
        if path == "posture":
            self.sneaking = bool(body.get("sneak"))
            return {"sneaking": self.sneaking}
        if path == "look":
            if "x" in body:
                self.look_target = (float(body["x"]), float(body["z"]))
            return {"yaw": 0.0}
        if path == "control":
            if self.frozen:
                return {"moved": 0, "pos": list(self.pos)}
            if body.get("forward") and self.look_target is not None:
                tx, tz = self.look_target
                dx = tx - self.pos[0]
                dz = tz - self.pos[2]
                step = 1.0
                if abs(dx) >= abs(dz):
                    self.pos[0] += math.copysign(min(step, abs(dx)), dx) if dx else 0.0
                else:
                    self.pos[2] += math.copysign(min(step, abs(dz)), dz) if dz else 0.0
                self.blocks.setdefault((math.floor(self.pos[0]), math.floor(self.pos[1]) - 1, math.floor(self.pos[2])), "stone")
            if body.get("jump"):
                self.on_ground = True
            return {"moved": 1, "pos": list(self.pos), "on_ground": self.on_ground}
        if path == "move_to":
            if not self.move_ok or self.move_fail_times > 0:
                if self.move_fail_times > 0:
                    self.move_fail_times -= 1
                raise ApiError("path_not_found", "no path")
            self.pos[0] = float(body.get("x", self.pos[0]))
            self.pos[2] = float(body.get("z", self.pos[2]))
            if body.get("y") is not None:
                self.pos[1] = float(body["y"])
            return {"pos": list(self.pos)}
        if path == "interact":
            if "entity_id" in body and self.in_vehicle is None:
                self.in_vehicle = {"id": int(body["entity_id"]), "name": "oak_boat"}
                return {"in_vehicle": self.in_vehicle, "vehicle_changed": True}
            return {"changed": True}
        if path == "dismount":
            if self.in_vehicle is None:
                raise ApiError("not_in_vehicle", "not riding")
            self.in_vehicle = None
            self.on_ground = False
            return {"dismounted": True, "pos": list(self.pos)}
        if path == "use_item":
            return {"used": 1, "entity": {"id": 77, "name": "oak_boat"}, "pos": body.get("target", {}).get("pos")}
        if path == "attack":
            return {"killed": True, "hits": 2}
        if path == "get_entities":
            return {"entities": [{"id": 77, "name": "oak_boat", "pos": [2.5, 63.0, 0.5], "dist": 1.5}]}
        if path == "find_blocks":
            return {"blocks": []}
        return {}


def _inv(*items: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"name": n, "count": c, "slot": 36 + i} for i, (n, c) in enumerate(items)]


# ---------------------------------------------------------------------------
# BreadcrumbTrail
# ---------------------------------------------------------------------------


def test_breadcrumb_sampling_spacing_and_dimension_isolation() -> None:
    bot = FakeBot()
    trail = BreadcrumbTrail(bot)
    assert trail.note_status({"pos": [0.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    assert trail.points("overworld") == [[0, 65, 0]]
    # 3 m: too close
    assert not trail.note_status({"pos": [3.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    # 8 m: pushed
    assert trail.note_status({"pos": [8.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    # 22 m from the top (8): above max_step -> skipped
    assert not trail.note_status({"pos": [30.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    # airborne / water never sampled
    assert not trail.note_status({"pos": [14.5, 65.0, 0.5], "on_ground": False, "dimension": "overworld"})
    assert not trail.note_status({"pos": [14.5, 65.0, 0.5], "on_ground": True, "in_water": True, "dimension": "overworld"})
    # dim isolation
    assert trail.note_status({"pos": [1.5, 65.0, 0.5], "on_ground": True, "dimension": "the_nether"})
    assert trail.points("the_nether") == [[1, 65, 0]]
    assert len(trail.points("overworld")) == 2


def test_breadcrumb_teleport_starts_new_segment() -> None:
    bot = FakeBot()
    trail = BreadcrumbTrail(bot)
    trail.note_status({"pos": [0.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    trail.note_status({"pos": [9.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    assert len(trail.points("overworld")) == 2
    # 90 m jump -> teleport, stack restarts
    trail.note_status({"pos": [100.5, 65.0, 0.5], "on_ground": True, "dimension": "overworld"})
    assert trail.points("overworld") == [[100, 65, 0]]
    assert trail.segments["overworld"] == 1


def test_breadcrumb_backtrack_is_reverse_order_and_distance_bounded() -> None:
    bot = FakeBot(pos=(32.5, 65.0, 0.5))
    trail = BreadcrumbTrail(bot)
    trail.stacks["overworld"] = [[10, 65, 0], [20, 65, 0], [30, 65, 0]]
    res = trail.backtrack(steps=3, max_dist=12.0)
    # 32->30 (2m) then 30->20 (10m) would exceed 12m with the third hop
    assert res["ok"] is True
    assert res["visited"] == [[30, 65, 0], [20, 65, 0]]
    assert trail.points("overworld") == [[10, 65, 0]]


# ---------------------------------------------------------------------------
# Traps / doors
# ---------------------------------------------------------------------------


def test_clear_traps_forces_death_only_and_opens_door() -> None:
    blocks = {
        (0, 65, 0): "sweet_berry_bush",
        (1, 65, 0): "cobweb",
        (0, 66, 1): "oak_door",
    }
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks=blocks)
    nav = TerrainNavigator(bot)
    res = nav.clear_traps_and_doors(dx=1, dz=0)
    assert res["ok"] is True
    digs = [b for p, b in bot.calls if p == "dig"]
    assert digs and all(b.get("stop_on") == ["death"] for b in digs)
    assert any(p == "interact" and b.get("pos") == [0, 66, 1] for p, b in bot.calls)
    assert {d["block"] for d in res["cleared"]} >= {"sweet_berry_bush", "cobweb"}


def test_powder_snow_uses_bucket_before_digging() -> None:
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks={(0, 65, 0): "powder_snow"}, items=_inv(("bucket", 1)))
    nav = TerrainNavigator(bot)
    res = nav.clear_traps_and_doors()
    assert res["scooped"] and res["scooped"][0]["block"] == "powder_snow"
    assert not any(p == "dig" for p, _ in bot.calls)
    assert any(p == "interact" and b.get("item") == "bucket" for p, b in bot.calls)


def test_fence_with_gate_walks_to_gate_instead_of_blind_jump() -> None:
    bot = FakeBot(
        pos=(0.5, 65.0, 0.5),
        blocks={(1, 65, 0): "oak_fence", (0, 65, 3): "oak_fence_gate"},
    )
    nav = TerrainNavigator(bot)
    original = bot.call

    def _find_blocks(path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if path == "find_blocks":
            return {"blocks": [{"name": "oak_fence_gate", "pos": [0, 65, 3], "dist": 3.0}]}
        return original(path, body)

    bot.call = _find_blocks  # type: ignore[assignment]
    res = nav.clear_traps_and_doors(dx=1, dz=0)
    assert any("fence_gate" in o["block"] for o in res["opened"])


# ---------------------------------------------------------------------------
# Sneak bridge
# ---------------------------------------------------------------------------


def test_sneak_bridge_reinforces_floating_sand_and_releases_sneak() -> None:
    blocks = {
        (0, 64, 0): "stone",
        (1, 64, 0): "sand",
        (1, 63, 0): "air",
        (6, 64, 0): "stone",
    }
    for x in range(2, 6):
        blocks[(x, 64, 0)] = "air"
        blocks[(x, 63, 0)] = "air"
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks=blocks, items=_inv(("cobblestone", 16)))
    nav = TerrainNavigator(bot)
    res = nav.sneak_bridge(6.0, 0.5)
    assert res["ok"] is True, res
    assert res["reinforced_gravity"], res
    posture = [b for p, b in bot.calls if p == "posture"]
    assert {"sneak": True} in posture
    assert posture[-1] == {}
    assert res["sneaking"] is False


def test_sneak_bridge_refuses_floating_gravity_liftoff() -> None:
    blocks = {(0, 63, 0): "sand", (0, 62, 0): "air"}
    bot = FakeBot(pos=(0.5, 64.0, 0.5), blocks=blocks, items=_inv(("cobblestone", 16)))
    bot.frozen = True
    nav = TerrainNavigator(bot)
    res = nav.sneak_bridge(6.0, 0.5)
    assert res["ok"] is False
    assert res["error"] == "unsafe_gravity_liftoff"


def test_sneak_bridge_releases_sneak_even_when_place_fails() -> None:
    blocks = {(0, 64, 0): "stone", (1, 64, 0): "air", (1, 63, 0): "air"}
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks=blocks, items=_inv(("cobblestone", 16)))
    bot.place_fail_at = {1}
    nav = TerrainNavigator(bot)
    res = nav.sneak_bridge(6.0, 0.5)
    assert res["ok"] is False
    assert [b for p, b in bot.calls if p == "posture"][-1] == {}


def test_sneak_bridge_without_filler_reports_missing_item() -> None:
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks={(0, 64, 0): "stone"}, items=[])
    nav = TerrainNavigator(bot)
    res = nav.sneak_bridge(6.0, 0.5)
    assert res["ok"] is False and res["error"] == "missing_item"


# ---------------------------------------------------------------------------
# Boat crossing
# ---------------------------------------------------------------------------


def test_boat_crossing_dismounts_near_shore_and_recovers_boat() -> None:
    blocks = {(12, 63, 0): "stone", (12, 64, 0): "stone"}
    for x in range(1, 8):
        blocks[(x, 63, 0)] = "water"
        blocks[(x, 64, 0)] = "air"
    bot = FakeBot(pos=(0.5, 64.0, 0.5), blocks=blocks, items=_inv(("oak_boat", 1)))
    nav = TerrainNavigator(bot)
    res = nav.cross_water_with_boat(10.5, 0.5)
    assert res["ok"] is True, res
    assert res["dismounted"] is True
    assert res["stood_on_boat_after_dismount"] is True
    assert any(p == "dismount" for p, _ in bot.calls)
    assert any(p == "attack" and b.get("entity_id") == 77 for p, b in bot.calls)
    assert res["boat_recovered"] is True


def test_boat_crossing_requires_boat() -> None:
    bot = FakeBot(pos=(0.5, 64.0, 0.5), blocks={(1, 63, 0): "water"}, items=[])
    nav = TerrainNavigator(bot)
    res = nav.cross_water_with_boat(10.5, 0.5)
    assert res["ok"] is False and res["error"] == "missing_item"


# ---------------------------------------------------------------------------
# navigate_segmented limited recovery
# ---------------------------------------------------------------------------


class _StubNav:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def recover(self, **_: Any) -> dict[str, Any]:
        ok = self.outcomes[self.calls] if self.calls < len(self.outcomes) else False
        self.calls += 1
        return {"ok": ok, "op": "stub", "diagnosis": "stub"}

    def note_status(self, *_: Any, **__: Any) -> bool:
        return True


def test_navigate_segmented_stops_after_two_recoveries() -> None:
    bot = FakeBot(pos=(0.5, 65.0, 0.5))
    bot.move_ok = False
    skills = MacroSkills(bot)
    stub = _StubNav([False, False, False])
    skills._terrain_nav = stub  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        skills.navigate_segmented(120.0, 0.5)
    assert stub.calls == 2
    assert len(exc.value.context["recoveries"]) == 2
    assert exc.value.context["blocked_reason"]


def test_navigate_segmented_resumes_after_successful_recovery() -> None:
    bot = FakeBot(pos=(0.5, 65.0, 0.5))
    bot.move_fail_times = 4  # direct + 3 hops fail, recovery unblocks
    skills = MacroSkills(bot)
    stub = _StubNav([True])
    skills._terrain_nav = stub  # type: ignore[assignment]
    res = skills.navigate_segmented(40.0, 0.5, step_m=12.0)
    assert res["ok"] is True
    assert stub.calls == 1
    assert res["recoveries"][0]["ok"] is True


def test_forward_gap_diagnosis_uses_gap_width() -> None:
    blocks = {(0, 64, 0): "stone"}
    for x in range(1, 5):
        blocks[(x, 64, 0)] = "air"
        blocks[(x, 63, 0)] = "air"
    bot = FakeBot(pos=(0.5, 65.0, 0.5), blocks=blocks, items=_inv(("cobblestone", 16)))
    nav = TerrainNavigator(bot)
    res = nav.recover(x=8.0, z=0.5)
    assert res["diagnosis"].startswith("gap_")
    assert res["op"] in {None, "sneak_bridge"}
