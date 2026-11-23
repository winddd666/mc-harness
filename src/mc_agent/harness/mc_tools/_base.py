"""Minecraft Agent API v1.3 声明式工具集（对接 nano-agent ToolRegistry / ToolScheduler）。

观察类工具标记为 `concurrency="shared"`（可单轮并发查询）；
动作类工具标记为 `concurrency="exclusive"`（自动串行执行，避免触发 API `busy` 锁冲突）。
业务错误（如缺材料、寻路失败、受伤打断）序列化为 JSON 字符串回填给模型纠错；认证失效或致命冷却抛 RuntimeError 终止。
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass, replace
import json
import math
import re
import threading
import time
from typing import Any, Callable

import httpx

from ...bot import BotSession
from ...errors import ApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ...kb import assess_world_target, build_jit_error_hint, lookup_kb, normalize_dimension, resolve_item_alias
from ...skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier
from ...skills.combat_adapter import run_combat_skill
from ...skills.library import (
    BED_BLOCK_TYPES,
    BLOCK_MIN_PICKAXE_TIER,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    SURFACE_SCOUT_DIRECTIONS,
    TABLE_RECIPES,
    VILLAGE_LANDMARK_BLOCKS,
    VILLAGE_SURVEY_CLUTTER,
    _PICKUP_INTERRUPTION_CODES,
    _SkillBudgetSession,
    _SkillDeadline,
    _StairDeadline,
    _StairSession,
    _best_pickaxe_in_inv,
    _can_fit_item_in_inventory,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _merge_counts,
    _normalize_drop_name,
    best_melee_weapon_in_inv,
    explain_material_craft_error,
    is_passable_sight_block,
    rejected_find_block,
)
from ...skills.safety import SAFE_FOOD_PRIORITY
from ..geofence import VoidGeofence
from ..tool_registry import Parameter, Tool, ToolRegistry
from ..world_memory import WorldMemory, default_memory_path_for_bot

class _ScoutNumberParameter(Parameter):
    """Keep a numeric schema and validate number strings in the scout handler."""

    def __post_init__(self) -> None:
        pass

    def json_schema(self) -> dict[str, Any]:
        return {"type": "number", "description": self.description}


_BLOCK_COLORS: tuple[str, ...] = tuple(name.removesuffix("_bed") for name in BED_BLOCK_TYPES)
_BLOCK_WOODS: tuple[str, ...] = tuple(name.removesuffix("_log") for name in LOG_TYPES)
_FIND_BLOCK_ALIASES: dict[str, tuple[str, ...]] = {
    "bed": BED_BLOCK_TYPES, "beds": BED_BLOCK_TYPES, "*_bed": BED_BLOCK_TYPES,
    "log": LOG_TYPES, "logs": LOG_TYPES,
    "planks": tuple(f"{wood}_planks" for wood in (*_BLOCK_WOODS, "crimson", "warped", "bamboo")),
    "wool": tuple(f"{color}_wool" for color in _BLOCK_COLORS),
    "grass": ("short_grass", "tall_grass"),
    "portal": ("nether_portal",), "nether_rack": ("netherrack",),
    "cave": ("cave_air",), "flowing_water": ("water",), "flowing_lava": ("lava",),
    "cobble": ("cobblestone",),
}


# These r11 inputs are item/entity registry IDs, never block IDs. Other
# unfamiliar names remain version-authoritative and are checked by the API.
_NON_BLOCK_FIND_TYPES = frozenset({"shield", "leather", "sheep"})


def _invalid_find_block_query(ignored: list[str]) -> str:
    return json.dumps({
        "ok": False, "requires_replan": True, "ignored_types": ignored,
        "error": {"code": "bad_args", "message": "没有有效方块类型。示例：mc_find_blocks(types=['oak_log','stone'])；查实体请用 mc_get_entities。"},
    }, ensure_ascii=False)


def _normalize_find_block_types(
    types: list[str] | tuple[str, ...], *, dimension: str | None = None
) -> list[str]:
    """Expand block families; leave registry validation to the server version."""
    nether = str(dimension or "").lower() == "the_nether"
    out: list[str] = []
    for raw in types:
        name = str(raw).strip().lower().removeprefix("minecraft:")
        if not name or name in _NON_BLOCK_FIND_TYPES:
            continue
        if nether and name in {"log", "logs"}:
            # Generic "log" in the nether means the local wood family. Expanding
            # to overworld logs only makes the server reject/empty the query.
            out.extend(NETHER_STEM_TYPES)
        elif nether and name == "planks":
            out.extend(("crimson_planks", "warped_planks"))
        else:
            out.extend(_FIND_BLOCK_ALIASES.get(name, (name,)))
    return list(dict.fromkeys(out))


COMPACT_CORE_TOOLS: tuple[str, ...] = (
    "mc_find_blocks",
    "mc_get_entities",
    "mc_move_to",
    "mc_dig",
    "mc_place",
    "mc_attack",
    "mc_skill_harvest_logs",
    "mc_skill_craft_gear",
    "mc_skill_mine_blocks",
    "mc_skill_smelt",
    "mc_skill_staircase",
    "mc_skill_tunnel",
    "mc_skill_mine_dfs",
    "mc_skill_flee",
    "mc_skill_unstick",
)

_PASSABLE_SIGHT_BLOCKS: set[str] = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "flowing_water",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "torch",
    "wall_torch",
    "vine",
    "snow",
    "leaf_litter",
    "sugar_cane",
    "dandelion",
    "poppy",
    "dead_bush",
    "glass",
    "glass_pane",
}


@dataclass(frozen=True)
class _FailedMoveWaypoint:
    dimension: str
    origin: tuple[float, ...]
    x: float
    z: float
    range_m: float
    expires_at: float

_COMPACT_TOOL_DESC: dict[str, str] = {
    "mc_status": "查位置/血/食/昼夜",
    "mc_inventory": "查背包/护甲/副手",
    "mc_find_blocks": "扫暴露方块",
    "mc_get_block": "查单格方块",
    "mc_get_entities": "扫生物/物品",
    "mc_events": "拉取新事件",
    "mc_advancements": "查进度状态",
    "mc_move_to": "寻路(x,z;禁y)",
    "mc_skill_scout_surface": "主世界寻村或下界搜要塞，连走最多5段，命中或遇险即停",
    "mc_dig": "挖格拾取",
    "mc_place": "放方块",
    "mc_craft": "合成(3x3需桌)",
    "mc_equip": "装备(hand/off-hand/head/torso/legs/feet)",
    "mc_interact": "右键方块/实体",
    "mc_use_item": "吃/用物品",
    "mc_attack": "近战攻击",
    "mc_shoot": "射箭/弩/三叉戟",
    "mc_window_view": "看容器或交易窗",
    "mc_window_act": "操作容器/炉/附魔/交易",
    "mc_skill_container": "容器批量存取",
    "mc_skill_cast_obsidian": "主世界指定岩浆源浇铸黑曜石、回收水桶",
    "mc_skill_build_portal": "一键搭建并进入下界传送门",
    "mc_skill_enchant": "附魔台单次连招",
    "mc_skill_trade": "村民按选项交易",
    "mc_skill_brew": "酿造台投料并等待",
    "mc_skill_anvil": "铁砧合成/改名",
    "mc_skill_smithing": "锻造台升级",
    "mc_skill_stonecutter": "切石机切料",
    "mc_posture": "切换sneak/shield/jump",
    "mc_dismount": "下船/下马",
    "mc_drop": "丢弃物品",
    "mc_chat": "发公屏消息",
    "mc_stop": "停当前动作",
    "mc_control": "底层按键挥砍",
    "mc_look": "转向坐标/实体",
    "mc_advancement_frontier": "查可解锁进度",
    "mc_skill_stabilize": "保命进食浮水",
    "mc_skill_harvest_logs": "自动换斧砍树",
    "mc_skill_craft_gear": "合成任意物品/工具/护甲(自动木板木棍工作台)",
    "mc_skill_mine_blocks": "自动换镐挖露头矿/石",
    "mc_skill_smelt": "自动放炉配燃料烧制",
    "mc_skill_collect_drops": "拾掉落",
    "mc_skill_bind_bed": "走近床设重生点",
    "mc_skill_unstick": "卡死/水体脱困",
    "mc_kb_lookup": "查配方/矿/成就",
    "mc_skill_bunker": "竖井避怪",
    "mc_skill_flee": "无遮挡遇险时封堵矿道或冲刺逃离",
    "mc_skill_combat": "主动战术接战(auto/rush/melee/creeper/pillar/crit/wtap/gap)，受围栏与30s预算保护",
    "mc_skill_staircase": "爬出天坑/垫高(up/pillar)；下井探矿用mine_dfs",
    "mc_skill_tunnel": "定向凿墙平推(隔墙目标/绕水/下界穿墙；盲探矿用mine_dfs)",
    "mc_skill_mine_dfs": "路径压缩探矿DFS/返井口",
}

_SELF_EVIDENT_PARAMS: frozenset[str] = frozenset(
    {
        "x", "y", "z", "entity_id", "radius", "limit", "steps", "query",
        "message", "sneak", "shield", "jump", "forward", "back", "left",
        "right", "sprint", "yaw", "pitch", "ms", "collect", "mature",
        "ignore_hurt", "max_pickups", "target_logs", "stop_on_ore",
        "interval_ms", "timeout_ms", "hit", "hit_interval_ms", "stop_on",
        "wait", "charge_ms", "goal_count", "target_y",
        "item", "count", "range", "max_hits", "block", "block_types",
        "input_item", "fuel_item", "slot", "equip_slot", "types", "distance",
        "action", "option", "get_item", "times", "left_item", "right_item",
        "text", "take", "template_item", "base_item", "addition_item",
        "bottles", "wait", "ingredient_item",
    }
)

_COMPACT_PARAM_DESC: dict[tuple[str, str], str] = {
    ("mc_get_entities", "types"): "hostile/animal/player/item",
    ("mc_advancements", "status"): "completed/not_started",
    ("mc_shoot", "weapon"): "bow/crossbow/trident",
    ("mc_window_act", "ops"): "[{op,item,count,to/from}]",
    ("mc_kb_lookup", "category"): "all/recipe/ore/mob/advancement",
    ("mc_skill_staircase", "direction"): "north/south/east/west",
    ("mc_skill_staircase", "vertical"): "down/up/pillar",
    ("mc_skill_tunnel", "direction"): "north/south/east/west",
    ("mc_skill_mine_dfs", "target"): "iron/diamond/coal/gold",
    ("mc_skill_mine_dfs", "action"): "explore/return_collar/climb_out(自动爬回地表)/reset(换点重开)",
}


def _compactify_tool(tool: Tool) -> Tool:
    new_params: dict[str, Parameter] = {}
    for pname, p in tool.parameters.items():
        if (tool.name, pname) in _COMPACT_PARAM_DESC:
            pdesc = _COMPACT_PARAM_DESC[(tool.name, pname)]
        elif pname in _SELF_EVIDENT_PARAMS:
            pdesc = ""
        else:
            pdesc = p.description[:26]
        new_params[pname] = replace(p, description=pdesc)
    return Tool(
        name=tool.name,
        description=_COMPACT_TOOL_DESC.get(tool.name, tool.description[:40]),
        parameters=new_params,
        handler=tool.handler,
        concurrency=tool.concurrency,
        interrupt=tool.interrupt,
        compact_schema=True,
    )


def _build_window_target(
    block: str | None,
    entity_id: int | None,
    x: int | None,
    y: int | None,
    z: int | None,
) -> dict[str, Any] | str:
    if entity_id is not None:
        return {"entity_id": entity_id}
    if x is not None and y is not None and z is not None:
        return {"pos": [x, y, z]}
    if block:
        return {"block": block}
    return json.dumps(
        {"ok": False, "error": {"code": "bad_args", "message": "需要提供 block、entity_id 或 (x, y, z) 之一"}},
        ensure_ascii=False,
    )
