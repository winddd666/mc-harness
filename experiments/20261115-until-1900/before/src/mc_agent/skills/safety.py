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


def check_and_stabilize(
    bot: BotSession,
    *,
    emergency_health: float = 6.0,
    eat_food_threshold: int = 16,
) -> dict[str, Any]:
    """Run deterministic L0 reflexes: water float, lava emergency, auto-eat, and lethal health guard."""
    st = bot.status()
    actions: list[str] = []

    if st.get("alive") is False:
        raise SafetyEmergency("bot_dead", st)

    # 1. Water buoyancy reflex (avoid drowning while idle or waiting for LLM)
    in_water = bool(st.get("in_water"))
    submerged = bool(st.get("submerged"))
    oxygen = st.get("oxygen", 300)
    jumping = bool(st.get("jumping"))
    if in_water and (
        submerged
        or not bool(st.get("on_ground"))
        or (isinstance(oxygen, (int, float)) and oxygen < 240)
    ):
        if not jumping:
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
        try:
            inv = bot.inventory()
            if _item_counts(inv).get("water_bucket", 0) > 0:
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

    if isinstance(health, (int, float)) and health <= emergency_health:
        raise SafetyEmergency(f"low_health={health}", st)

    return {
        "ok": True,
        "health": st.get("health"),
        "food": st.get("food"),
        "pos": st.get("pos"),
        "in_water": st.get("in_water"),
        "actions": actions,
    }
