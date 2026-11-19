"""L0 deterministic survival guardian: lava rescue, water tread, auto-eat, low-HP emergency stop."""

from __future__ import annotations

from typing import Any

from ..bot import BotSession
from ..errors import ApiError

# Ordered roughly from highest saturation/hunger recovery to basic safe fallback food.
SAFE_FOOD_PRIORITY: tuple[str, ...] = (
    "golden_carrot",
    "cooked_beef",
    "cooked_porkchop",
    "cooked_mutton",
    "cooked_salmon",
    "cooked_chicken",
    "cooked_cod",
    "cooked_rabbit",
    "baked_potato",
    "bread",
    "pumpkin_pie",
    "apple",
    "carrot",
    "melon_slice",
    "sweet_berries",
    "glow_berries",
    "dried_kelp",
    "potato",
    "beetroot",
    "beef",
    "porkchop",
    "mutton",
    "salmon",
    "cod",
    "rabbit",
)


ARMOR_SLOT_PRIORITIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "head",
        "head",
        (
            "netherite_helmet",
            "diamond_helmet",
            "iron_helmet",
            "turtle_helmet",
            "golden_helmet",
            "chainmail_helmet",
            "leather_helmet",
        ),
    ),
    (
        "chest",
        "torso",
        (
            "netherite_chestplate",
            "diamond_chestplate",
            "iron_chestplate",
            "golden_chestplate",
            "chainmail_chestplate",
            "leather_chestplate",
        ),
    ),
    (
        "legs",
        "legs",
        (
            "netherite_leggings",
            "diamond_leggings",
            "iron_leggings",
            "golden_leggings",
            "chainmail_leggings",
            "leather_leggings",
        ),
    ),
    (
        "feet",
        "feet",
        (
            "netherite_boots",
            "diamond_boots",
            "iron_boots",
            "golden_boots",
            "chainmail_boots",
            "leather_boots",
        ),
    ),
)


class SafetyEmergency(RuntimeError):
    """Raised when the bot is in imminent lethal danger and should disconnect immediately."""

    def __init__(self, reason: str, status: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status or {}


def _item_counts(inv: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in inv.get("items") or []:
        name = item.get("name")
        cnt = int(item.get("count") or 0)
        if name and cnt > 0:
            counts[name] = counts.get(name, 0) + cnt
    return counts


def _pick_best_food(counts: dict[str, int]) -> str | None:
    for food in SAFE_FOOD_PRIORITY:
        if counts.get(food, 0) > 0:
            return food
    return None


def prepare_water_shield(
    bot: BotSession,
    st: dict[str, Any],
    inv: dict[str, Any] | None = None,
) -> list[str]:
    """Equip and raise off-hand shield (keeping jump=True in water) when shield and threat exist."""
    if st.get("blocking"):
        return []
    nh = st.get("nearest_hostile")
    if inv is None and not (isinstance(nh, dict) and nh.get("name")):
        return []
    if inv is None:
        try:
            inv = bot.inventory()
        except ApiError:
            return []
    offhand = inv.get("offhand") if isinstance(inv, dict) else None
    offhand_name = str((offhand or {}).get("name") or "").removeprefix("minecraft:")
    counts = _item_counts(inv) if isinstance(inv, dict) else {}
    bag_shield = sum(cnt for name, cnt in counts.items() if str(name).removeprefix("minecraft:") == "shield")
    if offhand_name != "shield" and bag_shield <= 0:
        return []

    taken: list[str] = []
    if offhand_name != "shield":
        try:
            bot.call("equip", {"item": "shield", "slot": "off-hand"})
            taken.append("equip_shield")
        except ApiError:
            return taken
    if isinstance(nh, dict) and nh.get("id") is not None:
        try:
            bot.call("look", {"entity_id": nh["id"]})
        except ApiError:
            pass
    try:
        bot.call(
            "posture",
            {
                "sneak": bool(st.get("sneaking")),
                "shield": True,
                "jump": bool(st.get("in_water")),
            },
        )
        taken.append("posture_water_shield")
    except ApiError:
        pass
    return taken


def auto_equip_survival_gear(
    bot: Any,
    inv: dict[str, Any] | None = None,
    *,
    allow_armor: bool = True,
    allow_shield: bool = True,
) -> list[str]:
    """Equip highest-tier armor and off-hand shield from bag when those slots report empty/lower tier."""
    if inv is None:
        try:
            inv = bot.inventory()
        except Exception:
            return []
    if not isinstance(inv, dict):
        return []

    raw_counts = _item_counts(inv)
    counts = {str(k).removeprefix("minecraft:"): int(v) for k, v in raw_counts.items()}
    equipped: list[str] = []

    armor_map = inv.get("armor")
    if allow_armor and isinstance(armor_map, dict):
        for inv_key, equip_slot, order in ARMOR_SLOT_PRIORITIES:
            cur_piece = armor_map.get(inv_key)
            cur_name = str((cur_piece or {}).get("name") or "").removeprefix("minecraft:")
            cur_rank = order.index(cur_name) if cur_name in order else len(order)
            best_idx = next((idx for idx, item in enumerate(order) if counts.get(item, 0) > 0), len(order))
            if best_idx < cur_rank:
                best_item = order[best_idx]
                try:
                    bot.call("equip", {"item": best_item, "slot": equip_slot})
                    equipped.append(f"equip:{best_item}")
                except ApiError:
                    pass

    if allow_shield and "offhand" in inv:
        off_name = str((inv.get("offhand") or {}).get("name") or "").removeprefix("minecraft:")
        if off_name != "shield" and counts.get("shield", 0) > 0:
            try:
                bot.call("equip", {"item": "shield", "slot": "off-hand"})
                equipped.append("equip:shield")
            except ApiError:
                pass
    return equipped


def check_and_stabilize(
    bot: BotSession,
    *,
    emergency_health: float = 6.0,
    eat_food_threshold: int = 16,
    raise_on_low_health: bool = True,
) -> dict[str, Any]:
    """Run deterministic L0 reflexes: water float, lava emergency, auto-eat, and lethal health guard."""
    st = bot.status()
    actions: list[str] = []

    if st.get("alive") is False:
        raise SafetyEmergency("bot_dead", st)

    # 1. Water buoyancy & threat shield reflex (avoid drowning or unshielded trident hits)
    in_water = bool(st.get("in_water"))
    submerged = bool(st.get("submerged"))
    oxygen = st.get("oxygen", 300)
    jumping = bool(st.get("jumping"))
    nh = st.get("nearest_hostile")
    if in_water and isinstance(nh, dict) and nh.get("name"):
        actions.extend(prepare_water_shield(bot, st))
    if in_water and (
        submerged
        or not bool(st.get("on_ground"))
        or (isinstance(oxygen, (int, float)) and oxygen < 240)
    ):
        if not jumping and "posture_water_shield" not in actions:
            bot.call(
                "posture",
                {
                    "sneak": bool(st.get("sneaking")),
                    "shield": bool(st.get("blocking")),
                    "jump": True,
                },
            )
            actions.append("posture_water_float")
    elif not in_water and bool(st.get("on_ground")) and jumping:
        bot.call(
            "posture",
            {
                "sneak": bool(st.get("sneaking")),
                "shield": bool(st.get("blocking")),
                "jump": False,
            },
        )
        actions.append("posture_jump_reset")

    # 2. Lava first-aid attempt with water_bucket
    if st.get("in_lava"):
        dim = str(st.get("dimension") or "").lower()
        try:
            inv = bot.inventory()
            if "nether" not in dim and _item_counts(inv).get("water_bucket", 0) > 0:
                bot.call("use_item", {"item": "water_bucket"})
                actions.append("lava_water_bucket")
                st = bot.status()
        except ApiError:
            pass
        if st.get("in_lava"):
            raise SafetyEmergency("in_lava", st)

    # 3. Auto-eat when hungry or injured
    health = st.get("health")
    food = st.get("food")
    needs_food = (
        (isinstance(food, (int, float)) and food <= eat_food_threshold)
        or (
            isinstance(health, (int, float))
            and health < 18
            and isinstance(food, (int, float))
            and food < 20
        )
    )
    if needs_food:
        inv = bot.inventory()
        best_food = _pick_best_food(_item_counts(inv))
        if best_food:
            try:
                bot.call("use_item", {"item": best_food})
                actions.append(f"ate:{best_food}")
                st = bot.status()
                health = st.get("health")
                food = st.get("food")
            except ApiError as exc:
                actions.append(f"eat_failed:{best_food}:{exc.code}")

    low_health_warning = isinstance(health, (int, float)) and health <= emergency_health
    if low_health_warning and bool(st.get("in_water")):
        if isinstance(st.get("nearest_hostile"), dict) and st["nearest_hostile"].get("name") and "posture_water_shield" not in actions:
            actions.extend(prepare_water_shield(bot, st))
        if not bool(st.get("jumping")) and "posture_water_float" not in actions and "posture_water_shield" not in actions:
            try:
                bot.call(
                    "posture",
                    {
                        "sneak": bool(st.get("sneaking")),
                        "shield": bool(st.get("blocking")),
                        "jump": True,
                    },
                )
                actions.append("posture_water_float")
            except ApiError:
                pass
    if low_health_warning and raise_on_low_health:
        raise SafetyEmergency(f"low_health={health}", st)

    return {
        "ok": True,
        "health": st.get("health"),
        "food": st.get("food"),
        "pos": st.get("pos"),
        "in_water": st.get("in_water"),
        "low_health_warning": low_health_warning,
        "actions": actions,
    }
