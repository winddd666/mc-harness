"""1.21.11 advancement catalog loader and unlocked-frontier planner."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from ..bot import BotSession
from ..kb import ADVANCEMENT_PLAYBOOKS

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "advancements_1_21_11.json"

_CATEGORY_ORDER = {
    "story": 0,
    "husbandry": 1,
    "adventure": 2,
    "nether": 3,
    "end": 4,
}
_FRAME_ORDER = {
    "task": 0,
    "goal": 1,
    "challenge": 2,
}


def _norm_id(adv_id: str | None) -> str:
    if not adv_id:
        return ""
    s = str(adv_id).strip()
    if ":" not in s:
        s = f"minecraft:{s}"
    return s


@functools.lru_cache(maxsize=1)
def load_advancements_catalog() -> dict[str, Any]:
    """Load the bundled 125-item Minecraft 1.21.11 non-recipe advancement tree."""
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


_DIFFICULTY_ORDER = {
    "easy": 0,
    "medium": 1,
    "hard": 2,
}


def compute_advancement_frontier(bot: BotSession, *, limit: int = 15) -> dict[str, Any]:
    """Cross-reference live `/advancements` status with the 125-item tree to list immediately unlockable targets."""
    catalog = load_advancements_catalog()
    items: list[dict[str, Any]] = catalog.get("advancements") or []

    live = bot.advancements(include_recipes=False)
    live_rows = live.get("advancements") or []

    completed_ids: set[str] = set()
    live_by_id: dict[str, dict[str, Any]] = {}
    for row in live_rows:
        if not isinstance(row, dict):
            continue
        nid = _norm_id(row.get("id"))
        if not nid:
            continue
        live_by_id[nid] = row
        if row.get("status") == "completed":
            completed_ids.add(nid)

    frontier: list[dict[str, Any]] = []
    for entry in items:
        eid = _norm_id(entry.get("id"))
        if eid in completed_ids:
            continue
        parent = _norm_id(entry.get("parent")) if entry.get("parent") else ""
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

    return {
        "total_catalog": len(items),
        "completed_count": len(completed_ids),
        "completed_ids": sorted(completed_ids),
        "frontier_count": len(frontier),
        "frontier": frontier[: max(1, limit)],
    }
