"""A nearby surface hint and a jump-apex response must not prove arrival."""
import pytest

from mc_agent.skills.library import MacroSkills


class StairBot:
    def __init__(self, phantom=False):
        self.pos = [0.5, 60, 0.5]
        self.phantom = phantom
        self.dug = set()

    def status(self):
        return {"pos": list(self.pos), "health": 20, "food": 20, "on_ground": True}

    def inventory(self):
        return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 8}]}

    def call(self, action, body):
        if action == "get_block":
            pos = tuple(body["pos"])
            return {"name": "air" if pos in self.dug or pos == (2, 64, 0) else "stone"}
        if action == "dig":
            self.dug.add(tuple(body["pos"]))
            return {"dug": "stone"}
        if action == "move_to":
            target = [float(body["x"]), float(body["y"]), float(body["z"])]
            if not self.phantom:
                self.pos = target
            return {"pos": target}  # Simulates an optimistic/transient backend result.
        if action == "find_blocks":
            return {"blocks": [{"name": "grass_block", "pos": [2, 65, 0], "dist": 4}]}
        if action == "equip":
            return {}
        raise AssertionError(action)


def test_nearby_grass_above_ceiling_is_not_arrival(monkeypatch):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    result = MacroSkills(StairBot()).dig_staircase(direction="east", steps=2, vertical="up", stop_on_ore=False)
    assert result["steps_done"] == 2
    assert result["stop_reason"] == "completed"
    assert "surface_evidence" not in result
    assert result["pos_after"][1] == 62


@pytest.mark.parametrize("vertical", ["up", "down"])
def test_step_counter_uses_settled_status_not_transient_move_result(monkeypatch, vertical):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    result = MacroSkills(StairBot(phantom=True)).dig_staircase(direction="east", steps=2, vertical=vertical, stop_on_ore=False)
    assert result["ok"] is False
    assert result["steps_done"] == 0
    assert result["stop_reason"] == "movement_not_confirmed"
    assert result["requires_replan"] is True
    assert result["pos_after"] == [0.5, 60, 0.5]
