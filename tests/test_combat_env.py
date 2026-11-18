"""Offline unit tests for `CombatEnv` v1 probes, delegates, and `CombatControllerSlot`."""

from __future__ import annotations

import math
from typing import Any

from mc_agent.errors import ApiError
from mc_agent.skills.combat_env import (
    CombatControllerSlot,
    CombatEnv,
    SubcorticalInstinctController,
    aim_yaw_pitch,
    yaw_to_unit_xz,
)


class FakeCombatBot:
    def __init__(self) -> None:
        self.pos = [0.5, 64.0, 0.5]
        self.health = 20.0
        self.food = 20
        self.in_water = False
        self.yaw = 0.0
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.items: list[dict[str, Any]] = []
        self.offhand: dict[str, Any] = {}
        self.held: dict[str, Any] = {}
        self.entities: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.posture_state: dict[str, Any] = {}

    def _block_at(self, x: int, y: int, z: int) -> str:
        if (x, y, z) in self.blocks:
            return self.blocks[(x, y, z)]
        return "stone" if y < 64 else "air"

    def status(self) -> dict[str, Any]:
        nh = self.entities[0] if self.entities else None
        return {
            "pos": list(self.pos),
            "health": self.health,
            "food": self.food,
            "alive": True,
            "ready": True,
            "on_ground": True,
            "in_water": self.in_water,
            "yaw": self.yaw,
            "nearest_hostile": nh,
        }

    def inventory(self) -> dict[str, Any]:
        return {"items": list(self.items), "offhand": dict(self.offhand), "held": dict(self.held)}

    def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        payload = body or {}
        self.calls.append((action, payload))
        if action == "status":
            return self.status()
        if action == "inventory":
            return self.inventory()
        if action == "get_block":
            p = payload["pos"]
            return {"name": self._block_at(int(p[0]), int(p[1]), int(p[2]))}
        if action == "find_blocks":
            radius = int(payload.get("radius", 8))
            types = set(payload.get("types") or [])
            found = []
            bx, by, bz = math.floor(self.pos[0]), math.floor(self.pos[1]), math.floor(self.pos[2])
            for (x, y, z), bname in self.blocks.items():
                if bname in types:
                    d = math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)
                    if d <= radius:
                        found.append({"name": bname, "pos": [x, y, z], "dist": round(d, 2)})
            found.sort(key=lambda r: r["dist"])
            return {"blocks": found}
        if action == "get_entities":
            return {"entities": list(self.entities)}
        if action == "place":
            p = payload["pos"]
            self.blocks[(int(p[0]), int(p[1]), int(p[2]))] = str(payload["item"])
            return {"pos": p}
        if action == "equip":
            if payload.get("slot") == "off-hand":
                self.offhand = {"name": payload["item"], "count": 1}
            else:
                self.held = {"name": payload["item"], "count": 1}
            return {"equipped": payload["item"]}
        if action == "posture":
            self.posture_state = dict(payload)
            return dict(payload)
        if action == "control":
            if payload.get("forward"):
                ux, uz = yaw_to_unit_xz(self.yaw)
                self.pos[0] += ux * 2.2
                self.pos[2] += uz * 2.2
            return {"pos": list(self.pos)}
        if action == "look":
            if "yaw" in payload:
                self.yaw = float(payload["yaw"])
            return {"yaw": self.yaw}
        if action == "move_to":
            if "x" in payload and "z" in payload:
                self.pos[0] = float(payload["x"])
                self.pos[2] = float(payload["z"])
            return {"pos": list(self.pos)}
        if action == "attack":
            eid = payload.get("entity_id")
            self.entities = [e for e in self.entities if e.get("id") != eid]
            return {"hits": 1, "killed": True}
        if action in {"stop", "use_item", "interact"}:
            if action == "use_item":
                self.health = min(20.0, self.health + 4.0)
                self.food = 20
            return {"ok": True}
        raise AssertionError(f"Unhandled action: {action}")


def test_yaw_roundtrip_matches_mineflayer_convention() -> None:
    # Target due North (-Z): yaw should be 0, unit vector (0, -1)
    yaw_n, _ = aim_yaw_pitch([0.0, 64.0, 0.0], [0.0, 64.0, -5.0])
    ux, uz = yaw_to_unit_xz(yaw_n)
    assert abs(ux) < 1e-6 and abs(uz - (-1.0)) < 1e-6

    # Target due East (+X): yaw should be -pi/2, unit vector (+1, 0)
    yaw_e, _ = aim_yaw_pitch([0.0, 64.0, 0.0], [5.0, 64.0, 0.0])
    uxe, uze = yaw_to_unit_xz(yaw_e)
    assert abs(uxe - 1.0) < 1e-6 and abs(uze) < 1e-6


def test_has_los_distinguishes_solid_wall_vs_tall_grass_and_uses_cache() -> None:
    bot = FakeCombatBot()
    bot.blocks[(0, 65, -2)] = "short_grass"
    env = CombatEnv(bot)

    r1 = env.has_los([0.5, 64.0, 0.5], [0.5, 64.0, -4.5])
    assert r1["clear"] is True
    get_block_calls_1 = len([c for c in bot.calls if c[0] == "get_block"])
    assert get_block_calls_1 > 0

    # Second call hits 3s block cache -> 0 new get_block HTTP calls
    r2 = env.has_los([0.5, 64.0, 0.5], [0.5, 64.0, -4.5])
    assert r2["clear"] is True
    assert len([c for c in bot.calls if c[0] == "get_block"]) == get_block_calls_1

    # Place solid stone pillar in between and invalidate cache
    bot.blocks[(0, 65, -2)] = "stone"
    env.invalidate_block_cache()
    r3 = env.has_los([0.5, 64.0, 0.5], [0.5, 64.0, -4.5])
    assert r3["clear"] is False
    assert r3["blocker"]["name"] == "stone"
    assert r3["blocker"]["pos"] == [0, 65, -2]


def test_probe_ray_detects_flat_step_up_lava_and_cliff() -> None:
    bot = FakeCombatBot()
    env = CombatEnv(bot)

    # Due North (yaw=0): clear flat ground
    flat = env.probe_ray([0.5, 64.0, 0.5], 0.0, dist=3.0)
    assert flat["safe"] is True
    assert flat["walkable_flat"] is True
    assert flat["needs_jump"] is False

    # Due East (yaw=-pi/2): 1-block step up at x=1, y=64
    bot.blocks[(1, 64, 0)] = "dirt"
    bot.blocks[(2, 64, 0)] = "dirt"
    bot.blocks[(3, 64, 0)] = "dirt"
    env.invalidate_block_cache()
    step = env.probe_ray([0.5, 64.0, 0.5], -math.pi / 2, dist=3.0)
    assert step["safe"] is True
    assert step["needs_jump"] is True
    assert step["walkable_flat"] is False

    # Due South (yaw=pi): lava pool at z=2, y=63
    bot.blocks[(0, 63, 2)] = "lava"
    env.invalidate_block_cache()
    lava_ray = env.probe_ray([0.5, 64.0, 0.5], math.pi, dist=3.0)
    assert lava_ray["safe"] is False
    assert lava_ray["hazard"] == "lava"

    # Due West (yaw=+pi/2): 5-block deep cliff at x=-1
    for y_drop in range(59, 64):
        bot.blocks[(-1, y_drop, 0)] = "air"
    env.invalidate_block_cache()
    cliff_ray = env.probe_ray([0.5, 64.0, 0.5], math.pi / 2, dist=3.0)
    assert cliff_ray["safe"] is False
    assert cliff_ray["hazard"] == "cliff"
    assert cliff_ray["max_drop"] >= 4


def test_scan_safe_yaws_avoids_blocked_back_and_detects_cornered() -> None:
    bot = FakeCombatBot()
    # Block North (yaw=0) with a 2-high stone wall
    bot.blocks[(0, 64, -1)] = "stone"
    bot.blocks[(0, 65, -1)] = "stone"
    env = CombatEnv(bot)

    scan = env.scan_safe_yaws([0.5, 64.0, 0.5], 0.0, dist=3.0)
    assert scan["cornered"] is False
    assert scan["best_yaw"] is not None
    assert abs(scan["best_yaw"]) > 0.2  # steered around the north wall!

    # Surround completely -> cornered == True
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            if dx == 0 and dz == 0:
                continue
            bot.blocks[(dx, 64, dz)] = "stone"
            bot.blocks[(dx, 65, dz)] = "stone"
    env.invalidate_block_cache()
    boxed = env.scan_safe_yaws([0.5, 64.0, 0.5], 0.0, dist=3.0)
    assert boxed["cornered"] is True
    assert boxed["best_yaw"] is None


def test_arena_snapshot_detects_carved_shaft_and_target_over_lava() -> None:
    bot = FakeCombatBot()
    # Carve a 1x2 North-South shaft with stone walls East/West and stone ceiling at y=66
    bot.blocks[(1, 64, 0)] = "stone"
    bot.blocks[(1, 65, 0)] = "stone"
    bot.blocks[(-1, 64, 0)] = "stone"
    bot.blocks[(-1, 65, 0)] = "stone"
    bot.blocks[(0, 66, 0)] = "stone"
    # Blaze hovering above lava at (4, 65, 0)
    bot.blocks[(4, 64, 0)] = "air"
    bot.blocks[(4, 63, 0)] = "lava"
    env = CombatEnv(bot)

    snap = env.arena_snapshot([0.5, 64.0, 0.5], target_pos=[4.5, 65.0, 0.5])
    assert snap["enclosure"] == "carved_shaft"
    assert snap["max_pillar_height"] == 0
    assert snap["target_over_hazard"] is True
    assert any(h["name"] == "lava" for h in snap["hazards"])


def test_slot_counter_kills_lone_zombie_eats_and_resets_posture() -> None:
    bot = FakeCombatBot()
    bot.health = 14.0
    bot.food = 14
    bot.items = [
        {"name": "iron_sword", "count": 1},
        {"name": "shield", "count": 1},
        {"name": "bread", "count": 4},
    ]
    bot.entities = [{"id": 77, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.8}]
    # Simulate dirty posture before entering slot
    bot.posture_state = {"shield": True, "sneak": True}

    env = CombatEnv(bot)
    slot = CombatControllerSlot(SubcorticalInstinctController())
    out = slot.handle(
        env,
        {"source": "tool_interrupted_hurt", "primary_entity_id": 77, "primary_type": "zombie", "distance": 1.8},
    )

    assert out["handled"] is True
    assert out["mode"] == "counter_killed"
    assert any(a["action"] == "melee_attack" for a in out["actions"])
    assert any(a["action"] == "post_combat_eat" for a in out["actions"])
    # Entry called stop, and exit reset posture to {}!
    assert bot.calls[0][0] == "stop"
    assert bot.posture_state == {}


def test_slot_seals_shaft_against_skeleton_in_corridor() -> None:
    bot = FakeCombatBot()
    bot.items = [{"name": "cobblestone", "count": 16}]
    bot.blocks[(1, 64, 0)] = "stone"
    bot.blocks[(1, 65, 0)] = "stone"
    bot.blocks[(-1, 64, 0)] = "stone"
    bot.blocks[(-1, 65, 0)] = "stone"
    bot.blocks[(0, 66, 0)] = "stone"
    bot.entities = [{"id": 88, "name": "skeleton", "pos": [0.5, 64.0, -6.5], "dist": 7.0}]

    env = CombatEnv(bot)
    slot = CombatControllerSlot(SubcorticalInstinctController())
    out = slot.handle(
        env,
        {"source": "pre_hurt_ranged", "primary_entity_id": 88, "primary_type": "skeleton", "distance": 7.0},
    )

    assert out["handled"] is True
    assert out["mode"] == "sealed_shaft"
    assert bot.blocks[(0, 64, -1)] == "cobblestone"
    assert bot.blocks[(0, 65, -1)] == "cobblestone"
    assert bot.posture_state == {}


def test_slot_evades_creeper_with_shield_instead_of_melee() -> None:
    bot = FakeCombatBot()
    bot.items = [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]
    bot.entities = [{"id": 99, "name": "creeper", "pos": [0.5, 64.0, 3.0], "dist": 2.5}]

    env = CombatEnv(bot)
    slot = CombatControllerSlot(SubcorticalInstinctController())
    out = slot.handle(
        env,
        {"source": "pre_hurt_creeper", "primary_entity_id": 99, "primary_type": "creeper", "distance": 2.5},
    )

    assert out["handled"] is True
    assert out["mode"] == "creeper_evade"
    assert not any(a["action"] == "melee_attack" for a in out["actions"])
    assert any(a["action"] == "retreat_from_creeper" for a in out["actions"])
    assert bot.posture_state == {}
