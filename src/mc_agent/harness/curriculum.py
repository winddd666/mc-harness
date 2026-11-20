"""8 小时竞赛课程督导器（二期全闭环：持久会话 + 0-LLM 客观硬前置 + 双向反馈门控 + 单层抢占恢复 + 柔性异步熔炉工单）。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import json
import math
from pathlib import Path
import time
from typing import Any, Callable

import httpx

from ..bot import BotSession
from ..errors import ApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ..kb import RECIPES_1_21, normalize_dimension
from ..skills import InventoryManager, compute_advancement_frontier, highest_pickaxe_tier, total_owned_counts
from ..skills.ensure import EXTRA_OBJECTIVE_RECIPES, PhysicalPrerequisiteResolver, SMELT_SOURCES, collect_smelt_batch
from .events import EventSink, JsonlEvents
from .geofence import VoidGeofence
from .guardian import GenerationGuard, ReflexPreempted
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
    "micro": (8, 180.0),
    "short": (14, 300.0),
    "medium": (22, 600.0),
    "deep": (40, 1500.0),
    "expedition": (45, 1800.0),
}

PICK_MAX_DURABILITY: dict[str, int] = {
    "wooden_pickaxe": 59,
    "golden_pickaxe": 32,
    "stone_pickaxe": 131,
    "iron_pickaxe": 250,
    "diamond_pickaxe": 1561,
    "netherite_pickaxe": 2031,
}

# `shiny_gear` 穿好钻石靴锁住 75 分后的有界顺手摸奖窗口（秒）与主镐安全耐久红线
SHINY_GEAR_BONUS_WINDOW_S = 150.0
MAIN_PICK_DURABILITY_FLOOR = 35


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
    post_ensure_task: str | None = None

    def is_objectively_satisfied(self, completed_advs: set[str], bag_counts: dict[str, int]) -> bool:
        """Objective completion predicate — NEVER trusts historical advancements on `bootstrap_*` or LLM claims!"""
        if self.key == "bootstrap_stone_age":
            return highest_pickaxe_tier(bag_counts) >= 2
        if self.key == "bootstrap_iron_age":
            return highest_pickaxe_tier(bag_counts) >= 3
        if self.advancement_id:
            return self.advancement_id in completed_advs
        return False


@dataclasses.dataclass
class WorldJob:
    job_id: str
    kind: str
    dim: str
    anchor_pos: list[int]
    furnaces: list[dict[str, Any]]
    input_item: str
    output_item: str
    total_count: int
    started_at: float
    ready_at: float
    parent_target_key: str | None = None


@dataclasses.dataclass
class SuspendedEpisode:
    target: CurriculumTarget
    remaining_calls: int
    remaining_timeout_s: float
    anchor_pos: list[float] | None
    reason: str
    progress_digest: str
    waiting_job_id: str | None = None
    preempted_by_key: str | None = None


class WorldJobRegistry:
    """Tracks asynchronous furnace/station world jobs, `in_conversion_counts`, and 80m simulation leashes."""

    def __init__(self) -> None:
        self._seq = 0
        self.jobs: dict[str, WorldJob] = {}

    def register_furnace_job(
        self,
        *,
        dim: str,
        furnaces: list[dict[str, Any]],
        input_item: str,
        output_item: str,
        total_count: int,
        ready_at: float,
        parent_target_key: str | None = None,
    ) -> WorldJob:
        self._seq += 1
        jid = f"fjob-{self._seq}"
        anchor = list(furnaces[0]["pos"]) if furnaces and isinstance(furnaces[0].get("pos"), list) else [0, 64, 0]
        job = WorldJob(
            job_id=jid,
            kind="furnace",
            dim=normalize_dimension(dim),
            anchor_pos=[int(anchor[0]), int(anchor[1]), int(anchor[2])],
            furnaces=list(furnaces),
            input_item=input_item,
            output_item=output_item,
            total_count=int(total_count),
            started_at=time.monotonic(),
            ready_at=float(ready_at),
            parent_target_key=parent_target_key,
        )
        self.jobs[jid] = job
        return job

    @staticmethod
    def _horiz_dist(p1: Sequence[float] | None, p2: Sequence[float]) -> float:
        if p1 is None or len(p1) < 3:
            return 0.0
        return math.hypot(float(p1[0]) - float(p2[0]), float(p1[2]) - float(p2[2]))

    def find_jobs_for_output(
        self,
        output_item: str,
        *,
        origin: list[float] | tuple[float, float, float] | None = None,
        dim: str = "overworld",
        max_dist: float = 96.0,
    ) -> list[WorldJob]:
        norm_dim = normalize_dimension(dim)
        hits = [
            j
            for j in self.jobs.values()
            if j.dim == norm_dim
            and j.output_item == output_item
            and (origin is None or self._horiz_dist(origin, j.anchor_pos) <= max_dist)
        ]
        hits.sort(key=lambda j: j.ready_at)
        return hits

    def in_conversion_counts(
        self,
        *,
        origin: list[float] | tuple[float, float, float] | None = None,
        dim: str = "overworld",
        max_dist: float = 96.0,
        world_memory: WorldMemory | None = None,
    ) -> dict[str, int]:
        norm_dim = normalize_dimension(dim)
        counts: dict[str, int] = {}
        for j in self.jobs.values():
            if j.dim != norm_dim:
                continue
            if origin is not None and self._horiz_dist(origin, j.anchor_pos) > max_dist:
                continue
            counts[j.output_item] = counts.get(j.output_item, 0) + int(j.total_count)
        if world_memory is not None and hasattr(world_memory, "get_in_conversion_counts"):
            try:
                ext = world_memory.get_in_conversion_counts(origin=origin, dim=norm_dim, max_dist=max_dist)
                for k, v in (ext or {}).items():
                    counts[k] = max(counts.get(k, 0), int(v or 0))
            except Exception:
                pass
        return counts

    def active_leash(
        self,
        *,
        dim: str = "overworld",
        now: float | None = None,
    ) -> tuple[list[int], float] | None:
        """Return `(anchor_pos, 80.0)` simulation-distance leash when an unready furnace job is burning."""
        now_t = time.monotonic() if now is None else now
        norm_dim = normalize_dimension(dim)
        burning = [j for j in self.jobs.values() if j.dim == norm_dim and j.ready_at > now_t]
        if not burning:
            return None
        earliest = min(burning, key=lambda j: j.ready_at)
        return list(earliest.anchor_pos), 80.0

    def ready_jobs(
        self,
        *,
        origin: list[float] | tuple[float, float, float] | None = None,
        dim: str = "overworld",
        now: float | None = None,
        max_dist: float = 96.0,
    ) -> list[WorldJob]:
        now_t = time.monotonic() if now is None else now
        norm_dim = normalize_dimension(dim)
        out = [
            j
            for j in self.jobs.values()
            if j.dim == norm_dim
            and now_t >= j.ready_at
            and (origin is None or self._horiz_dist(origin, j.anchor_pos) <= max_dist)
        ]
        out.sort(key=lambda j: j.ready_at)
        return out

    def collect_job(self, skills: Any, job_id: str, *, wait_remaining_s: float = 12.0) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "unknown_job"}
        try:
            res = collect_smelt_batch(skills, job.furnaces, wait_remaining_s=wait_remaining_s)
        except (httpx.HTTPError, ApiError, TimeoutError, OSError) as exc:
            job.ready_at = max(job.ready_at, time.monotonic() + 10.0)
            return {
                "ok": False,
                "deferred": True,
                "still_burning": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if res.get("ok") and not res.get("still_burning"):
            self.jobs.pop(job_id, None)
        elif isinstance(res.get("remaining_furnaces"), list) and res["remaining_furnaces"]:
            job.furnaces = list(res["remaining_furnaces"])
        return res

    def collect_ready_jobs(
        self,
        skills: Any,
        *,
        origin: list[float] | tuple[float, float, float] | None = None,
        dim: str = "overworld",
        now: float | None = None,
        max_dist: float = 96.0,
    ) -> list[dict[str, Any]]:
        collected_reports: list[dict[str, Any]] = []
        for rjob in self.ready_jobs(origin=origin, dim=dim, now=now, max_dist=max_dist):
            rep = self.collect_job(skills, rjob.job_id, wait_remaining_s=6.0)
            collected_reports.append({"job_id": rjob.job_id, "output_item": rjob.output_item, **rep})
        return collected_reports


from typing import Sequence  # noqa: E402


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


# 优先级剧本库：客观镐级硬前置 + `post_ensure_task` 终局纯交互微文案
CURRICULUM_PLAYBOOKS: dict[str, CurriculumTarget] = {
    "bootstrap_stone_age": CurriculumTarget(
        key="bootstrap_stone_age",
        title="Day 1 早晨石器与据点箱：半组原木/木斧/石镐/据点留底箱",
        points=10,
        tier=0,
        budget_class="short",
        pre_ensure=(("stone_pickaxe", 1),),
        task=(
            "确保完成 Day 1 早晨石器与据点箱基建：0) 若包内已有 cobblestone>=3 且有木棍/木板或工作台，直接就地调 mc_skill_craft_gear('stone_pickaxe')，切勿在地下砍树；"
            "1) 若在水中先调 mc_skill_unstick 上岸；"
            "2) 在地表优先用 mc_skill_harvest_logs(target_logs=16) 收集约半组原木（16~20 块，凑足 12 块以上即可推进，不死磕）；"
            "3) 合工作台、wooden_axe（木斧，跳劈可 1 斧秒猪/牛/羊）与 wooden_pickaxe；"
            "4) 挖掘至少 11 块圆石合出 stone_pickaxe、stone_sword 与 furnace；"
            "5) 若在出生安全地表且木板充足，顺手合 1 个 chest 并放置为据点箱，把多余原木（随身留 6~8 块）存箱留底，防死亡爆装返贫。"
        ),
        extra_tools=("mc_skill_container",),
        advancement_id="minecraft:story/upgrade_tools",
    ),
    "bootstrap_iron_age": CurriculumTarget(
        key="bootstrap_iron_age",
        title="Day 1 铁煤层发育：封口下矿凑 8 铁+8 煤，合出铁镐+盾牌+铁甲",
        points=10,
        tier=0,
        budget_class="medium",
        # D3 根治：仅客观保底石镐（若包/近箱已有铁料则由督导器动态直出铁镐，绝不在 pre_ensure 盲挖地下矿井）
        pre_ensure=(("stone_pickaxe", 1),),
        task=(
            "将装备升级至铁器并完成铁煤层发育：1) 若记忆箱已有备用铁镐/铁锭，优先调 mc_skill_container 取回；"
            "2) 用石镐采露头铁煤或调 mc_skill_mine_dfs(target='iron', target_y=16, goal_count=8) 在铁/煤层凑齐 6~8 块 raw_iron 与至少 8 块 coal/charcoal；"
            "3) 调 mc_skill_smelt('raw_iron', count=6) 熔炼铁锭（若包内有生肉可顺手烤熟）；"
            "4) 优先调 mc_skill_craft_gear('iron_pickaxe')，铁锭充裕时顺手合出 shield（副手盾）与 1 件铁甲（如 iron_boots/iron_chestplate）完成安全成型即收工。"
        ),
        post_ensure_task=(
            "石镐已备妥：先看实时HUD的16m暴露列表，若已有暴露 iron_ore/deepslate_iron_ore 直接调 mc_skill_mine_blocks(['iron_ore', 'deepslate_iron_ore'], count=6) 采入；"
            "若地表无暴露铁矿再调 mc_skill_mine_dfs(target='iron', target_y=16, goal_count=8) 下井在铁/煤层发育；随后调 mc_skill_smelt('raw_iron', count=6) -> mc_skill_craft_gear('iron_pickaxe')（余铁顺出 shield 与 1 件铁甲），成型后立即收工。"
        ),
        extra_tools=("mc_skill_container",),
        advancement_id="minecraft:story/iron_tools",
    ),
    "minecraft:story/shiny_gear": CurriculumTarget(
        key="minecraft:story/shiny_gear",
        title="60分深潜钻石套餐：首4钻合 diamond_boots 锁分 + 150s 有界顺采",
        points=60,
        tier=1,
        budget_class="deep",
        pre_ensure=(("iron_pickaxe", 1), ("stone_pickaxe", 1)),
        pre_equip=(),
        task=(
            "执行 Y=-54 鱼骨深潜钻石套餐（目标 story/mine_diamond 15分 + story/shiny_gear 60分）："
            "1) 确认包内有 iron_pickaxe 与备用 stone_pickaxe；"
            "2) 调 mc_skill_mine_dfs(target='diamond', target_y=-54, goal_count=4) 在深板岩层鱼骨探钻；"
            "3) 一旦包内凑满首 4 颗 diamond，**第一时间立即就地调 mc_skill_craft_gear('diamond_boots', equip_slot='feet') 先锁定 75 分**！"
            "4) 穿好钻石靴后在主控允许的 150s 顺采窗口内可在同井顺采额外至多 5 颗钻石备钻镐与附魔台。"
        ),
        post_ensure_task=(
            "铁镐与备用石镐已齐备：直接持续调用 mc_skill_mine_dfs(action='explore', target='diamond', target_y=-54, goal_count=4) 深潜采钻"
            "（注意从地表/中层下探至 Y=-54 约需连续 10~12 次 explore 时间片，到达 Y=-54 后继续调 explore 会自动展开鱼骨支巷，严禁在深层调 reset 或 climb_out！）；"
            "一旦包内凑满首 4 颗 diamond，立即就地调 mc_skill_craft_gear('diamond_boots', equip_slot='feet') 穿戴锁住 75 分。"
        ),
        advancement_id="minecraft:story/shiny_gear",
    ),
    "minecraft:story/mine_diamond": CurriculumTarget(
        key="minecraft:story/mine_diamond",
        title="15分：Y=-54 深层挖掘首颗钻石",
        points=15,
        tier=2,
        budget_class="deep",
        pre_ensure=(("iron_pickaxe", 1),),
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
            "收集 12 圆石烧成 12 stone -> 12 stone_bricks -> 18 stone_brick_slab -> 8 chiseled_stone_bricks，"
            "与 5 铁锭、1 红石合出 lodestone 与 compass，放下 lodestone 后手持 compass 调 mc_interact(x,y,z,item='compass')。"
        ),
        post_ensure_task=(
            "解锁 adventure/use_lodestone(15分)：背包已备齐 lodestone×1 与 compass×1（无需再去箱子取料或烧石头！）！"
            "第1步：直接调 mc_place(item='lodestone')（切勿传 x/y/z，系统会自动在身旁空格放下并返回 placed pos）；"
            "第2步：读取 mc_place 返回的 pos=[px, py, pz]，调 mc_interact(x=px, y=py, z=pz, item='compass') 完成绑定。"
        ),
        extra_tools=("mc_interact",),
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
        post_ensure_task=(
            "解锁 story/lava_bucket(15分)：背包已备好空铁桶 bucket×1，直接前往静止 lava 旁安全岸边调 mc_interact(x,y,z,item='bucket') 舀取岩浆入包。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:story/lava_bucket",
    ),
    "minecraft:story/form_obsidian": CurriculumTarget(
        key="minecraft:story/form_obsidian",
        title="15分：钻石镐采集黑曜石入包",
        points=15,
        tier=1,
        budget_class="medium",
        pre_ensure=(("diamond_pickaxe", 1),),
        task=(
            "解锁 story/form_obsidian(15分)：原版要求将 obsidian 物品采集入包（仅生成黑曜石方块不触发，且必须使用 diamond_pickaxe 挖掘）；"
            "持 diamond_pickaxe 挖掘现成 obsidian（或用水桶浇静止岩浆生成后再用 diamond_pickaxe 挖取入包）。"
        ),
        post_ensure_task=(
            "解锁 story/form_obsidian(15分)：背包已备好 diamond_pickaxe！先调 mc_find_blocks(types=['obsidian', 'lava', 'water'], radius=48) 搜索："
            "1) 若已有现成 obsidian，走近安全侧调 mc_skill_mine_blocks(['obsidian'], count=1) 挖入背包；"
            "2) 若只有静止 lava 而无现成 obsidian：若附近有 water，用铁锭合 bucket 对 water 装水浇在静止 lava 旁侧方块硬化为 obsidian 后挖入背包；"
            "若当前在深层地下(Y<45)且 48m 内既无 obsidian 也无 water，可先用 bucket 舀一桶 lava_bucket 带走，再调 mc_skill_staircase(direction='north', steps=16, vertical='up') 分段回到地表水源(Y>=58)旁浇铸采集。"
        ),
        extra_tools=("mc_interact",),
        advancement_id="minecraft:story/form_obsidian",
    ),
    "minecraft:husbandry/make_a_sign_glow": CurriculumTarget(
        key="minecraft:husbandry/make_a_sign_glow",
        title="30分：发光墨囊涂亮木告示牌",
        points=30,
        tier=2,
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
        post_ensure_task=(
            "解锁 husbandry/tactical_fishing(15分)：背包已有 bucket，先对水源 mc_interact 装满成 water_bucket，再走近水中 cod/salmon/tropical_fish 调 mc_interact(entity_id=..., item='water_bucket')。"
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
        pre_ensure=(("crafter", 1), ("lever", 1), ("iron_ingot", 5), ("crafting_table", 1), ("redstone", 1), ("dropper", 1)),
        task=(
            "解锁 adventure/crafters_crafting_crafters(15分)：准备 1 个已合成的 crafter + 第 2 个 crafter 的配方原料（5 iron_ingot + 1 crafting_table + 1 redstone + 1 dropper），"
            "放置第 1 个 crafter 并用 mc_window_act 按槽位摆入 crafter 配方、接红石脉冲（如 redstone_torch/lever）激活产出。"
        ),
        post_ensure_task=(
            "解锁 adventure/crafters_crafting_crafters(15分)：背包已备齐 crafter×1 及第二套配方原料（5 iron_ingot + 1 crafting_table + 1 redstone + 1 dropper），"
            "直接放置 crafter，调 mc_window_act 按 3x3 槽位摆入配方并在旁边放置 lever/redstone_torch 触发红石脉冲吐出新 crafter。"
        ),
        extra_tools=("mc_interact", "mc_window_view", "mc_window_act"),
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
        extra_tools=("mc_shoot",),
        advancement_id="minecraft:adventure/ol_betsy",
    ),
    "minecraft:story/deflect_arrow": CurriculumTarget(
        key="minecraft:story/deflect_arrow",
        title="15分：举盾弹反骷髅箭矢",
        points=15,
        tier=2,
        budget_class="short",
        pre_ensure=(("shield", 1),),
        pre_equip=(("shield", "off-hand"),),
        task=(
            "解锁 story/deflect_arrow(15分)：确认副手已装备 shield，寻找附近 skeleton 面向其朝向，用 mc_posture(shield=True) 举盾等待弹反箭矢。"
        ),
        post_ensure_task=(
            "解锁 story/deflect_arrow(15分)：副手已装备好 shield，寻找附近 skeleton 对准其视线调 mc_posture(shield=True) 弹反一箭即可。"
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
        pre_equip=(),
        task="用现有铁锭调 mc_skill_craft_gear('iron_boots')（仅需4铁锭入包即触发 story/obtain_armor）。",
        post_ensure_task="iron_boots 已合成入包，检查进度即可完成 story/obtain_armor。",
        advancement_id="minecraft:story/obtain_armor",
    ),
    "minecraft:adventure/sleep_in_bed": CurriculumTarget(
        key="minecraft:adventure/sleep_in_bed",
        title="5分：做床并绑定/入睡",
        points=5,
        tier=2,
        budget_class="short",
        task="收集 3 羊毛+3木板合 white_bed（或前往村庄已有床）放置，调 mc_skill_bind_bed。",
        extra_tools=("mc_skill_bind_bed",),
        advancement_id="minecraft:adventure/sleep_in_bed",
    ),
    "minecraft:adventure/trade": CurriculumTarget(
        key="minecraft:adventure/trade",
        title="15分：与村民完成一次交易",
        points=15,
        tier=2,
        budget_class="short",
        task=(
            "解锁 adventure/trade(15分)：前往已知村民聚落扫描 villager 实体，用 mc_window_view(entity_id=...) 查看交易选项；"
            "一旦某个村民返回 available=true 且背包已备齐对应 give 物品（如 paper/emerald/wheat/coal），立即对该村民调 mc_skill_trade(entity_id=..., option=...) 完成交易（不要再逛其他村民）！"
            "若某村民返回 no_window 说明其无业或在睡觉，严禁对同一村民重复重试。"
        ),
        extra_tools=("mc_skill_trade", "mc_window_view"),
        advancement_id="minecraft:adventure/trade",
    ),
    "minecraft:adventure/kill_a_mob": CurriculumTarget(
        key="minecraft:adventure/kill_a_mob",
        title="5分：击杀任意敌对生物",
        points=5,
        tier=2,
        budget_class="short",
        task="装备最高近战剑/斧与盾，扫描附近敌对生物（僵尸/蜘蛛/骷髅）并安全击杀，注意低血量进掩体恢复。",
        extra_tools=("mc_posture",),
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
        extra_tools=("mc_skill_enchant",),
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
        post_ensure_task=(
            "背包已备齐 4 个 iron_block 与 shears：寻找南瓜用 shears 调 mc_interact 剪出 carved_pumpkin，摆好 4 铁块 T 字形最后在顶端放置 carved_pumpkin。"
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
        pre_ensure=(("obsidian", 10), ("flint_and_steel", 1)),
        task=(
            "解锁 story/enter_the_nether(30分)：准备至少 10 块 obsidian 与 flint_and_steel 后，直接调用 mc_skill_build_portal() 一键清地、搭建 4x5 黑曜石门框、点火并走入传送门进入下界。"
        ),
        post_ensure_task=(
            "背包已备齐 obsidian>=10 与 flint_and_steel！严禁再去挖矿或找铁，请立即调用 mc_skill_build_portal() 一键搭建 4x5 黑曜石传送门、点火并步入下界！"
        ),
        extra_tools=("mc_skill_build_portal", "mc_interact", "mc_posture"),
        advancement_id="minecraft:story/enter_the_nether",
    ),
    "minecraft:nether/distract_piglin": CurriculumTarget(
        key="minecraft:nether/distract_piglin",
        title="5分：用金锭转移猪灵注意力（下界）",
        points=5,
        tier=3,
        budget_class="short",
        task=(
            "解锁 nether/distract_piglin(5分)：在下界先确保穿戴 golden_boots（避免猪灵敌对），扫描附近 piglin 实体，"
            "靠近至 6 格内用 mc_interact(entity_id=..., item='gold_ingot') 或丢出 1 个 gold_ingot 让猪灵拾取交易即可解锁。"
        ),
        extra_tools=("mc_interact", "mc_drop"),
        advancement_id="minecraft:nether/distract_piglin",
    ),
    "minecraft:nether/find_fortress": CurriculumTarget(
        key="minecraft:nether/find_fortress",
        title="30分：在下界探索并定位下界要塞",
        points=30,
        tier=3,
        budget_class="expedition",
        task=(
            "解锁 nether/find_fortress(30分)：当前已身处下界，请勿穿回主世界传送门！沿安全实心下界岩地形分段探索，"
            "扫描 nether_bricks / nether_brick_fence / blaze / wither_skeleton，进入下界要塞区域即可解锁。"
        ),
        extra_tools=("mc_posture",),
        advancement_id="minecraft:nether/find_fortress",
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
    if any(k in text for k in ("interact", "bucket", "lodestone", "sign", "seed", "breed", "tame", "honey", "wax", "flint", "portal", "shears", "boat", "fish")):
        extra.append("mc_interact")
    if any(k in text for k in ("potion", "milk", "ominous", "diet", "eat", "golden_apple", "stew", "kelp", "spyglass", "ender_pearl", "goat_horn", "trident")):
        extra.append("mc_use_item")
    if any(k in text for k in ("shield", "deflect", "sneak", "avoid_vibration")):
        extra.append("mc_posture")
    if any(k in text for k in ("spyglass", "deflect")):
        extra.append("mc_look")
    if any(k in text for k in ("bow", "arrow", "crossbow", "trident", "bullseye", "sniper", "arbalistic", "two_birds")):
        extra.extend(["mc_shoot", "mc_posture"])
    if "enchant" in text or "附魔" in text:
        extra.append("mc_skill_enchant")
    if any(k in text for k in ("trade", "villager", "交易", "村民")):
        extra.extend(["mc_skill_trade", "mc_window_view"])
    if any(k in text for k in ("brew", "酿", "药水")):
        extra.append("mc_skill_brew")
    if any(k in text for k in ("trim", "smithing", "netherite_armor", "锻造", "纹饰", "下界合金")):
        extra.append("mc_skill_smithing")
    if any(k in text for k in ("chest", "loot", "bastion", "箱", "战利品")):
        extra.append("mc_skill_container")
    if any(k in text for k in ("crafter", "decorated_pot", "vault", "bookshelf", "jukebox")):
        extra.extend(["mc_window_view", "mc_window_act"])
    if "bed" in text or "sleep" in text:
        extra.append("mc_skill_bind_bed")
    return tuple(dict.fromkeys(extra))


_LATE_GRIND_FRONTIER_IDS: frozenset[str] = frozenset(
    {
        "minecraft:adventure/crafters_crafting_crafters",  # mc-agent-api returns unsupported_window (minecraft:crafter_3x3)
        "minecraft:adventure/throw_trident",
        "minecraft:adventure/arbalistic",
        "minecraft:adventure/two_birds_one_arrow",
        "minecraft:adventure/sniper_duel",
        "minecraft:adventure/totem_of_undying",
        "minecraft:adventure/voluntary_exile",
        "minecraft:adventure/whos_the_pillager_now",
    }
)

# Advancements whose required Minecraft GUI/protocol is currently unsupported by mc-agent-api.
_UNSUPPORTED_API_ADVANCEMENTS: frozenset[str] = frozenset(
    {
        "minecraft:adventure/crafters_crafting_crafters",
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
        "http 525",
        "http 526",
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
    origin: Sequence[float] | None = None,
    dim: str = "overworld",
    max_chest_dist: float = 72.0,
    world_jobs: WorldJobRegistry | None = None,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return `(bag_and_equipped, reachable_chest_only, total_combined_with_conversions)` in `dim`."""
    bag_counts = total_owned_counts(inv)
    chest_counts: dict[str, int] = {}
    norm_dim = normalize_dimension(dim)
    if world_memory is not None:
        for (cdim, cx, cy, cz), meta in world_memory.containers.items():
            if cdim != norm_dim:
                continue
            if origin is not None and len(origin) >= 3:
                dist = math.sqrt((cx - float(origin[0])) ** 2 + (cy - float(origin[1])) ** 2 + (cz - float(origin[2])) ** 2)
                if dist > max_chest_dist:
                    continue
            for k, v in (meta.get("items") or {}).items():
                iv = int(v or 0)
                if iv > 0:
                    chest_counts[k] = chest_counts.get(k, 0) + iv
    combined = dict(bag_counts)
    for k, v in chest_counts.items():
        combined[k] = combined.get(k, 0) + v
    conv_counts = (
        world_jobs.in_conversion_counts(origin=origin, dim=norm_dim, max_dist=max_chest_dist + 24.0, world_memory=world_memory)
        if world_jobs is not None
        else {}
    )
    for k, v in conv_counts.items():
        combined[k] = combined.get(k, 0) + v
    return bag_counts, chest_counts, combined


def _can_attempt_target(adv_id: str, counts: dict[str, int], *, has_iron_or_better: bool) -> bool:
    """客观物理硬前置过滤（修复 D1 ★：严禁把空包铁镐当成缺红石/缺钻镐任务的万能通行证）。"""
    iron_equiv = counts.get("iron_ingot", 0) + counts.get("raw_iron", 0) + counts.get("iron_block", 0) * 9
    has_bucket = any(
        counts.get(k, 0) > 0 for k in ("bucket", "water_bucket", "lava_bucket", "cod_bucket", "salmon_bucket")
    )
    has_diamond_pick = counts.get("diamond_pickaxe", 0) > 0 or counts.get("netherite_pickaxe", 0) > 0

    if adv_id in {"minecraft:story/shiny_gear", "minecraft:story/mine_diamond"}:
        return has_iron_or_better or counts.get("diamond", 0) >= 1
    if adv_id == "minecraft:story/form_obsidian":
        # Vanilla 1.21 `form_obsidian` triggers on `obsidian` item entering inventory -> requires diamond pickaxe!
        return has_diamond_pick or counts.get("obsidian", 0) >= 1 or counts.get("diamond", 0) >= 3
    if adv_id == "minecraft:story/enchant_item":
        return (
            has_diamond_pick
            or counts.get("diamond", 0) >= 5
            or counts.get("obsidian", 0) >= 4
            or counts.get("enchanting_table", 0) > 0
        )
    if adv_id == "minecraft:adventure/crafters_crafting_crafters":
        return (
            counts.get("crafter", 0) >= 2
            or (counts.get("crafter", 0) >= 1 and iron_equiv >= 5 and counts.get("redstone", 0) >= 1)
            or (iron_equiv >= 6 and counts.get("redstone", 0) >= 2)
        )
    if adv_id == "minecraft:adventure/use_lodestone":
        has_lode = counts.get("lodestone", 0) >= 1
        has_comp = counts.get("compass", 0) >= 1
        return (
            (has_lode and has_comp)
            or (has_lode and (has_comp or (iron_equiv >= 4 and counts.get("redstone", 0) >= 1)))
            or (iron_equiv >= 5 and counts.get("redstone", 0) >= 1)
        )
    if adv_id in {"minecraft:story/lava_bucket", "minecraft:husbandry/tactical_fishing"}:
        return has_bucket or iron_equiv >= 3
    if adv_id in {
        "minecraft:adventure/ol_betsy",
        "minecraft:adventure/whos_the_pillager_now",
        "minecraft:adventure/arbalistic",
        "minecraft:adventure/two_birds_one_arrow",
    }:
        return counts.get("crossbow", 0) > 0 or (iron_equiv >= 2 and counts.get("string", 0) >= 2)
    if adv_id in {"minecraft:adventure/shoot_arrow", "minecraft:adventure/sniper_duel"}:
        return (
            counts.get("bow", 0) > 0
            or counts.get("crossbow", 0) > 0
            or counts.get("string", 0) >= 3
        )
    if adv_id == "minecraft:husbandry/fishy_business":
        return counts.get("fishing_rod", 0) > 0 or counts.get("string", 0) >= 2
    if adv_id == "minecraft:husbandry/tame_an_animal":
        return any(counts.get(k, 0) >= 1 for k in ("bone", "cod", "salmon", "tropical_fish", "saddle"))
    if adv_id == "minecraft:story/deflect_arrow":
        return counts.get("shield", 0) > 0 or iron_equiv >= 1
    if adv_id == "minecraft:story/obtain_armor":
        return iron_equiv >= 4 or any(
            counts.get(a, 0) > 0 for a in ("iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots")
        )
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
        return has_diamond_pick or has_bucket or counts.get("obsidian", 0) >= 10
    if adv_id == "minecraft:story/iron_tools":
        return iron_equiv >= 3 or has_iron_or_better
    if adv_id == "minecraft:husbandry/make_a_sign_glow":
        return counts.get("glow_ink_sac", 0) >= 1 or has_iron_or_better
    if adv_id == "minecraft:nether/ride_strider":
        return counts.get("saddle", 0) >= 1
    if adv_id in {"minecraft:nether/obtain_ancient_debris", "minecraft:nether/obtain_crying_obsidian"}:
        return has_diamond_pick
    if adv_id == "minecraft:story/follow_ender_eye":
        return counts.get("ender_eye", 0) >= 1 or (
            (counts.get("blaze_rod", 0) >= 1 or counts.get("blaze_powder", 0) >= 1)
            and counts.get("ender_pearl", 0) >= 1
        )
    if adv_id == "minecraft:nether/distract_piglin":
        return counts.get("gold_ingot", 0) >= 1 or counts.get("gold_nugget", 0) >= 9
    if adv_id == "minecraft:adventure/heart_transplanter":
        return counts.get("creaking_heart", 0) >= 1 or counts.get("pale_oak_log", 0) >= 2
    if adv_id == "minecraft:adventure/kill_mob_near_sculk_catalyst":
        return counts.get("sculk_catalyst", 0) >= 1
    if adv_id == "minecraft:story/cure_zombie_villager":
        return counts.get("golden_apple", 0) >= 1 and (
            counts.get("splash_potion", 0) >= 1 or counts.get("fermented_spider_eye", 0) >= 1
        )
    if adv_id == "minecraft:husbandry/plant_seed":
        return any(
            counts.get(k, 0) >= 1
            for k in (
                "wheat_seeds",
                "beetroot_seeds",
                "melon_seeds",
                "pumpkin_seeds",
                "carrot",
                "potato",
                "wooden_hoe",
                "stone_hoe",
                "iron_hoe",
                "diamond_hoe",
            )
        )
    return True


def _enrich_target_with_world_memory(
    target: CurriculumTarget,
    *,
    world_memory: WorldMemory | None,
    bag_counts: dict[str, int],
    chest_counts: dict[str, int],
    origin: list[float] | tuple[float, float, float] | None,
    dim: str = "overworld",
    include_storage_hint: bool = True,
    task_override: str | None = None,
) -> tuple[CurriculumTarget, bool]:
    """Attach concrete WorldMemory coordinates / storage chest withdrawal hints and report if boosted."""
    base_task = task_override if task_override is not None else target.task
    if world_memory is None:
        return (dataclasses.replace(target, task=base_task) if task_override is not None else target), False

    notes: list[str] = []
    extra_add: list[str] = []
    boosted = False
    pos = origin if (origin is not None and len(origin) >= 3) else [0.0, 64.0, 0.0]
    norm_dim = normalize_dimension(dim)

    if include_storage_hint:
        watch_items = (
            "iron_pickaxe",
            "diamond_pickaxe",
            "iron_ingot",
            "raw_iron",
            "diamond",
            "obsidian",
            "bucket",
            "water_bucket",
            "redstone",
            "lapis_lazuli",
            "emerald",
            "wheat",
            "white_bed",
        )
        stored_useful = [
            f"{it}×{chest_counts[it]}"
            for it in watch_items
            if chest_counts.get(it, 0) > 0 and bag_counts.get(it, 0) == 0
        ]
        if stored_useful:
            hits = world_memory.query_container_item(stored_useful[0].split("×")[0], origin=pos, dim=norm_dim)
            if hits and float(hits[0].get("dist", 999.0)) <= 72.0:
                h0 = hits[0]
                notes.append(
                    f"[仓储提示：记忆库 {h0['block']}@{h0['pos']}({h0['dist']}m) 存有 {', '.join(stored_useful[:4])}，"
                    "优先调 mc_skill_container(action='take') 取出复用，勿重复采矿]"
                )
                extra_add.append("mc_skill_container")

    adv_id = target.advancement_id or target.key
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

    elif adv_id == "minecraft:story/deflect_arrow":
        skel_hits = world_memory.query_habitats(["skeleton"], origin=pos, dim=norm_dim, limit=1)
        if skel_hits:
            sk0 = skel_hits[0]
            notes.append(f"[记忆库提权：已知 skeleton 活动点位于 {sk0['approx_pos']}（{sk0['dist']}m），副手持盾前往弹反一箭]")
            boosted = True

    elif adv_id == "minecraft:husbandry/make_a_sign_glow":
        glow_hits = world_memory.query_habitats(["glow_squid"], origin=pos, dim=norm_dim, limit=1)
        if (bag_counts.get("glow_ink_sac", 0) + chest_counts.get("glow_ink_sac", 0)) >= 1:
            notes.append("[物资就绪：包/箱内已有 glow_ink_sac，直接合成木告示牌放置并调 mc_interact(item='glow_ink_sac') 立拿 30 分]")
            boosted = True
        elif glow_hits:
            g0 = glow_hits[0]
            gy = float((g0.get("approx_pos") or [0, 0, 0])[1])
            if float(g0.get("dist", 999.0)) <= 28.0 and gy >= 42.0:
                notes.append(f"[记忆库提权：已探明浅层 glow_squid 位于 {g0['approx_pos']}（{g0['dist']}m），前往击杀拾取 glow_ink_sac 并涂亮木告示牌]")
                boosted = True

    if not notes and not extra_add and not boosted and task_override is None:
        return target, False

    merged_extra = tuple(dict.fromkeys([*target.extra_tools, *extra_add]))
    new_task = ("\n".join(notes) + "\n" + base_task) if notes else base_task
    new_tier = 1 if (boosted and target.tier == 2) else target.tier
    updated = dataclasses.replace(target, tier=new_tier, task=new_task, extra_tools=merged_extra)
    return updated, boosted


def _compute_reserved_materials(
    suspended_ep: SuspendedEpisode | None,
    bag_counts: dict[str, int],
) -> dict[str, int]:
    """Compute raw/ingot strategic materials reserved by a suspended parent episode's unmet `pre_ensure` items."""
    if suspended_ep is None or not suspended_ep.target.pre_ensure:
        return {}
    reserved: dict[str, int] = {}

    def _accumulate(item: str, need: int, depth: int = 0) -> None:
        if need <= 0 or depth > 6:
            return
        rec = RECIPES_1_21.get(item) or EXTRA_OBJECTIVE_RECIPES.get(item)
        if rec is not None:
            y_cnt = max(1, int(rec.get("yield_count", 1)))
            batches = math.ceil(need / y_cnt)
            for ing, per_b in (rec.get("ingredients") or {}).items():
                _accumulate(str(ing), int(per_b) * batches, depth + 1)
            return
        if item in SMELT_SOURCES:
            reserved[item] = reserved.get(item, 0) + need
            return
        reserved[item] = reserved.get(item, 0) + need

    for req_item, req_cnt in suspended_ep.target.pre_ensure:
        deficit = int(req_cnt) - int(bag_counts.get(req_item, 0))
        if deficit > 0:
            _accumulate(req_item, deficit)
    return reserved


def _subtract_reserved_counts(counts: dict[str, int], reserved: dict[str, int]) -> dict[str, int]:
    """Subtract suspended parent's reserved strategic items so opportunistic tasks never cannibalize them."""
    if not reserved:
        return counts
    out = dict(counts)
    # Handle iron equivalence (`iron_ingot` + `raw_iron`)
    iron_res = int(reserved.get("iron_ingot", 0)) + int(reserved.get("raw_iron", 0))
    if iron_res > 0:
        take_ing = min(out.get("iron_ingot", 0), iron_res)
        out["iron_ingot"] = out.get("iron_ingot", 0) - take_ing
        rem_iron = iron_res - take_ing
        if rem_iron > 0:
            out["raw_iron"] = max(0, out.get("raw_iron", 0) - rem_iron)
    for k, r_cnt in reserved.items():
        if k in {"iron_ingot", "raw_iron"} or r_cnt <= 0:
            continue
        if k in out:
            out[k] = max(0, out[k] - int(r_cnt))
    return out


def _build_island_recon_target(
    origin_pos: Sequence[float] | None,
    unexp: Sequence[str],
    *,
    tier: int,
) -> CurriculumTarget:
    dirs = list(unexp[:3]) if unexp else ["east", "north", "west"]
    dir_hint = "/".join(dirs)
    if origin_pos is not None and len(origin_pos) >= 3:
        ox = int(round(float(origin_pos[0])))
        oz = int(round(float(origin_pos[2])))
        dir_vec = {"north": (0, -70), "south": (0, 70), "east": (70, 0), "west": (-70, 0)}
        examples = []
        for d in dirs[:2]:
            dx, dz = dir_vec.get(d, (70, 0))
            examples.append(f"{d}->mc_move_to(x={ox + dx}, z={oz + dz}, range=4)")
        anchor_clause = (
            f"当前基准坐标为 ({ox}, {oz})，请沿未探索方向({dir_hint})基于当前真实坐标加减 60~80 格移动"
            f"（严禁传 (0, ±75) 等世界原点坐标！示例：{'，'.join(examples)}；"
            f"注意 range 固定填 4！若途中进入水域 in_water=true 必须立即退回 ({ox}, {oz}) 附近干燥陆地！），"
        )
    else:
        anchor_clause = (
            f"请先读取最新 `[实时HUD]` 格坐标，沿未探索方向({dir_hint})基于真实坐标加减 60~80 格调 mc_move_to(x=..., z=..., range=4)"
            "（严禁盲传 (0, ±75) 原点坐标！注意 range 固定填 4！若途中进入水域 in_water=true 必须立即退回出发干燥陆地！），"
        )
    return CurriculumTarget(
        key="island_recon_sweep",
        title="Day 1 白天肉食补给与环岛摸底：木斧收肉 + 探浅矿洞/村庄/岩浆并选下矿点",
        points=20,
        tier=tier,
        budget_class="micro",
        task=(
            "执行一次紧凑 Day 1 白天地表补给与环岛摸底（严禁挖矿、严禁入海）："
            + anchor_clause
            + "1) 若途中 24m 内发现猪/牛/羊且包内食物不足 6 块，顺手装备木斧/石斧调 mc_attack 收 2~3 只基础肉类与羊毛（无动物则直接跳过，绝不纠缠）；"
            "2) 每到一处干燥陆地调 mc_find_blocks(radius=32, types=['lava','water','coal_ore','iron_ore','chest','bell','lectern','hay_block','sugar_cane','pumpkin','obsidian'])"
            "与 mc_get_entities(radius=32)（不传 types 同时扫描 villager 与动物），探明附近浅矿洞/露头煤铁并选定干燥平地作为晚上下矿点，探明 2 个方向后回到干燥陆地汇报收工。"
        ),
        extra_tools=(),
        advancement_id=None,
    )


def select_next_target(
    bot: BotSession,
    *,
    cooldowns: dict[str, float] | None = None,
    now: float | None = None,
    world_memory: WorldMemory | None = None,
    enable_island_recon: bool = False,
    recon_done: bool = False,
    world_jobs: WorldJobRegistry | None = None,
    leash: tuple[Sequence[float], float] | None = None,
    exclude_keys: set[str] | None = None,
    reserved_counts: dict[str, int] | None = None,
) -> tuple[CurriculumTarget, dict[str, Any]]:
    """结合随身+72m仓储箱+在途熔炉联合库存、WorldMemory 地标提权、保活拴绳与冷却选出最优目标。"""
    now_t = time.monotonic() if now is None else now
    cd = dict(cooldowns or {})
    excluded = set(exclude_keys or ())

    def _is_cooled(key: str) -> bool:
        return key in excluded or cd.get(key, 0.0) > now_t

    origin_pos: list[float] | None = None
    cur_dim = "overworld"
    is_day_now = True
    time_of_day: int | None = None
    has_hostile_now = False
    try:
        st = bot.status()
        if isinstance(st.get("pos"), (list, tuple)) and len(st["pos"]) >= 3:
            origin_pos = [float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])]
        cur_dim = normalize_dimension(st.get("dimension"))
        is_day_now = bool(st.get("is_day", True))
        raw_tod = st.get("time_of_day")
        if isinstance(raw_tod, (int, float)):
            time_of_day = int(raw_tod) % 24000
        nh_s = st.get("nearest_hostile")
        has_hostile_now = isinstance(nh_s, dict) and bool(nh_s.get("name"))
    except Exception:
        pass
    is_dusk_or_night = (not is_day_now) or (time_of_day is not None and 11000 <= time_of_day < 23200)

    inv = bot.inventory()
    bag_counts, chest_counts, raw_combined = _combined_available_counts(
        inv,
        world_memory,
        origin=origin_pos,
        dim=cur_dim,
        max_chest_dist=72.0,
        world_jobs=world_jobs,
    )
    counts = _subtract_reserved_counts(raw_combined, reserved_counts or {})
    eff_bag_for_boost = _subtract_reserved_counts(bag_counts, reserved_counts or {})
    eff_chest_for_boost = (
        _subtract_reserved_counts(chest_counts, {k: max(0, v - bag_counts.get(k, 0)) for k, v in (reserved_counts or {}).items()})
        if reserved_counts
        else chest_counts
    )

    frontier_data = compute_advancement_frontier(bot, limit=50)
    completed = set(frontier_data.get("completed_ids") or [])
    frontier_rows = frontier_data.get("frontier") or []
    frontier_by_id = {str(r.get("id")): r for r in frontier_rows if isinstance(r, dict) and r.get("id")}

    has_stone_or_better = any(
        counts.get(k, 0) > 0 for k in ("stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")
    )
    has_iron_or_better = any(counts.get(k, 0) > 0 for k in ("iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"))

    # When an active furnace leash is in effect, restrict opportunistic work to micro/short surface/local tasks!
    leash_active = leash is not None

    if not leash_active:
        if cur_dim == "overworld" and not has_stone_or_better and not _is_cooled("bootstrap_stone_age"):
            pb0, _ = _enrich_target_with_world_memory(
                CURRICULUM_PLAYBOOKS["bootstrap_stone_age"],
                world_memory=world_memory,
                bag_counts=bag_counts,
                chest_counts=chest_counts,
                origin=origin_pos,
                dim=cur_dim,
            )
            has_wood_for_pick = (
                counts.get("stick", 0) >= 2
                or any(k.endswith("_planks") and v >= 2 for k, v in counts.items())
                or any(k.endswith("_log") and v >= 1 for k, v in counts.items())
            )
            if origin_pos is not None and float(origin_pos[1]) < 55.0 and not has_wood_for_pick:
                pb0 = dataclasses.replace(
                    pb0,
                    task=(
                        f"[地下无树警示：你当前位于地下 y={int(origin_pos[1])}，地下不存在原木且包内缺木料！"
                        "请先调 mc_skill_mine_dfs(action='climb_out') 或 mc_skill_staircase(vertical='up', steps=10) 爬回地表 y>=60 后再砍树，"
                        "严禁在地下调 mc_skill_harvest_logs 或反复调用缺木棍的 mc_skill_craft_gear！]\n"
                        + pb0.task
                    ),
                )
            return pb0, frontier_data
        # Run 1 fast daytime surface recon right after stone_pickaxe (ONLY during safe daytime; dusk/night skips straight underground!)
        if (
            enable_island_recon
            and not recon_done
            and not is_dusk_or_night
            and world_memory is not None
            and cur_dim == "overworld"
            and (origin_pos is None or float(origin_pos[1]) >= 52.0)
            and not _is_cooled("island_recon_sweep")
        ):
            has_villager_mem = bool(world_memory.habitats.get(("overworld", "villager")))
            has_lava_mem = any(e.name == "lava" and e.dim == "overworld" for e in world_memory.pois.values())
            if not (has_villager_mem and has_lava_mem):
                unexp = world_memory.unexplored_directions(origin_pos or [0.0, 64.0, 0.0], dim="overworld")
                return _build_island_recon_target(origin_pos, unexp, tier=0), frontier_data
        if cur_dim == "overworld" and not has_iron_or_better and not _is_cooled("bootstrap_iron_age"):
            pb1, _ = _enrich_target_with_world_memory(
                CURRICULUM_PLAYBOOKS["bootstrap_iron_age"],
                world_memory=world_memory,
                bag_counts=bag_counts,
                chest_counts=chest_counts,
                origin=origin_pos,
                dim=cur_dim,
            )
            if is_dusk_or_night:
                night_banner = (
                    "[入夜强制转地下铁煤层发育：天已入黄昏/黑夜，下井前 2 步先用圆石封堵井口防夜怪跌入！"
                    "请安心在 Y=16 铁/煤层凑足 6~8 块铁与 8+ 煤，就地熔炼并合出 iron_pickaxe + shield + 1 件铁甲，天亮前勿盲目回地表！]\n"
                )
                pb1 = dataclasses.replace(
                    pb1,
                    task=night_banner + pb1.task,
                    post_ensure_task=(night_banner + pb1.post_ensure_task) if pb1.post_ensure_task else None,
                )
            return pb1, frontier_data

    candidates: list[tuple[tuple[int, int, int, str], CurriculumTarget]] = []

    if (
        not leash_active
        and enable_island_recon
        and not recon_done
        and world_memory is not None
        and cur_dim == "overworld"
        and not _is_cooled("island_recon_sweep")
    ):
        ow_chunks = sum(1 for (d, _, _) in world_memory.visited_chunks if d == "overworld")
        has_villager_mem = bool(world_memory.habitats.get(("overworld", "villager")))
        has_lava_mem = any(e.name == "lava" and e.dim == "overworld" for e in world_memory.pois.values())
        if ow_chunks < 16 and not (has_villager_mem and has_lava_mem) and (origin_pos is None or float(origin_pos[1]) >= 52.0):
            unexp = world_memory.unexplored_directions(origin_pos or [0.0, 64.0, 0.0], dim="overworld")
            recon_target = _build_island_recon_target(origin_pos, unexp, tier=1)
            candidates.append(((1, 0, -65, "island_recon_sweep"), recon_target))

    _SURFACE_ONLY_TARGETS = frozenset({
        "minecraft:husbandry/plant_seed",
        "minecraft:husbandry/breed_an_animal",
        "minecraft:adventure/sleep_in_bed",
        "minecraft:husbandry/tame_an_animal",
        "minecraft:adventure/trade",
    })
    is_deep_underground = cur_dim == "overworld" and origin_pos is not None and float(origin_pos[1]) < 48.0

    for key, pb in CURRICULUM_PLAYBOOKS.items():
        if key.startswith("bootstrap_"):
            continue
        if is_deep_underground and key in _SURFACE_ONLY_TARGETS:
            continue
        if key == "minecraft:adventure/sleep_in_bed" and is_day_now:
            continue
        if leash_active and pb.budget_class not in {"micro", "short"}:
            continue
        if leash_active and key == "minecraft:adventure/kill_a_mob" and is_day_now and not has_hostile_now:
            continue
        adv_id = pb.advancement_id or key
        if adv_id in completed or adv_id in _UNSUPPORTED_API_ADVANCEMENTS or _is_cooled(key):
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
            bag_counts=eff_bag_for_boost,
            chest_counts=eff_chest_for_boost,
            origin=origin_pos,
            dim=cur_dim,
            include_storage_hint=not leash_active,
        )
        if leash_active and key in {"minecraft:adventure/sleep_in_bed", "minecraft:husbandry/breed_an_animal"} and not mem_boosted:
            continue
        if leash_active and leash is not None:
            l_pos, l_rad = leash
            enriched_pb = dataclasses.replace(
                enriched_pb,
                task=f"[后台熔炉保活围栏：请保持在熔炉 {list(l_pos)} 周围 {int(l_rad)}m 内顺手执行，勿远航或跨维度]\n" + enriched_pb.task,
            )
        if key == "minecraft:story/deflect_arrow" and not is_day_now and enriched_pb.tier == 2:
            enriched_pb = dataclasses.replace(enriched_pb, tier=1)
            mem_boosted = True
        if key == "minecraft:story/enter_the_nether" and counts.get("obsidian", 0) >= 10 and counts.get("flint_and_steel", 0) >= 1:
            enriched_pb = dataclasses.replace(enriched_pb, tier=1)
            mem_boosted = True
        if cur_dim == "the_nether":
            if key.startswith("minecraft:nether/"):
                enriched_pb = dataclasses.replace(enriched_pb, tier=1)
                mem_boosted = True
            else:
                enriched_pb = dataclasses.replace(enriched_pb, tier=3)
                mem_boosted = False
        is_uncond = 0 if (adv_id in UNCONDITIONAL_ADVANCEMENTS or mem_boosted) else 1
        sort_key = (enriched_pb.tier, is_uncond, -enriched_pb.points, key)
        candidates.append((sort_key, enriched_pb))

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
        if leash_active and budget_cls not in {"micro", "short"}:
            continue
        if cur_dim == "the_nether" and adv_id.startswith("minecraft:nether/") and adv_id not in _LATE_GRIND_FRONTIER_IDS:
            frontier_tier = 1
        elif cur_dim == "the_nether" and not adv_id.startswith("minecraft:nether/"):
            frontier_tier = 3
        elif adv_id.startswith(("minecraft:nether/", "minecraft:end/")) or adv_id in _LATE_GRIND_FRONTIER_IDS:
            frontier_tier = 3
        else:
            frontier_tier = 2
        raw_target = CurriculumTarget(
            key=adv_id,
            title=f"{pts}分：{title_zh} ({adv_id.removeprefix('minecraft:')})",
            points=pts,
            tier=frontier_tier,
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
            bag_counts=eff_bag_for_boost,
            chest_counts=eff_chest_for_boost,
            origin=origin_pos,
            dim=cur_dim,
            include_storage_hint=not leash_active,
        )
        if leash_active and leash is not None:
            l_pos, l_rad = leash
            enriched_t = dataclasses.replace(
                enriched_t,
                task=f"[后台熔炉保活围栏：请保持在熔炉 {list(l_pos)} 周围 {int(l_rad)}m 内顺手执行，勿远航或跨维度]\n" + enriched_t.task,
            )
        is_uncond = 0 if (adv_id in UNCONDITIONAL_ADVANCEMENTS or mem_boosted) else 1
        candidates.append(((enriched_t.tier, is_uncond, -pts, adv_id), enriched_t))

    if leash_active and not candidates and leash is not None:
        l_pos, l_rad = leash
        return (
            CurriculumTarget(
                key="furnace_leash_prep",
                title="熔炉等待期顺手整备（砍木/打草/扫周边地标）",
                points=0,
                tier=1,
                budget_class="micro",
                task=(
                    f"附近熔炉@{list(l_pos)} 正在后台烧炼物资；请在距离熔炉 <= {int(l_rad)}m 范围内顺手执行轻量整备："
                    "若原木 < 12 则调 mc_skill_harvest_logs 补少量木头，并调 mc_find_blocks(radius=32) 与 mc_get_entities(radius=32) 扫描周边箱子/动物/水源，切勿下潜或跑远。"
                ),
                extra_tools=("mc_interact",),
                advancement_id=None,
            ),
            frontier_data,
        )

    if not candidates and cd:
        non_excl = {k: v for k, v in cd.items() if k not in excluded}
        if non_excl:
            earliest_key = min(non_excl, key=non_excl.get)
            cd.pop(earliest_key, None)
            return select_next_target(
                bot,
                cooldowns=cd,
                now=now_t,
                world_memory=world_memory,
                enable_island_recon=enable_island_recon,
                recon_done=recon_done,
                world_jobs=world_jobs,
                leash=leash,
                exclude_keys=excluded,
                reserved_counts=reserved_counts,
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
                extra_tools=("mc_interact", "mc_posture"),
                advancement_id=None,
            ),
            frontier_data,
        )

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], frontier_data


class CurriculumSupervisor:
    """8 小时全自动计分课程督导器（二期：持久会话 + 双向门控 + 单层抢占恢复 + 柔性异步熔炉工单）。"""

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
        tool_output_chars: int = 2200,
        cooldown_s: float = 1200.0,
        strike_limit: int = 2,
        api_error_backoff_s: float = 8.0,
        geofence: VoidGeofence | None = None,
        toolset: McToolset | None = None,
        enable_pre_ensure: bool = False,
        enable_island_recon: bool = False,
        enable_async_world_jobs: bool = True,
        on_episode_event: Callable[[dict[str, Any]], None] | None = None,
        combat_controller: Any | None = None,
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
        self.max_calls_per_episode: int = self._explicit_max_calls or EPISODE_BUDGET_PROFILES["medium"][0]
        self.episode_timeout_s: float = self._explicit_timeout_s or EPISODE_BUDGET_PROFILES["medium"][1]
        self.tool_output_chars = max(256, int(tool_output_chars))
        self.cooldown_s = max(1.0, float(cooldown_s))
        self.strike_limit = max(1, int(strike_limit))
        self.api_error_backoff_s = max(0.0, float(api_error_backoff_s))
        self.geofence = geofence if geofence is not None else VoidGeofence.from_env()
        self.enable_pre_ensure = bool(enable_pre_ensure)
        self.enable_island_recon = bool(enable_island_recon)
        self.enable_async_world_jobs = bool(enable_async_world_jobs)
        self.on_episode_event = on_episode_event
        self.combat_controller = combat_controller

        self.toolset: McToolset = (
            toolset
            if toolset is not None
            else McToolset(self.bot, geofence=self.geofence, wait_for_cooldown=True)
        )
        self.inventory_manager = InventoryManager(
            self.bot,
            world_memory=self.toolset.world_memory,
            skills=self.toolset.skills,
        )
        self.world_jobs = WorldJobRegistry()
        self.suspended_episode: SuspendedEpisode | None = None
        self._pending_preempt_key: str | None = None
        self.recon_completed: bool = False
        self._last_dry_pos: list[float] | None = None
        self.strikes: dict[str, int] = {}
        self.cooldown_tiers: dict[str, int] = {}
        self.cooldowns: dict[str, float] = {}
        self.episodes: list[dict[str, Any]] = []

    def _query_completed_advs(self) -> set[str]:
        adv = self.bot.advancements(include_recipes=False)
        return {
            (str(r["id"]) if ":" in str(r["id"]) else f"minecraft:{r['id']}")
            for r in (adv.get("advancements") or [])
            if isinstance(r, dict) and r.get("status") == "completed" and r.get("id")
        }

    def _detect_surface_preempt_opportunity(
        self,
        *,
        current_key: str,
        pos: Sequence[float] | None,
        dim: str,
        is_day: bool,
        completed_advs: set[str],
        combined_counts: dict[str, int],
    ) -> str | None:
        """Detect ready-to-cash `>=15pt` Tier-1 opportunities (or nightfall village bed) within `<= 32m` at `Y >= 48`."""
        if pos is None or len(pos) < 3 or float(pos[1]) < 48.0:
            return None
        wm = self.toolset.world_memory
        norm_dim = normalize_dimension(dim)
        if norm_dim != "overworld":
            return None

        iron_equiv = combined_counts.get("iron_ingot", 0) + combined_counts.get("raw_iron", 0)
        has_bucket = any(combined_counts.get(k, 0) > 0 for k in ("bucket", "water_bucket"))

        # 1) Villager within <= 32m + trade currency ready -> 15pt `adventure/trade`
        trade_id = "minecraft:adventure/trade"
        if (
            trade_id != current_key
            and trade_id not in completed_advs
            and self.cooldowns.get(trade_id, 0.0) <= time.monotonic()
            and (combined_counts.get("emerald", 0) >= 1 or iron_equiv >= 4 or combined_counts.get("paper", 0) >= 24)
        ):
            v_hits = wm.query_habitats(["villager"], origin=pos, dim=norm_dim, limit=1)
            if v_hits and float(v_hits[0].get("dist", 999.0)) <= 32.0:
                return trade_id

        # 2) Lava pool within <= 32m + bucket/iron ready -> 15pt `story/lava_bucket`
        lava_id = "minecraft:story/lava_bucket"
        if (
            lava_id != current_key
            and lava_id not in completed_advs
            and self.cooldowns.get(lava_id, 0.0) <= time.monotonic()
            and (has_bucket or iron_equiv >= 3)
        ):
            l_hits = wm.query_pois(["lava"], origin=pos, dim=norm_dim, limit=1)
            if l_hits and float(l_hits[0].get("dist", 999.0)) <= 32.0:
                return lava_id

        # 3) Nightfall + village bed within <= 32m -> 5pt `adventure/sleep_in_bed` + spawn safety
        bed_id = "minecraft:adventure/sleep_in_bed"
        if (
            not is_day
            and bed_id != current_key
            and bed_id not in completed_advs
            and self.cooldowns.get(bed_id, 0.0) <= time.monotonic()
        ):
            b_hits = wm.query_pois(["bed"], origin=pos, dim=norm_dim, limit=1)
            if b_hits and float(b_hits[0].get("dist", 999.0)) <= 32.0:
                return bed_id

        return None

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
        consecutive_recovery_waits = 0
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

            resumed_ep: SuspendedEpisode | None = None
            is_preempt_branch = False

            try:
                try:
                    st = self.bot.status()
                except ApiError as status_exc:
                    if status_exc.code == "not_connected":
                        self.bot.connect(respect_reconnect_interval=True)
                        st = self.bot.wait_until_ready(timeout_s=min(45.0, max(5.0, remaining_s)), poll_interval_s=1.0)
                    elif status_exc.code == "not_loaded":
                        time.sleep(min(1.5, max(0.2, remaining_s)))
                        st = self.bot.wait_until_ready(timeout_s=min(45.0, max(5.0, remaining_s)), poll_interval_s=1.0)
                    else:
                        raise
                if not st.get("ready"):
                    st = self.bot.wait_until_ready(timeout_s=min(60.0, remaining_s), poll_interval_s=1.0, initial_status=st)
                if st.get("busy"):
                    try:
                        self.bot.call("stop")
                    except Exception:
                        pass
                if st.get("blocking"):
                    try:
                        self.bot.call("posture", {"shield": False})
                    except Exception:
                        pass
                consecutive_recovery_waits = 0
                self.toolset._update_status_cache(st)
                cur_pos = [float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])] if isinstance(st.get("pos"), (list, tuple)) and len(st["pos"]) >= 3 else None
                cur_dim = normalize_dimension(st.get("dimension"))

                # Re-anchor `VoidGeofence` if dimension changed (e.g. Overworld <-> Nether) or server teleported/respawned the bot > 400m away!
                prev_dim = getattr(self, "_last_dim", None)
                dim_changed = prev_dim is not None and cur_dim != prev_dim
                self._last_dim = cur_dim
                if self.geofence is not None and self.geofence.enabled and cur_pos is not None:
                    if dim_changed or math.hypot(cur_pos[0] - self.geofence.center_x, cur_pos[2] - self.geofence.center_z) > self.geofence.radius:
                        self.geofence = dataclasses.replace(self.geofence, center_x=cur_pos[0], center_z=cur_pos[2])
                        self.toolset.geofence = self.geofence
                        self._last_dry_pos = None

                # Track dry-land anchor & auto-recover to shore if previous episode left the bot floating in ocean/river!
                if cur_dim == "overworld" and cur_pos is not None:
                    if not bool(st.get("in_water")) and float(cur_pos[1]) >= 63.0:
                        self._last_dry_pos = list(cur_pos)
                    elif bool(st.get("in_water")) and self._last_dry_pos is not None:
                        try:
                            self.toolset.skills.escape_water()
                            st_w = self.bot.status()
                            if bool(st_w.get("in_water")):
                                self.toolset.skills.navigate_segmented(
                                    self._last_dry_pos[0],
                                    self._last_dry_pos[2],
                                    range_m=3.0,
                                    geofence=self.geofence,
                                )
                                st = self.bot.status()
                                self.toolset._update_status_cache(st)
                                if isinstance(st.get("pos"), (list, tuple)) and len(st["pos"]) >= 3:
                                    cur_pos = [float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])]
                        except Exception:
                            pass

                # Soft ready collection: harvest any finished nearby furnace jobs between episodes!
                if self.enable_async_world_jobs:
                    try:
                        self.world_jobs.collect_ready_jobs(self.toolset.skills, origin=cur_pos, dim=cur_dim)
                    except Exception:
                        pass

                # AltoClef between-episode hygiene: auto-upgrade armor (`torso` safe) + free clutter when bag slots <= 5!
                nh_ep = st.get("nearest_hostile")
                nh_ep_dist = float(nh_ep.get("dist", 999.0)) if isinstance(nh_ep, dict) and nh_ep.get("name") else 999.0
                if (
                    self.enable_pre_ensure
                    and bool(st.get("on_ground", True))
                    and not bool(st.get("in_water"))
                    and not bool(st.get("in_lava"))
                    and nh_ep_dist > 10.0
                ):
                    try:
                        self.inventory_manager.auto_equip_best(dimension=cur_dim, preserve_offhand=True)
                        inv_hyg = self.bot.inventory()
                        if isinstance(inv_hyg.get("empty_slots"), int) and int(inv_hyg["empty_slots"]) <= 5:
                            prot_mats = (
                                _compute_reserved_materials(self.suspended_episode, total_owned_counts(inv_hyg))
                                if self.suspended_episode is not None
                                else {}
                            )
                            with self.inventory_manager.protect(prot_mats.keys()):
                                self.inventory_manager.free_slots(min_free=6)
                    except Exception:
                        pass

                # Priority 1: Run a 1-level preempting opportunity target if queued
                if self._pending_preempt_key is not None and self._pending_preempt_key in CURRICULUM_PLAYBOOKS:
                    pkey = self._pending_preempt_key
                    self._pending_preempt_key = None
                    is_preempt_branch = True
                    inv_p = self.bot.inventory()
                    bag_p, chest_p, _ = _combined_available_counts(
                        inv_p, self.toolset.world_memory, origin=cur_pos, dim=cur_dim, world_jobs=self.world_jobs
                    )
                    target, _ = _enrich_target_with_world_memory(
                        CURRICULUM_PLAYBOOKS[pkey],
                        world_memory=self.toolset.world_memory,
                        bag_counts=bag_p,
                        chest_counts=chest_p,
                        origin=cur_pos,
                        dim=cur_dim,
                    )
                    frontier_before = compute_advancement_frontier(self.bot, limit=50)
                # Priority 2: Resume suspended parent episode if its waiting WorldJob is done (or preempt branch finished)
                elif self.suspended_episode is not None and (
                    self.suspended_episode.waiting_job_id is None
                    or self.suspended_episode.waiting_job_id not in self.world_jobs.jobs
                    or time.monotonic() >= self.world_jobs.jobs[self.suspended_episode.waiting_job_id].ready_at - 8.0
                ):
                    resumed_ep = self.suspended_episode
                    self.suspended_episode = None
                    if resumed_ep.waiting_job_id and resumed_ep.waiting_job_id in self.world_jobs.jobs:
                        self.world_jobs.collect_job(self.toolset.skills, resumed_ep.waiting_job_id, wait_remaining_s=12.0)
                    inv_r = self.bot.inventory()
                    bag_r, chest_r, _ = _combined_available_counts(
                        inv_r, self.toolset.world_memory, origin=cur_pos, dim=cur_dim, world_jobs=self.world_jobs
                    )
                    target, _ = _enrich_target_with_world_memory(
                        resumed_ep.target,
                        world_memory=self.toolset.world_memory,
                        bag_counts=bag_r,
                        chest_counts=chest_r,
                        origin=cur_pos,
                        dim=cur_dim,
                    )
                    frontier_before = compute_advancement_frontier(self.bot, limit=50)
                # Priority 3: Standard target selection (with 80m furnace leash if a WorldJob is still burning!)
                else:
                    leash = self.world_jobs.active_leash(dim=cur_dim) if self.enable_async_world_jobs else None
                    excl = {self.suspended_episode.target.key} if self.suspended_episode is not None else None
                    reserved_mats = (
                        _compute_reserved_materials(self.suspended_episode, total_owned_counts(self.bot.inventory()))
                        if self.suspended_episode is not None
                        else None
                    )
                    if leash is not None:
                        is_preempt_branch = True
                    target, frontier_before = select_next_target(
                        self.bot,
                        cooldowns=self.cooldowns,
                        world_memory=self.toolset.world_memory,
                        enable_island_recon=self.enable_island_recon,
                        recon_done=self.recon_completed,
                        world_jobs=self.world_jobs,
                        leash=leash,
                        exclude_keys=excl,
                        reserved_counts=reserved_mats,
                    )
                    if (
                        target.key == "furnace_leash_prep"
                        and self.episodes
                        and self.episodes[-1].get("target") == "furnace_leash_prep"
                        and self.suspended_episode is not None
                        and self.suspended_episode.waiting_job_id in self.world_jobs.jobs
                    ):
                        resumed_ep = self.suspended_episode
                        self.suspended_episode = None
                        is_preempt_branch = False
                        rem_j = max(0.0, self.world_jobs.jobs[resumed_ep.waiting_job_id].ready_at - time.monotonic())
                        self.world_jobs.collect_job(
                            self.toolset.skills,
                            resumed_ep.waiting_job_id,
                            wait_remaining_s=min(75.0, rem_j + 6.0),
                        )
                        target = resumed_ep.target

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
                ep_index -= 1
                consecutive_recovery_waits += 1
                wait_cd = min(max(5.0, float(exc.cooldown_remaining_seconds or 30.0)), max(5.0, remaining_s))
                time.sleep(wait_cd)
                continue
            except (ApiError, BotNotReadyError, RuntimeError, TimeoutError, OSError) as exc:
                ep_index -= 1
                consecutive_recovery_waits += 1
                if consecutive_recovery_waits > 12:
                    final_status = "disconnected"
                    self.episodes.append({"episode": ep_index + 1, "status": "disconnected", "error": str(exc)})
                    break
                time.sleep(min(5.0, max(1.0, remaining_s)))
                continue

            base_playbook = CURRICULUM_PLAYBOOKS.get(target.key, target)
            if resumed_ep is not None:
                ep_max_calls = max(2, int(resumed_ep.remaining_calls))
                raw_ep_timeout = max(30.0, float(resumed_ep.remaining_timeout_s))
            else:
                ep_max_calls, raw_ep_timeout = resolve_episode_budget(
                    base_playbook,
                    explicit_max_calls=self._explicit_max_calls,
                    explicit_timeout_s=self._explicit_timeout_s,
                )
            ep_timeout = min(raw_ep_timeout, max(0.5, remaining_s))
            if (
                resumed_ep is None
                and base_playbook.budget_class == "deep"
                and self._explicit_timeout_s is None
                and self.duration_s <= 4200.0
            ):
                ep_timeout = min(ep_timeout, max(540.0, self.duration_s * 0.30))
            if (
                is_preempt_branch
                and self.suspended_episode is not None
                and self.suspended_episode.waiting_job_id
                and self.suspended_episode.waiting_job_id in self.world_jobs.jobs
            ):
                job_rem_s = max(0.0, self.world_jobs.jobs[self.suspended_episode.waiting_job_id].ready_at - time.monotonic())
                ep_timeout = min(ep_timeout, max(45.0, job_rem_s + 40.0))

            ep_stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            ep_events_path = self.runs_dir / f"{ep_stem}.events.jsonl"
            ep_report_path = self.runs_dir / f"{ep_stem}.json"

            pre_ensure_res: dict[str, Any] | None = None
            satisfied_by_zero_llm = False
            paused_for_world_job = False
            eff_task = target.task
            eff_extra_tools = target.extra_tools

            with JsonlEvents(ep_events_path) as listener:
                def _sink(evt: dict[str, Any]) -> None:
                    listener(evt)
                    if self.on_episode_event is not None:
                        self.on_episode_event(evt)

                # 1) 0-LLM `pre_ensure` (wrapped with Subcortical Reflex preemption & objective completion check!)
                eff_pre_ensure = dict(base_playbook.pre_ensure)
                if base_playbook.key == "bootstrap_stone_age":
                    owned_bs = total_owned_counts(inv_before)
                    if owned_bs.get("diamond", 0) >= 3:
                        eff_pre_ensure = {"diamond_pickaxe": 1, "stone_pickaxe": 1}
                    elif owned_bs.get("iron_ingot", 0) >= 3:
                        eff_pre_ensure = {"iron_pickaxe": 1, "stone_pickaxe": 1}
                    elif self.enable_pre_ensure and cur_pos is not None and float(cur_pos[1]) >= 58.0:
                        try:
                            near_bs_trees = self.bot.call(
                                "find_blocks",
                                {
                                    "radius": 36,
                                    "types": ["oak_log", "spruce_log", "birch_log", "dark_oak_log", "acacia_log", "jungle_log", "cherry_log"],
                                    "limit": 1,
                                },
                            )
                            if (near_bs_trees or {}).get("blocks"):
                                eff_pre_ensure = {"any_log": 5, "stone_pickaxe": 1}
                        except Exception:
                            pass
                elif base_playbook.key == "bootstrap_iron_age":
                    # Promote `bootstrap_iron_age` pre_ensure to `iron_pickaxe` if >=3 iron in bag/72m chest or exposed within 28m!
                    bag_i, chest_i, _ = _combined_available_counts(
                        inv_before, self.toolset.world_memory, origin=cur_pos, dim=cur_dim, world_jobs=self.world_jobs
                    )
                    avail_iron = (
                        bag_i.get("iron_ingot", 0)
                        + bag_i.get("raw_iron", 0)
                        + chest_i.get("iron_ingot", 0)
                        + chest_i.get("raw_iron", 0)
                    )
                    if avail_iron < 3:
                        try:
                            exp_fe = self.bot.call(
                                "find_blocks",
                                {"radius": 28, "types": ["iron_ore", "deepslate_iron_ore"], "limit": 4},
                            )
                            avail_iron += len((exp_fe or {}).get("blocks") or [])
                        except Exception:
                            pass
                    if avail_iron >= 3:
                        eff_pre_ensure = {"stone_pickaxe": 1, "iron_pickaxe": 1}

                # Surface-to-underground wood & portable crafting_table reserve:
                # Prevents running out of sticks/tables at Y=16 or Y=-54 and wasting 10+ mins climbing back up!
                if (
                    self.enable_pre_ensure
                    and base_playbook.key in {"bootstrap_iron_age", "minecraft:story/shiny_gear", "minecraft:story/mine_diamond"}
                    and cur_pos is not None
                    and float(cur_pos[1]) >= 52.0
                ):
                    owned_pre = total_owned_counts(inv_before)
                    wood_units = sum(v for k, v in owned_pre.items() if k.endswith(("_log", "_stem"))) * 4 + sum(
                        v for k, v in owned_pre.items() if k.endswith("_planks")
                    ) + owned_pre.get("stick", 0) // 2
                    want_logs = 12
                    if wood_units < 36 or owned_pre.get("crafting_table", 0) < 2:
                        try:
                            near_trees = self.bot.call(
                                "find_blocks",
                                {
                                    "radius": 40,
                                    "types": [
                                        "oak_log",
                                        "spruce_log",
                                        "birch_log",
                                        "dark_oak_log",
                                        "acacia_log",
                                        "jungle_log",
                                        "cherry_log",
                                        "mangrove_log",
                                    ],
                                    "limit": 2,
                                },
                            )
                            if (near_trees or {}).get("blocks"):
                                eff_pre_ensure["any_log"] = max(int(eff_pre_ensure.get("any_log", 0)), want_logs)
                                eff_pre_ensure["any_planks"] = max(int(eff_pre_ensure.get("any_planks", 0)), 8)
                                eff_pre_ensure["crafting_table"] = max(int(eff_pre_ensure.get("crafting_table", 0)), 2)
                                eff_pre_ensure["stone_pickaxe"] = max(int(eff_pre_ensure.get("stone_pickaxe", 0)), 2)
                        except Exception:
                            pass

                if self.enable_pre_ensure and eff_pre_ensure:
                    try:
                        max_pre_cap = 45.0 if base_playbook.key.startswith("bootstrap_") else 90.0
                        pre_slice_s = min(ep_timeout * 0.5, max_pre_cap)
                        _sink(
                            {
                                "type": "curriculum.pre_ensure.started",
                                "episode": ep_index,
                                "target": target.key,
                                "items": eff_pre_ensure,
                            }
                        )
                        pre_guard = GenerationGuard(
                            self.bot,
                            EventSink(_sink),
                            checkpoint=lambda: None,
                            geofence=self.geofence,
                            skills=self.toolset.skills,
                            combat_controller=self.combat_controller,
                        )
                        pre_guard.prime()
                        resolver = PhysicalPrerequisiteResolver(
                            self.toolset.skills,
                            geofence=self.geofence,
                            stop_check=_is_stopped,
                            allow_underground_dfs=False,
                            world_jobs=self.world_jobs if self.enable_async_world_jobs else None,
                            parent_target_key=base_playbook.key,
                            async_smelt_threshold_s=45.0,
                        )
                        try:
                            with pre_guard._intercept_bot_calls(enable_creeper_watcher=True):
                                pre_ensure_res = resolver.ensure_items(
                                    eff_pre_ensure,
                                    max_duration_s=max(10.0, pre_slice_s),
                                    equip_slots=dict(base_playbook.pre_equip) if base_playbook.pre_equip else None,
                                )
                        except ReflexPreempted as preempt:
                            reflex_rec = pre_guard.handle_preemption(preempt.trigger, interrupted_name="pre_ensure")
                            pre_ensure_res = {
                                "ok": False,
                                "stop_reason": "reflex_preempted",
                                "steps_executed": 1,
                                "remaining_deficit": eff_pre_ensure,
                                "reflex": reflex_rec,
                            }

                        _sink(
                            {
                                "type": "curriculum.pre_ensure.completed",
                                "episode": ep_index,
                                "target": target.key,
                                "ok": pre_ensure_res.get("ok"),
                                "stop_reason": pre_ensure_res.get("stop_reason"),
                                "steps_executed": pre_ensure_res.get("steps_executed", 0),
                                "remaining_deficit": pre_ensure_res.get("remaining_deficit", {}),
                            }
                        )

                        # Check async WorldJob yield (e.g., 12 stone started in dual furnaces!)
                        if (
                            pre_ensure_res.get("stop_reason") == "waiting_for_world_job"
                            and pre_ensure_res.get("waiting_job_id")
                            and self.suspended_episode is None
                        ):
                            paused_for_world_job = True
                            self.suspended_episode = SuspendedEpisode(
                                target=base_playbook,
                                remaining_calls=ep_max_calls,
                                remaining_timeout_s=max(60.0, raw_ep_timeout),
                                anchor_pos=cur_pos,
                                reason="waiting_for_world_job",
                                progress_digest=f"已启动后台多炉熔炼工单 {pre_ensure_res.get('waiting_job_id')}",
                                waiting_job_id=str(pre_ensure_res["waiting_job_id"]),
                            )
                        else:
                            # D2 ★ Fix: Verify completion strictly via `is_objectively_satisfied` on live inventory & advancements!
                            fresh_inv = self.bot.inventory()
                            fresh_st = self.bot.status()
                            fresh_pos = (
                                [float(fresh_st["pos"][0]), float(fresh_st["pos"][1]), float(fresh_st["pos"][2])]
                                if isinstance(fresh_st.get("pos"), (list, tuple)) and len(fresh_st["pos"]) >= 3
                                else cur_pos
                            )
                            fresh_bag, fresh_chest, _ = _combined_available_counts(
                                fresh_inv,
                                self.toolset.world_memory,
                                origin=fresh_pos,
                                dim=cur_dim,
                                world_jobs=self.world_jobs,
                            )
                            done_chk = self._query_completed_advs()
                            if base_playbook.is_objectively_satisfied(done_chk, fresh_bag):
                                satisfied_by_zero_llm = True
                            else:
                                # D4.2 Fix: Dynamically re-render prompt AFTER `pre_ensure` using fresh WorldMemory!
                                if pre_ensure_res.get("ok"):
                                    post_base = base_playbook.post_ensure_task or base_playbook.task
                                    re_enriched, _ = _enrich_target_with_world_memory(
                                        base_playbook,
                                        world_memory=self.toolset.world_memory,
                                        bag_counts=fresh_bag,
                                        chest_counts=fresh_chest,
                                        origin=fresh_pos,
                                        dim=cur_dim,
                                        include_storage_hint=False,
                                        task_override=post_base,
                                    )
                                    eff_task = re_enriched.task
                                    eff_extra_tools = re_enriched.extra_tools
                                else:
                                    re_enriched, _ = _enrich_target_with_world_memory(
                                        base_playbook,
                                        world_memory=self.toolset.world_memory,
                                        bag_counts=fresh_bag,
                                        chest_counts=fresh_chest,
                                        origin=fresh_pos,
                                        dim=cur_dim,
                                        include_storage_hint=True,
                                    )
                                    prefix = ""
                                    if pre_ensure_res.get("steps_executed", 0) > 0:
                                        prefix = (
                                            f"[开局0-LLM备料进展：已执行 {pre_ensure_res.get('steps_executed')} 步，"
                                            f"剩余缺口={pre_ensure_res.get('remaining_deficit')}，原因={pre_ensure_res.get('stop_reason')}]\n"
                                        )
                                    eff_task = prefix + re_enriched.task
                                    eff_extra_tools = re_enriched.extra_tools
                    except Exception:
                        pre_ensure_res = None

                # Clean Snapshot Resume Briefing (`/grill-me` Decision #3)
                if resumed_ep is not None and not satisfied_by_zero_llm and not paused_for_world_job:
                    st_res = self.bot.status()
                    eff_task = (
                        f"[挂起恢复简报：原任务「{base_playbook.title}」在锚点 {resumed_ep.anchor_pos} 处暂挂（{resumed_ep.progress_digest}），"
                        f"现已恢复执行；当前最新坐标={st_res.get('pos')}，请严格依据最新实时坐标与背包完成剩余目标，勿引用挂起前旧坐标]\n"
                        + eff_task
                    )

                if paused_for_world_job:
                    report = {
                        "status": "paused_for_world_job",
                        "actor_requests": 0,
                        "model_requests": 0,
                        "pre_ensure": pre_ensure_res,
                        "task": eff_task,
                    }
                elif satisfied_by_zero_llm:
                    report = {
                        "status": "answered",
                        "actor_requests": 0,
                        "model_requests": 0,
                        "zero_llm_pre_ensure": pre_ensure_res,
                        "task": eff_task,
                    }
                else:
                    shiny_bonus_deadline: list[float | None] = [None]

                    def _episode_gate(feedback: dict[str, Any]) -> dict[str, Any] | None:
                        now_g = time.monotonic()
                        try:
                            st_g = self.bot.status()
                            self.toolset._update_status_cache(st_g)
                            inv_g = self.bot.inventory()
                            done_g = self._query_completed_advs()
                        except Exception:
                            return None

                        pos_g = (
                            [float(st_g["pos"][0]), float(st_g["pos"][1]), float(st_g["pos"][2])]
                            if isinstance(st_g.get("pos"), (list, tuple)) and len(st_g["pos"]) >= 3
                            else None
                        )
                        dim_g = normalize_dimension(st_g.get("dimension"))
                        bag_g, _, comb_g = _combined_available_counts(
                            inv_g, self.toolset.world_memory, origin=pos_g, dim=dim_g, world_jobs=self.world_jobs
                        )

                        # 1. Objective `COMPLETE` gate (with 150s bounded bonus window for `shiny_gear`!)
                        if base_playbook.key == "minecraft:story/shiny_gear":
                            if "minecraft:story/shiny_gear" in done_g:
                                if shiny_bonus_deadline[0] is None:
                                    shiny_bonus_deadline[0] = now_g + SHINY_GEAR_BONUS_WINDOW_S
                                rem_bonus = shiny_bonus_deadline[0] - now_g
                                main_dur = _best_main_pick_durability(inv_g)
                                extra_dia = bag_g.get("diamond", 0) + (3 if bag_g.get("diamond_pickaxe", 0) > 0 else 0)
                                if rem_bonus <= 0 or main_dur <= MAIN_PICK_DURABILITY_FLOOR or extra_dia >= 5:
                                    return {"action": "complete", "reason": "shiny_gear_bonus_window_complete"}
                                return {
                                    "action": "continue",
                                    "runtime_note": (
                                        f"已穿戴钻石靴锁定75分！顺手摸奖窗口剩余 {int(rem_bonus)}s（主镐耐久={main_dur}），"
                                        "可在当前 Y=-54 巷道顺采额外至多 5 颗钻石备钻镐/附魔台，切勿损耗铁镐至 35 以下。"
                                    ),
                                }
                        elif base_playbook.is_objectively_satisfied(done_g, bag_g):
                            # Immediate 0-request shutdown (protects newly crafted `iron_pickaxe`!)
                            return {"action": "complete", "reason": "objective_goal_satisfied"}

                        # 2. Critical tool-loss `ABORT` gate
                        if base_playbook.key in {"minecraft:story/shiny_gear", "minecraft:story/mine_diamond"}:
                            if highest_pickaxe_tier(bag_g) < 3 and (bag_g.get("iron_ingot", 0) + bag_g.get("raw_iron", 0)) < 3:
                                return {"action": "abort", "reason": "lost_iron_pickaxe"}

                        # 3. Single-level surface `PAUSE` preemption (`/grill-me` Decision #3: Y>=48 only, max depth 1; never interrupt micro/short cash-ins except recon)
                        if (
                            not is_preempt_branch
                            and self.suspended_episode is None
                            and (base_playbook.budget_class not in {"micro", "short"} or base_playbook.key == "island_recon_sweep")
                        ):
                            opp_key = self._detect_surface_preempt_opportunity(
                                current_key=base_playbook.key,
                                pos=pos_g,
                                dim=dim_g,
                                is_day=bool(st_g.get("is_day", True)),
                                completed_advs=done_g,
                                combined_counts=comb_g,
                            )
                            if opp_key is not None:
                                return {
                                    "action": "pause",
                                    "reason": "preempt_opportunity",
                                    "payload": {"preempt_key": opp_key, "anchor_pos": pos_g},
                                }
                        return None

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
                        extra_tools=eff_extra_tools,
                        geofence=self.geofence,
                        target_advancement=target.advancement_id,
                        toolset=self.toolset,
                        episode_gate=_episode_gate,
                        combat_controller=self.combat_controller,
                    )
                    if pre_ensure_res is not None:
                        report["pre_ensure"] = pre_ensure_res

                    if report.get("status") == "paused" and isinstance(report.get("pause_payload"), dict):
                        pp = report["pause_payload"]
                        pkey = str(pp.get("preempt_key") or "")
                        if pkey in CURRICULUM_PLAYBOOKS and self.suspended_episode is None:
                            self._pending_preempt_key = pkey
                            used_s = float((report.get("events") or [{}])[-1].get("elapsed", 30.0)) if report.get("events") else 30.0
                            self.suspended_episode = SuspendedEpisode(
                                target=base_playbook,
                                remaining_calls=max(2, int(report.get("remaining_calls") or (ep_max_calls - int(report.get("actor_requests") or 1)))),
                                remaining_timeout_s=max(60.0, ep_timeout - used_s),
                                anchor_pos=pp.get("anchor_pos") or cur_pos,
                                reason="preempt_opportunity",
                                progress_digest=f"已推进 {report.get('actor_requests', 0)} 轮，途中抢占支线 {pkey}",
                                preempted_by_key=pkey,
                            )

            if target.key == "island_recon_sweep" and report.get("status") not in {"paused", "paused_for_world_job"}:
                self.recon_completed = True

            report["curriculum_episode"] = ep_index
            report["curriculum_target"] = target.key
            report["budget_class"] = target.budget_class
            report["budget_max_calls"] = ep_max_calls
            report["budget_timeout_s"] = round(ep_timeout, 1)
            report["event_log"] = str(ep_events_path)
            ep_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            # Objective post-episode verification (`/advancements` + `/inventory` + `WorldMemory`)
            completed_after = set(completed_before)
            pick_after = pick_before
            iron_after = iron_before
            diamond_after = diamond_before
            obsidian_after = obsidian_before
            chunks_after = len(self.toolset.world_memory.visited_chunks)
            try:
                completed_after.update(self._query_completed_advs())
                inv_after = self.bot.inventory()
                pick_after = _highest_pick_rank(inv_after)
                iron_after = _iron_units(inv_after)
                diamond_after = _strategic_units(inv_after, ("diamond",))
                obsidian_after = _strategic_units(inv_after, ("obsidian",))
            except Exception:
                inv_after = {}

            gained_advs = sorted(completed_after - completed_before)
            score_after, breakdown = score_completed_advancements(completed_after)
            bag_after_counts = total_owned_counts(inv_after) if isinstance(inv_after, dict) else {}
            objective_verified = base_playbook.is_objectively_satisfied(completed_after, bag_after_counts)

            material_progress = (
                objective_verified
                or pick_after > pick_before
                or (target.key == "bootstrap_iron_age" and iron_after > iron_before)
                or (target.key in {"minecraft:story/shiny_gear", "minecraft:story/mine_diamond"} and diamond_after > diamond_before)
                or (target.key in {"minecraft:story/enchant_item", "minecraft:story/enter_the_nether"} and obsidian_after > obsidian_before)
                or (target.key == "island_recon_sweep" and chunks_after > chunks_before)
            )
            progress_made = (
                objective_verified
                or material_progress
                or (bool(gained_advs) and not target.advancement_id)
            )
            is_pause_yield = report.get("status") in {"paused", "paused_for_world_job"}
            transient_api_err = (not progress_made) and _is_transient_llm_error(report)

            if progress_made:
                self.strikes.pop(target.key, None)
                self.cooldown_tiers.pop(target.key, None)
                # Prevent an unfinished `deep` slice (`shiny_gear`/`mine_diamond`) from monopolizing back-to-back slices when
                # the bot already accumulated diamonds/iron that can immediately cash in +35~65 pts on fast deterministic targets!
                if (
                    base_playbook.budget_class == "deep"
                    and not objective_verified
                    and report.get("status") in {"timeout", "budget_exhausted"}
                    and (diamond_after >= 3 or iron_after >= 3)
                ):
                    self.cooldowns[target.key] = max(self.cooldowns.get(target.key, 0.0), time.monotonic() + 240.0)
            elif not transient_api_err and not is_pause_yield and target.key != "furnace_leash_prep":
                cnt = self.strikes.get(target.key, 0) + 1
                self.strikes[target.key] = cnt
                _LOCAL_RESOURCE_TARGETS = frozenset({
                    "minecraft:husbandry/make_a_sign_glow",
                    "minecraft:story/lava_bucket",
                    "minecraft:adventure/sleep_in_bed",
                    "minecraft:husbandry/breed_an_animal",
                    "minecraft:husbandry/tame_an_animal",
                    "minecraft:husbandry/plant_seed",
                    "minecraft:adventure/heart_transplanter",
                    "minecraft:story/cure_zombie_villager",
                    "minecraft:nether/ride_strider",
                })
                final_ans_text = str(report.get("final_answer") or report.get("answer") or report.get("output") or "")
                explicit_blocked = any(
                    m in final_ans_text
                    for m in ("阻断报告", "不可达", "不可达成", "无此结构", "数量均为 0", "数量均为0", "判定阻断")
                )
                eff_strike_limit = (
                    1
                    if explicit_blocked
                    or target.key == "minecraft:husbandry/make_a_sign_glow"
                    or (target.key in _LOCAL_RESOURCE_TARGETS and report.get("status") == "answered")
                    else self.strike_limit
                )
                if cnt >= eff_strike_limit:
                    tier = self.cooldown_tiers.get(target.key, 0) + (2 if explicit_blocked else 1)
                    self.cooldown_tiers[target.key] = tier
                    if target.key.startswith("bootstrap_"):
                        cd_dur = min(self.cooldown_s, 90.0)
                    else:
                        # BUG-050 exponential backoff: 1x (20m) -> 3x (60m) -> 9x (3h)
                        mult = 1 if tier <= 1 else (3 if tier == 2 else 9)
                        cd_dur = self.cooldown_s * mult
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
                "objective_verified": objective_verified,
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
                "active_world_jobs": len(self.world_jobs.jobs),
                "episodes": self.episodes,
            }
            summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            if report.get("status") == "interrupted":
                final_status = "interrupted"
                break
            if report.get("status") == "disconnected" and time.monotonic() < deadline_mono and not _is_stopped():
                try:
                    self.bot.connect(respect_reconnect_interval=True)
                    self.bot.wait_until_ready(timeout_s=30.0, poll_interval_s=1.0)
                except Exception:
                    pass
            if transient_api_err and self.api_error_backoff_s > 0 and time.monotonic() < deadline_mono and not _is_stopped():
                time.sleep(min(self.api_error_backoff_s, max(0.0, deadline_mono - time.monotonic())))

        try:
            self.toolset.world_memory.save(force=True)
        except Exception:
            pass

        try:
            final_completed = self._query_completed_advs()
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


def _best_main_pick_durability(inv: dict[str, Any]) -> int:
    """Return remaining durability of the highest-tier iron/diamond/netherite pickaxe owned."""
    best_tier = 0
    best_rem = 0
    rows = list(inv.get("items") or [])
    off = inv.get("offhand")
    if isinstance(off, dict) and off.get("name"):
        rows.append(off)
    ranks = {"iron_pickaxe": 3, "diamond_pickaxe": 4, "netherite_pickaxe": 5}
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = str(row.get("name") or "").removeprefix("minecraft:")
        tier = ranks.get(nm, 0)
        if tier <= 0 or int(row.get("count") or 0) <= 0:
            continue
        max_d = int(row.get("max_durability") or PICK_MAX_DURABILITY.get(nm, 250))
        if isinstance(row.get("remaining_durability"), int):
            rem = int(row["remaining_durability"])
        elif isinstance(row.get("durability_used"), int):
            rem = max(1, max_d - int(row["durability_used"]))
        elif isinstance(row.get("damage"), int):
            rem = max(1, max_d - int(row["damage"]))
        else:
            rem = max_d
        if (tier, rem) > (best_tier, best_rem):
            best_tier, best_rem = tier, rem
    return best_rem


def _iron_units(inv: dict[str, Any]) -> int:
    owned = total_owned_counts(inv)
    return owned.get("iron_ingot", 0) + owned.get("raw_iron", 0) + owned.get("iron_block", 0) * 9


def _strategic_units(inv: dict[str, Any], names: tuple[str, ...]) -> int:
    owned = total_owned_counts(inv)
    return sum(owned.get(n, 0) for n in names)
