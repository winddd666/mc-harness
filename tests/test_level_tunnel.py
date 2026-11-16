"""Corridor safety and progress use authoritative world/position evidence."""
import pytest

from mc_agent.errors import ApiError
from mc_agent.skills.library import MacroSkills


class Corridor:
    def __init__(self, *, phantom_move=False, injury=False, failed_dig=False):
        self.pos = [-2.5, 60.0, -3.5]
        self.health = 20
        self.world = {(-3, 60, -4): "air", (-3, 61, -4): "air"}
        self.calls = []
        self.phantom_move = phantom_move
        self.injury = injury
        self.failed_dig = failed_dig
        self.pick = "stone_pickaxe"

    def status(self):
        return {"pos": list(self.pos), "health": self.health, "ready": True, "alive": True, "on_ground": True}

    def inventory(self):
        return {"items": [{"name": self.pick, "count": 1}, {"name": "cobblestone", "count": 10}]}

    def call(self, action, body):
        self.calls.append((action, body))
        if action == "get_block":
            return {"name": self.world.get(tuple(body["pos"]), "stone")}
        if action == "equip":
            return {}
        if action == "dig":
            if self.failed_dig:
                raise ApiError("interrupted", "hurt", context={"by": "hurt"})
            self.world[tuple(body["pos"])] = "air"
            if self.injury:
                self.health -= 2
            return {"dug": "stone"}
        if action == "move_to":
            if not self.phantom_move:
                self.pos = [body["x"], body["y"], body["z"]]
            return {"pos": [body["x"], body["y"], body["z"]]}
        raise AssertionError(action)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)


def test_level_tunnel_advances_at_negative_coordinates_without_claiming_uncollected_material():
    bot = Corridor()
    result = MacroSkills(bot).dig_level_tunnel("east", 2)
    assert result["ok"] is True
    assert result["steps_done"] == 2
    assert result["pos_after"] == [-0.5, 60, -3.5]
    assert len(result["dug"]) == 4
    assert result["gained"] == {}
    digs = [b for a, b in bot.calls if a == "dig"]
    assert digs[0]["pos"] == [-2, 61, -4]  # Head first, floor never excavated.
    assert all(b["collect"] is False and b["stop_on"] == ["hurt", "death"] for b in digs)


@pytest.mark.parametrize("position,name,reason", [
    ((-2, 59, -4), "air", "unsafe_or_unknown_floor"),
    ((-2, 62, -4), "water", "fluid_adjacent"),
    ((-2, 60, -3), "lava", "fluid_adjacent"),
    ((-2, 62, -4), "gravel", "falling_material_adjacent"),
    ((-2, 60, -4), "crafting_table", "unsupported_face_block"),
    ((-2, 60, -4), "iron_ore", "ore_at_face"),
])
def test_preflight_refuses_hazards_structures_or_stops_at_ore_before_dig(position, name, reason):
    bot = Corridor()
    bot.world[position] = name
    result = MacroSkills(bot).dig_level_tunnel("east", 2)
    assert result["stop_reason"] == reason
    assert result["steps_done"] == 0
    assert not any(a in {"dig", "move_to"} for a, _ in bot.calls)


@pytest.mark.parametrize("options,reason", [
    ({"phantom_move": True}, "movement_not_confirmed"),
    ({"injury": True}, "hurt"),
    ({"failed_dig": True}, "api_error"),
])
def test_failed_or_interrupted_action_never_advances_the_next_step(options, reason):
    bot = Corridor(**options)
    result = MacroSkills(bot).dig_level_tunnel("east", 3)
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["steps_done"] == 0
    assert result["stop_reason"] == reason
    if reason in {"hurt", "api_error"}:
        assert len([a for a, _ in bot.calls if a == "dig"]) == 1
        assert not any(a == "move_to" for a, _ in bot.calls)
    else:
        assert len([a for a, _ in bot.calls if a == "move_to"]) == 1


def test_ore_digging_still_requires_correct_pickaxe_tier():
    bot = Corridor()
    bot.pick = "wooden_pickaxe"
    bot.world[(-2, 60, -4)] = "iron_ore"
    result = MacroSkills(bot).dig_level_tunnel("east", 1, stop_on_ore=False)
    assert result["stop_reason"] == "insufficient_pickaxe_tier"
    assert not any(a == "dig" for a, _ in bot.calls)
