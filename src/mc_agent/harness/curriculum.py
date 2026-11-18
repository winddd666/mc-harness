"""8 小时竞赛课程督导器（依据 RULES.md 分值表 + 动态工具投影 + 2-Strike 冷却黑名单）。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import json
import math
from pathlib import Path
import time
from typing import Any, Callable

from ..bot import BotSession
from ..errors import ApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ..skills import compute_advancement_frontier
from .events import JsonlEvents
from .geofence import VoidGeofence

# 严格对齐 /home/xzq/projects/mc/RULES.md 的 99 个计分成就（不含 21 个不计分项与 5 个 0 分根节点）
RULES_ADVANCEMENT_POINTS: dict[str, int] = {
    # story (15 项)
    "minecraft:story/cure_zombie_villager": 60,
    "minecraft:story/enter_the_end": 60,
    "minecraft:story/follow_ender_eye": 60,
    "minecraft:story/shiny_gear": 60,
    "minecraft:story/enchant_item": 30,
    "minecraft:story/enter_the_nether": 30,
    "minecraft:story/deflect_arrow": 15,
    "minecraft:story/form_obsidian": 15,
    "minecraft:story/lava_bucket": 15,
    "minecraft:story/mine_diamond": 15,
    "minecraft:story/iron_tools": 5,
    "minecraft:story/mine_stone": 5,
    "minecraft:story/obtain_armor": 5,
    "minecraft:story/smelt_iron": 5,
    "minecraft:story/upgrade_tools": 5,
    # nether (20 项；fast_travel / return_to_sender 不计分)
    "minecraft:nether/all_effects": 100,
    "minecraft:nether/all_potions": 100,
    "minecraft:nether/create_full_beacon": 100,
    "minecraft:nether/netherite_armor": 100,
    "minecraft:nether/summon_wither": 100,
    "minecraft:nether/uneasy_alliance": 100,
    "minecraft:nether/create_beacon": 60,
    "minecraft:nether/get_wither_skull": 60,
    "minecraft:nether/obtain_ancient_debris": 60,
    "minecraft:nether/ride_strider_in_overworld_lava": 60,
    "minecraft:nether/charge_respawn_anchor": 30,
    "minecraft:nether/explore_nether": 30,
    "minecraft:nether/find_bastion": 30,
    "minecraft:nether/find_fortress": 30,
    "minecraft:nether/loot_bastion": 30,
    "minecraft:nether/obtain_blaze_rod": 30,
    "minecraft:nether/ride_strider": 30,
    "minecraft:nether/brew_potion": 15,
    "minecraft:nether/obtain_crying_obsidian": 15,
    "minecraft:nether/distract_piglin": 5,
    # end (4 项)
    "minecraft:end/elytra": 60,
    "minecraft:end/find_end_city": 60,
    "minecraft:end/levitate": 60,
    "minecraft:end/enter_end_gateway": 30,
    # adventure (35 项)
    "minecraft:adventure/arbalistic": 100,
    "minecraft:adventure/hero_of_the_village": 60,
    "minecraft:adventure/revaulting": 60,
    "minecraft:adventure/throw_trident": 60,
    "minecraft:adventure/totem_of_undying": 60,
    "minecraft:adventure/trade_at_world_height": 60,
    "minecraft:adventure/two_birds_one_arrow": 60,
    "minecraft:adventure/craft_decorated_pot_using_only_sherds": 30,
    "minecraft:adventure/heart_transplanter": 30,
    "minecraft:adventure/kill_mob_near_sculk_catalyst": 30,
    "minecraft:adventure/play_jukebox_in_meadows": 30,
    "minecraft:adventure/sniper_duel": 30,
    "minecraft:adventure/summon_iron_golem": 30,
    "minecraft:adventure/trim_with_any_armor_pattern": 30,
    "minecraft:adventure/under_lock_and_key": 30,
    "minecraft:adventure/voluntary_exile": 30,
    "minecraft:adventure/who_needs_rockets": 30,
    "minecraft:adventure/avoid_vibration": 15,
    "minecraft:adventure/brush_armadillo": 15,
    "minecraft:adventure/bullseye": 15,
    "minecraft:adventure/crafters_crafting_crafters": 15,
    "minecraft:adventure/lighten_up": 15,
    "minecraft:adventure/minecraft_trials_edition": 15,
    "minecraft:adventure/ol_betsy": 15,
    "minecraft:adventure/read_power_of_chiseled_bookshelf": 15,
    "minecraft:adventure/salvage_sherd": 15,
    "minecraft:adventure/spyglass_at_ghast": 15,
    "minecraft:adventure/spyglass_at_parrot": 15,
    "minecraft:adventure/trade": 15,
    "minecraft:adventure/use_lodestone": 15,
    "minecraft:adventure/walk_on_powder_snow_with_leather_boots": 15,
    "minecraft:adventure/whos_the_pillager_now": 15,
    "minecraft:adventure/kill_a_mob": 5,
    "minecraft:adventure/shoot_arrow": 5,
    "minecraft:adventure/sleep_in_bed": 5,
    # husbandry (25 项)
    "minecraft:husbandry/feed_snifflet": 60,
    "minecraft:husbandry/froglights": 60,
    "minecraft:husbandry/leash_all_frog_variants": 60,
    "minecraft:husbandry/obtain_netherite_hoe": 60,
    "minecraft:husbandry/allay_deliver_cake_to_note_block": 30,
    "minecraft:husbandry/allay_deliver_item_to_player": 30,
    "minecraft:husbandry/axolotl_in_a_bucket": 30,
    "minecraft:husbandry/kill_axolotl_target": 30,
    "minecraft:husbandry/make_a_sign_glow": 30,
    "minecraft:husbandry/obtain_sniffer_egg": 30,
    "minecraft:husbandry/plant_any_sniffer_seed": 30,
    "minecraft:husbandry/remove_wolf_armor": 30,
    "minecraft:husbandry/repair_wolf_armor": 30,
    "minecraft:husbandry/ride_a_boat_with_a_goat": 30,
    "minecraft:husbandry/silk_touch_nest": 30,
    "minecraft:husbandry/tadpole_in_a_bucket": 30,
    "minecraft:husbandry/fishy_business": 15,
    "minecraft:husbandry/place_dried_ghast_in_water": 15,
    "minecraft:husbandry/safely_harvest_honey": 15,
    "minecraft:husbandry/tactical_fishing": 15,
    "minecraft:husbandry/tame_an_animal": 15,
    "minecraft:husbandry/wax_on": 15,
    "minecraft:husbandry/breed_an_animal": 5,
    "minecraft:husbandry/plant_seed": 5,
    "minecraft:husbandry/wax_off": 5,
}

# 45 个“无条件计分”（不依赖特定随机遗迹/群系）成就
UNCONDITIONAL_ADVANCEMENTS: frozenset[str] = frozenset(
    {
        "minecraft:story/shiny_gear",
        "minecraft:story/enchant_item",
        "minecraft:story/enter_the_nether",
        "minecraft:story/deflect_arrow",
        "minecraft:story/form_obsidian",
        "minecraft:story/lava_bucket",
        "minecraft:story/mine_diamond",
        "minecraft:story/iron_tools",
        "minecraft:story/mine_stone",
        "minecraft:story/obtain_armor",
        "minecraft:story/smelt_iron",
        "minecraft:story/upgrade_tools",
        "minecraft:nether/uneasy_alliance",
        "minecraft:nether/obtain_ancient_debris",
        "minecraft:nether/charge_respawn_anchor",
        "minecraft:nether/ride_strider",
        "minecraft:nether/obtain_crying_obsidian",
        "minecraft:nether/distract_piglin",
        "minecraft:end/elytra",
        "minecraft:end/find_end_city",
        "minecraft:end/levitate",
        "minecraft:end/enter_end_gateway",
        "minecraft:adventure/arbalistic",
        "minecraft:adventure/throw_trident",
        "minecraft:adventure/two_birds_one_arrow",
        "minecraft:adventure/sniper_duel",
        "minecraft:adventure/summon_iron_golem",
        "minecraft:adventure/trim_with_any_armor_pattern",
        "minecraft:adventure/voluntary_exile",
        "minecraft:adventure/bullseye",
        "minecraft:adventure/crafters_crafting_crafters",
        "minecraft:adventure/ol_betsy",
        "minecraft:adventure/read_power_of_chiseled_bookshelf",
        "minecraft:adventure/spyglass_at_ghast",
        "minecraft:adventure/use_lodestone",
        "minecraft:adventure/whos_the_pillager_now",
        "minecraft:adventure/kill_a_mob",
        "minecraft:adventure/shoot_arrow",
        "minecraft:adventure/sleep_in_bed",
        "minecraft:husbandry/make_a_sign_glow",
        "minecraft:husbandry/fishy_business",
        "minecraft:husbandry/tactical_fishing",
        "minecraft:husbandry/tame_an_animal",
        "minecraft:husbandry/breed_an_animal",
        "minecraft:husbandry/plant_seed",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class CurriculumTarget:
    key: str
    title: str
    points: int
    tier: int  # 0=工具基建, 1=主世界高ROI稳拿, 2=钻石链, 3=下界末地, 4=其余frontier兜底
    task: str
    extra_tools: tuple[str, ...] = ()
    advancement_id: str | None = None


# 优先级剧本库：先稳基建（石镐->铁镐），再抢高ROI主世界项，再下 Y=-54 拿 diamond_boots(60) + enchant_item(30)
CURRICULUM_PLAYBOOKS: dict[str, CurriculumTarget] = {
    "bootstrap_stone_age": CurriculumTarget(
        key="bootstrap_stone_age",
        title="石器基建：原木/工作台/木镐/石镐",
        points=10,
        tier=0,
        task=(
            "确保完成石器基建：0) 若包内已有 cobblestone>=3 且有木棍/木板或工作台，直接就地调 mc_skill_craft_gear('stone_pickaxe')，切勿在地下去砍树或重做木镐；"
            "1) 若在水中先调 mc_skill_unstick 上岸；"
            "2) 仅在木料不足时用 mc_skill_harvest_logs 凑足至少 4 块原木；"
            "3) 合工作台并用 mc_skill_craft_gear('wooden_pickaxe') 合木镐；"
            "4) 挖掘至少 11 块圆石并用 mc_skill_craft_gear('stone_pickaxe') 与 stone_sword/furnace 完成石器工具。"
        ),
        advancement_id="minecraft:story/upgrade_tools",
    ),
    "bootstrap_iron_age": CurriculumTarget(
        key="bootstrap_iron_age",
        title="铁器基建：采集煤铁并合出 iron_pickaxe + shield",
        points=10,
        tier=0,
        task=(
            "将装备升级至铁器：0) 若尚无 *_sword/*_axe 且圆石>=2、木棍>=1 且(旁有crafting_table或包内有原木/木板)，先调 mc_skill_craft_gear('stone_sword') 防身（若已在地下探矿穴且无木头，勿为合剑弃坑回地表，直接继续采矿）；"
            "1) 用石镐采集至少 4 块 coal_ore 与 4 块 iron_ore（若地表无露头，在干燥地面调 mc_skill_staircase(..., vertical='down', stop_on_ore=True) 下探；若矿石够不着，按 approach_hint 指引方向下探或 mc_skill_tunnel 开矿道接近；若地下阶梯下探 viable_directions=[]，按 hint 用 mc_skill_tunnel 水平绕开或 mc_skill_staircase(vertical='up', steps=4) 退回上层分支下探）；"
            "2) 调 mc_skill_smelt('raw_iron', count=4) 熔炼出至少 4 块铁锭；"
            "3) 调 mc_skill_craft_gear('iron_pickaxe') 与 mc_skill_craft_gear('shield', equip_slot='off-hand')。"
        ),
        advancement_id="minecraft:story/iron_tools",
    ),
    "minecraft:story/shiny_gear": CurriculumTarget(
        key="minecraft:story/shiny_gear",
        title="60分高性价比：4颗钻石合 diamond_boots 触发 Shiny Gear",
        points=60,
        tier=1,
        task=(
            "冲刺 story/mine_diamond(15分) + story/shiny_gear(60分)："
            "1) 确认持有 iron_pickaxe；2) 调 mc_skill_staircase 下探至 Y=-54 深层，扫描并采集至少 4 颗 diamond_ore（优先保命与避开岩浆）；"
            "3) 收集掉落物后调 mc_skill_craft_gear('diamond_boots', equip_slot='feet') 穿戴钻石靴子即可立即解锁 60 分成就！"
        ),
        advancement_id="minecraft:story/shiny_gear",
    ),
    "minecraft:story/mine_diamond": CurriculumTarget(
        key="minecraft:story/mine_diamond",
        title="15分：Y=-54 深层挖掘首颗钻石",
        points=15,
        tier=2,
        task=(
            "持有 iron_pickaxe 下探至 Y=-54 附近寻找并挖掘 diamond_ore，确保掉落物进入背包解锁 story/mine_diamond。"
        ),
        advancement_id="minecraft:story/mine_diamond",
    ),
    "minecraft:adventure/use_lodestone": CurriculumTarget(
        key="minecraft:adventure/use_lodestone",
        title="15分：1.21.11 便宜磁石合成+指南针绑定",
        points=15,
        tier=1,
        task=(
            "解锁 adventure/use_lodestone(15分)：在 1.21.11 中磁石(lodestone)仅需 1 铁锭 + 8 个錾制石砖(chiseled_stone_bricks)，指南针(compass)需 4 铁锭 + 1 红石；"
            "先收集 8 圆石烧成 stone 再合 stone_bricks -> stone_brick_slab -> chiseled_stone_bricks，"
            "与 5 铁锭、1 红石合出 lodestone 与 compass，放下 lodestone 后手持 compass 调 mc_interact(x,y,z,item='compass')。"
        ),
        extra_tools=("mc_interact", "mc_kb_lookup"),
        advancement_id="minecraft:adventure/use_lodestone",
    ),
    "minecraft:story/lava_bucket": CurriculumTarget(
        key="minecraft:story/lava_bucket",
        title="15分：铁桶装岩浆",
        points=15,
        tier=1,
        task=(
            "解锁 story/lava_bucket(15分)：确保有 3 铁锭合 bucket，寻找附近露头或地下 lava（先用 mc_find_blocks(types=['lava'], radius=48)），"
            "走到岩浆旁安全位置手持 bucket 调 mc_interact(x,y,z,item='bucket') 装取 lava_bucket。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:story/lava_bucket",
    ),
    "minecraft:story/form_obsidian": CurriculumTarget(
        key="minecraft:story/form_obsidian",
        title="15分：水桶浇岩浆生成并采集黑曜石",
        points=15,
        tier=1,
        task=(
            "解锁 story/form_obsidian(15分)：备好 water_bucket（或用 3 铁锭合 bucket 对水源 mc_interact），"
            "若背包有 diamond_pickaxe 可直接挖暴露 obsidian；否则找到静止岩浆池边缘倒水生成 obsidian 并想办法采集入手。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:story/form_obsidian",
    ),
    "minecraft:husbandry/make_a_sign_glow": CurriculumTarget(
        key="minecraft:husbandry/make_a_sign_glow",
        title="30分：发光墨囊涂亮木告示牌",
        points=30,
        tier=1,
        task=(
            "解锁 husbandry/make_a_sign_glow(30分)：寻找地下水池或海沟中的 glow_squid 击杀拾取 glow_ink_sac，"
            "合成并放置任意木告示牌(如 oak_sign)，手持 glow_ink_sac 对告示牌调 mc_interact(x,y,z,item='glow_ink_sac')。"
        ),
        extra_tools=("mc_interact", "mc_attack"),
        advancement_id="minecraft:husbandry/make_a_sign_glow",
    ),
    "minecraft:husbandry/tactical_fishing": CurriculumTarget(
        key="minecraft:husbandry/tactical_fishing",
        title="15分：水桶装鱼",
        points=15,
        tier=1,
        task=(
            "解锁 husbandry/tactical_fishing(15分)：备好 water_bucket，扫描水域中的 cod/salmon/tropical_fish 实体，走近后手持 water_bucket 调 mc_interact(entity_id=..., item='water_bucket')。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:husbandry/tactical_fishing",
    ),
    "minecraft:adventure/crafters_crafting_crafters": CurriculumTarget(
        key="minecraft:adventure/crafters_crafting_crafters",
        title="15分：合成器套娃合成合成器",
        points=15,
        tier=1,
        task=(
            "解锁 adventure/crafters_crafting_crafters(15分)：准备 2 份 crafter 原料（每份需 5 iron_ingot + 1 crafting_table + 1 redstone + 2 dropper），"
            "放置第 1 个 crafter 并用 mc_window_act 摆入合成配方、接红石脉冲激活触发产出。"
        ),
        extra_tools=("mc_interact", "mc_window_view", "mc_window_act", "mc_kb_lookup"),
        advancement_id="minecraft:adventure/crafters_crafting_crafters",
    ),
    "minecraft:adventure/ol_betsy": CurriculumTarget(
        key="minecraft:adventure/ol_betsy",
        title="15分：制作弩并装填发射",
        points=15,
        tier=1,
        task=(
            "解锁 adventure/ol_betsy(15分)：收集 3 stick + 2 string + 1 iron_ingot + 1 tripwire_hook 合 crossbow 与箭矢(arrow)，"
            "用 mc_shoot(weapon='crossbow', ...) 瞄准附近实体或方块装填发射。"
        ),
        extra_tools=("mc_shoot", "mc_use_item", "mc_attack"),
        advancement_id="minecraft:adventure/ol_betsy",
    ),
    "minecraft:story/deflect_arrow": CurriculumTarget(
        key="minecraft:story/deflect_arrow",
        title="15分：举盾弹反骷髅箭矢",
        points=15,
        tier=1,
        task=(
            "解锁 story/deflect_arrow(15分)：确认副手已装备 shield，寻找附近 skeleton 面向其朝向，用 mc_posture(shield=True) 举盾等待弹反箭矢。"
        ),
        extra_tools=("mc_posture", "mc_attack", "mc_look"),
        advancement_id="minecraft:story/deflect_arrow",
    ),
    "minecraft:story/obtain_armor": CurriculumTarget(
        key="minecraft:story/obtain_armor",
        title="5分：制作并穿戴任一铁护甲",
        points=5,
        tier=2,
        task="用现有铁锭调 mc_skill_craft_gear('iron_boots', equip_slot='feet')（仅需4铁锭），立得 story/obtain_armor。",
        advancement_id="minecraft:story/obtain_armor",
    ),
    "minecraft:adventure/sleep_in_bed": CurriculumTarget(
        key="minecraft:adventure/sleep_in_bed",
        title="5分：做床并绑定/入睡",
        points=5,
        tier=2,
        task="收集 3 羊毛+3木板合 white_bed 放置，调 mc_skill_bind_bed。",
        extra_tools=("mc_skill_bind_bed", "mc_interact", "mc_attack"),
        advancement_id="minecraft:adventure/sleep_in_bed",
    ),
    "minecraft:adventure/kill_a_mob": CurriculumTarget(
        key="minecraft:adventure/kill_a_mob",
        title="5分：击杀任意敌对生物",
        points=5,
        tier=2,
        task="装备最高近战剑/斧与盾，扫描附近敌对生物（僵尸/蜘蛛/骷髅）并安全击杀，注意低血量进掩体恢复。",
        extra_tools=("mc_attack", "mc_posture"),
        advancement_id="minecraft:adventure/kill_a_mob",
    ),
    "minecraft:husbandry/plant_seed": CurriculumTarget(
        key="minecraft:husbandry/plant_seed",
        title="5分：锄地播种小麦",
        points=5,
        tier=2,
        task="合一把 wooden_hoe/stone_hoe 并打草获取 wheat_seeds，在泥土/草方块旁水源附近右键锄地并 mc_interact 播种。",
        extra_tools=("mc_interact",),
        advancement_id="minecraft:husbandry/plant_seed",
    ),
    "minecraft:husbandry/breed_an_animal": CurriculumTarget(
        key="minecraft:husbandry/breed_an_animal",
        title="5分：繁殖一对动物",
        points=5,
        tier=2,
        task="找到两只同种动物（牛/羊用 wheat，猪用 carrot/potato，鸡用 wheat_seeds），分别走近调 mc_interact 喂食配对繁殖。",
        extra_tools=("mc_interact",),
        advancement_id="minecraft:husbandry/breed_an_animal",
    ),
    "minecraft:story/enchant_item": CurriculumTarget(
        key="minecraft:story/enchant_item",
        title="30分：制造附魔台完成一次附魔",
        points=30,
        tier=2,
        task=(
            "解锁 story/enchant_item(30分)：准备 diamond_pickaxe 挖 4 块 obsidian，合 book(3 paper+1 leather) + 2 diamond 合 enchanting_table，"
            "放置后准备 lapis_lazuli 与待附魔工具/盔甲调 mc_window_act 附魔。"
        ),
        extra_tools=("mc_interact", "mc_window_view", "mc_window_act", "mc_kb_lookup"),
        advancement_id="minecraft:story/enchant_item",
    ),
    "minecraft:adventure/summon_iron_golem": CurriculumTarget(
        key="minecraft:adventure/summon_iron_golem",
        title="30分：搭建 T 型铁块与雕刻南瓜召唤铁傀儡",
        points=30,
        tier=2,
        task=(
            "解锁 adventure/summon_iron_golem(30分)：当有 36 铁锭(4 iron_block) 与 carved_pumpkin（用剪刀 mc_interact 南瓜）时，搭建 T 字底座并顶置 carved_pumpkin。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:adventure/summon_iron_golem",
    ),
    "minecraft:story/enter_the_nether": CurriculumTarget(
        key="minecraft:story/enter_the_nether",
        title="30分：搭建并点燃下界传送门进入下界",
        points=30,
        tier=3,
        task=(
            "解锁 story/enter_the_nether(30分)：准备至少 10 块 obsidian（水桶浇筑或钻石镐采集）与 flint_and_steel（1铁锭+1燧石），搭建 4x5 黑曜石门框并用 mc_interact 点燃站入传送。"
        ),
        extra_tools=("mc_interact", "mc_posture"),
        advancement_id="minecraft:story/enter_the_nether",
    ),
}


def score_completed_advancements(completed_ids: set[str] | list[str]) -> tuple[int, dict[str, int]]:
    """根据 RULES.md 精确计算已完成成就集合的当前理论游戏原始分与明细。"""
    total = 0
    breakdown: dict[str, int] = {}
    for adv_id in sorted(set(completed_ids)):
        norm = adv_id if ":" in adv_id else f"minecraft:{adv_id}"
        pts = RULES_ADVANCEMENT_POINTS.get(norm, 0)
        if pts > 0:
            breakdown[norm] = pts
            total += pts
    return total, breakdown


def _infer_extra_tools_for_advancement(adv_id: str, how_to: str = "") -> tuple[str, ...]:
    extra: list[str] = []
    text = f"{adv_id} {how_to}".lower()
    if any(k in text for k in ("interact", "bucket", "lodestone", "sign", "seed", "breed", "tame", "honey", "wax", "flint", "portal", "trade", "villager", "bed")):
        extra.extend(["mc_interact", "mc_use_item"])
    if any(k in text for k in ("kill", "mob", "squid", "skeleton", "wither", "blaze", "ghast", "piglin", "golem", "slime")):
        extra.extend(["mc_attack", "mc_posture"])
    if any(k in text for k in ("bow", "arrow", "crossbow", "trident", "bullseye", "sniper", "arbalistic", "two_birds")):
        extra.extend(["mc_shoot", "mc_use_item", "mc_posture"])
    if "enchant" in text or "附魔" in text:
        extra.extend(["mc_skill_enchant", "mc_window_view"])
    if any(k in text for k in ("trade", "villager", "交易", "村民")):
        extra.extend(["mc_skill_trade", "mc_window_view"])
    if any(k in text for k in ("brew", "酿", "药水")):
        extra.extend(["mc_skill_brew", "mc_window_view"])
    if any(k in text for k in ("trim", "smithing", "netherite_armor", "锻造", "纹饰", "下界合金")):
        extra.extend(["mc_skill_smithing", "mc_window_view"])
    if any(k in text for k in ("chest", "loot", "bastion", "箱", "战利品")):
        extra.extend(["mc_skill_container", "mc_window_view"])
    if any(k in text for k in ("crafter", "pot", "vault", "bookshelf", "jukebox")):
        extra.extend(["mc_window_view", "mc_window_act"])
    if "bed" in text or "sleep" in text:
        extra.append("mc_skill_bind_bed")
    extra.append("mc_kb_lookup")
    # 保持顺序去重
    return tuple(dict.fromkeys(extra))


_LATE_GRIND_FRONTIER_IDS: frozenset[str] = frozenset(
    {
        "minecraft:adventure/throw_trident",
        "minecraft:adventure/arbalistic",
        "minecraft:adventure/two_birds_one_arrow",
        "minecraft:adventure/sniper_duel",
        "minecraft:adventure/totem_of_undying",
        "minecraft:adventure/voluntary_exile",
        "minecraft:adventure/whos_the_pillager_now",
    }
)


def is_transient_stream_error_text(err_text: str) -> bool:
    """判断大模型错误信息是否属于可单轮内自动重连的瞬时流式断线/传输/线程退出故障（不含 429/5xx）。"""
    err = (err_text or "").lower()
    markers = (
        "qwen api 连接或读取失败",
        "模型流中断",
        "模型请求线程意外退出",
        "未取得完整响应",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "connecterror",
        "readerror",
        "remoteprotocolerror",
        "connection reset",
        "server disconnected",
    )
    return any(m in err for m in markers)


def is_transient_llm_error_text(err_text: str) -> bool:
    """判断大模型错误信息是否属于可豁免/重连的瞬时网络/限流/流式断线故障。"""
    if is_transient_stream_error_text(err_text):
        return True
    err = (err_text or "").lower()
    rate_or_server_markers = (
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "http 520",
        "http 521",
        "http 522",
        "http 523",
        "http 524",
        "concurrency limit",
        "simultaneous requests",
    )
    return any(m in err for m in rate_or_server_markers)


def _is_transient_llm_error(report: dict[str, Any]) -> bool:
    if report.get("status") != "runtime_error":
        return False
    return is_transient_llm_error_text(str(report.get("error") or ""))


def _can_attempt_target(adv_id: str, counts: dict[str, int], *, has_iron_or_better: bool) -> bool:
    """过滤缺少铁器/钻石/桶等前置材料、当前回合几乎必败的高阶目标。"""
    iron_equiv = counts.get("iron_ingot", 0) + counts.get("raw_iron", 0) + counts.get("iron_block", 0) * 9
    has_bucket = any(
        counts.get(k, 0) > 0 for k in ("bucket", "water_bucket", "lava_bucket", "cod_bucket", "salmon_bucket")
    )
    if adv_id in {"minecraft:story/shiny_gear", "minecraft:story/mine_diamond"}:
        return has_iron_or_better or counts.get("diamond", 0) >= 1
    if adv_id == "minecraft:story/enchant_item":
        return (
            counts.get("diamond_pickaxe", 0) > 0
            or counts.get("netherite_pickaxe", 0) > 0
            or counts.get("obsidian", 0) >= 4
            or counts.get("enchanting_table", 0) > 0
        )
    if adv_id == "minecraft:adventure/crafters_crafting_crafters":
        return has_iron_or_better or iron_equiv >= 5 or counts.get("crafter", 0) >= 1
    if adv_id == "minecraft:adventure/use_lodestone":
        return has_iron_or_better or (iron_equiv >= 5 and counts.get("redstone", 0) >= 1) or counts.get("lodestone", 0) >= 1
    if adv_id in {"minecraft:story/lava_bucket", "minecraft:story/form_obsidian", "minecraft:husbandry/tactical_fishing"}:
        return has_iron_or_better or has_bucket or iron_equiv >= 3
    if adv_id == "minecraft:adventure/ol_betsy":
        return counts.get("crossbow", 0) > 0 or has_iron_or_better or (iron_equiv >= 2 and counts.get("string", 0) >= 2)
    if adv_id == "minecraft:adventure/shoot_arrow":
        return (
            counts.get("bow", 0) > 0
            or counts.get("crossbow", 0) > 0
            or counts.get("string", 0) >= 3
            or has_iron_or_better
        )
    if adv_id == "minecraft:story/deflect_arrow":
        return counts.get("shield", 0) > 0 or iron_equiv >= 1 or has_iron_or_better
    if adv_id == "minecraft:story/obtain_armor":
        return iron_equiv >= 4 or has_iron_or_better
    if adv_id == "minecraft:adventure/summon_iron_golem":
        return iron_equiv >= 36 or counts.get("iron_block", 0) >= 4
    if adv_id == "minecraft:story/enter_the_nether":
        return has_iron_or_better or has_bucket or counts.get("obsidian", 0) >= 10
    if adv_id == "minecraft:story/iron_tools":
        return iron_equiv >= 3 or has_iron_or_better
    return True


def select_next_target(
    bot: BotSession,
    *,
    cooldowns: dict[str, float] | None = None,
    now: float | None = None,
) -> tuple[CurriculumTarget, dict[str, Any]]:
    """结合当前背包工具链阶段 + RULES.md 分值权重 + 进度树前线 + 冷却黑名单选出最优本局目标。"""
    now_t = time.monotonic() if now is None else now
    cd = cooldowns or {}

    def _is_cooled(key: str) -> bool:
        return cd.get(key, 0.0) > now_t

    inv = bot.inventory()
    counts: dict[str, int] = {}
    for row in inv.get("items") or []:
        if isinstance(row, dict) and row.get("name"):
            nm = str(row["name"]).removeprefix("minecraft:")
            counts[nm] = counts.get(nm, 0) + int(row.get("count") or 0)

    frontier_data = compute_advancement_frontier(bot, limit=50)
    completed = set(frontier_data.get("completed_ids") or [])
    frontier_rows = frontier_data.get("frontier") or []
    frontier_by_id = {str(r.get("id")): r for r in frontier_rows if isinstance(r, dict) and r.get("id")}

    has_stone_or_better = any(
        counts.get(k, 0) > 0 for k in ("stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")
    )
    has_iron_or_better = any(counts.get(k, 0) > 0 for k in ("iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"))

    # Stage 0: 工具链不到位先快速补齐基建（防止裸手死磕高阶任务）
    if not has_stone_or_better and not _is_cooled("bootstrap_stone_age"):
        return CURRICULUM_PLAYBOOKS["bootstrap_stone_age"], frontier_data
    if not has_iron_or_better and not _is_cooled("bootstrap_iron_age"):
        return CURRICULUM_PLAYBOOKS["bootstrap_iron_age"], frontier_data

    # 构建所有当前已解锁前置 & 计分的候选（合并高ROI预设剧本与进度树动态前沿）
    candidates: list[tuple[tuple[int, int, int, str], CurriculumTarget]] = []

    # 特判：若已有 4 钻石或已具备铁镐，shiny_gear（60分）虽挂在 story/mine_diamond 后，但整链应优先下挖
    for key, pb in CURRICULUM_PLAYBOOKS.items():
        if key.startswith("bootstrap_"):
            continue
        adv_id = pb.advancement_id or key
        if adv_id in completed or _is_cooled(key):
            continue
        # 必须父前置已解锁（或在 shiny_gear 时已有铁镐）
        is_unlocked = adv_id in frontier_by_id or (
            key == "minecraft:story/shiny_gear" and has_iron_or_better
        )
        if not is_unlocked:
            continue
        if not _can_attempt_target(adv_id, counts, has_iron_or_better=has_iron_or_better):
            continue
        is_uncond = 0 if adv_id in UNCONDITIONAL_ADVANCEMENTS else 1
        sort_key = (pb.tier, is_uncond, -pb.points, key)
        candidates.append((sort_key, pb))

    # 动态兜底纳入其余所有 frontier 中分值 > 0 的计分成就
    for row in frontier_rows:
        adv_id = str(row.get("id") or "")
        pts = RULES_ADVANCEMENT_POINTS.get(adv_id, 0)
        if pts <= 0 or adv_id in completed or adv_id in CURRICULUM_PLAYBOOKS or _is_cooled(adv_id):
            continue
        if not _can_attempt_target(adv_id, counts, has_iron_or_better=has_iron_or_better):
            continue
        is_uncond = 0 if adv_id in UNCONDITIONAL_ADVANCEMENTS else 1
        title_zh = row.get("title_zh") or adv_id
        how_to = row.get("how_to") or row.get("description_zh") or ""
        target = CurriculumTarget(
            key=adv_id,
            title=f"{pts}分：{title_zh} ({adv_id.removeprefix('minecraft:')})",
            points=pts,
            tier=3 if (adv_id.startswith(("minecraft:nether/", "minecraft:end/")) or adv_id in _LATE_GRIND_FRONTIER_IDS) else 2,
            task=(
                f"尝试解锁竞赛高分成就 `{adv_id}`（RULES.md 计 {pts} 分，{title_zh}）。"
                + (f"\n官方做法线索：{how_to}" if how_to else "")
                + "\n行动前缺配方或ID不确定请先调 mc_kb_lookup；若判定当前岛屿无此结构或暂不可达，报告阻断原因立即停止以便切换高分目标。"
            ),
            extra_tools=_infer_extra_tools_for_advancement(adv_id, str(how_to)),
            advancement_id=adv_id,
        )
        candidates.append(((target.tier, is_uncond, -pts, adv_id), target))

    if not candidates and cd:
        # 若全部目标都在冷却中，清空最早一个冷却重试
        earliest_key = min(cd, key=cd.get)
        cd.pop(earliest_key, None)
        return select_next_target(bot, cooldowns=cd, now=now_t)

    if not candidates:
        # 兜底：无特定目标时的通用生存+探索拿分
        return (
            CurriculumTarget(
                key="free_explore_scoring",
                title="自由探索与高分推进",
                points=0,
                tier=4,
                task="勘察周围未知区域、收集铁/钻石与食物，寻找村庄/要塞/深水结构并顺手完成沿途容易解锁的进度。",
                extra_tools=("mc_interact", "mc_attack", "mc_posture", "mc_kb_lookup"),
                advancement_id=None,
            ),
            frontier_data,
        )

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], frontier_data


class CurriculumSupervisor:
    """8 小时全自动计分课程循环：按 Episode 切片运行，带冷却拉黑与故障自愈。"""

    def __init__(
        self,
        bot: BotSession,
        *,
        chat_fn: Callable,
        model_name: str = "default",
        runs_dir: Path | str = "runs",
        duration_s: float = 28800.0,
        max_calls_per_episode: int = 18,
        episode_timeout_s: float = 420.0,
        tool_output_chars: int = 1600,
        cooldown_s: float = 1200.0,
        strike_limit: int = 2,
        api_error_backoff_s: float = 8.0,
        geofence: VoidGeofence | None = None,
        on_episode_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.bot = bot
        self.chat_fn = chat_fn
        self.model_name = model_name
        self.runs_dir = Path(runs_dir)
        self.duration_s = max(1.0, float(duration_s))
        self.max_calls_per_episode = max(1, int(max_calls_per_episode))
        self.episode_timeout_s = max(30.0, float(episode_timeout_s))
        self.tool_output_chars = max(256, int(tool_output_chars))
        self.cooldown_s = max(1.0, float(cooldown_s))
        self.strike_limit = max(1, int(strike_limit))
        self.api_error_backoff_s = max(0.0, float(api_error_backoff_s))
        self.geofence = geofence if geofence is not None else VoidGeofence.from_env()
        self.on_episode_event = on_episode_event

        self.strikes: dict[str, int] = {}
        self.cooldowns: dict[str, float] = {}
        self.episodes: list[dict[str, Any]] = []

    def run(
        self,
        *,
        max_episodes: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        from .agent import run_mc_agent

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        summary_path = self.runs_dir / f"curriculum-{run_stem}.json"

        started_mono = time.monotonic()
        deadline_mono = started_mono + self.duration_s
        ep_index = 0
        final_status = "completed"

        def _is_stopped() -> bool:
            return bool(stop_check is not None and stop_check())

        while time.monotonic() < deadline_mono:
            if _is_stopped():
                final_status = "interrupted"
                break
            if max_episodes is not None and ep_index >= max_episodes:
                break

            ep_index += 1
            remaining_s = deadline_mono - time.monotonic()
            ep_timeout = min(self.episode_timeout_s, max(0.5, remaining_s))

            # 确保 Bot 连接就绪；若触发死亡冷却则按实际秒数休眠恢复，绝不崩溃退出
            try:
                st = self.bot.status()
                if not st.get("ready"):
                    self.bot.wait_until_ready(timeout_s=min(60.0, remaining_s), poll_interval_s=1.0, initial_status=st)
                target, frontier_before = select_next_target(self.bot, cooldowns=self.cooldowns)
                completed_before = set(frontier_before.get("completed_ids") or [])
                score_before, _ = score_completed_advancements(completed_before)
                inv_before = self.bot.inventory()
                pick_before = _highest_pick_rank(inv_before)
                iron_before = _iron_units(inv_before)
            except AuthError as exc:
                final_status = "auth_error"
                self.episodes.append({"episode": ep_index, "status": "auth_error", "error": str(exc)})
                break
            except DeathLoopCooldownError as exc:
                wait_cd = min(max(5.0, float(exc.cooldown_remaining_seconds or 30.0)), max(5.0, remaining_s))
                time.sleep(wait_cd)
                continue
            except (ApiError, BotNotReadyError, RuntimeError) as exc:
                self.episodes.append({"episode": ep_index, "status": "recovery_wait", "error": str(exc)})
                time.sleep(min(5.0, max(1.0, remaining_s)))
                continue

            ep_stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            ep_events_path = self.runs_dir / f"{ep_stem}.events.jsonl"
            ep_report_path = self.runs_dir / f"{ep_stem}.json"

            with JsonlEvents(ep_events_path) as listener:
                def _sink(evt: dict[str, Any]) -> None:
                    listener(evt)
                    if self.on_episode_event is not None:
                        self.on_episode_event(evt)

                report = run_mc_agent(
                    target.task,
                    self.bot,
                    chat_fn=self.chat_fn,
                    model_name=self.model_name,
                    max_calls=self.max_calls_per_episode,
                    run_timeout_s=ep_timeout,
                    tool_output_chars=self.tool_output_chars,
                    stream=True,
                    on_event=_sink,
                    include_initial_snapshot=False,
                    stop_check=stop_check,
                    survival_guard=True,
                    hud_mode="ephemeral",
                    context_mode="sliding",
                    tool_profile="compact",
                    extra_tools=target.extra_tools,
                    geofence=self.geofence,
                    target_advancement=target.advancement_id,
                )

            report["curriculum_episode"] = ep_index
            report["curriculum_target"] = target.key
            report["event_log"] = str(ep_events_path)
            ep_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            # 统计本局收获与进榜分增量
            completed_after = set(completed_before)
            pick_after = pick_before
            iron_after = iron_before
            try:
                adv_after = self.bot.advancements(include_recipes=False)
                for r in adv_after.get("advancements") or []:
                    if isinstance(r, dict) and r.get("status") == "completed" and r.get("id"):
                        nid = str(r["id"])
                        completed_after.add(nid if ":" in nid else f"minecraft:{nid}")
                inv_after = self.bot.inventory()
                pick_after = _highest_pick_rank(inv_after)
                iron_after = _iron_units(inv_after)
            except Exception:
                pass

            gained_advs = sorted(completed_after - completed_before)
            score_after, breakdown = score_completed_advancements(completed_after)
            progress_made = (
                bool(gained_advs)
                or pick_after > pick_before
                or (target.key == "bootstrap_iron_age" and iron_after > iron_before)
            )
            transient_api_err = (not progress_made) and _is_transient_llm_error(report)

            if progress_made:
                self.strikes.pop(target.key, None)
            elif not transient_api_err:
                cnt = self.strikes.get(target.key, 0) + 1
                self.strikes[target.key] = cnt
                if cnt >= self.strike_limit:
                    cd_dur = min(self.cooldown_s, 90.0) if target.key.startswith("bootstrap_") else self.cooldown_s
                    self.cooldowns[target.key] = time.monotonic() + cd_dur
                    self.strikes[target.key] = 0

            ep_row = {
                "episode": ep_index,
                "target": target.key,
                "target_title": target.title,
                "target_points": target.points,
                "status": report.get("status"),
                "actor_requests": report.get("actor_requests"),
                "score_before": score_before,
                "score_after": score_after,
                "score_delta": score_after - score_before,
                "gained_advancements": gained_advs,
                "cooled_down": target.key in self.cooldowns and self.cooldowns[target.key] > time.monotonic(),
                "transient_api_error": transient_api_err,
                "report_file": str(ep_report_path),
            }
            self.episodes.append(ep_row)

            # 持续写盘更新课程总账
            try:
                latest_score, latest_bd = score_completed_advancements(completed_after)
            except Exception:
                latest_score, latest_bd = score_after, breakdown
            summary_payload = {
                "status": "running",
                "elapsed_s": round(time.monotonic() - started_mono, 1),
                "duration_s": self.duration_s,
                "episodes_completed": len(self.episodes),
                "current_score": latest_score,
                "completed_count": len(completed_after),
                "score_breakdown": latest_bd,
                "active_cooldowns": sorted(k for k, t in self.cooldowns.items() if t > time.monotonic()),
                "episodes": self.episodes,
            }
            summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            if report.get("status") == "interrupted":
                final_status = "interrupted"
                break
            if transient_api_err and self.api_error_backoff_s > 0 and time.monotonic() < deadline_mono and not _is_stopped():
                time.sleep(min(self.api_error_backoff_s, max(0.0, deadline_mono - time.monotonic())))

        # 结束前写最终态
        try:
            adv_final = self.bot.advancements(include_recipes=False)
            final_completed = {
                (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
                for r in (adv_final.get("advancements") or [])
                if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
            }
            final_score, final_bd = score_completed_advancements(final_completed)
        except Exception:
            final_score = self.episodes[-1].get("score_after", 0) if self.episodes else 0
            final_bd = {}
            final_completed = set()

        final_summary = {
            "status": final_status,
            "elapsed_s": round(time.monotonic() - started_mono, 1),
            "duration_s": self.duration_s,
            "episodes_completed": len(self.episodes),
            "final_score": final_score,
            "completed_count": len(final_completed),
            "score_breakdown": final_bd,
            "episodes": self.episodes,
            "summary_file": str(summary_path),
        }
        summary_path.write_text(json.dumps(final_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return final_summary


def _highest_pick_rank(inv: dict[str, Any]) -> int:
    ranks = {
        "wooden_pickaxe": 1,
        "stone_pickaxe": 2,
        "iron_pickaxe": 3,
        "diamond_pickaxe": 4,
        "netherite_pickaxe": 5,
    }
    best = 0
    for row in inv.get("items") or []:
        if isinstance(row, dict) and row.get("name") and int(row.get("count") or 0) > 0:
            best = max(best, ranks.get(str(row["name"]).removeprefix("minecraft:"), 0))
    return best


def _iron_units(inv: dict[str, Any]) -> int:
    total = 0
    for row in inv.get("items") or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        cnt = int(row.get("count") or 0)
        if cnt <= 0:
            continue
        nm = str(row["name"]).removeprefix("minecraft:")
        if nm == "raw_iron":
            total += cnt
        elif nm == "iron_ingot":
            total += 2 * cnt
        elif nm == "iron_block":
            total += 18 * cnt
    return total
