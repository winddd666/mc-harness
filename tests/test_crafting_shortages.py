import json

from mc_agent.errors import ApiError
from mc_agent.harness.mc_tools import McToolset
from mc_agent.skills.library import MacroSkills
from mc_agent.skills.crafting import material_shortage


class Ingredients:
    def __init__(self, counts):
        self.counts = counts
        self.calls = []

    def inventory(self):
        return {"items": [{"name": k, "count": v} for k, v in self.counts.items()]}

    def call(self, action, body):
        self.calls.append((action, body))
        raise AssertionError("No world mutation is needed to explain this shortage.")


def test_table_deficit_uses_all_plank_stacks_without_requiring_cherry_wood():
    bot = Ingredients({"birch_planks": 1, "cobblestone": 121})
    result = MacroSkills(bot).craft_gear("crafting_table")
    assert result["ok"] is False
    assert result["required_materials"] == {"any_planks": 4}
    assert result["missing_materials"] == {"any_planks": 3}
    assert bot.calls == []
    mixed = material_shortage("crafting_table", 1, {"oak_planks": 2, "birch_planks": 2})
    assert mixed["missing_materials"] == {}


def test_raw_iron_is_not_ingots_and_no_table_or_stick_crafting_precedes_missing_iron():
    bot = Ingredients({"raw_iron": 3, "oak_log": 2})
    result = MacroSkills(bot).craft_gear("iron_pickaxe")
    assert result["missing_materials"] == {"iron_ingot": 3, "stick": 2}
    assert result["inventory"] == bot.counts
    assert bot.calls == []


def test_atomic_error_labels_arbitrary_recipe_example_and_reports_actual_deficit():
    error = ApiError("missing_ingredients", "server placed no result", context={
        "example_recipe_needs": {"cherry_planks": 4}, "inventory": {"birch_planks": 1}})
    result = json.loads(McToolset._format_api_error("craft", {"item": "crafting_table", "count": 1}, error))["error"]
    assert result["missing_materials"] == {"any_planks": 3}
    assert result["server_recipe_example"] == {"cherry_planks": 4}
    assert "example_recipe_needs" not in result
