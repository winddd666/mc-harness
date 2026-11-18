"""Unit tests for the no-strategy window/GUI atomic wrappers in MacroSkills."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from mc_agent.errors import ApiError
from mc_agent.harness.mc_tools import McToolset
from mc_agent.skills.library import MacroSkills


class FakeBot:
    def __init__(self, responses: dict[str, Any] | None = None, *, status: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.status_data = status or {"dimension": "overworld"}
        self.calls: list[tuple[str, Any]] = []

    def call(self, path: str, body: Any = None, *, method: str = "POST", **_: Any) -> dict[str, Any]:
        self.calls.append((path, copy.deepcopy(body)))
        response = self.responses.get(path, {})
        if isinstance(response, list):
            response = response.pop(0)
        if callable(response):
            response = response(body)
        return copy.deepcopy(response)

    def status(self) -> dict[str, Any]:
        return dict(self.status_data)


class FakeMemory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def update_container_from_window(self, window_result: dict[str, Any], **kwargs: Any) -> bool:
        self.calls.append({"window_result": copy.deepcopy(window_result), **kwargs})
        return True


@pytest.fixture()
def memory() -> FakeMemory:
    return FakeMemory()


def _skills(responses: dict[str, Any], memory: FakeMemory) -> tuple[MacroSkills, FakeBot]:
    bot = FakeBot(responses)
    skills = MacroSkills(bot)  # type: ignore[arg-type]
    skills.world_memory = memory
    return skills, bot


def test_container_deposit_batches_put_and_syncs_world_memory(memory: FakeMemory) -> None:
    inner = {"window": "generic_9x3", "sections": {"container": [{"name": "cobblestone", "count": 64}]}}
    skills, bot = _skills({"window/act": {"done": [{"op": "put"}], "window": inner}}, memory)

    res = skills.container_deposit(
        [{"item": "cobblestone", "count": 64}, {"item": "iron_ingot", "count": 5}],
        x=10,
        y=64,
        z=-3,
    )

    assert res["ok"] is True
    assert res["action"] == "put"
    assert res["container_pos"] == [10, 64, -3]
    assert res["memory_synced"] is True
    assert bot.calls[0][0] == "window/act"
    assert bot.calls[0][1]["target"] == {"pos": [10, 64, -3]}
    assert bot.calls[0][1]["ops"] == [
        {"op": "put", "item": "cobblestone", "count": 64, "to": "container"},
        {"op": "put", "item": "iron_ingot", "count": 5, "to": "container"},
    ]
    assert len(memory.calls) == 1
    assert memory.calls[0]["window_result"] == inner
    assert memory.calls[0]["fallback_pos"] == [10, 64, -3]
    assert memory.calls[0]["dim"] == "overworld"


def test_container_withdraw_batches_take_and_reports_api_error(memory: FakeMemory) -> None:
    skills, bot = _skills({"window/act": {"done": [], "window": {"window": "generic_9x3", "sections": {"container": []}}}}, memory)
    res = skills.container_withdraw({"oak_log": 12}, block="barrel")
    assert res["ok"] is True
    assert bot.calls[0][1]["target"] == {"block": "barrel"}
    assert bot.calls[0][1]["ops"] == [{"op": "take", "item": "oak_log", "count": 12, "from": "container"}]

    # ApiError is converted into a structured failure instead of escaping the wrapper.
    from mc_agent.errors import ApiError

    def boom(_body: Any) -> dict[str, Any]:
        raise ApiError(code="section_full", message="container is full")

    bad_skills, _ = _skills({"window/act": boom}, memory)
    err = bad_skills.container_deposit({"stone": 1}, x=0, y=0, z=0)
    assert err["ok"] is False
    assert err["error"] == "section_full"


def test_enchant_combo_order_and_default_lapis(memory: FakeMemory) -> None:
    skills, bot = _skills({"window/act": {"done": [], "window": {"window": "enchantment"}}}, memory)
    res = skills.enchant_item_once("iron_sword", 2, x=1, y=2, z=3)
    assert res["ok"] is True
    assert bot.calls[0][1]["ops"] == [
        {"op": "put", "item": "iron_sword", "to": "item"},
        {"op": "put", "item": "lapis_lazuli", "count": 3, "to": "lapis"},
        {"op": "select", "option": 2},
        {"op": "take", "from": "item"},
    ]


def test_merchant_trade_reads_options_then_selects_by_get_item(memory: FakeMemory) -> None:
    options = [
        {"index": 0, "give": [{"name": "emerald", "count": 3}], "get": {"name": "bread", "count": 6}, "available": True},
        {"index": 1, "give": [{"name": "emerald", "count": 1}], "get": {"name": "arrow", "count": 16}, "available": False},
    ]
    skills, bot = _skills(
        {
            "window/view": {"window": "merchant", "options": options, "sections": {}},
            "window/act": {"done": [{"op": "select", "times": 2}], "window": {"window": "merchant"}},
        },
        memory,
    )
    res = skills.merchant_trade(entity_id=812, get_item="bread", times=2)
    assert res["ok"] is True
    assert res["option"] == 0
    assert res["trade"]["get"]["name"] == "bread"
    assert bot.calls[0] == ("window/view", {"target": {"entity_id": 812}})
    assert bot.calls[1][0] == "window/act"
    assert bot.calls[1][1]["ops"] == [{"op": "select", "option": 0, "times": 2}]


def test_merchant_trade_no_match_returns_options(memory: FakeMemory) -> None:
    skills, _ = _skills({"window/view": {"options": [{"index": 0, "get": {"name": "bread"}}]}}, memory)
    res = skills.merchant_trade(entity_id=9, get_item="diamond")
    assert res["ok"] is False
    assert res["error"] == "no_matching_trade"
    assert res["options"]


def test_brew_loads_three_bottles_and_polls_brew_time(
    memory: FakeMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda *_: None)
    views = [
        {"window": "brewing_stand", "sections": {"bottles": []}, "info": {"brew_time_left": 10.0}},
        {
            "window": "brewing_stand",
            "sections": {"bottles": [{"name": "potion", "potion": "swiftness", "count": 1}]},
            "info": {"brew_time_left": 0},
        },
    ]
    skills, bot = _skills(
        {"window/act": {"done": [{"op": "put"}], "window": {"window": "brewing_stand"}}, "window/view": views},
        memory,
    )
    res = skills.brew_once(
        "blaze_powder",
        bottles=[
            {"item": "potion", "potion": "water"},
            {"item": "potion", "potion": "water"},
            {"item": "potion", "potion": "water"},
        ],
        fuel_item="blaze_powder",
        x=0,
        y=64,
        z=0,
    )
    assert res["ok"] is True
    assert [c[0] for c in bot.calls] == ["window/act", "window/view", "window/view"]
    assert bot.calls[0][1]["ops"] == [
        {"op": "put", "item": "potion", "count": 1, "to": "bottles", "potion": "water", "slot": 0},
        {"op": "put", "item": "potion", "count": 1, "to": "bottles", "potion": "water", "slot": 1},
        {"op": "put", "item": "potion", "count": 1, "to": "bottles", "potion": "water", "slot": 2},
        {"op": "put", "item": "blaze_powder", "count": 1, "to": "ingredient"},
        {"op": "put", "item": "blaze_powder", "count": 1, "to": "fuel"},
    ]
    assert res["info"] == {"brew_time_left": 0}


def test_anvil_smithing_and_stonecutter_ops(memory: FakeMemory) -> None:
    skills, bot = _skills({"window/act": {"done": [], "window": {"window": "anvil"}}}, memory)

    skills.anvil_operate("iron_sword", right_item="diamond", text="Agent Blade", x=1, y=64, z=1)
    assert bot.calls[-1][1]["ops"] == [
        {"op": "put", "item": "iron_sword", "to": "left"},
        {"op": "put", "item": "diamond", "to": "right"},
        {"op": "text", "value": "Agent Blade"},
        {"op": "take", "from": "output"},
    ]

    skills.smithing_apply("netherite_upgrade_smithing_template", "diamond_chestplate", "netherite_ingot", x=1, y=64, z=1)
    assert bot.calls[-1][1]["ops"] == [
        {"op": "put", "item": "netherite_upgrade_smithing_template", "to": "template"},
        {"op": "put", "item": "diamond_chestplate", "to": "base"},
        {"op": "put", "item": "netherite_ingot", "to": "addition"},
        {"op": "take", "from": "output"},
    ]

    skills.stonecutter_cut("stone", 3, count=8, x=1, y=64, z=1)
    assert bot.calls[-1][1]["ops"] == [
        {"op": "put", "item": "stone", "count": 8, "to": "input"},
        {"op": "select", "option": 3},
        {"op": "take", "from": "output"},
    ]


def test_atomic_gui_tools_registered_in_full_profile_but_not_compact() -> None:
    toolset = McToolset(FakeBot())  # type: ignore[arg-type]
    full = {schema["function"]["name"] for schema in toolset.registry().schemas()}
    compact = {schema["function"]["name"] for schema in toolset.registry(profile="compact").schemas()}
    new_tools = {
        "mc_skill_container",
        "mc_skill_enchant",
        "mc_skill_trade",
        "mc_skill_brew",
        "mc_skill_anvil",
        "mc_skill_smithing",
        "mc_skill_stonecutter",
    }
    assert new_tools <= full
    assert not (new_tools & compact)


class EnchantBot(FakeBot):
    """Fake bot that replays inventory snapshots around an enchant call."""

    def __init__(self, inventory_states: list[list[dict[str, Any]]]) -> None:
        super().__init__({})
        self.inventory_states = list(inventory_states)
        self.act_calls = 0
        self.disconnects = 0
        self.connects = 0

    def call(self, path: str, body: Any = None, *, method: str = "POST", **_: Any) -> dict[str, Any]:
        if path == "window/act":
            self.act_calls += 1
            return {"done": [{"op": "select", "selected": 2}], "window": {"window": "enchantment"}}
        if path == "inventory":
            items = self.inventory_states.pop(0) if self.inventory_states else []
            return {"items": items}
        return super().call(path, body, method=method)

    def disconnect(self) -> dict[str, Any]:
        self.disconnects += 1
        return {"ok": True}

    def connect(self, **_: Any) -> dict[str, Any]:
        self.connects += 1
        return {"ok": True}

    def wait_until_ready(self, **_: Any) -> dict[str, Any]:
        return {"ready": True}


def _enchant_toolset(bot: EnchantBot) -> McToolset:
    toolset = McToolset(bot)  # type: ignore[arg-type]
    toolset.skills.world_memory = FakeMemory()
    return toolset


def test_enchant_verification_reconnects_but_does_not_double_enchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.sleep", lambda *_: None)
    clean = [{"name": "iron_sword", "count": 1, "slot": 36}]
    enchanted = [{"name": "iron_sword", "count": 1, "slot": 36, "enchantments": [{"name": "sharpness", "level": 1}]}]
    bot = EnchantBot([clean, clean, [*clean, *enchanted]])
    result = json.loads(_enchant_toolset(bot).mc_skill_enchant("iron_sword", 2, block="enchanting_table"))

    assert result["ok"] is True
    assert result["inventory_verified"] is True
    assert result["reconnected_for_sync"] is True
    assert bot.act_calls == 1  # reconnect alone recovered the stale snapshot; no retry
    assert bot.disconnects == 1 and bot.connects == 1


def test_enchant_verification_retries_once_after_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.sleep", lambda *_: None)
    clean = [{"name": "iron_sword", "count": 1, "slot": 36}]
    enchanted = [{"name": "iron_sword", "count": 1, "slot": 36, "enchantments": [{"name": "sharpness", "level": 1}]}]
    bot = EnchantBot([clean, clean, clean, [*clean, *enchanted]])
    result = json.loads(_enchant_toolset(bot).mc_skill_enchant("iron_sword", 2, block="enchanting_table"))

    assert result["ok"] is True
    assert result["inventory_verified"] is True
    assert result["retried_after_reconnect"] is True
    assert bot.act_calls == 2
    assert bot.disconnects == 1 and bot.connects == 1


def test_enchant_reset_session_falls_back_to_30s_cooldown_on_strict_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.sleep", lambda *_: None)
    clean = [{"name": "iron_sword", "count": 1, "slot": 36}]
    enchanted = [{"name": "iron_sword", "count": 1, "slot": 36, "enchantments": [{"name": "sharpness", "level": 1}]}]
    bot = EnchantBot([clean, clean, [*clean, *enchanted]])
    connect_flags: list[bool] = []

    def strict_connect(*, respect_reconnect_interval: bool = True, **_: Any) -> dict[str, Any]:
        connect_flags.append(respect_reconnect_interval)
        if not respect_reconnect_interval:
            raise ApiError("reconnect_cooldown", "must wait 30s before reconnecting")
        bot.connects += 1
        return {"ok": True}

    monkeypatch.setattr(bot, "connect", strict_connect)
    result = json.loads(_enchant_toolset(bot).mc_skill_enchant("iron_sword", 2, block="enchanting_table"))

    assert result["ok"] is True
    assert result["inventory_verified"] is True
    assert connect_flags == [False, True]
    assert bot.connects == 1


def test_compact_mode_unlocks_safe_gui_skills_for_tasks_and_advancements() -> None:
    from mc_agent.harness.agent import _infer_extra_tools_for_task
    from mc_agent.harness.curriculum import _infer_extra_tools_for_advancement

    assert "mc_skill_enchant" in _infer_extra_tools_for_task("去附魔台给铁剑附魔")
    assert "mc_skill_trade" in _infer_extra_tools_for_task("找村民交易绿宝石")
    assert "mc_skill_brew" in _infer_extra_tools_for_task("用酿造台酿造药水")
    assert "mc_skill_container" in _infer_extra_tools_for_task("把多余物资存进箱子")
    assert "mc_skill_smithing" in _infer_extra_tools_for_task("用锻造台升级下界合金镐")

    assert "mc_skill_enchant" in _infer_extra_tools_for_advancement("minecraft:story/enchant_item", "附魔台附魔")
    assert "mc_skill_trade" in _infer_extra_tools_for_advancement("minecraft:adventure/trade", "与村民交易")
    assert "mc_skill_brew" in _infer_extra_tools_for_advancement("minecraft:nether/brew_potion", "酿造药水")
    assert "mc_skill_smithing" in _infer_extra_tools_for_advancement(
        "minecraft:adventure/trim_with_any_armor_pattern", "锻造台纹饰"
    )


def test_container_without_xyz_resolves_pos_and_anvil_diagnoses_no_output_and_remote_disconnect_retries() -> None:
    import httpx
    from mc_agent.bot import BotSession

    # 1) Container put with ONLY block='chest' (no x/y/z) resolves nearby chest pos and syncs WorldMemory!
    bot = FakeBot(
        {
            "find_blocks": {"blocks": [{"name": "chest", "pos": [14, 64, -3], "dist": 2.1}]},
            "window/act": {
                "done": [{"op": "put", "put": "iron_ingot", "count": 4, "to": "container"}],
                "window": {
                    "window": "generic_9x3",
                    "target": "chest",
                    "sections": {"container": [{"name": "iron_ingot", "count": 4}]},
                },
            },
        }
    )
    ts = McToolset(bot)  # type: ignore[arg-type]
    res_c = json.loads(ts.mc_skill_container([{"item": "iron_ingot", "count": 4}], action="put", block="chest"))
    assert res_c["ok"] is True
    assert res_c["memory_synced"] is True
    assert res_c["container_pos"] == [14, 64, -3]
    assert ts.world_memory.containers[("overworld", 14, 64, -3)]["items"] == {"iron_ingot": 4}

    # 2) Anvil `left_enchanted`/`right_enchanted` filters + `no_output` full-durability diagnostic hint (114143 bug)
    def _anvil_fail(_body: Any) -> dict[str, Any]:
        raise ApiError(
            "no_output",
            "nothing to take from output",
            context={
                "window": {
                    "window": "anvil",
                    "sections": {
                        "left": [{"name": "iron_sword", "count": 1, "enchantments": [{"name": "smite", "level": 1}]}],
                        "right": [{"name": "iron_sword", "count": 1, "durability_used": 100}],
                        "output": [],
                    },
                    "info": {"repair_cost": 0},
                }
            },
        )

    bot_anvil = FakeBot({"window/act": _anvil_fail})
    ts_anvil = McToolset(bot_anvil)  # type: ignore[arg-type]
    res_a = json.loads(ts_anvil.mc_skill_anvil("iron_sword", right_item="iron_sword", left_enchanted=True))
    assert res_a["ok"] is False
    assert "满耐久" in res_a.get("hint", "")
    sent_ops = bot_anvil.calls[0][1]["ops"]
    assert sent_ops[0].get("enchanted") is True

    # 3) BotSession.call retries once on idle keep-alive RemoteProtocolError (120453 check #27 bug)
    attempts = {"n": 0}

    def _flaky_keepalive(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=req)
        return httpx.Response(200, json={"ok": True, "result": {"ready": True, "pos": [0, 64, 0]}})

    with BotSession("tok", transport=httpx.MockTransport(_flaky_keepalive)) as real_bot:
        st = real_bot.status()
        assert st["ready"] is True
        assert attempts["n"] == 2



