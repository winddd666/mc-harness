"""8 小时竞赛课程督导器（持久单会话 + 动态轮数/时长预算 + 0-LLM 物理硬前置 + WorldMemory 联动与同层套餐合并）。"""

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
from ..kb import normalize_dimension
from ..skills import compute_advancement_frontier, total_owned_counts
from .events import JsonlEvents
from .geofence import VoidGeofence
from .mc_tools import McToolset
from .world_memory import WorldMemory

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

# 按任务物理复杂度划分的 5 档动态预算：(max_calls, episode_timeout_s)
EPISODE_BUDGET_PROFILES: dict[str, tuple[int, float]] = {
    "micro": (8, 180.0),         # 材料齐备的一步合成/穿戴类
    "short": (14, 300.0),        # 近场交互、种地、睡床、摸底侦察
    "medium": (22, 600.0),       # 铁器基建、磁石、合成器、村民交易
    "deep": (40, 1500.0),        # Y=-54 鱼骨深潜钻石套餐、附魔台全链（实测 720s~1500s）
    "expedition": (45, 1800.0),  # 下界传送门建造与下界/末地远征
}


@dataclasses.dataclass(frozen=True, slots=True)
class CurriculumTarget:
    key: str
    title: str
    points: int
    tier: int  # 0=工具基建, 1=主世界高ROI稳拿, 2=钻石链与支线, 3=下界末地, 4=其余frontier兜底
    task: str
    extra_tools: tuple[str, ...] = ()
    advancement_id: str | None = None
    budget_class: str = "medium"
    pre_ensure: tuple[tuple[str, int], ...] = ()
    pre_equip: tuple[tuple[str, str], ...] = ()


def resolve_episode_budget(
    target: CurriculumTarget,
    *,
    explicit_max_calls: int | None = None,
    explicit_timeout_s: float | None = None,
) -> tuple[int, float]:
    """Resolve `(max_calls, timeout_s)` dynamically from `target.budget_class` unless overridden."""
    prof_calls, prof_timeout = EPISODE_BUDGET_PROFILES.get(target.budget_class, EPISODE_BUDGET_PROFILES["medium"])
    calls = max(1, int(explicit_max_calls)) if explicit_max_calls is not None else prof_calls
    timeout_s = max(10.0, float(explicit_timeout_s)) if explicit_timeout_s is not None else prof_timeout
    return calls, timeout_s


# 优先级剧本库：先稳基建（石镐->铁镐），再抢高ROI主世界项与同层钻石合并套餐
CURRICULUM_PLAYBOOKS: dict[str, CurriculumTarget] = {
    "bootstrap_stone_age": CurriculumTarget(
        key="bootstrap_stone_age",
        title="石器基建：原木/工作台/木镐/石镐",
        points=10,
        tier=0,
        budget_class="short",
        pre_ensure=(("stone_pickaxe", 1),),
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
        title="铁器基建：采集煤铁并合出 iron_pickaxe",
        points=10,
        tier=0,
        budget_class="medium",
        pre_ensure=(("iron_pickaxe", 1),),
        task=(
            "将装备升级至铁器：1) 用石镐调 mc_skill_mine_dfs(target='iron', target_y=16, goal_count=4) 或采露头煤铁凑齐至少 3~4 块 raw_iron；"
            "2) 调 mc_skill_smelt('raw_iron', count=4) 熔炼铁锭；"
            "3) 立即调 mc_skill_craft_gear('iron_pickaxe') 完成铁镐基建（若铁锭富余>=4 可顺手合 shield，不强求）。"
        ),
        advancement_id="minecraft:story/iron_tools",
    ),
    "minecraft:story/shiny_gear": CurriculumTarget(
        key="minecraft:story/shiny_gear",
        title="60分深潜钻石套餐：首4钻合 diamond_boots 锁分 + 同层顺采钻镐/附魔台钻料",
        points=60,
        tier=1,
        budget_class="deep",
        # Keep deep Y=-54 mining inside `run_mc_agent` where background GenerationGuard runs continuously!
        pre_ensure=(),
        pre_equip=(),
        task=(
            "执行 Y=-54 鱼骨深潜钻石套餐（目标 story/mine_diamond 15分 + story/shiny_gear 60分，并顺手备后续钻镐/附魔台钻料）："
            "1) 确认包内有 iron_pickaxe 与备用 stone_pickaxe；"
            "2) 调 mc_skill_mine_dfs(target='diamond', target_y=-54, goal_count=4) 在深板岩层鱼骨探钻；"
            "3) 一旦包内凑满首 4 颗 diamond，**第一时间立即就地调 mc_skill_craft_gear('diamond_boots', equip_slot='feet') 先锁定 75 分**！"
            "4) 穿好钻石靴后，若本局时间与镐耐久仍充裕且状态健康，留在同一条 Y=-54 鱼骨矿井顺手再调 mc_skill_mine_dfs(target='diamond', target_y=-54, goal_count=5) 多采最多 5 颗钻石（3 颗合 diamond_pickaxe + 2 颗留作附魔台）及沿途青金石/红石，随后结束汇报。"
        ),
        advancement_id="minecraft:story/shiny_gear",
    ),
    "minecraft:story/mine_diamond": CurriculumTarget(
        key="minecraft:story/mine_diamond",
        title="15分：Y=-54 深层挖掘首颗钻石",
        points=15,
        tier=2,
        budget_class="deep",
        pre_ensure=(),
        task=(
            "持有 iron_pickaxe 调 mc_skill_mine_dfs(target='diamond', target_y=-54, goal_count=4) 寻找并挖掘 diamond_ore，确保钻石真实入包解锁 story/mine_diamond。"
        ),
        advancement_id="minecraft:story/mine_diamond",
    ),
    "minecraft:adventure/use_lodestone": CurriculumTarget(
        key="minecraft:adventure/use_lodestone",
        title="15分：1.21.11 便宜磁石合成+指南针绑定",
        points=15,
        tier=1,
        budget_class="medium",
        pre_ensure=(("lodestone", 1), ("compass", 1)),
        task=(
            "解锁 adventure/use_lodestone(15分)：在 1.21.11 中磁石(lodestone)仅需 1 铁锭 + 8 个錾制石砖(chiseled_stone_bricks)，指南针(compass)需 4 铁锭 + 1 红石；"
            "若走工作台合成需收集 12 圆石烧成 12 stone -> 12 stone_bricks -> 18 stone_brick_slab -> 8 chiseled_stone_bricks（或造切石机用 8 stone 1:1 切出），"
            "与 5 铁锭、1 红石合出 lodestone 与 compass，放下 lodestone 后手持 compass 调 mc_interact(x,y,z,item='compass')。"
        ),
        extra_tools=("mc_interact", "mc_skill_ensure_items", "mc_kb_lookup"),
        advancement_id="minecraft:adventure/use_lodestone",
    ),
    "minecraft:story/lava_bucket": CurriculumTarget(
        key="minecraft:story/lava_bucket",
        title="15分：铁桶装岩浆",
        points=15,
        tier=1,
        budget_class="short",
        pre_ensure=(("bucket", 1),),
        task=(
            "解锁 story/lava_bucket(15分)：确保有 3 铁锭合 bucket，寻找附近露头或地下静止 lava（先用 mc_find_blocks(types=['lava'], radius=48)），"
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
        budget_class="medium",
        pre_ensure=(("bucket", 1),),
        task=(
            "解锁 story/form_obsidian(15分)：备好 water_bucket（或用 3 铁锭合 bucket 对水源 mc_interact），"
            "若背包有 diamond_pickaxe 可直接挖暴露 obsidian（或水浇静止岩浆生成 obsidian 后用 diamond_pickaxe 挖取入包）。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:story/form_obsidian",
    ),
    "minecraft:husbandry/make_a_sign_glow": CurriculumTarget(
        key="minecraft:husbandry/make_a_sign_glow",
        title="30分：发光墨囊涂亮木告示牌",
        points=30,
        tier=1,
        budget_class="medium",
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
        budget_class="short",
        pre_ensure=(("bucket", 1),),
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
        budget_class="medium",
        pre_ensure=(("crafter", 1), ("iron_ingot", 5), ("crafting_table", 1), ("redstone", 1), ("dropper", 1)),
        task=(
            "解锁 adventure/crafters_crafting_crafters(15分)：准备 1 个已合成的 crafter + 第 2 个 crafter 的配方原料（5 iron_ingot + 1 crafting_table + 1 redstone + 1 dropper），"
            "放置第 1 个 crafter 并用 mc_window_act 按槽位摆入 crafter 配方、接红石脉冲（如 redstone_torch/lever）激活产出。"
        ),
        extra_tools=("mc_interact", "mc_window_view", "mc_window_act", "mc_kb_lookup"),
        advancement_id="minecraft:adventure/crafters_crafting_crafters",
    ),
    "minecraft:adventure/ol_betsy": CurriculumTarget(
        key="minecraft:adventure/ol_betsy",
        title="15分：制作弩并装填发射",
        points=15,
        tier=1,
        budget_class="short",
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
        budget_class="short",
        pre_ensure=(("shield", 1),),
        pre_equip=(("shield", "off-hand"),),
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
        budget_class="micro",
        pre_ensure=(("iron_boots", 1),),
        pre_equip=(("iron_boots", "feet"),),
        task="用现有铁锭调 mc_skill_craft_gear('iron_boots', equip_slot='feet')（仅需4铁锭），立得 story/obtain_armor。",
        advancement_id="minecraft:story/obtain_armor",
    ),
    "minecraft:adventure/sleep_in_bed": CurriculumTarget(
        key="minecraft:adventure/sleep_in_bed",
        title="5分：做床并绑定/入睡",
        points=5,
        tier=2,
        budget_class="short",
        task="收集 3 羊毛+3木板合 white_bed（或前往村庄已有床）放置，调 mc_skill_bind_bed。",
        extra_tools=("mc_skill_bind_bed", "mc_interact", "mc_attack"),
        advancement_id="minecraft:adventure/sleep_in_bed",
    ),
    "minecraft:adventure/kill_a_mob": CurriculumTarget(
        key="minecraft:adventure/kill_a_mob",
        title="5分：击杀任意敌对生物",
        points=5,
        tier=2,
        budget_class="short",
        task="装备最高近战剑/斧与盾，扫描附近敌对生物（僵尸/蜘蛛/骷髅）并安全击杀，注意低血量进掩体恢复。",
        extra_tools=("mc_attack", "mc_posture"),
        advancement_id="minecraft:adventure/kill_a_mob",
    ),
    "minecraft:husbandry/plant_seed": CurriculumTarget(
        key="minecraft:husbandry/plant_seed",
        title="5分：锄地播种小麦",
        points=5,
        tier=2,
        budget_class="micro",
        task="合一把 wooden_hoe/stone_hoe 并打草获取 wheat_seeds，在泥土/草方块旁水源附近右键锄地并 mc_interact 播种。",
        extra_tools=("mc_interact",),
        advancement_id="minecraft:husbandry/plant_seed",
    ),
    "minecraft:husbandry/breed_an_animal": CurriculumTarget(
        key="minecraft:husbandry/breed_an_animal",
        title="5分：繁殖一对动物",
        points=5,
        tier=2,
        budget_class="short",
        task="找到两只同种动物（牛/羊用 wheat，猪用 carrot/potato，鸡用 wheat_seeds），分别走近调 mc_interact 喂食配对繁殖。",
        extra_tools=("mc_interact",),
        advancement_id="minecraft:husbandry/breed_an_animal",
    ),
    "minecraft:story/enchant_item": CurriculumTarget(
        key="minecraft:story/enchant_item",
        title="30分：制造附魔台完成一次附魔",
        points=30,
        tier=2,
        budget_class="deep",
        task=(
            "解锁 story/enchant_item(30分)：准备 diamond_pickaxe 挖 4 块 obsidian，合 book(3 paper+1 leather) + 2 diamond 合 enchanting_table，"
            "放置后准备 lapis_lazuli 与待附魔铁/钻装备调 mc_skill_enchant(item=..., option=0) 安全完成附魔。"
        ),
        extra_tools=("mc_interact", "mc_skill_enchant", "mc_window_view", "mc_kb_lookup"),
        advancement_id="minecraft:story/enchant_item",
    ),
    "minecraft:adventure/summon_iron_golem": CurriculumTarget(
        key="minecraft:adventure/summon_iron_golem",
        title="30分：搭建 T 型铁块与雕刻南瓜召唤铁傀儡",
        points=30,
        tier=2,
        budget_class="short",
        pre_ensure=(("iron_block", 4), ("shears", 1)),
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
        budget_class="expedition",
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


def _combined_available_counts(
    inv: dict[str, Any],
    world_memory: WorldMemory | None,
    *,
    dim: str = "overworld",
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return `(bag_and_equipped, chest_only, total_combined)` counts in `dim`."""
    bag_counts = total_owned_counts(inv)
    chest_counts: dict[str, int] = {}
    if world_memory is not None:
        norm_dim = normalize_dimension(dim)
        for (cdim, _, _, _), meta in world_memory.containers.items():
            if cdim != norm_dim:
                continue
            for k, v in (meta.get("items") or {}).items():
                iv = int(v or 0)
                if iv > 0:
                    chest_counts[k] = chest_counts.get(k, 0) + iv
    combined = dict(bag_counts)
    for k, v in chest_counts.items():
        combined[k] = combined.get(k, 0) + v
    return bag_counts, chest_counts, combined


def _can_attempt_target(adv_id: str, counts: dict[str, int], *, has_iron_or_better: bool) -> bool:
    """过滤缺少铁器/钻石/桶等客观前置材料、当前回合几乎必败的高阶目标。"""
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
            or counts.get("diamond", 0) >= 5
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
    if adv_id == "minecraft:adventure/trim_with_any_armor_pattern":
        return any(k.endswith("_smithing_template") and v > 0 for k, v in counts.items())
    if adv_id == "minecraft:adventure/read_power_of_chiseled_bookshelf":
        return counts.get("comparator", 0) > 0 or counts.get("quartz", 0) >= 1
    if adv_id == "minecraft:adventure/spyglass_at_ghast":
        return counts.get("spyglass", 0) > 0 or counts.get("amethyst_shard", 0) >= 1
    if adv_id == "minecraft:adventure/bullseye":
        return counts.get("target", 0) > 0 or (
            (counts.get("hay_block", 0) >= 1 or counts.get("wheat", 0) >= 9) and counts.get("redstone", 0) >= 4
        )
    if adv_id == "minecraft:story/enter_the_nether":
        return has_iron_or_better or has_bucket or counts.get("obsidian", 0) >= 10
    if adv_id == "minecraft:story/iron_tools":
        return iron_equiv >= 3 or has_iron_or_better
    return True


def _enrich_target_with_world_memory(
    target: CurriculumTarget,
    *,
    world_memory: WorldMemory | None,
    bag_counts: dict[str, int],
    chest_counts: dict[str, int],
    origin: list[float] | tuple[float, float, float] | None,
    dim: str = "overworld",
) -> tuple[CurriculumTarget, bool]:
    """Attach concrete WorldMemory coordinates / storage chest withdrawal hints and report if boosted."""
    if world_memory is None:
        return target, False

    notes: list[str] = []
    extra_add: list[str] = []
    boosted = False
    pos = origin if (origin is not None and len(origin) >= 3) else [0.0, 64.0, 0.0]
    norm_dim = normalize_dimension(dim)

    # 1) Storage chest hint if strategic items sit in chests while short in bag
    watch_items = (
        "iron_pickaxe", "diamond_pickaxe", "iron_ingot", "raw_iron", "diamond",
        "obsidian", "bucket", "water_bucket", "redstone", "lapis_lazuli", "emerald", "wheat", "white_bed",
    )
    stored_useful = [
        f"{it}×{chest_counts[it]}"
        for it in watch_items
        if chest_counts.get(it, 0) > 0 and bag_counts.get(it, 0) == 0
    ]
    if stored_useful:
        hits = world_memory.query_container_item(stored_useful[0].split("×")[0], origin=pos, dim=norm_dim)
        if hits:
            h0 = hits[0]
            notes.append(
                f"[仓储提示：记忆库 {h0['block']}@{h0['pos']}({h0['dist']}m) 存有 {', '.join(stored_useful[:4])}，"
                "优先调 mc_skill_container(action='take') 取出复用，勿重复采矿]"
            )
            extra_add.extend(["mc_skill_container", "mc_window_view"])

    adv_id = target.advancement_id or target.key
    # 2) Landmark / Habitat dynamic boosts
    if adv_id == "minecraft:adventure/trade":
        v_hits = world_memory.query_habitats(["villager"], origin=pos, dim=norm_dim, limit=1)
        poi_hits = world_memory.query_pois(["lectern", "bell", "composter", "blast_furnace"], origin=pos, dim=norm_dim, limit=1)
        if v_hits:
            v0 = v_hits[0]
            notes.append(
                f"[记忆库提权：已探明 villager 聚落位于 {v0['approx_pos']}（距此 {v0['dist']}m，{v0['bearing']}），"
                "前往扫描实体后调 mc_skill_trade 完成交易]"
            )
            extra_add.extend(["mc_skill_trade", "mc_window_view"])
            boosted = True
        elif poi_hits:
            p0 = poi_hits[0]
            notes.append(f"[记忆库提权：已探明村庄职业方块 {p0['name']}@{p0['pos']}（{p0['dist']}m），前往寻找村民交易]")
            extra_add.extend(["mc_skill_trade", "mc_window_view"])
            boosted = True

    elif adv_id in {"minecraft:story/lava_bucket", "minecraft:story/form_obsidian"}:
        lava_hits = world_memory.query_pois(["lava"], origin=pos, dim=norm_dim, limit=1)
        obs_hits = world_memory.query_pois(["obsidian"], origin=pos, dim=norm_dim, limit=1)
        if adv_id == "minecraft:story/form_obsidian" and obs_hits and (bag_counts.get("diamond_pickaxe", 0) + chest_counts.get("diamond_pickaxe", 0)) > 0:
            o0 = obs_hits[0]
            notes.append(f"[记忆库地标：已知现成 obsidian@{o0['pos']}（{o0['dist']}m），可直接持钻石镐前往采集]")
            boosted = True
        elif lava_hits:
            l0 = lava_hits[0]
            notes.append(f"[记忆库地标：已知 lava@{l0['pos']}（{l0['dist']}m，{l0['bearing']}），可直接分段前往操作]")
            boosted = True

    elif adv_id == "minecraft:adventure/sleep_in_bed":
        bed_hits = world_memory.query_pois(["bed"], origin=pos, dim=norm_dim, limit=1)
        sheep_hits = world_memory.query_habitats(["sheep"], origin=pos, dim=norm_dim, limit=1)
        has_bed_or_wool = any(k.endswith("_bed") and v > 0 for k, v in bag_counts.items()) or sum(
            v for k, v in bag_counts.items() if k.endswith("_wool")
        ) >= 3
        if bed_hits:
            b0 = bed_hits[0]
            notes.append(f"[记忆库地标：已知现成床 {b0['name']}@{b0['pos']}（{b0['dist']}m），可直接前往调 mc_skill_bind_bed]")
            boosted = True
        elif has_bed_or_wool or sheep_hits:
            if sheep_hits and not has_bed_or_wool:
                s0 = sheep_hits[0]
                notes.append(f"[记忆库聚落：已知 sheep 位于 {s0['approx_pos']}（{s0['dist']}m），可前往获取羊毛制床]")
            boosted = True

    elif adv_id == "minecraft:husbandry/breed_an_animal":
        hab_hits = world_memory.query_habitats(["sheep", "cow", "pig", "chicken"], origin=pos, dim=norm_dim, limit=2)
        has_feed = any((bag_counts.get(f, 0) + chest_counts.get(f, 0)) >= 2 for f in ("wheat", "wheat_seeds", "carrot", "potato"))
        if hab_hits and has_feed:
            h0 = hab_hits[0]
            notes.append(f"[记忆库聚落：已知 {h0['name']} 位于 {h0['approx_pos']}（{h0['dist']}m），携饲料前往繁殖]")
            boosted = True

    elif adv_id == "minecraft:story/enter_the_nether":
        portal_hits = world_memory.query_pois(["nether_portal", "obsidian"], origin=pos, dim=norm_dim, limit=1)
        if portal_hits:
            p0 = portal_hits[0]
            notes.append(f"[记忆库地标：已知 {p0['name']}@{p0['pos']}（{p0['dist']}m），优先复用]")
            boosted = True

    elif adv_id == "minecraft:story/obtain_armor":
        if (bag_counts.get("iron_ingot", 0) + chest_counts.get("iron_ingot", 0)) >= 4:
            boosted = True

    if not notes and not extra_add and not boosted:
        return target, False

    merged_extra = tuple(dict.fromkeys([*target.extra_tools, *extra_add]))
    new_task = ("\n".join(notes) + "\n" + target.task) if notes else target.task
    new_tier = 1 if (boosted and target.tier == 2) else target.tier
    updated = dataclasses.replace(target, tier=new_tier, task=new_task, extra_tools=merged_extra)
    return updated, boosted


def select_next_target(
    bot: BotSession,
    *,
    cooldowns: dict[str, float] | None = None,
    now: float | None = None,
    world_memory: WorldMemory | None = None,
    enable_island_recon: bool = False,
    recon_done: bool = False,
) -> tuple[CurriculumTarget, dict[str, Any]]:
    """结合背包+穿戴+仓储箱联合库存、WorldMemory 地标提权、同层套餐合并与冷却黑名单选出最优本局目标。"""
    now_t = time.monotonic() if now is None else now
    cd = cooldowns or {}

    def _is_cooled(key: str) -> bool:
        return cd.get(key, 0.0) > now_t

    origin_pos: list[float] | None = None
    cur_dim = "overworld"
    if world_memory is not None:
        try:
            st = bot.status()
            if isinstance(st.get("pos"), (list, tuple)) and len(st["pos"]) >= 3:
                origin_pos = [float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])]
            cur_dim = normalize_dimension(st.get("dimension"))
        except Exception:
            pass

    inv = bot.inventory()
    bag_counts, chest_counts, counts = _combined_available_counts(inv, world_memory, dim=cur_dim)

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
        pb0, _ = _enrich_target_with_world_memory(
            CURRICULUM_PLAYBOOKS["bootstrap_stone_age"],
            world_memory=world_memory,
            bag_counts=bag_counts,
            chest_counts=chest_counts,
            origin=origin_pos,
            dim=cur_dim,
        )
        return pb0, frontier_data
    if not has_iron_or_better and not _is_cooled("bootstrap_iron_age"):
        pb1, _ = _enrich_target_with_world_memory(
            CURRICULUM_PLAYBOOKS["bootstrap_iron_age"],
            world_memory=world_memory,
            bag_counts=bag_counts,
            chest_counts=chest_counts,
            origin=origin_pos,
            dim=cur_dim,
        )
        return pb1, frontier_data

    # 构建所有当前已解锁前置 & 计分的候选（合并高ROI预设剧本与进度树动态前沿）
    candidates: list[tuple[tuple[int, int, int, str], CurriculumTarget]] = []

    # 可选：铁器成型后若主世界地图一片空白（<16 区块且未探得村民/岩浆），插入一次有界地表环岛摸底（排在 60/30 分稳拿项之后、15 分盲找项之前）
    if (
        enable_island_recon
        and not recon_done
        and world_memory is not None
        and cur_dim == "overworld"
        and not _is_cooled("island_recon_sweep")
    ):
        ow_chunks = sum(1 for (d, _, _) in world_memory.visited_chunks if d == "overworld")
        has_villager_mem = bool(world_memory.habitats.get(("overworld", "villager")))
        has_lava_mem = any(e.name == "lava" and e.dim == "overworld" for e in world_memory.pois.values())
        if ow_chunks < 16 and not (has_villager_mem and has_lava_mem):
            unexp = world_memory.unexplored_directions(origin_pos or [0.0, 64.0, 0.0], dim="overworld")
            dir_hint = "/".join(unexp[:3]) if unexp else "east/north/west/south"
            recon_target = CurriculumTarget(
                key="island_recon_sweep",
                title="环岛摸底侦察：扫描村庄/岩浆池/动物聚落充实 WorldMemory",
                points=20,
                tier=1,
                budget_class="short",
                task=(
                    f"执行一次有界地表环岛摸底探索（严禁挖矿或不要下潜）：优先沿未探索方向({dir_hint})用 mc_move_to 分段推进 120~240m（严守虚空岛边界），"
                    "沿途调用 mc_find_blocks(radius=32) 与 mc_get_entities(radius=32) 扫描记录村庄、箱子、地表岩浆池、甘蔗、南瓜及牛羊群，探明后即可结束汇报。"
                ),
                extra_tools=("mc_interact", "mc_kb_lookup"),
                advancement_id=None,
            )
            candidates.append(((1, 0, -20, "island_recon_sweep"), recon_target))

    for key, pb in CURRICULUM_PLAYBOOKS.items():
        if key.startswith("bootstrap_"):
            continue
        adv_id = pb.advancement_id or key
        if adv_id in completed or _is_cooled(key):
            continue
        is_unlocked = adv_id in frontier_by_id or (
            key == "minecraft:story/shiny_gear" and has_iron_or_better
        )
        if not is_unlocked:
            continue
        if not _can_attempt_target(adv_id, counts, has_iron_or_better=has_iron_or_better):
            continue
        enriched_pb, mem_boosted = _enrich_target_with_world_memory(
            pb,
            world_memory=world_memory,
            bag_counts=bag_counts,
            chest_counts=chest_counts,
            origin=origin_pos,
            dim=cur_dim,
        )
        is_uncond = 0 if (adv_id in UNCONDITIONAL_ADVANCEMENTS or mem_boosted) else 1
        sort_key = (enriched_pb.tier, is_uncond, -enriched_pb.points, key)
        candidates.append((sort_key, enriched_pb))

    # 动态兜底纳入其余所有 frontier 中分值 > 0 的计分成就
    for row in frontier_rows:
        adv_id = str(row.get("id") or "")
        pts = RULES_ADVANCEMENT_POINTS.get(adv_id, 0)
        if pts <= 0 or adv_id in completed or adv_id in CURRICULUM_PLAYBOOKS or _is_cooled(adv_id):
            continue
        if not _can_attempt_target(adv_id, counts, has_iron_or_better=has_iron_or_better):
            continue
        title_zh = row.get("title_zh") or adv_id
        how_to = row.get("how_to") or row.get("description_zh") or ""
        budget_cls = (
            "expedition"
            if adv_id.startswith(("minecraft:nether/", "minecraft:end/"))
            else ("medium" if pts >= 30 else "short")
        )
        raw_target = CurriculumTarget(
            key=adv_id,
            title=f"{pts}分：{title_zh} ({adv_id.removeprefix('minecraft:')})",
            points=pts,
            tier=3 if (adv_id.startswith(("minecraft:nether/", "minecraft:end/")) or adv_id in _LATE_GRIND_FRONTIER_IDS) else 2,
            budget_class=budget_cls,
            task=(
                f"尝试解锁竞赛高分成就 `{adv_id}`（RULES.md 计 {pts} 分，{title_zh}）。"
                + (f"\n官方做法线索：{how_to}" if how_to else "")
                + "\n行动前缺配方或ID不确定请先调 mc_kb_lookup；若判定当前岛屿无此结构或暂不可达，报告阻断原因立即停止以便切换高分目标。"
            ),
            extra_tools=_infer_extra_tools_for_advancement(adv_id, str(how_to)),
            advancement_id=adv_id,
        )
        enriched_t, mem_boosted = _enrich_target_with_world_memory(
            raw_target,
            world_memory=world_memory,
            bag_counts=bag_counts,
            chest_counts=chest_counts,
            origin=origin_pos,
            dim=cur_dim,
        )
        is_uncond = 0 if (adv_id in UNCONDITIONAL_ADVANCEMENTS or mem_boosted) else 1
        candidates.append(((enriched_t.tier, is_uncond, -pts, adv_id), enriched_t))

    if not candidates and cd:
        earliest_key = min(cd, key=cd.get)
        cd.pop(earliest_key, None)
        return select_next_target(
            bot,
            cooldowns=cd,
            now=now_t,
            world_memory=world_memory,
            enable_island_recon=enable_island_recon,
            recon_done=recon_done,
        )

    if not candidates:
        return (
            CurriculumTarget(
                key="free_explore_scoring",
                title="自由探索与高分推进",
                points=0,
                tier=4,
                budget_class="medium",
                task="勘察周围未知区域、收集铁/钻石与食物，寻找村庄/要塞/深水结构并顺手完成沿途容易解锁的进度。",
                extra_tools=("mc_interact", "mc_attack", "mc_posture", "mc_kb_lookup"),
                advancement_id=None,
            ),
            frontier_data,
        )

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], frontier_data


class CurriculumSupervisor:
    """8 小时全自动计分课程循环：持久共享 McToolset + 动态轮数/时长预算 + 可选 0-LLM 预备料 + 2-Strike 冷却。"""

    def __init__(
        self,
        bot: BotSession,
        *,
        chat_fn: Callable,
        model_name: str = "default",
        runs_dir: Path | str = "runs",
        duration_s: float = 28800.0,
        max_calls_per_episode: int | None = None,
        episode_timeout_s: float | None = None,
        tool_output_chars: int = 1600,
        cooldown_s: float = 1200.0,
        strike_limit: int = 2,
        api_error_backoff_s: float = 8.0,
        geofence: VoidGeofence | None = None,
        toolset: McToolset | None = None,
        enable_pre_ensure: bool = False,
        enable_island_recon: bool = False,
        on_episode_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.bot = bot
        self.chat_fn = chat_fn
        self.model_name = model_name
        self.runs_dir = Path(runs_dir)
        self.duration_s = max(1.0, float(duration_s))
        self._explicit_max_calls: int | None = (
            max(1, int(max_calls_per_episode)) if max_calls_per_episode is not None else None
        )
        self._explicit_timeout_s: float | None = (
            max(30.0, float(episode_timeout_s)) if episode_timeout_s is not None else None
        )
        # Keep backwards-compatible public attributes
        self.max_calls_per_episode: int = self._explicit_max_calls or EPISODE_BUDGET_PROFILES["medium"][0]
        self.episode_timeout_s: float = self._explicit_timeout_s or EPISODE_BUDGET_PROFILES["medium"][1]
        self.tool_output_chars = max(256, int(tool_output_chars))
        self.cooldown_s = max(1.0, float(cooldown_s))
        self.strike_limit = max(1, int(strike_limit))
        self.api_error_backoff_s = max(0.0, float(api_error_backoff_s))
        self.geofence = geofence if geofence is not None else VoidGeofence.from_env()
        self.enable_pre_ensure = bool(enable_pre_ensure)
        self.enable_island_recon = bool(enable_island_recon)
        self.on_episode_event = on_episode_event

        # Persistent single-session McToolset shared across ALL episodes (retains WorldMemory + MiningDFS tree!)
        self.toolset: McToolset = (
            toolset
            if toolset is not None
            else McToolset(self.bot, geofence=self.geofence, wait_for_cooldown=True)
        )
        self.recon_completed: bool = False
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

            # 确保 Bot 连接就绪；同时喂给持久 WorldMemory 首帧状态
            try:
                st = self.bot.status()
                if not st.get("ready"):
                    st = self.bot.wait_until_ready(timeout_s=min(60.0, remaining_s), poll_interval_s=1.0, initial_status=st)
                self.toolset._update_status_cache(st)
                target, frontier_before = select_next_target(
                    self.bot,
                    cooldowns=self.cooldowns,
                    world_memory=self.toolset.world_memory,
                    enable_island_recon=self.enable_island_recon,
                    recon_done=self.recon_completed,
                )
                completed_before = set(frontier_before.get("completed_ids") or [])
                score_before, _ = score_completed_advancements(completed_before)
                inv_before = self.bot.inventory()
                pick_before = _highest_pick_rank(inv_before)
                iron_before = _iron_units(inv_before)
                diamond_before = _strategic_units(inv_before, ("diamond",))
                obsidian_before = _strategic_units(inv_before, ("obsidian",))
                chunks_before = len(self.toolset.world_memory.visited_chunks)
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

            ep_max_calls, raw_ep_timeout = resolve_episode_budget(
                target,
                explicit_max_calls=self._explicit_max_calls,
                explicit_timeout_s=self._explicit_timeout_s,
            )
            ep_timeout = min(raw_ep_timeout, max(0.5, remaining_s))

            ep_stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            ep_events_path = self.runs_dir / f"{ep_stem}.events.jsonl"
            ep_report_path = self.runs_dir / f"{ep_stem}.json"

            pre_ensure_res: dict[str, Any] | None = None
            satisfied_by_zero_llm = False
            eff_task = target.task

            with JsonlEvents(ep_events_path) as listener:
                def _sink(evt: dict[str, Any]) -> None:
                    listener(evt)
                    if self.on_episode_event is not None:
                        self.on_episode_event(evt)

                if self.enable_pre_ensure and target.pre_ensure:
                    try:
                        pre_slice_s = min(ep_timeout * 0.35, 90.0)
                        _sink(
                            {
                                "type": "curriculum.pre_ensure.started",
                                "episode": ep_index,
                                "target": target.key,
                                "items": dict(target.pre_ensure),
                            }
                        )
                        pre_ensure_res = self.toolset.skills.ensure_items(
                            dict(target.pre_ensure),
                            max_duration_s=max(10.0, pre_slice_s),
                            equip_slots=dict(target.pre_equip) if target.pre_equip else None,
                            geofence=self.geofence,
                            stop_check=_is_stopped,
                        )
                        _sink(
                            {
                                "type": "curriculum.pre_ensure.completed",
                                "episode": ep_index,
                                "target": target.key,
                                "ok": pre_ensure_res.get("ok"),
                                "steps_executed": pre_ensure_res.get("steps_executed", 0),
                                "remaining_deficit": pre_ensure_res.get("remaining_deficit", {}),
                            }
                        )
                        if pre_ensure_res.get("ok") and target.advancement_id:
                            adv_chk = self.bot.advancements(include_recipes=False)
                            done_chk = {
                                (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
                                for r in (adv_chk.get("advancements") or [])
                                if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
                            }
                            if target.advancement_id in done_chk:
                                satisfied_by_zero_llm = True
                        if not satisfied_by_zero_llm and pre_ensure_res.get("steps_executed", 0) > 0:
                            if pre_ensure_res.get("ok"):
                                eff_task = (
                                    f"[开局0-LLM自动备料已完成：{dict(target.pre_ensure)} 已入包/穿戴，请直接执行剩余放置/交互步骤]\n"
                                    + target.task
                                )
                            else:
                                eff_task = (
                                    f"[开局0-LLM自动备料进展：已执行 {pre_ensure_res.get('steps_executed')} 步，"
                                    f"剩余缺口={pre_ensure_res.get('remaining_deficit')}，原因={pre_ensure_res.get('stop_reason')}]\n"
                                    + target.task
                                )
                    except Exception:
                        pre_ensure_res = None

                if satisfied_by_zero_llm:
                    report: dict[str, Any] = {
                        "status": "answered",
                        "actor_requests": 0,
                        "model_requests": 0,
                        "zero_llm_pre_ensure": pre_ensure_res,
                        "task": eff_task,
                    }
                else:
                    rem_ep_timeout = max(0.5, min(ep_timeout, deadline_mono - time.monotonic()))
                    report = run_mc_agent(
                        eff_task,
                        self.bot,
                        chat_fn=self.chat_fn,
                        model_name=self.model_name,
                        max_calls=ep_max_calls,
                        run_timeout_s=rem_ep_timeout,
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
                        toolset=self.toolset,
                    )
                    if pre_ensure_res is not None:
                        report["pre_ensure"] = pre_ensure_res

            if target.key == "island_recon_sweep":
                self.recon_completed = True

            report["curriculum_episode"] = ep_index
            report["curriculum_target"] = target.key
            report["budget_class"] = target.budget_class
            report["budget_max_calls"] = ep_max_calls
            report["budget_timeout_s"] = round(ep_timeout, 1)
            report["event_log"] = str(ep_events_path)
            ep_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            # 统计本局收获与进榜分增量
            completed_after = set(completed_before)
            pick_after = pick_before
            iron_after = iron_before
            diamond_after = diamond_before
            obsidian_after = obsidian_before
            chunks_after = len(self.toolset.world_memory.visited_chunks)
            try:
                adv_after = self.bot.advancements(include_recipes=False)
                for r in adv_after.get("advancements") or []:
                    if isinstance(r, dict) and r.get("status") == "completed" and r.get("id"):
                        nid = str(r["id"])
                        completed_after.add(nid if ":" in nid else f"minecraft:{nid}")
                inv_after = self.bot.inventory()
                pick_after = _highest_pick_rank(inv_after)
                iron_after = _iron_units(inv_after)
                diamond_after = _strategic_units(inv_after, ("diamond",))
                obsidian_after = _strategic_units(inv_after, ("obsidian",))
            except Exception:
                pass

            gained_advs = sorted(completed_after - completed_before)
            score_after, breakdown = score_completed_advancements(completed_after)
            material_progress = (
                pick_after > pick_before
                or (target.key == "bootstrap_iron_age" and iron_after > iron_before)
                or (target.key in {"minecraft:story/shiny_gear", "minecraft:story/mine_diamond"} and diamond_after > diamond_before)
                or (target.key in {"minecraft:story/enchant_item", "minecraft:story/enter_the_nether"} and obsidian_after > obsidian_before)
                or (target.key == "island_recon_sweep" and chunks_after > chunks_before)
            )
            progress_made = bool(gained_advs) or material_progress
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
                "budget_class": target.budget_class,
                "budget_max_calls": ep_max_calls,
                "status": report.get("status"),
                "actor_requests": report.get("actor_requests"),
                "score_before": score_before,
                "score_after": score_after,
                "score_delta": score_after - score_before,
                "gained_advancements": gained_advs,
                "material_progress": material_progress,
                "cooled_down": target.key in self.cooldowns and self.cooldowns[target.key] > time.monotonic(),
                "transient_api_error": transient_api_err,
                "report_file": str(ep_report_path),
            }
            self.episodes.append(ep_row)

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

        try:
            self.toolset.world_memory.save(force=True)
        except Exception:
            pass

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
    owned = total_owned_counts(inv)
    best = 0
    for nm, cnt in owned.items():
        if cnt > 0:
            best = max(best, ranks.get(nm, 0))
    return best


def _iron_units(inv: dict[str, Any]) -> int:
    owned = total_owned_counts(inv)
    return owned.get("iron_ingot", 0) + owned.get("raw_iron", 0) + owned.get("iron_block", 0) * 9


def _strategic_units(inv: dict[str, Any], names: tuple[str, ...]) -> int:
    owned = total_owned_counts(inv)
    return sum(owned.get(n, 0) for n in names)
