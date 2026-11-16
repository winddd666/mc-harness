"""State normalization, VoxelCache, replay bundle parser, and live telemetry store."""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from ..kb import ADVANCEMENT_PLAYBOOKS, build_jit_error_hint
from ..skills.advancements import load_advancements_catalog

# Key visible blocks split into 2 semantic queries so abundant terrain doesn't drown out ores/stations.
VOXEL_QUERY_TERRAIN: list[str] = [
    "grass_block",
    "dirt",
    "stone",
    "cobblestone",
    "gravel",
    "sand",
    "water",
    "lava",
    "powder_snow",
]

VOXEL_QUERY_FEATURES: list[str] = [
    "oak_log",
    "birch_log",
    "spruce_log",
    "iron_ore",
    "coal_ore",
    "copper_ore",
    "diamond_ore",
    "crafting_table",
    "furnace",
    "chest",
    "white_bed",
]

ALL_SAMPLED_VOXEL_TYPES: tuple[str, ...] = tuple(VOXEL_QUERY_TERRAIN + VOXEL_QUERY_FEATURES)

_AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})

_STATION_BLOCKS = frozenset(
    {
        "crafting_table",
        "furnace",
        "blast_furnace",
        "smoker",
        "chest",
        "trapped_chest",
        "ender_chest",
        "stonecutter",
        "anvil",
        "enchanting_table",
    }
)

_STATION_LABELS: dict[str, str] = {
    "crafting_table": "工作台 (crafting_table)",
    "furnace": "熔炉 (furnace)",
    "blast_furnace": "高炉 (blast_furnace)",
    "smoker": "烟熏炉 (smoker)",
    "chest": "箱子 (chest)",
    "white_bed": "床/重生点 (bed)",
}

_CATEGORY_ORDER = {"story": 0, "husbandry": 1, "adventure": 2, "nether": 3, "end": 4}
_FRAME_ORDER = {"task": 0, "goal": 1, "challenge": 2}
_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def _norm_adv_id(adv_id: Any) -> str:
    if not adv_id:
        return ""
    s = str(adv_id).strip()
    if not s:
        return ""
    return s if ":" in s else f"minecraft:{s}"


def _clean_block_name(raw: Any) -> str:
    if not raw:
        return ""
    return str(raw).strip().lower().removeprefix("minecraft:")


def _is_station_block(name: str) -> bool:
    return name in _STATION_BLOCKS or name.endswith("_bed")


def _station_label(name: str) -> str:
    if name in _STATION_LABELS:
        return _STATION_LABELS[name]
    if name.endswith("_bed"):
        return f"床/重生点 ({name})"
    return f"站点 ({name})"


def _norm_pos_float(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return None
    return [round(x, 3), round(y, 3), round(z, 3)]


def _norm_pos_int(raw: Any) -> tuple[int, int, int] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        x, y, z = math.floor(float(raw[0])), math.floor(float(raw[1])), math.floor(float(raw[2]))
    except (TypeError, ValueError):
        return None
    return (int(x), int(y), int(z))


def _dist3(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class VoxelCache:
    """Bounded spatial cache of recently observed/modified world blocks."""

    def __init__(self, max_blocks: int = 2500) -> None:
        self.max_blocks = max(100, int(max_blocks))
        self._blocks: dict[tuple[int, int, int], tuple[str, int]] = {}
        self._tick = 0

    def __len__(self) -> int:
        return len(self._blocks)

    def clear(self) -> None:
        self._blocks.clear()
        self._tick = 0

    def put_blocks(
        self,
        items: list[dict[str, Any]],
        *,
        anchor_pos: list[float] | None = None,
    ) -> tuple[list[dict[str, Any]], list[list[int]]]:
        """Insert/update blocks; returns `(added_or_changed, removed_positions)`."""
        added: list[dict[str, Any]] = []
        removed: list[list[int]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = _norm_pos_int(item.get("pos"))
            name = _clean_block_name(item.get("name") or item.get("block"))
            if key is None or not name:
                continue
            if name in _AIR_BLOCKS:
                if key in self._blocks:
                    del self._blocks[key]
                removed.append([key[0], key[1], key[2]])
                continue
            self._tick += 1
            prev = self._blocks.get(key)
            self._blocks[key] = (name, self._tick)
            if prev is None or prev[0] != name:
                added.append({"pos": [key[0], key[1], key[2]], "name": name})

        if len(self._blocks) > self.max_blocks:
            self._evict(anchor_pos)
        return added, removed

    def remove_positions(self, positions: list[Any]) -> list[list[int]]:
        removed: list[list[int]] = []
        seen: set[tuple[int, int, int]] = set()
        for raw in positions:
            pos_val = raw.get("pos") if isinstance(raw, dict) else raw
            key = _norm_pos_int(pos_val)
            if key is None or key in seen:
                continue
            seen.add(key)
            self._blocks.pop(key, None)
            removed.append([key[0], key[1], key[2]])
        return removed

    def find_nearest_by_types(
        self,
        block_types: list[str],
        anchor_pos: list[float] | None,
    ) -> list[int] | None:
        wanted = {_clean_block_name(t) for t in block_types if t}
        if not wanted:
            return None
        matches = [pos for pos, (name, _) in self._blocks.items() if name in wanted]
        if not matches:
            return None
        if anchor_pos is None:
            p = matches[0]
            return [p[0], p[1], p[2]]
        best = min(matches, key=lambda p: _dist3(p, anchor_pos))
        return [best[0], best[1], best[2]]

    def _evict(self, anchor_pos: list[float] | None) -> None:
        excess = len(self._blocks) - self.max_blocks
        if excess <= 0:
            return
        if anchor_pos is not None and len(anchor_pos) >= 3:
            ax, ay, az = float(anchor_pos[0]), float(anchor_pos[1]), float(anchor_pos[2])

            def _score(kv: tuple[tuple[int, int, int], tuple[str, int]]) -> tuple[float, int]:
                (bx, by, bz), (name, tick) = kv
                dist = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2)
                # Prefer keeping stations and ores slightly longer than plain terrain
                bonus = -40.0 if (_is_station_block(name) or "ore" in name) else 0.0
                return (-(dist + bonus), tick)

            ordered = sorted(self._blocks.items(), key=_score)
        else:
            ordered = sorted(self._blocks.items(), key=lambda kv: kv[1][1])

        for key, _ in ordered[:excess]:
            self._blocks.pop(key, None)

    def to_list(self) -> list[dict[str, Any]]:
        return [{"pos": [k[0], k[1], k[2]], "name": name} for k, (name, _) in self._blocks.items()]


def merge_advancements_with_catalog(
    raw: dict[str, Any] | None,
    *,
    frontier_limit: int = 25,
) -> dict[str, Any]:
    """Merge live `/advancements` or `mc_advancement_frontier` data with local 125-item catalog."""
    catalog = load_advancements_catalog()
    catalog_items: list[dict[str, Any]] = catalog.get("advancements") or []
    cat_by_id = {_norm_adv_id(item.get("id")): item for item in catalog_items if isinstance(item, dict)}

    if not isinstance(raw, dict):
        raw = {}

    completed_ids: set[str] = set()
    live_by_id: dict[str, dict[str, Any]] = {}

    # Case 1: Already a frontier dict (`completed_ids` + `frontier`)
    for cid in raw.get("completed_ids") or []:
        nid = _norm_adv_id(cid)
        if nid and nid in cat_by_id:
            completed_ids.add(nid)
        elif nid:
            completed_ids.add(nid)

    # Case 2: Raw `/advancements` endpoint result (`advancements` list)
    for row in raw.get("advancements") or []:
        if not isinstance(row, dict):
            continue
        nid = _norm_adv_id(row.get("id"))
        if not nid:
            continue
        # Skip recipe advancements when non-recipe catalog is used
        if nid.startswith("minecraft:recipes/"):
            continue
        live_by_id[nid] = row
        if row.get("status") == "completed":
            completed_ids.add(nid)

    completed_details: list[dict[str, Any]] = []
    for cid in sorted(completed_ids):
        cat_entry = cat_by_id.get(cid) or {}
        live_entry = live_by_id.get(cid) or {}
        completed_details.append(
            {
                "id": cid,
                "category": cat_entry.get("category") or (cid.split(":", 1)[-1].split("/", 1)[0] if "/" in cid else "story"),
                "frame": cat_entry.get("frame") or live_entry.get("frame") or "task",
                "title_zh": cat_entry.get("title_zh") or live_entry.get("title") or cid,
                "description_zh": cat_entry.get("description_zh") or "",
                "completed_at": live_entry.get("completed_at"),
            }
        )

    if isinstance(raw.get("frontier"), list) and raw.get("frontier"):
        # Enrich existing frontier if needed while preserving full catalog frontier count
        frontier_all = _compute_frontier_from_completed(catalog_items, completed_ids, live_by_id)
        raw_frontier_by_id = {
            _norm_adv_id(f.get("id")): f
            for f in raw["frontier"]
            if isinstance(f, dict) and f.get("id")
        }
        for f_item in frontier_all:
            src_f = raw_frontier_by_id.get(f_item["id"])
            if not src_f:
                continue
            if f_item.get("progress") is None and src_f.get("progress") is not None:
                f_item["progress"] = src_f.get("progress")
            if not f_item.get("how_to") and src_f.get("how_to"):
                f_item["how_to"] = src_f["how_to"]
            if not f_item.get("difficulty") and src_f.get("difficulty"):
                f_item["difficulty"] = src_f["difficulty"]
    else:
        frontier_all = _compute_frontier_from_completed(catalog_items, completed_ids, live_by_id)

    completed_count = max(len(completed_ids), int(raw.get("completed_count") or 0))
    return {
        "total_catalog": len(catalog_items) or 125,
        "completed_count": completed_count,
        "completed_ids": sorted(completed_ids),
        "completed": completed_details,
        "frontier_count": int(raw.get("frontier_count") or len(frontier_all)),
        "frontier": frontier_all[: max(1, frontier_limit)],
    }


_ENGLISH_ADV_TITLES: dict[str, str] = {
    "minecraft": "minecraft:story/root",
    "stone age": "minecraft:story/mine_stone",
    "getting an upgrade": "minecraft:story/upgrade_tools",
    "acquire hardware": "minecraft:story/smelt_iron",
    "suit up": "minecraft:story/obtain_armor",
    "hot stuff": "minecraft:story/lava_bucket",
    "isn't it iron pick": "minecraft:story/iron_tools",
    "not today, thank you": "minecraft:story/deflect_arrow",
    "ice bucket challenge": "minecraft:story/form_obsidian",
    "diamonds!": "minecraft:story/mine_diamond",
    "we need to go deeper": "minecraft:story/enter_the_nether",
    "cover me with diamonds": "minecraft:story/shiny_gear",
    "enchanter": "minecraft:story/enchant_item",
    "zombie doctor": "minecraft:story/cure_zombie_villager",
    "eye spy": "minecraft:story/follow_ender_eye",
    "the end?": "minecraft:story/enter_the_end",
    "adventure": "minecraft:adventure/root",
    "monster hunter": "minecraft:adventure/kill_a_mob",
    "what a deal!": "minecraft:adventure/trade",
    "sweet dreams": "minecraft:adventure/sleep_in_bed",
    "take aim": "minecraft:adventure/shoot_arrow",
    "sneak 100": "minecraft:adventure/avoid_vibration",
    "caves & cliffs": "minecraft:adventure/fall_from_world_height",
    "light as a rabbit": "minecraft:adventure/walk_on_powder_snow_with_leather_boots",
    "husbandry": "minecraft:husbandry/root",
    "a seedy place": "minecraft:husbandry/plant_seed",
    "the parrots and the bats": "minecraft:husbandry/breed_an_animal",
    "best friends forever": "minecraft:husbandry/tame_an_animal",
    "fishy business": "minecraft:husbandry/fishy_business",
}


def _collect_adv_with_ancestors(seed_ids: set[str], cat_by_id: dict[str, dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for sid in seed_ids:
        nid = _norm_adv_id(sid)
        if not nid:
            continue
        out.add(nid)
        cur = cat_by_id.get(nid)
        visited: set[str] = {nid}
        while isinstance(cur, dict) and cur.get("parent"):
            pid = _norm_adv_id(cur.get("parent"))
            if not pid or pid in visited:
                break
            visited.add(pid)
            out.add(pid)
            cur = cat_by_id.get(pid)
    return out


def _resolve_advancement_token(token: Any) -> set[str]:
    """Resolve an advancement ID, Chinese title, or English broadcast title into catalog ID + ancestors."""
    if not token:
        return set()
    raw_str = str(token).strip()
    if not raw_str:
        return set()
    catalog_items: list[dict[str, Any]] = load_advancements_catalog().get("advancements") or []
    cat_by_id = {_norm_adv_id(item.get("id")): item for item in catalog_items if isinstance(item, dict)}

    cand_id = _norm_adv_id(raw_str)
    if cand_id in cat_by_id:
        return _collect_adv_with_ancestors({cand_id}, cat_by_id)

    lowered = raw_str.lower()
    mapped = _ENGLISH_ADV_TITLES.get(lowered)
    if mapped and mapped in cat_by_id:
        return _collect_adv_with_ancestors({mapped}, cat_by_id)

    for cid, entry in cat_by_id.items():
        if str(entry.get("title_zh") or "").strip() == raw_str:
            return _collect_adv_with_ancestors({cid}, cat_by_id)
    return set()


def _infer_advancements_from_summary(
    summary: dict[str, Any] | None,
    *,
    include_newly_unlocked: bool,
    extra_completed_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Reconstruct advancement completion & frontier from companion `.summary.json` + catalog parent chains."""
    if not isinstance(summary, dict):
        return None
    has_adv_fields = any(k in summary for k in ("completed_count", "newly_unlocked", "frontier_top10"))
    if not has_adv_fields:
        return None

    catalog_items: list[dict[str, Any]] = load_advancements_catalog().get("advancements") or []
    cat_by_id = {_norm_adv_id(item.get("id")): item for item in catalog_items if isinstance(item, dict)}

    new_set = {_norm_adv_id(x) for x in (summary.get("newly_unlocked") or []) if _norm_adv_id(x)}
    frontier_top = summary.get("frontier_top10") if isinstance(summary.get("frontier_top10"), list) else []

    ancestors: set[str] = set()
    for f in frontier_top:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        fid = _norm_adv_id(f.get("id"))
        cur = cat_by_id.get(fid)
        visited: set[str] = {fid}
        while isinstance(cur, dict) and cur.get("parent"):
            pid = _norm_adv_id(cur.get("parent"))
            if not pid or pid in visited:
                break
            visited.add(pid)
            ancestors.add(pid)
            cur = cat_by_id.get(pid)

    for nid in new_set:
        cur = cat_by_id.get(nid)
        visited = {nid}
        while isinstance(cur, dict) and cur.get("parent"):
            pid = _norm_adv_id(cur.get("parent"))
            if not pid or pid in visited:
                break
            visited.add(pid)
            ancestors.add(pid)
            cur = cat_by_id.get(pid)

    completed_set = (ancestors | new_set) if include_newly_unlocked else (ancestors - new_set)
    if extra_completed_ids:
        completed_set |= {_norm_adv_id(x) for x in extra_completed_ids if _norm_adv_id(x)}

    expected_count = 0
    if isinstance(summary.get("completed_count"), int):
        raw_cc = int(summary["completed_count"])
        expected_count = raw_cc if include_newly_unlocked else max(0, raw_cc - len(new_set))

    raw_payload: dict[str, Any] = {
        "completed_ids": sorted(completed_set),
        "completed_count": max(len(completed_set), expected_count),
    }
    if include_newly_unlocked and frontier_top:
        raw_payload["frontier"] = frontier_top
    return merge_advancements_with_catalog(raw_payload)


def _empty_inventory_state() -> dict[str, Any]:
    return {
        "items": [],
        "armor": {"head": None, "chest": None, "legs": None, "feet": None},
        "offhand": None,
        "held": None,
        "empty_slots": 36,
    }


def _compute_frontier_from_completed(
    catalog_items: list[dict[str, Any]],
    completed_ids: set[str],
    live_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for entry in catalog_items:
        eid = _norm_adv_id(entry.get("id"))
        if not eid or eid in completed_ids:
            continue
        parent = _norm_adv_id(entry.get("parent")) if entry.get("parent") else ""
        if parent and parent not in completed_ids:
            continue
        live_entry = live_by_id.get(eid) or {}
        row_out: dict[str, Any] = {
            "id": eid,
            "category": entry.get("category"),
            "frame": entry.get("frame"),
            "title_zh": entry.get("title_zh"),
            "description_zh": entry.get("description_zh"),
            "requirements_count": entry.get("requirements_count", 1),
            "progress": live_entry.get("progress"),
        }
        pb = ADVANCEMENT_PLAYBOOKS.get(eid)
        if pb:
            row_out["difficulty"] = pb["difficulty"]
            row_out["how_to"] = pb["how_to"]
        frontier.append(row_out)

    frontier.sort(
        key=lambda x: (
            _CATEGORY_ORDER.get(str(x.get("category")), 9),
            _FRAME_ORDER.get(str(x.get("frame")), 9),
            _DIFFICULTY_ORDER.get(str(x.get("difficulty")), 1),
            int(x.get("requirements_count") or 1),
            str(x.get("id")),
        )
    )
    return frontier


def parse_initial_snapshot_text(text: str) -> dict[str, Any] | None:
    """Extract `status`, `inventory`, and `blocks` from `[开局实时状态快照]` prompt text."""
    if not text or "[开局实时状态快照]" not in text:
        return None
    _, after = text.split("[开局实时状态快照]", 1)
    out: dict[str, Any] = {}
    for raw_line in after.splitlines():
        line = raw_line.strip()
        if line.startswith("status:"):
            payload = _try_json_dict(line[len("status:") :].strip())
            if payload:
                st = payload.get("result") if isinstance(payload.get("result"), dict) else payload
                if isinstance(st, dict):
                    out["status"] = st
        elif line.startswith("inventory:"):
            payload = _try_json_dict(line[len("inventory:") :].strip())
            if payload:
                inv = payload.get("result") if isinstance(payload.get("result"), dict) else payload
                if isinstance(inv, dict):
                    out["inventory"] = inv
        elif line.startswith("nearby_blocks_summary"):
            _, _, rhs = line.partition(":")
            payload = _try_json_dict(rhs.strip())
            if payload:
                res = payload.get("result") if isinstance(payload.get("result"), dict) else payload
                blocks = _extract_blocks_from_find_result(res)
                if blocks:
                    out["blocks"] = blocks
    return out or None


_HURT_NOTE_RE = re.compile(r"^\[受伤\s+health=([^\s\]]+)(?:\s+by\s+([^\]]+))?\]$")
_DEATH_NOTE_RE = re.compile(r"^\[死亡\s+pos=(\[[^\]]+\]|None)(?:\s+(.*))?\]$")
_RESPAWN_NOTE_RE = re.compile(r"^\[已重生\s+pos=(\[[^\]]+\]|None)\]$")
_ADV_NOTE_RE = re.compile(r"^\[新解锁进度:\s*([^\]]+)\]$")
_CHAT_NOTE_RE = re.compile(r"^\[不可信玩家公屏消息\s+([^:]+):\s*(.*)\]$")
_ADV_BROADCAST_RE = re.compile(
    r"([A-Za-z0-9_]{3,16})\s+has\s+(?:made\s+the\s+advancement|completed\s+the\s+challenge|reached\s+the\s+goal)\s+\[([^\]]+)\]"
)


def parse_notable_events_text(text: str, *, bot_username: str | None = None) -> list[dict[str, Any]]:
    """Parse `[期间新事件] ...` strings into structured inline game events."""
    if not text:
        return []
    raw = text.strip()
    if raw.startswith("[期间新事件]"):
        raw = raw[len("[期间新事件]") :].strip()
    if not raw:
        return []

    events: list[dict[str, Any]] = []
    for part in raw.split("; "):
        token = part.strip()
        if not token:
            continue
        m = _HURT_NOTE_RE.match(token)
        if m:
            try:
                hp = round(float(m.group(1)), 2)
            except ValueError:
                hp = None
            src = (m.group(2) or "").strip() or None
            events.append(
                {
                    "type": "hurt",
                    "health": hp,
                    "source": src,
                    "label": f"受伤 ({src or 'unknown'}) HP→{hp}" if hp is not None else f"受伤 ({src or 'unknown'})",
                }
            )
            continue

        m = _DEATH_NOTE_RE.match(token)
        if m:
            pos = _norm_pos_float(_try_json_any(m.group(1)))
            cause = (m.group(2) or "").strip() or None
            events.append(
                {
                    "type": "death",
                    "pos": pos,
                    "dropped_items_at": pos,
                    "cause": cause,
                    "label": f"死亡 {cause or ''}".strip(),
                }
            )
            continue

        m = _RESPAWN_NOTE_RE.match(token)
        if m:
            pos = _norm_pos_float(_try_json_any(m.group(1)))
            events.append({"type": "spawn", "pos": pos, "label": f"重生 {pos or ''}".strip()})
            continue

        m = _ADV_NOTE_RE.match(token)
        if m:
            adv = m.group(1).strip()
            events.append({"type": "advancement", "advancement": adv, "label": f"解锁进度: {adv}"})
            continue

        m = _CHAT_NOTE_RE.match(token)
        if m:
            sender = m.group(1).strip()
            msg = m.group(2).strip()
            adv_m = _ADV_BROADCAST_RE.search(msg)
            if adv_m:
                who, adv_title = adv_m.group(1), adv_m.group(2)
                if not bot_username or who.lower() == bot_username.lower():
                    events.append(
                        {
                            "type": "advancement",
                            "advancement": adv_title,
                            "label": f"解锁进度 [{adv_title}]",
                        }
                    )
                    continue
            # Filter noisy proxy lobby banners while keeping real chat messages
            if (
                "欢迎来到 NMO" in msg
                or "来到NMO群组服务器" in msg
                or "joined the game" in msg
                or "left the game" in msg
                or "已连接到 [" in msg
                or "已退出服务器" in msg
            ):
                continue
            events.append({"type": "chat", "sender": sender, "text": msg, "label": f"{sender}: {msg}"})
    return events


def _try_json_dict(raw: str) -> dict[str, Any] | None:
    if not raw or not raw.startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _try_json_any(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _extract_blocks_from_find_result(res: Any) -> list[dict[str, Any]]:
    if not isinstance(res, dict):
        return []
    blocks_field = res.get("blocks")
    out: list[dict[str, Any]] = []
    if isinstance(blocks_field, list):
        for b in blocks_field:
            if isinstance(b, dict) and b.get("pos") and b.get("name"):
                out.append({"pos": b["pos"], "name": b["name"]})
    elif isinstance(blocks_field, dict):
        for name, info in blocks_field.items():
            if isinstance(info, dict) and info.get("nearest"):
                out.append({"pos": info["nearest"], "name": name})
    return out


class ReplayIncrementer:
    """Stateful event-by-event normalizer shared by batch `/api/replay` and live `.events.jsonl` tailing."""

    def __init__(
        self,
        run_id: str,
        *,
        coalesce_thought_window_s: float = 0.6,
        companion_report: dict[str, Any] | None = None,
        companion_summary: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.coalesce_thought_window_s = coalesce_thought_window_s
        self.meta: dict[str, Any] = {
            "task": "",
            "bot": None,
            "model": None,
            "max_calls": None,
            "duration_s": 0.0,
            "status": "running",
            "actor_requests": 0,
        }
        self.frames: list[dict[str, Any]] = []
        self.trajectory: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self.voxel_cache = VoxelCache(max_blocks=2500)
        self.status: dict[str, Any] = {}
        self.inventory: dict[str, Any] = {}
        self.advancements: dict[str, Any] = merge_advancements_with_catalog(None)

        self._seen_marker_keys: set[tuple[Any, ...]] = set()
        self._known_completed_advs: set[str] = set()
        self._adv_seeded = False
        self._current_request = 0
        self._turn_text: dict[int, str] = {}
        self._turn_had_delta: set[int] = set()
        self._call_meta: dict[str, dict[str, Any]] = {}
        self._initial_snapshot: dict[str, Any] | None = None
        self._companion_notable_by_call_id: dict[str, str] = {}
        self._companion_summary = companion_summary if isinstance(companion_summary, dict) else None
        self._initial_seeded = False

        if isinstance(companion_report, dict):
            self._ingest_companion_report(companion_report)
        if self._companion_summary:
            if self._companion_summary.get("provider_model") and not self.meta.get("model"):
                self.meta["model"] = self._companion_summary["provider_model"]
            if self._companion_summary.get("max_calls") and not self.meta.get("max_calls"):
                self.meta["max_calls"] = self._companion_summary["max_calls"]

    def _ingest_companion_report(self, report: dict[str, Any]) -> None:
        if report.get("bot_username"):
            self.meta["bot"] = report.get("bot_username")
        if report.get("model"):
            self.meta["model"] = report.get("model")
        if report.get("task"):
            self.meta["task"] = report.get("task")
        # New runs record notable events directly; companion text is only a legacy fallback.
        recorded_calls = {
            str(ev.get("call_id")) for ev in report.get("events") or []
            if isinstance(ev, dict) and ev.get("type") == "mc.notable_events"
        }
        for msg in report.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            if role == "user" and self._initial_snapshot is None:
                snap = parse_initial_snapshot_text(content)
                if snap:
                    self._initial_snapshot = snap
            elif (role == "tool" and msg.get("tool_call_id")
                  and str(msg["tool_call_id"]) not in recorded_calls and "[期间新事件]" in content):
                _, _, notable_part = content.partition("[期间新事件]")
                self._companion_notable_by_call_id[str(msg["tool_call_id"])] = notable_part.strip()

    def _record_pos(
        self,
        pos_raw: Any,
        *,
        elapsed: float,
        yaw: float | None = None,
        dimension: str | None = None,
        health: float | None = None,
        food: float | None = None,
    ) -> list[float] | None:
        pos = _norm_pos_float(pos_raw)
        if pos is None:
            return None
        self.status["pos"] = pos
        if yaw is not None and isinstance(yaw, (int, float)) and math.isfinite(float(yaw)):
            self.status["yaw"] = round(float(yaw), 4)
        if dimension:
            self.status["dimension"] = str(dimension)
        if health is not None and isinstance(health, (int, float)):
            self.status["health"] = round(float(health), 2)
        if food is not None and isinstance(food, (int, float)):
            self.status["food"] = round(float(food), 1)

        cur_yaw = self.status.get("yaw")
        cur_dim = str(self.status.get("dimension") or "overworld")
        cur_hp = self.status.get("health")
        cur_food = self.status.get("food")

        point = {
            "elapsed": round(float(elapsed), 3),
            "pos": pos,
            "yaw": cur_yaw if isinstance(cur_yaw, (int, float)) else 0.0,
            "dimension": cur_dim,
            "health": cur_hp if isinstance(cur_hp, (int, float)) else 20,
            "food": cur_food if isinstance(cur_food, (int, float)) else 20,
        }
        if not self.trajectory:
            self.trajectory.append(point)
        else:
            prev = self.trajectory[-1]
            moved = _dist3(prev["pos"], pos)
            hp_changed = prev.get("health") != point["health"]
            dim_changed = prev.get("dimension") != point["dimension"]
            if moved >= 0.05 or hp_changed or dim_changed or (elapsed - prev["elapsed"] >= 15.0):
                self.trajectory.append(point)
        return pos

    def _add_marker(
        self,
        *,
        elapsed: float,
        kind: str,
        label: str,
        pos_raw: Any = None,
        dimension: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pos = _norm_pos_float(pos_raw) or _norm_pos_float(self.status.get("pos"))
        if pos is None:
            return None
        dim = str(dimension or self.status.get("dimension") or "overworld")
        int_pos = _norm_pos_int(pos)
        # Deduplicate station/protected/advancement by block coordinate + kind; hurt/death allow distinct moments
        if kind in ("station", "protected_region"):
            dedup_key: tuple[Any, ...] = (kind, dim, int_pos)
        elif kind == "advancement":
            dedup_key = (kind, label)
        elif kind == "death":
            dedup_key = (kind, dim, int_pos, label)
        else:
            # Coalesce hurt markers within same 2s window at same block
            dedup_key = (kind, dim, int_pos, int(elapsed // 2))

        if dedup_key in self._seen_marker_keys:
            return None
        self._seen_marker_keys.add(dedup_key)

        marker: dict[str, Any] = {
            "elapsed": round(float(elapsed), 3),
            "kind": kind,
            "label": label,
            "pos": pos,
            "dimension": dim,
        }
        if extra:
            marker.update(extra)
        self.markers.append(marker)
        return marker

    def _apply_status_dict(
        self,
        st: dict[str, Any],
        *,
        elapsed: float,
        patch: dict[str, Any],
        markers_out: list[dict[str, Any]],
    ) -> None:
        prev_hp = self.status.get("health")
        for k, v in st.items():
            if k == "pos":
                continue
            if isinstance(v, float):
                self.status[k] = round(v, 3)
            else:
                self.status[k] = v

        pos = self._record_pos(
            st.get("pos"),
            elapsed=elapsed,
            yaw=st.get("yaw"),
            dimension=st.get("dimension"),
            health=st.get("health"),
            food=st.get("food"),
        )
        if pos is not None:
            patch["pos"] = pos
        for key in ("yaw", "dimension", "health", "food", "held", "offhand", "oxygen", "armor_points", "busy", "in_water", "submerged", "in_lava", "on_fire", "biome", "time_of_day", "is_day", "nearest_hostile"):
            if key in self.status:
                patch[key] = self.status[key]
        patch["status"] = dict(self.status)

        hp_now = st.get("health")
        if (
            isinstance(prev_hp, (int, float))
            and isinstance(hp_now, (int, float))
            and prev_hp - hp_now >= 1.5
        ):
            cause = "环境/细雪冻伤" if (not st.get("in_water") and (pos or [0, 0, 0])[1] >= 120) else "受伤扣血"
            m = self._add_marker(
                elapsed=elapsed,
                kind="hurt",
                label=f"{cause} HP {round(float(prev_hp), 1)}→{round(float(hp_now), 1)}",
                pos_raw=pos,
            )
            if m:
                markers_out.append(m)

        last_death = st.get("last_death")
        if isinstance(last_death, dict):
            dpos = last_death.get("dropped_items_at") or last_death.get("pos")
            if dpos:
                cause_txt = last_death.get("cause") or "掉落物坐标"
                m = self._add_marker(
                    elapsed=elapsed,
                    kind="death",
                    label=f"死亡掉落: {cause_txt}",
                    pos_raw=dpos,
                    extra={"dropped_items_at": _norm_pos_float(dpos)},
                )
                if m:
                    markers_out.append(m)

    def _apply_blocks(
        self,
        raw_blocks: list[dict[str, Any]],
        *,
        elapsed: float,
        patch: dict[str, Any],
        markers_out: list[dict[str, Any]],
    ) -> None:
        if not raw_blocks:
            return
        added, removed = self.voxel_cache.put_blocks(
            raw_blocks,
            anchor_pos=_norm_pos_float(self.status.get("pos")),
        )
        if added:
            patch.setdefault("blocks_added", []).extend(added)
            for b in added:
                bname = b["name"]
                if _is_station_block(bname):
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="station",
                        label=_station_label(bname),
                        pos_raw=b["pos"],
                    )
                    if m:
                        markers_out.append(m)
        if removed:
            patch.setdefault("blocks_removed", []).extend(removed)

    def _clear_inventory_on_death(self, patch: dict[str, Any]) -> None:
        self.inventory = _empty_inventory_state()
        self.status["held"] = None
        self.status["offhand"] = None
        self.status["armor_points"] = 0
        patch["inventory"] = dict(self.inventory)
        patch["held"] = None
        patch["offhand"] = None
        patch["armor_points"] = 0
        patch["status"] = dict(self.status)

    def _seed_initial_state(self, elapsed: float) -> dict[str, Any]:
        if self._initial_seeded:
            return {}
        self._initial_seeded = True
        patch: dict[str, Any] = {}
        markers_out: list[dict[str, Any]] = []

        if self._initial_snapshot:
            if isinstance(self._initial_snapshot.get("status"), dict):
                self._apply_status_dict(
                    self._initial_snapshot["status"],
                    elapsed=elapsed,
                    patch=patch,
                    markers_out=markers_out,
                )
            if isinstance(self._initial_snapshot.get("inventory"), dict):
                self.inventory = dict(self._initial_snapshot["inventory"])
                patch["inventory"] = dict(self.inventory)
                if "held" in self.inventory:
                    self.status["held"] = self.inventory.get("held")
                    patch["held"] = self.inventory.get("held")
            if isinstance(self._initial_snapshot.get("blocks"), list):
                self._apply_blocks(
                    self._initial_snapshot["blocks"],
                    elapsed=elapsed,
                    patch=patch,
                    markers_out=markers_out,
                )

        if "pos" not in self.status and self._companion_summary and self._companion_summary.get("pos_before"):
            pos = self._record_pos(self._companion_summary["pos_before"], elapsed=elapsed)
            if pos is not None:
                patch["pos"] = pos
                patch["status"] = dict(self.status)

        if not self._adv_seeded and self._companion_summary:
            inferred_pre = _infer_advancements_from_summary(
                self._companion_summary,
                include_newly_unlocked=False,
            )
            if inferred_pre is not None:
                self._known_completed_advs |= set(inferred_pre["completed_ids"])
                self._adv_seeded = True
                self.advancements = inferred_pre
                patch["advancements"] = dict(inferred_pre)

        if markers_out:
            _prio = {"death": 0, "hurt": 1, "advancement": 2, "protected_region": 3, "station": 4}
            primary_m = min(markers_out, key=lambda m: _prio.get(str(m.get("kind")), 9))
            patch["marker"] = primary_m
            patch["markers"] = markers_out
        return patch

    def feed_events(
        self,
        raw_events: list[dict[str, Any]],
        *,
        coalesce_window_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """Process raw `.events.jsonl` event dicts and return newly emitted frames."""
        win_s = self.coalesce_thought_window_s if coalesce_window_s is None else coalesce_window_s
        emitted: list[dict[str, Any]] = []
        batch_start_idx = len(self.frames)

        for ev in raw_events:
            if not isinstance(ev, dict):
                continue
            ev_type = str(ev.get("type") or "")
            seq = int(ev.get("seq") or (len(self.frames) + 1))
            elapsed = round(float(ev.get("elapsed") or 0.0), 4)
            ts_iso = str(ev.get("time") or "")
            self.meta["duration_s"] = max(float(self.meta.get("duration_s") or 0.0), elapsed)

            if ev_type == "run.started":
                self.meta["task"] = ev.get("task") or self.meta.get("task") or ""
                self.meta["bot"] = ev.get("bot") or self.meta.get("bot")
                self.meta["model"] = ev.get("model") or self.meta.get("model")
                if ev.get("max_calls") is not None:
                    self.meta["max_calls"] = ev.get("max_calls")
                if isinstance(ev.get("initial_snapshot"), str):
                    self._initial_snapshot = parse_initial_snapshot_text(ev["initial_snapshot"]) or self._initial_snapshot
                if self._initial_snapshot is None and isinstance(ev.get("task"), str):
                    snap = parse_initial_snapshot_text(ev["task"])
                    if snap:
                        self._initial_snapshot = snap
                seed_patch = self._seed_initial_state(elapsed)
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "phase",
                    "data": {
                        "phase": "preparing",
                        "event_type": "run.started",
                        "task": self.meta["task"],
                        "bot": self.meta["bot"],
                        "model": self.meta["model"],
                        "max_calls": self.meta["max_calls"],
                    },
                    "state_patch": seed_patch,
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "run.phase":
                seed_patch = self._seed_initial_state(elapsed) if not self._initial_seeded else {}
                phase_name = str(ev.get("phase") or "preparing")
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "phase",
                    "data": {
                        "phase": phase_name,
                        "task_version": ev.get("task_version", 0),
                        "request": self._current_request,
                    },
                    "state_patch": seed_patch,
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type in ("model.request", "model.started"):
                req = int(ev.get("request") or (self._current_request + 1))
                self._current_request = max(self._current_request, req)
                self.meta["actor_requests"] = max(int(self.meta.get("actor_requests") or 0), req)

            elif ev_type == "model.rate_wait":
                req = int(ev.get("request") or self._current_request or 1)
                wait_s = round(float(ev.get("wait_s") or 0.0), 3)
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "rate_wait",
                    "data": {"request": req, "turn": req, "wait_s": wait_s},
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type in ("model.text.delta", "model.text"):
                req = int(ev.get("request") or self._current_request or 1)
                self._current_request = max(self._current_request, req)
                self.meta["actor_requests"] = max(int(self.meta.get("actor_requests") or 0), req)
                chunk = str(ev.get("text") or "")
                if not chunk:
                    continue
                self._turn_had_delta.add(req)
                acc = self._turn_text.get(req, "") + chunk
                self._turn_text[req] = acc

                if (
                    win_s > 0
                    and len(self.frames) > batch_start_idx
                    and self.frames[-1]["kind"] == "thought_delta"
                    and self.frames[-1]["data"].get("request") == req
                    and (elapsed - float(self.frames[-1]["elapsed"])) <= win_s
                ):
                    prev = self.frames[-1]
                    prev["seq"] = seq
                    prev["elapsed"] = elapsed
                    prev["time"] = ts_iso
                    prev["data"]["text"] += chunk
                    prev["data"]["delta"] += chunk
                    prev["data"]["accumulated"] = acc
                    if not emitted or emitted[-1] is not prev:
                        emitted.append(prev)
                else:
                    frame = {
                        "seq": seq,
                        "elapsed": elapsed,
                        "time": ts_iso,
                        "kind": "thought_delta",
                        "data": {
                            "request": req,
                            "turn": req,
                            "text": chunk,
                            "delta": chunk,
                            "accumulated": acc,
                        },
                        "state_patch": {},
                    }
                    self.frames.append(frame)
                    emitted.append(frame)

            elif ev_type == "model.tool.delta":
                # Pre-register streaming tool call metadata before model.completed arrives
                cid = str(ev.get("id") or "")
                name = str(ev.get("name") or "")
                if cid:
                    entry = self._call_meta.setdefault(
                        cid,
                        {"call_id": cid, "request": int(ev.get("request") or self._current_request or 1)},
                    )
                    if name:
                        entry["name"] = name

            elif ev_type == "model.usage":
                req = int(ev.get("request") or self._current_request or 1)
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
                cached_tokens = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
                hit_pct = round(cached_tokens * 100.0 / prompt_tokens, 1) if prompt_tokens > 0 else 0.0
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "usage",
                    "data": {
                        "request": req,
                        "turn": req,
                        "usage": usage,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "cached_tokens": cached_tokens,
                        "cache_hit_pct": hit_pct,
                    },
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "model.completed":
                req = int(ev.get("request") or self._current_request or 1)
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                content = str(msg.get("content") or "")
                if content and req not in self._turn_had_delta:
                    self._turn_had_delta.add(req)
                    self._turn_text[req] = content
                    frame = {
                        "seq": seq,
                        "elapsed": elapsed,
                        "time": ts_iso,
                        "kind": "thought_delta",
                        "data": {
                            "request": req,
                            "turn": req,
                            "text": content,
                            "delta": content,
                            "accumulated": content,
                        },
                        "state_patch": {},
                    }
                    self.frames.append(frame)
                    emitted.append(frame)

                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict) or not tc.get("id"):
                        continue
                    cid = str(tc["id"])
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    t_name = str(fn.get("name") or "")
                    t_args_raw = str(fn.get("arguments") or "{}")
                    t_args = _try_json_dict(t_args_raw) or {}
                    self._call_meta[cid] = {
                        "call_id": cid,
                        "request": req,
                        "name": t_name,
                        "arguments": t_args_raw,
                        "args": t_args,
                    }

            elif ev_type == "model.failed":
                req = int(ev.get("request") or self._current_request or 1)
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "phase",
                    "data": {
                        "phase": "failed",
                        "request": req,
                        "error": ev.get("error"),
                        "cancelled": bool(ev.get("cancelled")),
                    },
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "tool.ready":
                cid = str(ev.get("call_id") or "")
                meta = self._call_meta.setdefault(cid, {"call_id": cid, "request": self._current_request})
                conc = str(ev.get("concurrency") or "exclusive")
                meta["concurrency"] = conc
                t_name = str(meta.get("name") or "")
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "tool_ready",
                    "data": {
                        "call_id": cid,
                        "request": meta.get("request") or self._current_request,
                        "turn": meta.get("request") or self._current_request,
                        "name": t_name,
                        "tool_name": t_name,
                        "arguments": meta.get("arguments", "{}"),
                        "args": meta.get("args", {}),
                        "concurrency": conc,
                        "is_macro": t_name.startswith("mc_skill_"),
                        "interrupt": ev.get("interrupt", "wait"),
                    },
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "tool.started":
                cid = str(ev.get("call_id") or "")
                fn = ev.get("function") if isinstance(ev.get("function"), dict) else {}
                meta = self._call_meta.setdefault(cid, {"call_id": cid, "request": self._current_request})
                if fn.get("name"):
                    meta["name"] = str(fn["name"])
                if fn.get("arguments") is not None:
                    meta["arguments"] = str(fn["arguments"])
                    meta["args"] = _try_json_dict(meta["arguments"]) or {}
                meta["started_elapsed"] = elapsed
                t_name = str(meta.get("name") or "")
                conc = str(meta.get("concurrency") or ("shared" if t_name in {
                    "mc_status", "mc_inventory", "mc_find_blocks", "mc_get_block",
                    "mc_get_entities", "mc_events", "mc_advancements", "mc_advancement_frontier", "mc_kb_lookup"
                } else "exclusive"))
                meta["concurrency"] = conc
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "tool_started",
                    "data": {
                        "call_id": cid,
                        "request": meta.get("request") or self._current_request,
                        "turn": meta.get("request") or self._current_request,
                        "name": t_name,
                        "tool_name": t_name,
                        "function": {"name": t_name, "arguments": meta.get("arguments", "{}")},
                        "arguments": meta.get("arguments", "{}"),
                        "args": meta.get("args", {}),
                        "concurrency": conc,
                        "is_macro": t_name.startswith("mc_skill_"),
                    },
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "tool.skipped":
                cid = str(ev.get("call_id") or "")
                meta = self._call_meta.setdefault(cid, {"call_id": cid, "request": self._current_request})
                meta["skipped"] = True
                t_name = str(meta.get("name") or "")
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "tool_skipped",
                    "data": {
                        "call_id": cid,
                        "request": meta.get("request") or self._current_request,
                        "turn": meta.get("request") or self._current_request,
                        "name": t_name,
                        "tool_name": t_name,
                        "arguments": meta.get("arguments", "{}"),
                        "args": meta.get("args", {}),
                        "concurrency": meta.get("concurrency", "exclusive"),
                        "is_macro": t_name.startswith("mc_skill_"),
                        "reason": str(ev.get("reason") or "previous_action_failed"),
                    },
                    "state_patch": {},
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "tool.completed":
                cid = str(ev.get("call_id") or "")
                meta = self._call_meta.get(cid) or {"call_id": cid, "request": self._current_request}
                raw_res = str(ev.get("result") or "")
                # Omit synthetic scheduler post-skip error completion so `tool_skipped` remains clean
                if meta.get("skipped") and raw_res.startswith("错误：本项工具尚未启动"):
                    continue
                frame = self._process_tool_completed(
                    seq=seq,
                    elapsed=elapsed,
                    ts_iso=ts_iso,
                    cid=cid,
                    meta=meta,
                    raw_res=raw_res,
                )
                meta["ok"] = frame["data"]["ok"]
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "guardian.incident":
                status = ev.get("status") or {}
                patch: dict[str, Any] = {}
                if status.get("pos"):
                    pos = self._record_pos(status["pos"], elapsed=elapsed, health=status.get("health"), food=status.get("food"))
                    if pos is not None:
                        patch["pos"] = pos
                for key in ("health", "food", "ready", "alive", "in_water", "blocking", "busy"):
                    if key in status:
                        self.status[key] = status[key]
                        patch[key] = status[key]
                patch["status"] = dict(self.status)
                marker = self._add_marker(elapsed=elapsed, kind="guardian", label="生存响应 / 重新规划")
                if marker:
                    patch["marker"] = marker
                frame = {"seq": seq, "elapsed": elapsed, "time": ts_iso, "kind": "guardian",
                         "data": {"reasons": ev.get("reasons"), "actions": ev.get("actions")}, "state_patch": patch}
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "mc.notable_events":
                patch: dict[str, Any] = {}
                markers_out: list[dict[str, Any]] = []
                call_meta = self._call_meta.get(str(ev.get("call_id") or "")) or {}
                game_events = self._apply_notable_events(
                    str(ev.get("text") or ""), elapsed=elapsed,
                    patch=patch, markers_out=markers_out,
                    preserve_inventory=call_meta.get("name") == "mc_inventory" and call_meta.get("ok") is True,
                )
                if markers_out:
                    patch["markers"] = markers_out
                    patch["marker"] = next(
                        (m for m in markers_out if m["kind"] == "death"), markers_out[0],
                    )
                frame = {
                    "seq": seq, "elapsed": elapsed, "time": ts_iso,
                    "kind": "game_events",
                    "data": {"call_id": ev.get("call_id"), "game_events": game_events},
                    "state_patch": patch,
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type in ("input.received", "input.applied"):
                text_in = str(ev.get("text") or "")
                snap = parse_initial_snapshot_text(text_in)
                patch: dict[str, Any] = {}
                if snap and self._initial_snapshot is None:
                    self._initial_snapshot = snap
                    patch = self._seed_initial_state(elapsed)
                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "input",
                    "data": {
                        "event_type": ev_type,
                        "id": ev.get("id") or ev.get("input_id"),
                        "text": text_in,
                        "priority": ev.get("priority"),
                        "boundary": ev.get("boundary"),
                        "task_version": ev.get("task_version"),
                    },
                    "state_patch": patch,
                }
                self.frames.append(frame)
                emitted.append(frame)

            elif ev_type == "answer":
                self.meta["answer"] = ev.get("content")

            elif ev_type == "run.finished":
                status_str = str(ev.get("status") or "finished")
                self.meta["status"] = status_str
                if ev.get("actor_requests") is not None:
                    self.meta["actor_requests"] = ev.get("actor_requests")
                if ev.get("error"):
                    self.meta["error"] = ev.get("error")

                patch_end: dict[str, Any] = {}
                if self._companion_summary:
                    if self._companion_summary.get("pos_after"):
                        pos_end = self._record_pos(
                            self._companion_summary["pos_after"],
                            elapsed=elapsed,
                            health=self._companion_summary.get("health_after"),
                            food=self._companion_summary.get("food_after"),
                        )
                        if pos_end is not None:
                            patch_end["pos"] = pos_end
                            if self._companion_summary.get("health_after") is not None:
                                patch_end["health"] = self.status.get("health")
                            if self._companion_summary.get("food_after") is not None:
                                patch_end["food"] = self.status.get("food")
                            patch_end["status"] = dict(self.status)

                    inferred_post = _infer_advancements_from_summary(
                        self._companion_summary,
                        include_newly_unlocked=True,
                        extra_completed_ids=self._known_completed_advs,
                    )
                    if inferred_post is not None:
                        self._known_completed_advs |= set(inferred_post["completed_ids"])
                        self._adv_seeded = True
                        if (
                            inferred_post["completed_count"] >= self.advancements.get("completed_count", 0)
                            or not self.advancements.get("completed_ids")
                        ):
                            self.advancements = inferred_post
                            patch_end["advancements"] = dict(inferred_post)

                    inv_after = self._companion_summary.get("inv_after")
                    if isinstance(inv_after, dict):
                        if len(inv_after) == 0:
                            self._clear_inventory_on_death(patch_end)
                        else:
                            synth_items = [
                                {"name": k, "count": int(v), "slot": idx}
                                for idx, (k, v) in enumerate(sorted(inv_after.items()))
                                if int(v or 0) > 0
                            ]
                            cur_held = self.status.get("held")
                            if isinstance(cur_held, dict) and cur_held.get("name") not in inv_after:
                                self.status["held"] = None
                                patch_end["held"] = None
                            cur_off = self.status.get("offhand")
                            if isinstance(cur_off, dict) and cur_off.get("name") not in inv_after:
                                self.status["offhand"] = None
                                patch_end["offhand"] = None
                            self.inventory = {
                                **self.inventory,
                                "items": synth_items,
                                "held": self.status.get("held"),
                                "offhand": self.status.get("offhand"),
                                "empty_slots": max(0, 36 - len(synth_items)),
                            }
                            patch_end["inventory"] = dict(self.inventory)
                            patch_end["status"] = dict(self.status)

                frame = {
                    "seq": seq,
                    "elapsed": elapsed,
                    "time": ts_iso,
                    "kind": "finished",
                    "data": {
                        "status": status_str,
                        "error": ev.get("error"),
                        "actor_requests": self.meta["actor_requests"],
                        "answer": self.meta.get("answer"),
                    },
                    "state_patch": patch_end,
                }
                self.frames.append(frame)
                emitted.append(frame)

        return emitted

    def _process_tool_completed(
        self,
        *,
        seq: int,
        elapsed: float,
        ts_iso: str,
        cid: str,
        meta: dict[str, Any],
        raw_res: str,
    ) -> dict[str, Any]:
        t_name = str(meta.get("name") or "")
        args = meta.get("args") if isinstance(meta.get("args"), dict) else {}
        conc = str(meta.get("concurrency") or "exclusive")
        started_el = meta.get("started_elapsed")
        duration_s = round(max(0.0, elapsed - float(started_el)), 4) if isinstance(started_el, (int, float)) else 0.0
        duration_ms = int(round(duration_s * 1000))

        json_part = raw_res
        notable_part = ""
        if "\n[期间新事件]" in raw_res:
            json_part, _, notable_part = raw_res.partition("\n[期间新事件]")
        elif cid in self._companion_notable_by_call_id:
            notable_part = self._companion_notable_by_call_id[cid]

        parsed = _try_json_dict(json_part.strip())
        ok = True
        if raw_res.startswith("错误："):
            ok = False
        elif isinstance(parsed, dict) and parsed.get("ok") is False:
            ok = False

        patch: dict[str, Any] = {}
        markers_out: list[dict[str, Any]] = []
        kb_hint: str | None = None
        err_code: str | None = None
        err_msg: str | None = None

        if isinstance(parsed, dict):
            res_inner = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed

            # Extract error details + kb_hint (either native or via JIT KB helper)
            err_obj = parsed.get("error")
            partial_err = parsed.get("partial_error")
            if isinstance(err_obj, dict):
                err_code = str(err_obj.get("code") or "") or None
                err_msg = str(err_obj.get("message") or "") or None
                if isinstance(err_obj.get("kb_hint"), str):
                    kb_hint = err_obj["kb_hint"]
                if err_obj.get("pos"):
                    pos = self._record_pos(err_obj["pos"], elapsed=elapsed, health=err_obj.get("health"))
                    if pos is not None:
                        patch["pos"] = pos
                if err_obj.get("health") is not None:
                    patch["health"] = self.status.get("health")
                if isinstance(err_obj.get("nearest_hostile"), dict):
                    self.status["nearest_hostile"] = err_obj["nearest_hostile"]
                    patch["nearest_hostile"] = err_obj["nearest_hostile"]
                if err_code in ("protected_region", "dig_unconfirmed"):
                    target_pos = [args["x"], args["y"], args["z"]] if all(k in args for k in ("x", "y", "z")) else err_obj.get("pos")
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="protected_region",
                        label=f"保护区禁挖 ({t_name})",
                        pos_raw=target_pos,
                    )
                    if m:
                        markers_out.append(m)
                if err_obj.get("cause") == "hurt" or (err_code == "interrupted" and "hurt" in (err_msg or "").lower()):
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="hurt",
                        label=f"受击打断 ({t_name})",
                        pos_raw=err_obj.get("pos"),
                    )
                    if m:
                        markers_out.append(m)
                elif err_obj.get("cause") == "death":
                    self._clear_inventory_on_death(patch)
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="death",
                        label=f"死亡中断 ({t_name})",
                        pos_raw=err_obj.get("pos"),
                    )
                    if m:
                        markers_out.append(m)
            elif isinstance(err_obj, str):
                err_code = err_obj
                err_msg = str(parsed.get("message") or "")

            if isinstance(partial_err, dict):
                if not err_code:
                    err_code = str(partial_err.get("code") or "") or None
                if not err_msg:
                    err_msg = str(partial_err.get("message") or "") or None
                if partial_err.get("code") == "interrupted" and "hurt" in str(partial_err.get("message") or "").lower():
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="hurt",
                        label=f"受击中断 ({t_name})",
                    )
                    if m:
                        markers_out.append(m)

            # Protected blacklist from macro skills (`mc_skill_mine_blocks` / `mc_skill_harvest_logs`)
            prot_cnt = int(parsed.get("protected_blacklisted") or 0)
            if prot_cnt > 0 and (not ok or err_code == "protected_region" or int(parsed.get("dug_count") or 0) == 0):
                if not err_code:
                    err_code = "protected_region"
                cand_types = args.get("block_types") if isinstance(args.get("block_types"), list) else []
                prot_pos = self.voxel_cache.find_nearest_by_types(
                    cand_types,
                    _norm_pos_float(self.status.get("pos")),
                )
                m = self._add_marker(
                    elapsed=elapsed,
                    kind="protected_region",
                    label=f"保护区拉黑 ({prot_cnt} 处方块)",
                    pos_raw=prot_pos,
                )
                if m:
                    markers_out.append(m)

            if not ok and not kb_hint and err_code:
                kb_hint = build_jit_error_hint(
                    t_name.removeprefix("mc_"),
                    args,
                    err_code,
                    err_msg or "",
                )

            # Tool-specific state extraction
            if t_name == "mc_status" and ok and isinstance(res_inner, dict):
                self._apply_status_dict(res_inner, elapsed=elapsed, patch=patch, markers_out=markers_out)

            elif t_name == "mc_inventory" and ok and isinstance(res_inner, dict):
                self.inventory = dict(res_inner)
                patch["inventory"] = dict(self.inventory)
                if "held" in res_inner:
                    self.status["held"] = res_inner["held"]
                    patch["held"] = res_inner["held"]
                if "offhand" in res_inner:
                    self.status["offhand"] = res_inner["offhand"]
                    patch["offhand"] = res_inner["offhand"]

            elif t_name in ("mc_advancement_frontier", "mc_advancements") and ok:
                src_adv = parsed if t_name == "mc_advancement_frontier" else res_inner
                merged_adv = merge_advancements_with_catalog(src_adv)
                new_ids = set(merged_adv["completed_ids"]) - self._known_completed_advs
                if self._adv_seeded and new_ids:
                    for item in merged_adv["completed"]:
                        if item["id"] in new_ids:
                            m = self._add_marker(
                                elapsed=elapsed,
                                kind="advancement",
                                label=f"🏆 {item.get('title_zh') or item['id']}",
                            )
                            if m:
                                markers_out.append(m)
                self._known_completed_advs |= set(merged_adv["completed_ids"])
                self._adv_seeded = True
                self.advancements = merged_adv
                patch["advancements"] = merged_adv

            elif t_name == "mc_find_blocks" and ok:
                blocks = _extract_blocks_from_find_result(res_inner)
                self._apply_blocks(blocks, elapsed=elapsed, patch=patch, markers_out=markers_out)

            elif t_name == "mc_get_block" and ok and isinstance(res_inner, dict):
                if res_inner.get("pos") and res_inner.get("name"):
                    self._apply_blocks(
                        [{"pos": res_inner["pos"], "name": res_inner["name"]}],
                        elapsed=elapsed,
                        patch=patch,
                        markers_out=markers_out,
                    )

            elif t_name == "mc_get_entities" and ok and isinstance(res_inner, dict):
                ents = res_inner.get("entities")
                if isinstance(ents, list):
                    patch["entities"] = ents

            elif t_name in ("mc_move_to", "mc_control", "mc_look") and ok and isinstance(res_inner, dict):
                pos = self._record_pos(
                    res_inner.get("pos"),
                    elapsed=elapsed,
                    yaw=res_inner.get("yaw"),
                )
                if pos is not None:
                    patch["pos"] = pos
                if "yaw" in res_inner and isinstance(res_inner["yaw"], (int, float)):
                    patch["yaw"] = self.status.get("yaw")

            elif t_name == "mc_dig" and ok:
                dig_pos = res_inner.get("pos") if isinstance(res_inner, dict) else None
                if not dig_pos and all(k in args for k in ("x", "y", "z")):
                    dig_pos = [args["x"], args["y"], args["z"]]
                if dig_pos:
                    rem = self.voxel_cache.remove_positions([dig_pos])
                    if rem:
                        patch.setdefault("blocks_removed", []).extend(rem)

            elif t_name == "mc_place" and ok:
                place_pos = res_inner.get("pos") if isinstance(res_inner, dict) else None
                if not place_pos and all(k in args for k in ("x", "y", "z")):
                    place_pos = [args["x"], args["y"], args["z"]]
                item_name = (res_inner.get("item") if isinstance(res_inner, dict) else None) or args.get("item")
                if place_pos and item_name:
                    self._apply_blocks(
                        [{"pos": place_pos, "name": item_name}],
                        elapsed=elapsed,
                        patch=patch,
                        markers_out=markers_out,
                    )

            elif t_name == "mc_equip" and ok and isinstance(res_inner, dict):
                if res_inner.get("held") is not None:
                    self.status["held"] = res_inner["held"]
                    patch["held"] = res_inner["held"]

            elif t_name.startswith("mc_skill_"):
                self._extract_macro_skill_patch(
                    t_name=t_name,
                    parsed=parsed,
                    elapsed=elapsed,
                    patch=patch,
                    markers_out=markers_out,
                )

        game_events = self._apply_notable_events(
            notable_part, elapsed=elapsed, patch=patch, markers_out=markers_out,
            preserve_inventory=t_name == "mc_inventory" and ok,
        )

        if markers_out:
            _prio = {"death": 0, "hurt": 1, "advancement": 2, "protected_region": 3, "station": 4}
            primary_m = min(markers_out, key=lambda m: _prio.get(str(m.get("kind")), 9))
            patch["marker"] = primary_m
            patch["markers"] = markers_out

        return {
            "seq": seq,
            "elapsed": elapsed,
            "time": ts_iso,
            "kind": "tool_completed",
            "data": {
                "call_id": cid,
                "request": meta.get("request") or self._current_request,
                "turn": meta.get("request") or self._current_request,
                "name": t_name,
                "tool_name": t_name,
                "arguments": meta.get("arguments", "{}"),
                "args": args,
                "concurrency": conc,
                "is_macro": t_name.startswith("mc_skill_"),
                "ok": ok,
                "duration_s": duration_s,
                "duration_ms": duration_ms,
                "result": json_part,
                "parsed_result": parsed,
                "kb_hint": kb_hint,
                "error_code": err_code,
                "error_message": err_msg,
                "game_events": game_events,
            },
            "state_patch": patch,
        }

    def _apply_notable_events(
        self,
        text: str,
        *,
        elapsed: float,
        patch: dict[str, Any],
        markers_out: list[dict[str, Any]],
        preserve_inventory: bool = False,
    ) -> list[dict[str, Any]]:
        game_events = parse_notable_events_text(text, bot_username=self.meta.get("bot"))
        for gev in game_events:
            gtype = gev.get("type")
            if gtype == "hurt":
                if isinstance(gev.get("health"), (int, float)):
                    self.status["health"] = gev["health"]
                    patch["health"] = gev["health"]
                m = self._add_marker(
                    elapsed=elapsed,
                    kind="hurt",
                    label=str(gev.get("label") or "受伤"),
                    extra={"source": gev.get("source")},
                )
                if m:
                    markers_out.append(m)
            elif gtype == "death":
                if not preserve_inventory:
                    self._clear_inventory_on_death(patch)
                m = self._add_marker(
                    elapsed=elapsed,
                    kind="death",
                    label=str(gev.get("label") or "死亡"),
                    pos_raw=gev.get("pos"),
                    extra={"dropped_items_at": gev.get("dropped_items_at")},
                )
                if m:
                    markers_out.append(m)
            elif gtype == "spawn" and gev.get("pos"):
                pos = self._record_pos(gev["pos"], elapsed=elapsed, health=20, food=20)
                if pos is not None:
                    patch["pos"] = pos
                    patch["health"] = self.status["health"]
                    patch["food"] = self.status["food"]
            elif gtype == "advancement":
                resolved_advs = _resolve_advancement_token(gev.get("advancement"))
                if resolved_advs and not resolved_advs.issubset(self._known_completed_advs):
                    self._known_completed_advs |= resolved_advs
                    self._adv_seeded = True
                    self.advancements = merge_advancements_with_catalog(
                        {"completed_ids": sorted(self._known_completed_advs)}
                    )
                    patch["advancements"] = dict(self.advancements)
                m = self._add_marker(
                    elapsed=elapsed,
                    kind="advancement",
                    label=f"🏆 {gev.get('advancement')}",
                )
                if m:
                    markers_out.append(m)

        if "status" in patch:
            patch["status"] = dict(self.status)
        return game_events

    def _extract_macro_skill_patch(
        self,
        *,
        t_name: str,
        parsed: dict[str, Any],
        elapsed: float,
        patch: dict[str, Any],
        markers_out: list[dict[str, Any]],
    ) -> None:
        if parsed.get("pos_before"):
            self._record_pos(parsed["pos_before"], elapsed=max(0.0, elapsed - 0.05))

        ctrl = parsed.get("control") if isinstance(parsed.get("control"), dict) else {}
        yaw_val = ctrl.get("yaw") if isinstance(ctrl.get("yaw"), (int, float)) else None

        if parsed.get("pos_after"):
            pos = self._record_pos(
                parsed["pos_after"],
                elapsed=elapsed,
                yaw=yaw_val,
                health=parsed.get("health"),
                food=parsed.get("food"),
            )
            if pos is not None:
                patch["pos"] = pos
                if yaw_val is not None:
                    patch["yaw"] = self.status.get("yaw")
        elif parsed.get("pos"):
            prev_hp = self.status.get("health")
            pos = self._record_pos(
                parsed["pos"],
                elapsed=elapsed,
                yaw=yaw_val,
                health=parsed.get("health"),
                food=parsed.get("food"),
            )
            if pos is not None:
                patch["pos"] = pos
            hp_now = parsed.get("health")
            if isinstance(hp_now, (int, float)):
                patch["health"] = round(float(hp_now), 2)
                if isinstance(prev_hp, (int, float)) and prev_hp - hp_now >= 1.5:
                    m = self._add_marker(
                        elapsed=elapsed,
                        kind="hurt",
                        label=f"环境/细雪扣血 HP→{round(float(hp_now), 1)}",
                        pos_raw=pos,
                    )
                    if m:
                        markers_out.append(m)
            if isinstance(parsed.get("food"), (int, float)):
                patch["food"] = parsed["food"]
            if "in_water" in parsed:
                self.status["in_water"] = bool(parsed["in_water"])
                patch["in_water"] = self.status["in_water"]

        if isinstance(parsed.get("dug"), list) and parsed["dug"]:
            rem = self.voxel_cache.remove_positions(parsed["dug"])
            if rem:
                patch.setdefault("blocks_removed", []).extend(rem)

        if isinstance(parsed.get("ores_nearby"), list) and parsed["ores_nearby"]:
            self._apply_blocks(parsed["ores_nearby"], elapsed=elapsed, patch=patch, markers_out=markers_out)

        if parsed.get("roof_placed") and parsed.get("roof_pos") and parsed.get("roof_item"):
            self._apply_blocks(
                [{"pos": parsed["roof_pos"], "name": parsed["roof_item"]}],
                elapsed=elapsed,
                patch=patch,
                markers_out=markers_out,
            )
            m = self._add_marker(
                elapsed=elapsed,
                kind="station",
                label="安全掩体 (Bunker)",
                pos_raw=parsed.get("origin") or parsed.get("pos_after"),
            )
            if m:
                markers_out.append(m)

        for station_field, block_name in (
            ("table_pos", "crafting_table"),
            ("carved_pocket", "crafting_table"),
            ("furnace_pos", "furnace"),
            ("bed_pos", "white_bed"),
        ):
            spos = parsed.get(station_field)
            if spos:
                self._apply_blocks(
                    [{"pos": spos, "name": block_name}],
                    elapsed=elapsed,
                    patch=patch,
                    markers_out=markers_out,
                )

        used_tool = parsed.get("pickaxe_used") or parsed.get("axe_used") or parsed.get("weapon_used")
        if isinstance(used_tool, str) and used_tool and used_tool != "hand":
            self.status["held"] = {"name": used_tool, "count": 1}
            patch["held"] = self.status["held"]

        if t_name == "mc_skill_craft_gear" and parsed.get("ok"):
            crafted = parsed.get("crafted")
            eq_slot = parsed.get("equipped_slot")
            if crafted and eq_slot == "hand":
                self.status["held"] = {"name": crafted, "count": 1}
                patch["held"] = self.status["held"]
            elif crafted and eq_slot == "off-hand":
                self.status["offhand"] = {"name": crafted, "count": 1}
                patch["offhand"] = self.status["offhand"]
            inv_counts = parsed.get("inventory")
            if isinstance(inv_counts, dict) and "items" not in inv_counts:
                synth_items = [
                    {"name": k, "count": int(v), "slot": idx}
                    for idx, (k, v) in enumerate(sorted(inv_counts.items()))
                    if int(v or 0) > 0
                ]
                self.inventory = {
                    **self.inventory,
                    "items": synth_items,
                    "held": self.status.get("held"),
                    "offhand": self.status.get("offhand"),
                }
                patch["inventory"] = dict(self.inventory)

        # New collectors supply a fresh bot position. Legacy moved_to entries
        # used the target item's position and must not override that snapshot.
        if t_name == "mc_skill_collect_drops" and not parsed.get("pos_after") and isinstance(parsed.get("moved_to"), list) and parsed["moved_to"]:
            last_pick = parsed["moved_to"][-1]
            if isinstance(last_pick, dict) and last_pick.get("pos"):
                pos = self._record_pos(last_pick["pos"], elapsed=elapsed)
                if pos is not None:
                    patch["pos"] = pos

    def to_bundle(self) -> dict[str, Any]:
        return {
            "id": self.run_id,
            "meta": dict(self.meta),
            "frames": list(self.frames),
            "trajectory": list(self.trajectory),
            "markers": list(self.markers),
            "blocks": self.voxel_cache.to_list(),
            "final_state": {
                "status": dict(self.status),
                "inventory": dict(self.inventory),
                "advancements": dict(self.advancements),
            },
        }


def resolve_safe_run_path(runs_dir: Path, rel_id: str) -> Path:
    """Validate `rel_id` is a safe relative `.events.jsonl` path strictly inside `runs_dir`."""
    if not rel_id or not isinstance(rel_id, str):
        raise ValueError("Missing run id")
    cleaned = rel_id.strip()
    if (
        not cleaned
        or "\x00" in cleaned
        or "\\" in cleaned
        or cleaned.startswith("/")
        or not cleaned.endswith(".events.jsonl")
    ):
        raise ValueError(f"Invalid run id: {rel_id!r}")

    rel_path = Path(cleaned)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ValueError(f"Path traversal rejected: {rel_id!r}")

    base = runs_dir.resolve()
    candidate = (base / rel_path).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"Path escapes runs directory: {rel_id!r}")
    return candidate


def _load_companion_files(event_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    stem_str = event_path.name[: -len(".events.jsonl")]
    parent = event_path.parent
    report_obj: dict[str, Any] | None = None
    summary_obj: dict[str, Any] | None = None

    for suffix in (".report.json", ".json"):
        rp = parent / f"{stem_str}{suffix}"
        if rp.is_file():
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    report_obj = data
                    break
            except (OSError, ValueError):
                pass

    sp = parent / f"{stem_str}.summary.json"
    if sp.is_file():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                summary_obj = data
        except (OSError, ValueError):
            pass

    return report_obj, summary_obj


def parse_replay_bundle(runs_dir: Path, rel_id: str) -> dict[str, Any]:
    """Parse a `.events.jsonl` run log into a normalized replay bundle."""
    path = resolve_safe_run_path(runs_dir, rel_id)
    if not path.is_file():
        raise FileNotFoundError(rel_id)

    norm_rel_id = path.relative_to(runs_dir.resolve()).as_posix()
    report_obj, summary_obj = _load_companion_files(path)

    raw_events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except ValueError:
            continue
        if isinstance(ev, dict):
            raw_events.append(ev)

    inc = ReplayIncrementer(
        norm_rel_id,
        coalesce_thought_window_s=0.6,
        companion_report=report_obj,
        companion_summary=summary_obj,
    )
    inc.feed_events(raw_events)
    return inc.to_bundle()


def _quick_head_tail_summary(event_path: Path) -> dict[str, Any] | None:
    """Fast head+tail peek of `.events.jsonl` when no `.summary.json` exists."""
    try:
        size = event_path.stat().st_size
        if size == 0:
            return None
        with event_path.open("rb") as fh:
            head_bytes = fh.read(min(8192, size))
            tail_bytes = b""
            if size > 8192:
                fh.seek(max(0, size - 8192), os.SEEK_SET)
                tail_bytes = fh.read(8192)
    except OSError:
        return None

    first_ev: dict[str, Any] | None = None
    for line in head_bytes.decode("utf-8", errors="ignore").splitlines():
        obj = _try_json_dict(line.strip())
        if obj:
            first_ev = obj
            break

    last_ev: dict[str, Any] | None = None
    finished_ev: dict[str, Any] | None = None
    tail_src = tail_bytes if tail_bytes else head_bytes
    for line in reversed(tail_src.decode("utf-8", errors="ignore").splitlines()):
        obj = _try_json_dict(line.strip())
        if not obj:
            continue
        if last_ev is None:
            last_ev = obj
        if obj.get("type") == "run.finished":
            finished_ev = obj
            break

    if not first_ev and not last_ev:
        return None

    elapsed_s = float((finished_ev or last_ev or {}).get("elapsed") or 0.0)
    return {
        "stage": event_path.name[: -len(".events.jsonl")],
        "elapsed_s": round(elapsed_s, 2),
        "status": (finished_ev or {}).get("status") or "running",
        "bot": (first_ev or {}).get("bot"),
        "provider_model": (first_ev or {}).get("model"),
        "max_calls": (first_ev or {}).get("max_calls"),
        "actor_requests": (finished_ev or {}).get("actor_requests"),
    }


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    keep_keys = (
        "stage",
        "elapsed_s",
        "provider_model",
        "bot",
        "status",
        "error",
        "actor_requests",
        "max_calls",
        "pos_before",
        "pos_after",
        "health_after",
        "food_after",
        "completed_count",
        "newly_unlocked",
        "tool_error_count",
    )
    return {k: summary[k] for k in keep_keys if k in summary}


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """List all `.events.jsonl` logs under `runs_dir`, sorted newest first."""
    if not runs_dir.is_dir():
        return []
    base = runs_dir.resolve()
    rows: list[dict[str, Any]] = []
    for p in base.rglob("*.events.jsonl"):
        if p.is_symlink():
            continue
        try:
            resolved = p.resolve()
            if not resolved.is_relative_to(base) or not resolved.is_file():
                continue
            st = resolved.stat()
        except OSError:
            continue

        rel_id = resolved.relative_to(base).as_posix()
        _, summary_raw = _load_companion_files(resolved)
        if summary_raw is not None:
            summary = _compact_summary(summary_raw)
        else:
            summary = _quick_head_tail_summary(resolved)

        stem = resolved.name[: -len(".events.jsonl")]
        stage = (summary or {}).get("stage") or stem
        parent_rel = resolved.parent.relative_to(base).as_posix()
        label = f"{parent_rel} / {stage}" if parent_rel != "." else str(stage)
        rows.append(
            {
                "id": rel_id,
                "label": label,
                "mtime": round(st.st_mtime, 3),
                "size": int(st.st_size),
                "summary": summary,
            }
        )

    rows.sort(key=lambda r: (r["mtime"], r["id"]), reverse=True)
    return rows


class LiveStateStore:
    """In-memory live world & telemetry state merged from non-locking MC polls and active harness logs."""

    def __init__(self, *, max_trajectory: int = 2000) -> None:
        self.max_trajectory = max_trajectory
        self.online: bool = False
        self.ready: bool = False
        self.has_cached_token: bool = False
        self.bot_username: str | None = None
        self.auth_note: str | None = None

        self.status: dict[str, Any] = {}
        self.inventory: dict[str, Any] = {}
        self.advancements: dict[str, Any] = merge_advancements_with_catalog(None)
        self.entities: list[dict[str, Any]] = []
        self.voxel_cache = VoxelCache(max_blocks=2500)
        self.trajectory: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self._seen_markers: set[tuple[Any, ...]] = set()
        self._started_mono = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._started_mono, 3)

    def apply_polled_status(self, st: dict[str, Any]) -> dict[str, Any] | None:
        self.online = True
        self.ready = bool(st.get("ready"))
        if isinstance(st.get("username"), str) and st["username"]:
            self.bot_username = st["username"]
        self.status = dict(st)

        pos = _norm_pos_float(st.get("pos"))
        if pos is None:
            return None
        self.status["pos"] = pos
        point = {
            "elapsed": self.elapsed(),
            "pos": pos,
            "yaw": round(float(st.get("yaw") or 0.0), 4),
            "dimension": str(st.get("dimension") or "overworld"),
            "health": round(float(st.get("health") if st.get("health") is not None else 20), 2),
            "food": round(float(st.get("food") if st.get("food") is not None else 20), 1),
        }
        if not self.trajectory:
            self.trajectory.append(point)
            return point
        prev = self.trajectory[-1]
        if (
            _dist3(prev["pos"], pos) >= 0.08
            or prev.get("health") != point["health"]
            or prev.get("dimension") != point["dimension"]
        ):
            self.trajectory.append(point)
            if len(self.trajectory) > self.max_trajectory:
                self.trajectory = self.trajectory[-self.max_trajectory :]
            return point
        return point

    def apply_mc_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert newly polled `/events` (post-cursor-priming) into live tactical markers."""
        new_markers: list[dict[str, Any]] = []
        cur_pos = _norm_pos_float(self.status.get("pos"))
        cur_dim = str(self.status.get("dimension") or "overworld")
        now_el = self.elapsed()

        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type") or "").lower()
            if etype == "hurt":
                src = ev.get("source") or (ev.get("likely_attacker") or {}).get("name") or "unknown"
                hp = ev.get("health")
                m = self._add_marker(
                    elapsed=now_el,
                    kind="hurt",
                    label=f"受伤 ({src}) HP→{hp}",
                    pos=cur_pos,
                    dimension=cur_dim,
                    dedup_key=("mc_ev", ev.get("seq")),
                )
                if m:
                    new_markers.append(m)
            elif etype == "death":
                self.inventory = _empty_inventory_state()
                self.status["held"] = None
                self.status["offhand"] = None
                self.status["armor_points"] = 0
                dpos = _norm_pos_float(ev.get("dropped_items_at") or ev.get("pos")) or cur_pos
                m = self._add_marker(
                    elapsed=now_el,
                    kind="death",
                    label="Bot 死亡 (掉落物点)",
                    pos=dpos,
                    dimension=cur_dim,
                    extra={"dropped_items_at": dpos},
                    dedup_key=("mc_ev", ev.get("seq")),
                )
                if m:
                    new_markers.append(m)
            elif etype in ("advancement", "advancements"):
                title = ev.get("title") or ev.get("id") or ev.get("advancement") or "新进度"
                m = self._add_marker(
                    elapsed=now_el,
                    kind="advancement",
                    label=f"🏆 {title}",
                    pos=cur_pos,
                    dimension=cur_dim,
                    dedup_key=("mc_ev", ev.get("seq")),
                )
                if m:
                    new_markers.append(m)
        return new_markers

    def absorb_harness_patch(
        self,
        patch: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[list[int]]]:
        """Passively harvest blocks/markers/fallback state from active `.events.jsonl` frames."""
        if not patch:
            return [], []

        if not self.online:
            if isinstance(patch.get("status"), dict):
                self.status.update(patch["status"])
            else:
                for k in ("pos", "yaw", "dimension", "health", "food", "held", "offhand", "in_water"):
                    if k in patch:
                        self.status[k] = patch[k]
            if isinstance(patch.get("inventory"), dict):
                self.inventory = dict(patch["inventory"])
            if isinstance(patch.get("advancements"), dict):
                self.advancements = dict(patch["advancements"])
            if isinstance(patch.get("entities"), list):
                self.entities = list(patch["entities"])
            if patch.get("pos"):
                pos = _norm_pos_float(patch["pos"])
                if pos and (not self.trajectory or _dist3(self.trajectory[-1]["pos"], pos) >= 0.08):
                    self.trajectory.append(
                        {
                            "elapsed": self.elapsed(),
                            "pos": pos,
                            "yaw": float(self.status.get("yaw") or 0.0),
                            "dimension": str(self.status.get("dimension") or "overworld"),
                            "health": float(self.status.get("health") if self.status.get("health") is not None else 20),
                            "food": float(self.status.get("food") if self.status.get("food") is not None else 20),
                        }
                    )

        added_blocks: list[dict[str, Any]] = []
        removed_pos: list[list[int]] = []
        anchor = _norm_pos_float(self.status.get("pos") or patch.get("pos"))
        if isinstance(patch.get("blocks_added"), list) and patch["blocks_added"]:
            added, rem = self.voxel_cache.put_blocks(patch["blocks_added"], anchor_pos=anchor)
            added_blocks.extend(added)
            removed_pos.extend(rem)
        if isinstance(patch.get("blocks_removed"), list) and patch["blocks_removed"]:
            rem2 = self.voxel_cache.remove_positions(patch["blocks_removed"])
            removed_pos.extend(rem2)

        for m in patch.get("markers") or ([] if not patch.get("marker") else [patch["marker"]]):
            if isinstance(m, dict) and m.get("pos"):
                self._add_marker(
                    elapsed=float(m.get("elapsed") or self.elapsed()),
                    kind=str(m.get("kind") or "station"),
                    label=str(m.get("label") or ""),
                    pos=_norm_pos_float(m.get("pos")),
                    dimension=str(m.get("dimension") or "overworld"),
                    extra={k: v for k, v in m.items() if k not in ("elapsed", "kind", "label", "pos", "dimension")},
                    dedup_key=(m.get("kind"), _norm_pos_int(m.get("pos")), m.get("label")),
                )

        return added_blocks, removed_pos

    def seed_from_bundle_if_empty(self, bundle: dict[str, Any] | None) -> None:
        """Seed initial live viewport with the latest run's world state when Bot is not yet polled."""
        if not bundle:
            return
        final = bundle.get("final_state") or {}
        if not self.online:
            if not self.status and isinstance(final.get("status"), dict):
                self.status = dict(final["status"])
            if not self.inventory and isinstance(final.get("inventory"), dict):
                self.inventory = dict(final["inventory"])
            if isinstance(final.get("advancements"), dict) and final["advancements"].get("completed_count", 0) > 0:
                self.advancements = dict(final["advancements"])
            if not self.trajectory and isinstance(bundle.get("trajectory"), list):
                self.trajectory = list(bundle["trajectory"])[-self.max_trajectory :]
        if len(self.voxel_cache) == 0 and isinstance(bundle.get("blocks"), list):
            self.voxel_cache.put_blocks(bundle["blocks"], anchor_pos=_norm_pos_float(self.status.get("pos")))
        for m in bundle.get("markers") or []:
            if isinstance(m, dict) and m.get("pos"):
                self._add_marker(
                    elapsed=float(m.get("elapsed") or 0.0),
                    kind=str(m.get("kind") or "station"),
                    label=str(m.get("label") or ""),
                    pos=_norm_pos_float(m.get("pos")),
                    dimension=str(m.get("dimension") or "overworld"),
                    extra={k: v for k, v in m.items() if k not in ("elapsed", "kind", "label", "pos", "dimension")},
                    dedup_key=(m.get("kind"), _norm_pos_int(m.get("pos")), m.get("label")),
                )

    def _add_marker(
        self,
        *,
        elapsed: float,
        kind: str,
        label: str,
        pos: list[float] | None,
        dimension: str,
        extra: dict[str, Any] | None = None,
        dedup_key: tuple[Any, ...] | None = None,
    ) -> dict[str, Any] | None:
        if pos is None:
            return None
        key = dedup_key or (kind, dimension, _norm_pos_int(pos), label)
        if key in self._seen_markers:
            return None
        self._seen_markers.add(key)
        m: dict[str, Any] = {
            "elapsed": round(elapsed, 3),
            "kind": kind,
            "label": label,
            "pos": pos,
            "dimension": dimension,
        }
        if extra:
            m.update(extra)
        self.markers.append(m)
        if len(self.markers) > 300:
            self.markers = self.markers[-300:]
        return m

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "status": dict(self.status),
            "inventory": dict(self.inventory),
            "advancements": dict(self.advancements),
            "entities": list(self.entities),
            "blocks": self.voxel_cache.to_list(),
            "trajectory": list(self.trajectory),
            "markers": list(self.markers),
        }
