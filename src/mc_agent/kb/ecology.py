"""Declarative 1.21.11 world resource ecology & dimension physics oracle for small agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .lookup import resolve_item_alias

_PRODUCT_TO_SOURCE: dict[str, str] = {
    "iron_ingot": "iron_ore",
    "raw_iron": "iron_ore",
    "gold_ingot": "gold_ore",
    "raw_gold": "gold_ore",
    "copper_ingot": "copper_ore",
    "raw_copper": "copper_ore",
    "diamond": "diamond_ore",
    "emerald": "emerald_ore",
    "coal": "coal_ore",
    "lapis_lazuli": "lapis_ore",
    "redstone": "redstone_ore",
    "quartz": "nether_quartz_ore",
    "netherite_scrap": "ancient_debris",
    "netherite_ingot": "ancient_debris",
}

_DIM_NAMES_ZH: dict[str, str] = {
    "overworld": "主世界",
    "the_nether": "下界",
    "the_end": "末地",
}


def normalize_dimension(dim: str | None) -> str:
    """Normalize dimension string (with or without minecraft: prefix) to canonical ID."""
    raw = str(dim or "").strip().lower().removeprefix("minecraft:")
    if raw in ("", "overworld", "world"):
        return "overworld"
    if raw in ("the_nether", "nether", "world_nether"):
        return "the_nether"
    if raw in ("the_end", "end", "world_the_end"):
        return "the_end"
    return raw


def _dim_zh(dim: str) -> str:
    return _DIM_NAMES_ZH.get(dim, dim)


@dataclass(frozen=True)
class ResourceEcologyRule:
    id: str
    label_zh: str
    matches: frozenset[str] = field(default_factory=frozenset)
    suffixes: tuple[str, ...] = ()
    exclude_substrings: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ("overworld",)
    y_min: int = -64
    y_max: int = 320
    stand_y_min: int = -64
    stand_y_max: int = 320
    recommended_y: int = 64
    required_biomes: tuple[str, ...] = ()
    biome_y_overrides: tuple[tuple[tuple[str, ...], int, int, int, int], ...] = ()
    substitutes: tuple[str, ...] = ()
    dimension_substitutes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    note: str = ""

    def matches_target(self, target: str) -> bool:
        if target in self.matches:
            return True
        if self.exclude_substrings and any(ex in target for ex in self.exclude_substrings):
            return False
        return bool(self.suffixes and target.endswith(self.suffixes))

    def effective_bounds(self, biome_low: str) -> tuple[int, int, int, int]:
        if biome_low:
            for keywords, ymin, ymax, symin, symax in self.biome_y_overrides:
                if any(kw in biome_low for kw in keywords):
                    return ymin, ymax, symin, symax
        return self.y_min, self.y_max, self.stand_y_min, self.stand_y_max


@dataclass(frozen=True)
class ItemPhysicsRule:
    id: str
    label_zh: str
    matches: frozenset[str] = field(default_factory=frozenset)
    suffixes: tuple[str, ...] = ()
    blocked_dimensions: tuple[str, ...] = ()
    verdict: str = "invalid_dimension_physics"
    hint_template: str = ""
    substitutes: tuple[str, ...] = ()

    def matches_item(self, item: str) -> bool:
        if item in self.matches:
            return True
        return bool(self.suffixes and item.endswith(self.suffixes))


WORLD_ECOLOGY_RULES: tuple[ResourceEcologyRule, ...] = (
    ResourceEcologyRule(
        id="surface_trees",
        label_zh="地表树木(原木/树叶/树苗)",
        matches=frozenset(
            {
                "oak_log",
                "birch_log",
                "spruce_log",
                "dark_oak_log",
                "acacia_log",
                "jungle_log",
                "cherry_log",
                "mangrove_log",
                "pale_oak_log",
                "oak_wood",
                "birch_wood",
                "spruce_wood",
            }
        ),
        suffixes=("_log", "_leaves", "_sapling"),
        exclude_substrings=("azalea",),
        dimensions=("overworld",),
        y_min=58,
        y_max=320,
        stand_y_min=58,
        stand_y_max=260,
        recommended_y=66,
        dimension_substitutes={"the_nether": ("crimson_stem", "warped_stem")},
        note="主世界常规树木仅生成于地表/丘陵群系，地下岩层中不会自然长树。",
    ),
    ResourceEcologyRule(
        id="nether_fungi_stems",
        label_zh="下界巨型菌柄与菌丝木",
        matches=frozenset(
            {
                "crimson_stem",
                "warped_stem",
                "crimson_hyphae",
                "warped_hyphae",
                "shroomlight",
                "nether_wart_block",
                "warped_wart_block",
            }
        ),
        dimensions=("the_nether",),
        y_min=8,
        y_max=120,
        stand_y_min=8,
        stand_y_max=120,
        recommended_y=64,
        dimension_substitutes={"overworld": ("oak_log", "birch_log")},
        note="下界绯红/诡异森林特有木质替代物，可直接合成木板/木棍与工作台。",
    ),
    ResourceEcologyRule(
        id="surface_flora_and_crops",
        label_zh="地表植物与浅层水生方块",
        matches=frozenset(
            {
                "grass_block",
                "wheat_seeds",
                "wheat",
                "sugar_cane",
                "bamboo",
                "pumpkin",
                "melon",
                "sweet_berry_bush",
                "kelp",
                "clay",
                "lily_pad",
            }
        ),
        dimensions=("overworld",),
        y_min=55,
        y_max=320,
        stand_y_min=55,
        stand_y_max=260,
        recommended_y=64,
    ),
    ResourceEcologyRule(
        id="coal_ore",
        label_zh="煤矿石",
        matches=frozenset({"coal_ore", "deepslate_coal_ore"}),
        dimensions=("overworld",),
        y_min=0,
        y_max=320,
        stand_y_min=-4,
        stand_y_max=320,
        recommended_y=48,
        substitutes=("charcoal(熔炉烧原木替代煤炭)",),
        note="Y<0 深板岩层几乎不生成煤矿，可用原木烧制 charcoal 替代或向上攀升。",
    ),
    ResourceEcologyRule(
        id="copper_ore",
        label_zh="铜矿石",
        matches=frozenset({"copper_ore", "deepslate_copper_ore"}),
        dimensions=("overworld",),
        y_min=-16,
        y_max=112,
        stand_y_min=-20,
        stand_y_max=116,
        recommended_y=48,
    ),
    ResourceEcologyRule(
        id="iron_ore",
        label_zh="铁矿石",
        matches=frozenset({"iron_ore", "deepslate_iron_ore"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=320,
        stand_y_min=-64,
        stand_y_max=320,
        recommended_y=16,
    ),
    ResourceEcologyRule(
        id="lapis_ore",
        label_zh="青金石矿石",
        matches=frozenset({"lapis_ore", "deepslate_lapis_ore"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=64,
        stand_y_min=-64,
        stand_y_max=68,
        recommended_y=0,
    ),
    ResourceEcologyRule(
        id="gold_ore",
        label_zh="金矿石",
        matches=frozenset({"gold_ore", "deepslate_gold_ore"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=32,
        stand_y_min=-64,
        stand_y_max=36,
        recommended_y=-16,
        biome_y_overrides=((("badlands", "mesa"), -64, 256, -64, 256),),
        dimension_substitutes={"the_nether": ("nether_gold_ore",)},
    ),
    ResourceEcologyRule(
        id="redstone_ore",
        label_zh="红石矿石",
        matches=frozenset({"redstone_ore", "deepslate_redstone_ore"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=15,
        stand_y_min=-64,
        stand_y_max=20,
        recommended_y=-54,
    ),
    ResourceEcologyRule(
        id="diamond_ore",
        label_zh="钻石矿石",
        matches=frozenset({"diamond_ore", "deepslate_diamond_ore"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=16,
        stand_y_min=-64,
        stand_y_max=20,
        recommended_y=-54,
        note="1.21+ 钻石仅在 Y=-64~16 生成且越深越富集，推荐斜下掘进至 Y=-54。",
    ),
    ResourceEcologyRule(
        id="emerald_ore",
        label_zh="绿宝石矿石",
        matches=frozenset({"emerald_ore", "deepslate_emerald_ore"}),
        dimensions=("overworld",),
        y_min=-16,
        y_max=320,
        stand_y_min=-20,
        stand_y_max=320,
        recommended_y=120,
        required_biomes=(
            "meadow",
            "grove",
            "snowy_slopes",
            "jagged_peaks",
            "frozen_peaks",
            "stony_peaks",
            "windswept_hills",
            "windswept_gravelly_hills",
            "windswept_forest",
            "cherry_grove",
        ),
        substitutes=("村民交易获取绿宝石",),
        note="绿宝石矿石仅在山地/山坡/樱花树林类群系自然生成，其它群系推荐找村民交易。",
    ),
    ResourceEcologyRule(
        id="deepslate_rock",
        label_zh="深板岩岩层",
        matches=frozenset({"deepslate", "cobbled_deepslate"}),
        dimensions=("overworld",),
        y_min=-64,
        y_max=8,
        stand_y_min=-64,
        stand_y_max=12,
        recommended_y=-16,
    ),
    ResourceEcologyRule(
        id="nether_ores_and_blocks",
        label_zh="下界矿物与独有方块",
        matches=frozenset(
            {
                "nether_quartz_ore",
                "nether_gold_ore",
                "glowstone",
                "soul_sand",
                "soul_soil",
                "netherrack",
                "nether_wart",
                "magma_block",
                "basalt",
                "blackstone",
            }
        ),
        dimensions=("the_nether",),
        y_min=5,
        y_max=122,
        stand_y_min=5,
        stand_y_max=122,
        recommended_y=64,
    ),
    ResourceEcologyRule(
        id="ancient_debris",
        label_zh="远古残骸",
        matches=frozenset({"ancient_debris"}),
        dimensions=("the_nether",),
        y_min=8,
        y_max=119,
        stand_y_min=8,
        stand_y_max=119,
        recommended_y=15,
        note="远古残骸仅在下界深层生成且绝不暴露在空气中，需下界 Y=15 平推矿道或爆破发掘。",
    ),
    ResourceEcologyRule(
        id="end_blocks",
        label_zh="末地独有方块与植物",
        matches=frozenset({"end_stone", "chorus_plant", "chorus_flower", "purpur_block", "end_rod"}),
        dimensions=("the_end",),
        y_min=0,
        y_max=128,
        stand_y_min=0,
        stand_y_max=128,
        recommended_y=60,
    ),
)

ITEM_PHYSICS_RULES: tuple[ItemPhysicsRule, ...] = (
    ItemPhysicsRule(
        id="water_evaporates_nether",
        label_zh="下界水源放置",
        matches=frozenset({"water_bucket", "water", "flowing_water"}),
        blocked_dimensions=("the_nether",),
        verdict="evaporates_in_dimension",
        hint_template="当前位于下界(the_nether)：放置 {item} 会瞬间蒸发失效！严禁在下界用 water_bucket 灭火或落地水，请改用 twisting_vines、hay_block 或实体方块。",
        substitutes=("twisting_vines", "hay_block", "cobblestone"),
    ),
    ItemPhysicsRule(
        id="bed_explodes_non_overworld",
        label_zh="非主世界使用床",
        suffixes=("_bed",),
        blocked_dimensions=("the_nether", "the_end"),
        verdict="explodes_in_dimension",
        hint_template="物理禁忌拦截：当前位于{dim_zh}({dim})，放置/交互 {item} 会引发剧烈致命爆炸！下界绑定重生点请改用 respawn_anchor + glowstone。",
        substitutes=("respawn_anchor",),
    ),
    ItemPhysicsRule(
        id="respawn_anchor_explodes_outside_nether",
        label_zh="非下界激活重生锚",
        matches=frozenset({"respawn_anchor"}),
        blocked_dimensions=("overworld", "the_end"),
        verdict="explodes_in_dimension",
        hint_template="物理禁忌拦截：当前位于{dim_zh}({dim})，激活 respawn_anchor 会爆炸致死！主世界请改用 *_bed 绑定重生点。",
        substitutes=("white_bed",),
    ),
)


def _canonical_target(raw_name: str) -> str:
    canon = resolve_item_alias(raw_name)
    return _PRODUCT_TO_SOURCE.get(canon, canon)


def _match_ecology_rule(target: str) -> ResourceEcologyRule | None:
    canon = _canonical_target(target)
    for rule in WORLD_ECOLOGY_RULES:
        if rule.matches_target(canon):
            return rule
    return None


def _match_physics_rule(item: str) -> ItemPhysicsRule | None:
    canon = resolve_item_alias(item)
    for rule in ITEM_PHYSICS_RULES:
        if rule.matches_item(canon):
            return rule
    return None


def _evaluate_rule_for_find(
    rule: ResourceEcologyRule,
    *,
    target_display: str,
    dim: str,
    cur_y: int | None,
    biome: str,
) -> dict[str, Any]:
    biome_low = biome.strip().lower().removeprefix("minecraft:")
    ymin, ymax, stand_min, stand_max = rule.effective_bounds(biome_low)
    subs = list(rule.dimension_substitutes.get(dim, ())) + list(rule.substitutes)
    allowed_dims_zh = "/".join(_dim_zh(d) for d in rule.dimensions)

    if dim not in rule.dimensions:
        sub_note = f"；在当前{_dim_zh(dim)}可改采替代物: {', '.join(subs)}" if subs else ""
        return {
            "known": True,
            "available": False,
            "mode": "find",
            "verdict": "wrong_dimension",
            "family_id": rule.id,
            "target": target_display,
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "allowed_dimensions": list(rule.dimensions),
            "allowed_y_range": [ymin, ymax],
            "recommended_y": rule.recommended_y,
            "direction_needed": "portal",
            "substitutes": subs,
            "hint": (
                f"当前位于{_dim_zh(dim)}({dim})，{rule.label_zh}仅存在于{allowed_dims_zh}({','.join(rule.dimensions)})，"
                f"严禁在当前维度盲挖寻找 {target_display}{sub_note}。"
            ),
        }

    if rule.required_biomes and biome_low and not any(b in biome_low for b in rule.required_biomes):
        sub_note = f"；或直接替代方案: {', '.join(subs)}" if subs else ""
        return {
            "known": True,
            "available": False,
            "mode": "find",
            "verdict": "wrong_biome",
            "family_id": rule.id,
            "target": target_display,
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "allowed_dimensions": list(rule.dimensions),
            "allowed_y_range": [ymin, ymax],
            "recommended_y": rule.recommended_y,
            "direction_needed": "biome_travel",
            "substitutes": subs,
            "hint": (
                f"当前生物群系为 {biome}，{rule.label_zh}仅在山地/丘陵类群系生成（例如 {', '.join(rule.required_biomes[:4])} 等），"
                f"勿在原地反复白扫{sub_note}。"
            ),
        }

    if cur_y is not None and cur_y < stand_min:
        sub_note = f"；也可替代获取: {', '.join(subs)}" if subs else ""
        extra_note = f"（{rule.note}）" if rule.note else ""
        return {
            "known": True,
            "available": False,
            "mode": "find",
            "verdict": "below_generation_band",
            "family_id": rule.id,
            "target": target_display,
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "allowed_dimensions": list(rule.dimensions),
            "allowed_y_range": [ymin, ymax],
            "recommended_y": rule.recommended_y,
            "direction_needed": "up",
            "substitutes": subs,
            "hint": (
                f"当前在 y={cur_y}，低于{rule.label_zh}自然生成带(Y={ymin}~{ymax}，建议 Y>={rule.recommended_y})，"
                f"周围不可能刷出 {target_display}！严禁在当前高度横向或往下盲挖，请先用 mc_skill_staircase(vertical='up') 向上爬升至推荐高度{sub_note}。{extra_note}"
            ).strip(),
        }

    if cur_y is not None and cur_y > stand_max:
        sub_note = f"；或替代获取: {', '.join(subs)}" if subs else ""
        extra_note = f"（{rule.note}）" if rule.note else ""
        return {
            "known": True,
            "available": False,
            "mode": "find",
            "verdict": "above_generation_band",
            "family_id": rule.id,
            "target": target_display,
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "allowed_dimensions": list(rule.dimensions),
            "allowed_y_range": [ymin, ymax],
            "recommended_y": rule.recommended_y,
            "direction_needed": "down",
            "substitutes": subs,
            "hint": (
                f"当前在 y={cur_y}，高于{rule.label_zh}自然生成带(Y={ymin}~{ymax}，推荐下探至 Y={rule.recommended_y})，"
                f"原地浅层扫不到 {target_display}！请用 mc_skill_staircase(vertical='down', stop_on_ore=True) 斜向下探接近目标深度{sub_note}。{extra_note}"
            ).strip(),
        }

    return {
        "known": True,
        "available": True,
        "mode": "find",
        "verdict": "ok",
        "family_id": rule.id,
        "target": target_display,
        "dimension": dim,
        "y": cur_y,
        "biome": biome,
        "allowed_dimensions": list(rule.dimensions),
        "allowed_y_range": [ymin, ymax],
        "recommended_y": rule.recommended_y,
        "direction_needed": None,
        "substitutes": subs,
        "hint": "",
    }


def _evaluate_place(
    targets: list[str],
    *,
    dim: str,
    cur_y: int | None,
    biome: str,
) -> dict[str, Any] | None:
    for tgt in targets:
        p_rule = _match_physics_rule(tgt)
        if p_rule is None:
            continue
        if dim in p_rule.blocked_dimensions:
            hint = p_rule.hint_template.format(item=tgt, dim=dim, dim_zh=_dim_zh(dim))
            return {
                "known": True,
                "available": False,
                "mode": "place",
                "verdict": p_rule.verdict,
                "family_id": p_rule.id,
                "target": tgt,
                "dimension": dim,
                "y": cur_y,
                "biome": biome,
                "allowed_dimensions": [d for d in ("overworld", "the_nether", "the_end") if d not in p_rule.blocked_dimensions],
                "allowed_y_range": [-64, 320],
                "recommended_y": cur_y,
                "direction_needed": "portal",
                "substitutes": list(p_rule.substitutes),
                "hint": hint,
            }
        return {
            "known": True,
            "available": True,
            "mode": "place",
            "verdict": "ok",
            "family_id": p_rule.id,
            "target": tgt,
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "allowed_dimensions": [d for d in ("overworld", "the_nether", "the_end") if d not in p_rule.blocked_dimensions],
            "allowed_y_range": [-64, 320],
            "recommended_y": cur_y,
            "direction_needed": None,
            "substitutes": list(p_rule.substitutes),
            "hint": "",
        }
    return None


def assess_world_target(
    targets: str | list[str] | tuple[str, ...],
    *,
    dimension: str = "overworld",
    y: float | int | None = None,
    biome: str = "",
    radius: int = 16,
    mode: str = "find",
) -> dict[str, Any]:
    """Assess whether finding/mining or placing/using targets is viable at current (dimension, y, biome)."""
    del radius  # 保留参数签名，便于后续结合局部扫描半径扩展
    dim = normalize_dimension(dimension)
    cur_y = int(y) if isinstance(y, (int, float)) and not isinstance(y, bool) else None
    raw_list = [targets] if isinstance(targets, str) else [str(t) for t in (targets or ())]
    clean_targets = [t.strip() for t in raw_list if str(t or "").strip()]
    mode_norm = (mode or "find").strip().lower()

    if not clean_targets:
        return {
            "known": False,
            "available": True,
            "mode": mode_norm,
            "verdict": "unknown_target",
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "hint": "",
        }

    if mode_norm in ("place", "auto"):
        place_res = _evaluate_place(clean_targets, dim=dim, cur_y=cur_y, biome=biome)
        if mode_norm == "place":
            if place_res is not None:
                return place_res
            return {
                "known": False,
                "available": True,
                "mode": "place",
                "verdict": "ok",
                "dimension": dim,
                "y": cur_y,
                "biome": biome,
                "hint": "",
            }
        if place_res is not None and not place_res["available"]:
            return place_res

    evals: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for tgt in clean_targets:
        rule = _match_ecology_rule(tgt)
        if rule is None or rule.id in seen_rules:
            continue
        seen_rules.add(rule.id)
        evals.append(_evaluate_rule_for_find(rule, target_display=tgt, dim=dim, cur_y=cur_y, biome=biome))

    if not evals:
        return {
            "known": False,
            "available": True,
            "mode": "find",
            "verdict": "unknown_target",
            "dimension": dim,
            "y": cur_y,
            "biome": biome,
            "targets": clean_targets,
            "hint": "",
        }

    viable = [ev for ev in evals if ev["available"]]
    blocked = [ev for ev in evals if not ev["available"]]
    if viable:
        top = dict(viable[0])
        if blocked:
            top["unavailable_subset"] = [
                {
                    "family_id": b["family_id"],
                    "verdict": b["verdict"],
                    "allowed_y_range": b["allowed_y_range"],
                    "hint": b["hint"],
                }
                for b in blocked
            ]
        return top

    return dict(blocked[0])
