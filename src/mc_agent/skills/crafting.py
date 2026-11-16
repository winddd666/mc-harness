"""Explicit deficits for the two repeatedly misplanned iron-task recipes."""
from __future__ import annotations


_DIAMOND_NOTE = (
    "钻石装备需要已采集入包的 diamond（需用铁镐或更高等级镐挖掘 diamond_ore / deepslate_diamond_ore）。"
)


def material_shortage(item: str, count: int, counts: dict[str, int]) -> dict | None:
    recipes = {
        "crafting_table": {"any_planks": 4},
        "iron_pickaxe": {"iron_ingot": 3, "stick": 2},
        "diamond_pickaxe": {"diamond": 3, "stick": 2},
        "diamond_helmet": {"diamond": 5},
        "diamond_chestplate": {"diamond": 8},
        "diamond_leggings": {"diamond": 7},
        "diamond_boots": {"diamond": 4},
    }
    recipe = recipes.get(item)
    if recipe is None:
        return None  # The server remains authoritative for other recipes.
    required = {name: amount * count for name, amount in recipe.items()}
    available = {name: (sum(amount for key, amount in counts.items() if key.endswith("_planks"))
                        if name == "any_planks" else counts.get(name, 0)) for name in required}
    missing = {name: amount - available[name] for name, amount in required.items() if available[name] < amount}
    if item == "crafting_table":
        note = "any_planks 表示任意种类木板，可混用；不要求某种特定树木。"
    elif item == "iron_pickaxe":
        note = "铁镐需要铁锭；raw_iron 必须先熔炼，圆石或挖掘数不能替代铁锭。"
    else:
        note = _DIAMOND_NOTE
    return {
        "item": item,
        "target_count": count,
        "required_materials": required,
        "available_materials": available,
        "missing_materials": missing,
        "recipe_note": note,
    }


def explain_craft_error(item: str, count: int, error: dict) -> dict:
    if error.get("code") != "missing_ingredients" or not isinstance(error.get("inventory"), dict):
        return error
    plan = material_shortage(item, count, error["inventory"])
    if plan is None:
        return error
    enriched = {**error, **plan}
    if "example_recipe_needs" in enriched:
        enriched["server_recipe_example"] = enriched.pop("example_recipe_needs")
    return enriched
