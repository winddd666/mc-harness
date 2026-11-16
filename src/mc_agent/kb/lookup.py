"""Query, alias resolution, and zero-bloat JIT error hint helpers for 1.21.11 KB."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from .data_1_21_11 import (
    ADVANCEMENT_PLAYBOOKS,
    ITEM_ALIASES,
    MOBS_AND_MECHANICS,
    ORES_1_21,
    RECIPES_1_21,
)

_BLOCK_TARGET_ITEMS = (
    "bucket",
    "water_bucket",
    "lava_bucket",
    "flint_and_steel",
    "bone_meal",
)

_ADVANCEMENTS_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "advancements_1_21_11.json"


@functools.lru_cache(maxsize=1)
def _load_full_advancement_kb() -> tuple[dict[str, Any], ...]:
    """Load all 125 advancements merged with RULES.md points and curated playbooks."""
    from ..harness.curriculum import (
        CURRICULUM_PLAYBOOKS,
        RULES_ADVANCEMENT_POINTS,
        UNCONDITIONAL_ADVANCEMENTS,
    )

    raw = json.loads(_ADVANCEMENTS_DATA_PATH.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for item in raw.get("advancements") or []:
        adv_id = str(item.get("id") or "")
        if not adv_id:
            continue
        desc_zh = " ".join(str(item.get("description_zh") or "").split())
        desc_en = " ".join(str(item.get("description_en") or "").split())
        pb = ADVANCEMENT_PLAYBOOKS.get(adv_id) or {}
        curr = CURRICULUM_PLAYBOOKS.get(adv_id)
        points = int(RULES_ADVANCEMENT_POINTS.get(adv_id, 0))
        how_to = (curr.task if curr is not None else None) or pb.get("how_to") or desc_zh or desc_en
        entry: dict[str, Any] = {
            "id": adv_id,
            "title_zh": str(item.get("title_zh") or ""),
            "title_en": str(item.get("title_en") or ""),
            "description_zh": desc_zh,
            "description_en": desc_en,
            "parent": item.get("parent"),
            "points": points,
            "scored": points > 0,
            "unconditional": adv_id in UNCONDITIONAL_ADVANCEMENTS,
            "how_to": how_to,
            "triggers": list(item.get("triggers") or []),
        }
        if pb.get("difficulty"):
            entry["difficulty"] = pb["difficulty"]
        entries.append(entry)
    return tuple(entries)


def resolve_item_alias(name: str) -> str:
    """Resolve common informal item aliases to canonical Minecraft 1.21.11 IDs."""
    key = (name or "").strip().lower().removeprefix("minecraft:")
    return ITEM_ALIASES.get(key, key)


def _match_score(query_tokens: list[str], full_query: str, *texts: Any) -> int:
    haystack = " ".join(str(t).lower() for t in texts if t is not None)
    if not haystack:
        return 0
    if full_query in haystack:
        return 100 + sum(10 for tok in query_tokens if tok in haystack)
    hits = sum(1 for tok in query_tokens if tok in haystack)
    if hits == 0:
        return 0
    return hits * 20


def lookup_kb(query: str, *, category: str = "all", limit: int = 6) -> dict[str, Any]:
    """Search 1.21.11 recipes, ore heights, mob counterplay, API physics notes, and advancement playbooks."""
    raw = (query or "").strip()
    if not raw:
        return {"ok": False, "error": "bad_args", "message": "query 不能为空。"}

    norm_alias = resolve_item_alias(raw)
    full_q = raw.lower()
    sub_q = full_q.removeprefix("minecraft:").replace(":", " ").replace("/", " ").replace("_", " ")
    sub_tokens = [tok for tok in sub_q.split() if len(tok) >= 2 or not tok.isascii()]
    tokens = sorted({tok for tok in (full_q, norm_alias, *sub_tokens) if tok})
    cat = (category or "all").strip().lower()
    limit = max(1, min(12, limit))

    matches: list[tuple[int, dict[str, Any]]] = []

    if cat in ("all", "recipe", "recipes"):
        for item_id, rec in RECIPES_1_21.items():
            if item_id == norm_alias:
                score = 300
            else:
                score = _match_score(
                    tokens,
                    full_q,
                    item_id,
                    rec.get("notes"),
                    " ".join(rec.get("keywords") or []),
                    " ".join((rec.get("ingredients") or {}).keys()),
                )
            if score > 0:
                matches.append((score, {"kind": "recipe", **rec}))

    if cat in ("all", "ore", "ores"):
        for ore_id, info in ORES_1_21.items():
            score = 300 if ore_id == norm_alias else _match_score(
                tokens,
                full_q,
                ore_id,
                info.get("drop"),
                info.get("y_range"),
                info.get("tip"),
                " ".join(info.get("keywords") or []),
            )
            if score > 0:
                matches.append((score, {"kind": "ore", **info}))

    if cat in ("all", "mob", "mobs", "mechanic", "mechanics", "api"):
        for key, info in MOBS_AND_MECHANICS.items():
            score = 260 if key == full_q else _match_score(
                tokens,
                full_q,
                key,
                info.get("rule"),
                " ".join(info.get("keywords") or []),
            )
            if score > 0:
                matches.append((score, {"kind": info.get("category", "mechanic"), **info}))

    if cat in ("all", "advancement", "advancements"):
        for entry in _load_full_advancement_kb():
            adv_id = entry["id"]
            adv_lower = adv_id.lower()
            exact_targets = {
                adv_lower,
                adv_lower.removeprefix("minecraft:"),
                adv_lower.rsplit("/", 1)[-1],
                entry["title_zh"].lower(),
                entry["title_en"].lower(),
            }
            if full_q in exact_targets:
                score = 320
            else:
                score = _match_score(
                    tokens,
                    full_q,
                    adv_id,
                    entry["title_zh"],
                    entry["title_en"],
                    entry["description_zh"],
                    entry["description_en"],
                    entry["how_to"],
                    " ".join(entry.get("triggers") or []),
                )
            if score > 0:
                if entry["scored"]:
                    score += 15
                matches.append((score, {"kind": "advancement", **entry}))

    matches.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    results = [item for _, item in matches[:limit]]
    return {
        "ok": True,
        "query": raw,
        "resolved_alias": norm_alias if norm_alias != full_q else None,
        "count": len(results),
        "results": results,
    }


def build_jit_error_hint(
    action: str,
    params: dict[str, Any],
    error_code: str,
    error_msg: str,
) -> str | None:
    """Return a targeted, zero-bloat corrective hint for common small-model API/1.21 failures."""
    code = (error_code or "").lower()
    ep = (action or "").strip("/").lower()

    if code == "need_block_target" or (code == "cannot_use" and ep == "use_item"):
        item = str(params.get("item") or "").lower()
        if any(k in item for k in _BLOCK_TARGET_ITEMS) or item.endswith(("_hoe", "_seeds")):
            return (
                f"{item} 属于方块交互物品，不能用 mc_use_item；请改用 mc_interact(x, y, z, item='{item}') 对准目标方块坐标使用。"
            )
        return "该物品不能直接对空使用；若目标为方块（门/床/农田/桶/打火石），请改用 mc_interact(x, y, z, item=...)。"

    if code == "out_of_reach":
        if ep == "dig":
            return (
                "目标超出触及距离(mc_dig 仅能挖约4.5格内可达方块，不会自动远距离破墙掘进)！"
                "严禁向更远坐标连续递推调用 mc_dig；定向掘进请改用 mc_skill_tunnel / mc_skill_staircase，采附近矿请用 mc_skill_mine_blocks。"
            )
        return "目标超出交互距离，请先用 mc_move_to 靠近目标后再操作，勿对不可达远点重复调用。"

    if code == "path_not_found":
        if ep == "move_to" and params.get("y") is not None:
            return (
                "mc_move_to 传了 y 坐标容易导致跨地形无路径！水平跑图请省略 y（只传 x, z）；"
                "若感觉被地形/坑洞卡住，先调用 mc_skill_unstick。"
            )
        if ep == "dig":
            return (
                "目标方块被墙体/落差阻隔无可达路径(mc_dig 不会自动破墙开路)！"
                "严禁沿坐标序列继续递推调用 mc_dig；定向掘进请改用 mc_skill_tunnel / mc_skill_staircase。"
            )
        return "目标位置当前无可达路径（可能是坑洞落差/头顶遮挡/高墙）；可先调 mc_skill_unstick 脱困，或对目标点水平跑图时省略 y。"

    if code in ("no_recipe", "missing_ingredients", "missing_item", "need_crafting_table"):
        raw_item = str(params.get("item") or "")
        if raw_item:
            canon = resolve_item_alias(raw_item)
            rec = RECIPES_1_21.get(canon)
            alias_note = f"（注意官方 ID 为 '{canon}'）" if canon != raw_item.lower() else ""
            if rec:
                table_note = "需靠近工作台(3x3)" if rec["table_3x3"] else "2x2 背包即可"
                return (
                    f"配方提示{alias_note}：{canon} [{table_note}] 需要 {rec['ingredients']}，"
                    "可直接调用 mc_skill_craft_gear 自动备木板/木棍与工作台。"
                )
            if alias_note:
                return f"ID 提示{alias_note}；不确定具体材料时先调 mc_kb_lookup('{canon}')。"
        return "材料或工作站不足时可用 mc_kb_lookup 查询官方 1.21.11 配方，或用 mc_skill_craft_gear 自动处理中间产物。"

    if code == "use_window_api":
        return "容器/熔炉/交易需使用 mc_window_view / mc_window_act；若是熔炉烧矿/烧肉请直接调 mc_skill_smelt。"

    if code == "stalled":
        return "寻路 8 秒无位移，请立即调用 mc_skill_unstick 或改用 mc_control(forward=True, jump=True, ms=800, stop_on=[]) 脱困。"

    if code in ("protected_region", "dig_unconfirmed"):
        return "方块破坏未获服务端确认或位于保护区；可调 mc_skill_harvest_logs / mc_skill_mine_blocks 自动绕开保护位置，或移动至空旷新区域。"

    return None
