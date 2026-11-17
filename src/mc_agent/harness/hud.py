"""紧凑型实时世界状态面板 WorldHUD（严格控制在 ~220–280 tokens 内，按轮短暂注入请求末尾）。"""

from __future__ import annotations

import math
from typing import Any

from .geofence import VoidGeofence
from ..kb import assess_world_target, normalize_dimension
from ..skills.library import assess_hostile_occlusion, inspect_local_enclosure
from ..skills.safety import SAFE_FOOD_PRIORITY

# Minecraft 1.21.11 常用工具/装备最大耐久度表
TOOL_MAX_DURABILITY: dict[str, int] = {
    "wooden_pickaxe": 59,
    "wooden_axe": 59,
    "wooden_shovel": 59,
    "wooden_sword": 59,
    "wooden_hoe": 59,
    "stone_pickaxe": 131,
    "stone_axe": 131,
    "stone_shovel": 131,
    "stone_sword": 131,
    "stone_hoe": 131,
    "iron_pickaxe": 250,
    "iron_axe": 250,
    "iron_shovel": 250,
    "iron_sword": 250,
    "iron_hoe": 250,
    "golden_pickaxe": 32,
    "golden_axe": 32,
    "golden_shovel": 32,
    "golden_sword": 32,
    "golden_hoe": 32,
    "diamond_pickaxe": 1561,
    "diamond_axe": 1561,
    "diamond_shovel": 1561,
    "diamond_sword": 1561,
    "diamond_hoe": 1561,
    "netherite_pickaxe": 2031,
    "netherite_axe": 2031,
    "netherite_shovel": 2031,
    "netherite_sword": 2031,
    "netherite_hoe": 2031,
    "shield": 336,
    "bow": 384,
    "crossbow": 465,
    "flint_and_steel": 64,
    "shears": 238,
    "fishing_rod": 64,
}

# 按任务价值排序的 16m 暴露方块优先级（数值越小越优先展示）
_BLOCK_PRIORITY: dict[str, int] = {
    "diamond_ore": 1,
    "deepslate_diamond_ore": 1,
    "ancient_debris": 1,
    "iron_ore": 2,
    "deepslate_iron_ore": 2,
    "gold_ore": 3,
    "deepslate_gold_ore": 3,
    "nether_gold_ore": 3,
    "redstone_ore": 4,
    "deepslate_redstone_ore": 4,
    "lapis_ore": 4,
    "deepslate_lapis_ore": 4,
    "coal_ore": 5,
    "deepslate_coal_ore": 5,
    "copper_ore": 6,
    "deepslate_copper_ore": 6,
    "crafting_table": 7,
    "furnace": 7,
    "blast_furnace": 7,
    "chest": 8,
    "enchanting_table": 8,
    "lodestone": 8,
    "nether_portal": 8,
    "obsidian": 9,
    "lava": 10,
    "flowing_lava": 10,
    "water": 11,
    "stone": 12,
    "cobblestone": 12,
    "deepslate": 12,
    "wheat": 13,
    "carrots": 13,
    "potatoes": 13,
    "sugar_cane": 14,
}


def _block_rank(name: str) -> tuple[int, str]:
    clean = str(name or "").removeprefix("minecraft:")
    if clean in _BLOCK_PRIORITY:
        return (_BLOCK_PRIORITY[clean], clean)
    if clean.endswith("_log") or clean.endswith("_stem"):
        return (5, clean)
    if clean.endswith("_bed"):
        return (7, clean)
    if clean.endswith("_leaves"):
        return (90, clean)
    return (30, clean)


def _extract_durability_suffix(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").removeprefix("minecraft:")
    max_d = TOOL_MAX_DURABILITY.get(name)
    rem = item.get("durability_remaining")
    if isinstance(rem, (int, float)):
        return f"(剩{int(rem)})"
    used = item.get("durability_used", item.get("damage"))
    item_max = item.get("max_durability", max_d)
    if isinstance(used, (int, float)) and isinstance(item_max, (int, float)) and item_max > 0:
        return f"(剩{max(0, int(item_max) - int(used))})"
    dur = item.get("durability")
    if isinstance(dur, (int, float)) and isinstance(item_max, (int, float)) and item_max > 0:
        # 部分服务端返回的 durability 即剩余比率或剩余值
        if dur <= 1.0 and dur >= 0.0 and item_max > 1:
            return f"(剩{int(round(dur * item_max))})"
        return f"(剩{int(dur)})"
    if max_d is not None:
        return f"(满{max_d})"
    return ""


def _summarize_inventory(inv: dict[str, Any]) -> tuple[dict[str, int], str]:
    counts: dict[str, int] = {}
    tool_tags: dict[str, list[str]] = {}
    for row in inv.get("items") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").removeprefix("minecraft:")
        cnt = int(row.get("count") or 0)
        if not name or cnt <= 0:
            continue
        counts[name] = counts.get(name, 0) + cnt
        if name in TOOL_MAX_DURABILITY:
            tag = _extract_durability_suffix(row)
            if tag:
                tool_tags.setdefault(name, []).append(tag)

    parts: list[str] = []
    shown = 0
    # 关键装备与资源优先展示，避免杂物刷爆 HUD
    ordered_names = sorted(
        counts,
        key=lambda n: (
            0
            if (
                n in TOOL_MAX_DURABILITY
                or n.endswith(("_log", "_planks", "_ore"))
                or n in {
                    "stick", "crafting_table", "furnace", "cobblestone", "cobbled_deepslate",
                    "raw_iron", "iron_ingot", "coal", "charcoal", "diamond", "gold_ingot",
                    "redstone", "lapis_lazuli", "obsidian", "water_bucket", "lava_bucket",
                    "bucket", "flint_and_steel", "compass", "lodestone", "torch",
                }
            )
            else 1,
            -counts[n],
            n,
        ),
    )
    for name in ordered_names:
        if shown >= 14:
            rem_kinds = len(ordered_names) - shown
            parts.append(f"+{rem_kinds}种")
            break
        cnt = counts[name]
        suffix = ""
        if name in tool_tags:
            suffix = tool_tags[name][0]
        parts.append(f"{name}:{cnt}{suffix}" if cnt > 1 or not suffix else f"{name}{suffix}")
        shown += 1

    offhand = inv.get("offhand") or {}
    off_name = str(offhand.get("name") or "").removeprefix("minecraft:") if isinstance(offhand, dict) else ""
    armor = inv.get("armor") or {}
    armor_list = []
    if isinstance(armor, dict):
        for slot in ("head", "torso", "legs", "feet"):
            piece = armor.get(slot)
            if isinstance(piece, dict) and piece.get("name"):
                armor_list.append(str(piece["name"]).removeprefix("minecraft:"))

    meta_bits: list[str] = []
    if off_name and off_name != "air":
        meta_bits.append(f"副手={off_name}")
    if armor_list:
        meta_bits.append("护甲=" + ",".join(armor_list))
    if isinstance(inv.get("empty_slots"), int):
        meta_bits.append(f"空槽={inv['empty_slots']}")

    summary = (", ".join(parts) if parts else "空背包")
    if meta_bits:
        summary += " | " + " ".join(meta_bits)
    return counts, summary


def compute_goal_gap_hint(
    counts: dict[str, int],
    *,
    task: str = "",
    target_advancement: str | None = None,
    bot_y: int | None = None,
    has_nearby_table: bool = False,
    dimension: str = "overworld",
    biome: str = "",
) -> str:
    """结合当前背包数量与生态先验确定性计算 1.21.11 工具链与高价值目标材料缺口。"""
    logs = sum(cnt for name, cnt in counts.items() if name.endswith("_log") or name.endswith("_stem"))
    planks = sum(cnt for name, cnt in counts.items() if name.endswith("_planks"))
    avail_planks = planks + logs * 4
    sticks = counts.get("stick", 0)
    cobble = counts.get("cobblestone", 0) + counts.get("cobbled_deepslate", 0)
    raw_iron = counts.get("raw_iron", 0)
    iron = counts.get("iron_ingot", 0)
    coal = counts.get("coal", 0) + counts.get("charcoal", 0)
    diamonds = counts.get("diamond", 0)
    obsidian = counts.get("obsidian", 0)

    has_wood_pick = counts.get("wooden_pickaxe", 0) > 0
    has_stone_pick = counts.get("stone_pickaxe", 0) > 0
    has_iron_pick = counts.get("iron_pickaxe", 0) > 0
    has_diamond_pick = counts.get("diamond_pickaxe", 0) > 0
    has_table = counts.get("crafting_table", 0) > 0 or has_nearby_table

    dim = normalize_dimension(dimension)
    adv = (target_advancement or "").removeprefix("minecraft:")
    task_low = (task or "").lower()

    def _wood_shortage_hint(context_prefix: str) -> str | None:
        if bot_y is None:
            return None
        wood_check = assess_world_target("oak_log", dimension=dim, y=bot_y, biome=biome, mode="find")
        if wood_check.get("available"):
            return None
        if wood_check.get("verdict") == "wrong_dimension" and wood_check.get("substitutes"):
            subs = "/".join(wood_check["substitutes"][:2])
            return f"{context_prefix}；当前位于{dim}无主世界树林，改采 {subs} 可直接合成木板/木棍"
        if wood_check.get("direction_needed") == "up":
            return (
                f"{context_prefix}；"
                f"当前在地下(y={bot_y})无树，严禁在地下盲挖找木头！请先调mc_skill_staircase(vertical='up',steps=16)回地表再调mc_skill_harvest_logs砍树"
            )
        return f"{context_prefix}；{wood_check['hint']}"

    if not (has_wood_pick or has_stone_pick or has_iron_pick or has_diamond_pick):
        if avail_planks >= 7 and cobble >= 3:
            return "木板与圆石均已充足，可先工作台+木镐或直接调mc_skill_craft_gear合木镐后合石镐"
        if avail_planks >= 7:
            return f"可合成材料够(等效木板{avail_planks})，缺wooden_pickaxe，可直接mc_skill_craft_gear('wooden_pickaxe')"
        wood_hint = _wood_shortage_hint(f"缺原木/木镐(原木={logs},木板={planks},棍={sticks})")
        if wood_hint is not None:
            return wood_hint
        return f"缺原木/木镐(当前原木={logs},木板={planks},棍={sticks})，先mc_skill_harvest_logs(target_logs=4)"

    if not (has_stone_pick or has_iron_pick or has_diamond_pick):
        if cobble < 3:
            return f"有木镐，还差圆石{3 - cobble}个即可合stone_pickaxe(建议多挖到11圆石同时合furnace)"
        need_planks = (0 if sticks >= 2 else 2) + (0 if has_table else 4)
        if avail_planks >= need_planks:
            return "已有>=3圆石与足够木材，可直接mc_skill_craft_gear('stone_pickaxe')"
        wood_hint = _wood_shortage_hint(f"圆石充足({cobble})但缺木材合石镐/工作台(原木={logs},木板={planks},棍={sticks})")
        if wood_hint is not None:
            return wood_hint
        return f"圆石充足({cobble})但缺木材合石镐/工作台(原木={logs},木板={planks},棍={sticks})，请先调mc_skill_harvest_logs(target_logs=4)砍树补木头"

    if adv == "adventure/use_lodestone" or "lodestone" in task_low or "磁石" in task:
        need_stone = max(0, 8 - counts.get("chiseled_stone_bricks", 0))
        need_iron = max(0, 5 - iron)  # lodestone 1 + compass 4
        need_red = max(0, 1 - counts.get("redstone", 0))
        return (
            f"1.21.11磁石方案=1铁锭+8錾制石砖(chiseled_stone_bricks)，指南针=4铁锭+1红石；"
            f"当前铁锭={iron}(缺{need_iron},生铁={raw_iron}),红石={counts.get('redstone', 0)}(缺{need_red}),錾制石砖缺{need_stone}；摆出磁石后手持compass调mc_interact"
        )

    if adv == "story/shiny_gear" or "shiny_gear" in task_low or "钻石套" in task or "diamond_boots" in task_low:
        base_msg = (
            f"shiny_gear最省方案仅需4颗diamond合diamond_boots(当前diamond={diamonds},缺{max(0, 4 - diamonds)})；"
            f"若尚无iron_pickaxe则先凑3铁锭合铁镐再去Y=-54深层挖钻石"
        )
        if bot_y is not None:
            d_chk = assess_world_target("diamond_ore", dimension=dim, y=bot_y, biome=biome, mode="find")
            if not d_chk.get("available"):
                if d_chk.get("verdict") == "wrong_dimension":
                    return f"{base_msg}；当前({dim})无钻石矿需回主世界"
                if has_iron_pick and d_chk.get("direction_needed") == "down":
                    return f"{base_msg}；当前y={bot_y}高于钻石层(Y=-64~16)需下探至Y=-54"
        return base_msg

    if "钻石" in task or "diamond" in task_low:
        if not (has_iron_pick or has_diamond_pick):
            return (
                f"挖钻石需先备iron_pickaxe(现有diamond={diamonds},铁锭={iron},生铁={raw_iron},煤={coal})；"
                "先调mc_skill_mine_dfs(target='iron')凑3铁烧锭合铁镐"
            )
        return (
            f"已备铁镐+(当前diamond={diamonds})；直接调mc_skill_mine_dfs(target='diamond',target_y=-54)去深层Y=-54探钻，禁在浅层Y=8~16盲挖"
        )

    if adv == "story/enchant_item" or "enchant" in task_low or "附魔" in task:
        return (
            f"附魔台=2diamond(现有{diamonds})+4obsidian(现有{obsidian})+1book(现有{counts.get('book', 0)})，"
            f"附魔消耗lapis_lazuli(现有{counts.get('lapis_lazuli', 0)})"
        )

    if not (has_iron_pick or has_diamond_pick):
        if iron >= 3:
            return f"已有{iron}铁锭，直接mc_skill_craft_gear('iron_pickaxe')提升采集等级至3"
        if iron + raw_iron >= 3 and raw_iron > 0:
            return f"已有{raw_iron}生铁(铁锭={iron},煤/木炭={coal})，直接mc_skill_smelt('raw_iron', count={raw_iron})"
        return (
            f"当前最高stone_pickaxe，升级iron_pickaxe需3铁锭(现有铁锭={iron},生铁={raw_iron},煤={coal})；"
            "优先采露头iron_ore或调mc_skill_mine_dfs(target='iron')下井探铁"
        )

    return (
        f"当前已具备铁镐及以上(钻石={diamonds},铁锭={iron},黑曜石={obsidian},原木={logs},圆石={cobble})，"
        "直接推进目标或缺材补充"
    )


def build_world_hud(
    toolset: Any,
    *,
    task: str = "",
    target_advancement: str | None = None,
    geofence: VoidGeofence | None = None,
    failed_coords: list[str] | None = None,
) -> str:
    """用 3 个快速只读请求拼装 <300 token 的实时 [实时HUD] 文本。"""
    st = toolset.bot.status()
    if hasattr(toolset, "_update_status_cache"):
        toolset._update_status_cache(st)
    inv = toolset.bot.inventory()
    found = toolset.bot.call("find_blocks", {"radius": 16, "limit": 24})

    pos = st.get("pos") or [0.0, 64.0, 0.0]
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
    bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

    dim = str(st.get("dimension") or "overworld").removeprefix("minecraft:")
    biome = str(st.get("biome") or "?")
    day_tag = "白昼" if st.get("is_day") is True else ("夜晚" if st.get("is_day") is False else "?")
    hp = st.get("health")
    food = st.get("food")
    in_water = bool(st.get("in_water") or st.get("submerged"))

    flags: list[str] = []
    if in_water:
        flags.append(f"水中(oxy={st.get('oxygen')},禁水下强挖,先mc_skill_unstick)")
    if st.get("in_lava"):
        flags.append("岩浆危险!")
    if st.get("blocking"):
        flags.append("举盾中")
    if st.get("sneaking"):
        flags.append("潜行中")

    blocks = (found or {}).get("blocks") or []
    wm = getattr(toolset, "world_memory", None)
    if wm is not None and blocks:
        try:
            wm.observe_blocks(blocks, dim=dim)
        except Exception:
            pass
    counts, inv_summary = _summarize_inventory(inv)
    # 检测高空树冠站位（脚下紧邻树叶且高度>=66）
    near_leaves = sum(
        1
        for b in blocks
        if isinstance(b, dict)
        and str(b.get("name") or "").endswith("_leaves")
        and float(b.get("dist", 99)) <= 2.4
        and isinstance(b.get("pos"), list)
        and len(b["pos"]) >= 2
        and int(b["pos"][1]) <= by
    )
    if not in_water and by >= 66 and near_leaves >= 2:
        flags.append("树冠高处(防坠落,可用mc_skill_unstick安全下树)")

    gf = geofence or getattr(toolset, "geofence", None)
    if gf is not None and gf.enabled:
        rem = gf.margin_remaining(px, pz)
        flags.append(f"岛界余={rem:.1f}m")

    nh = st.get("nearest_hostile")
    has_nh = isinstance(nh, dict) and bool(nh.get("name"))
    block_cache: dict[tuple[int, int, int], str] = {}
    enclosure: dict[str, Any] = {}

    underground_rock = False
    surface_markers = False
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bname = str(b.get("name") or "").removeprefix("minecraft:")
        if (
            bname in {"stone", "deepslate", "cobbled_deepslate", "granite", "diorite", "andesite", "tuff"}
            or bname.endswith("_ore")
        ):
            underground_rock = True
        if bname in {"grass_block", "podzol", "sand", "red_sand", "mycelium"} or bname.endswith(("_leaves", "_log")):
            surface_markers = True

    if has_nh or (underground_rock and not surface_markers and by < 68):
        enclosure = inspect_local_enclosure(toolset.bot, [px, py, pz], block_cache=block_cache)
        if enclosure.get("structure") in {"sealed_bunker", "carved_shaft", "open_cave"}:
            flags.append(f"地形={enclosure['label_zh']}")

    if has_nh:
        threat = assess_hostile_occlusion(
            toolset.bot,
            [px, py, pz],
            radius=20,
            fallback_nearest=nh,
            block_cache=block_cache,
        )
        if threat["unoccluded"]:
            u0 = threat["unoccluded"][0]
            uid = u0.get("id")
            extra_cnt = f"+{len(threat['unoccluded']) - 1}怪" if len(threat["unoccluded"]) > 1 else ""
            flags.append(
                f"近敌(无遮挡!)={u0['name']}{f'#{uid}' if uid is not None else ''}@{u0['dist']}m({u0['bearing']},dy={u0['dy']:+.0f}{extra_cnt})"
            )
        elif threat["complete"] and threat["occluded"]:
            o0 = threat["occluded"][0]
            bname = (o0.get("blocker") or {}).get("name") or "墙"
            if enclosure.get("roofed") is True:
                flags.append(f"隔墙怪={o0['name']}@{o0['dist']}m({o0['bearing']},dy={o0['dy']:+.0f},{bname}阻隔,勿弃坑)")
            else:
                flags.append(f"遮挡怪={o0['name']}@{o0['dist']}m({o0['bearing']},dy={o0['dy']:+.0f},{bname}暂挡,露天注意绕行)")
        else:
            hid = nh.get("id")
            flags.append(f"近敌={nh.get('name')}{f'#{hid}' if hid is not None else ''}@{nh.get('dist')}m")

    if isinstance(hp, (int, float)) and hp <= 10.0:
        has_edible = any(
            counts.get(f, 0) > 0
            for f in (*SAFE_FOOD_PRIORITY, "golden_apple", "enchanted_golden_apple", "rotten_flesh")
        )
        if not has_edible:
            flags.append("包内无食物(禁调mc_use_item空吃)")

    line1 = (
        f"位置=[{bx},{by},{bz}]({dim}/{biome},{day_tag}) | "
        f"HP={hp} Food={food}"
        + (f" | 状态:{';'.join(flags)}" if flags else "")
    )

    nearby_stations: list[str] = []
    for station in ("crafting_table", "furnace", "blast_furnace"):
        match = next(
            (
                b
                for b in blocks
                if isinstance(b, dict)
                and str(b.get("name") or "").removeprefix("minecraft:") == station
                and float(b.get("dist", 99)) <= 5.5
            ),
            None,
        )
        if match and match.get("pos"):
            nearby_stations.append(f"{station}@{match['pos']}")

    line2 = f"背包: {inv_summary}" + (f" | 4格设施:{','.join(nearby_stations)}" if nearby_stations else "")

    by_type: dict[str, dict[str, Any]] = {}
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        bname = str(blk.get("name") or "").removeprefix("minecraft:")
        if not bname or bname.endswith("_leaves"):
            continue
        dist = float(blk.get("dist", 99.0))
        prev = by_type.get(bname)
        if prev is None or dist < float(prev.get("dist", 99.0)):
            by_type[bname] = blk

    sorted_types = sorted(by_type.items(), key=lambda kv: (_block_rank(kv[0]), float(kv[1].get("dist", 99.0))))
    res_bits = [
        f"{name}@{blk.get('pos')}({float(blk.get('dist', 0)):.1f}m)"
        for name, blk in sorted_types[:9]
    ]
    line3 = "16m暴露: " + (", ".join(res_bits) if res_bits else "暂无明显资源(可阶梯下探或换向寻路)")

    gap_hint = compute_goal_gap_hint(
        counts,
        task=task,
        target_advancement=target_advancement,
        bot_y=by,
        has_nearby_table=any(s.startswith("crafting_table@") for s in nearby_stations),
        dimension=dim,
        biome=str(st.get("biome") or ""),
    )
    line4 = f"缺口提示: {gap_hint}"

    lines = ["[实时HUD（刚采样，勿再空转查status/inventory）]", line1, line2, line3, line4]
    if wm is not None:
        try:
            mem_line = wm.format_hud_line(
                dim=dim,
                pos=(px, py, pz),
                current_biome=biome,
                local_visible_names=set(by_type.keys()),
                max_chars=84,
            )
            if mem_line:
                lines.append(mem_line)
        except Exception:
            pass
    if failed_coords:
        lines.append("近期失败坐标(禁止重试或沿序列递推): " + ", ".join(failed_coords[-5:]))
    return "\n".join(lines)
