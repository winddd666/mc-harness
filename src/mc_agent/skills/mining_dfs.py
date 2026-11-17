"""Path-compressed MiningDFS state machine for reliable underground exploration and mining."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import dataclasses
import json
import math
import os
import pathlib
import re
import time
from typing import Any

from ..errors import ApiError
from .library import (
    BLOCK_MIN_PICKAXE_TIER,
    EXPECTED_BLOCK_DROPS,
    PICKAXE_TIERS,
    SHAFT_SEAL_BLOCKS,
    _best_pickaxe_in_inv,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _inventory_snapshot_and_counts,
    _positive_inv_delta,
)

DIRECTIONS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}

OPPOSITE_DIR: dict[str, str] = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
}

LEFT_RIGHT_DIRS: dict[str, tuple[str, str]] = {
    "north": ("west", "east"),
    "south": ("east", "west"),
    "east": ("north", "south"),
    "west": ("south", "north"),
}

AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})
FLUID_BLOCKS = frozenset({"water", "lava", "flowing_water", "flowing_lava"})
LAVA_BLOCKS = frozenset({"lava", "flowing_lava"})
FALLING_BLOCKS = frozenset({"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"})
PASSABLE_BLOCKS = AIR_BLOCKS | frozenset(
    {
        "short_grass",
        "tall_grass",
        "fern",
        "large_fern",
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "red_tulip",
        "orange_tulip",
        "white_tulip",
        "pink_tulip",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "pink_petals",
        "glow_lichen",
        "sculk_vein",
        "cave_vines",
        "cave_vines_plant",
        "hanging_roots",
        "leaf_litter",
        "wildflowers",
        "bush",
        "dead_bush",
        "torch",
        "wall_torch",
        "snow",
    }
)
FLIMSY_FLOOR_BLOCKS = frozenset(
    {
        "vine",
        "lily_pad",
        "scaffolding",
        "azalea",
        "flowering_azalea",
        "Moss_carpet".lower(),
        "moss_carpet",
        "pale_moss_carpet",
    }
)
FLIMSY_FLOOR_SUFFIXES = ("_leaves", "_sapling", "_carpet", "_banner")
HAZARD_FLOOR_BLOCKS = frozenset(
    {"magma_block", "cactus", "campfire", "soul_campfire", "sweet_berry_bush", "powder_snow"}
)
ROCK_BLOCKS = frozenset(
    {
        "stone",
        "deepslate",
        "cobblestone",
        "cobbled_deepslate",
        "granite",
        "diorite",
        "andesite",
        "tuff",
        "calcite",
        "dirt",
        "coarse_dirt",
        "rooted_dirt",
        "grass_block",
        "sandstone",
        "red_sandstone",
    }
)
SURFACE_GROUND_BLOCKS: tuple[str, ...] = (
    "grass_block",
    "dirt",
    "coarse_dirt",
    "podzol",
    "rooted_dirt",
    "mud",
    "moss_block",
    "mycelium",
    "snow_block",
    "stone",
    "cobblestone",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "deepslate",
    "cobbled_deepslate",
    "sand",
    "red_sand",
    "sandstone",
    "red_sandstone",
    "gravel",
    "terracotta",
    "clay",
    "packed_ice",
)
TRACKED_ORES: tuple[str, ...] = (
    "diamond_ore",
    "deepslate_diamond_ore",
    "iron_ore",
    "deepslate_iron_ore",
    "gold_ore",
    "deepslate_gold_ore",
    "redstone_ore",
    "deepslate_redstone_ore",
    "lapis_ore",
    "deepslate_lapis_ore",
    "emerald_ore",
    "deepslate_emerald_ore",
    "coal_ore",
    "deepslate_coal_ore",
    "copper_ore",
    "deepslate_copper_ore",
)

ORE_DFS_PROFILES: dict[str, dict[str, Any]] = {
    "iron": {
        "target_blocks": ("iron_ore", "deepslate_iron_ore"),
        "expected_items": ("raw_iron", "iron_ore", "deepslate_iron_ore"),
        "default_target_y": 16,
        "min_tier": 2,
    },
    "coal": {
        "target_blocks": ("coal_ore", "deepslate_coal_ore"),
        "expected_items": ("coal", "coal_ore", "deepslate_coal_ore"),
        "default_target_y": 44,
        "min_tier": 1,
    },
    "copper": {
        "target_blocks": ("copper_ore", "deepslate_copper_ore"),
        "expected_items": ("raw_copper", "copper_ore", "deepslate_copper_ore"),
        "default_target_y": 44,
        "min_tier": 2,
    },
    "lapis": {
        "target_blocks": ("lapis_ore", "deepslate_lapis_ore"),
        "expected_items": ("lapis_lazuli", "lapis_ore", "deepslate_lapis_ore"),
        "default_target_y": 0,
        "min_tier": 2,
    },
    "gold": {
        "target_blocks": ("gold_ore", "deepslate_gold_ore"),
        "expected_items": ("raw_gold", "gold_ore", "deepslate_gold_ore"),
        "default_target_y": -16,
        "min_tier": 3,
    },
    "redstone": {
        "target_blocks": ("redstone_ore", "deepslate_redstone_ore"),
        "expected_items": ("redstone", "redstone_ore", "deepslate_redstone_ore"),
        "default_target_y": -54,
        "min_tier": 3,
    },
    "diamond": {
        "target_blocks": ("diamond_ore", "deepslate_diamond_ore"),
        "expected_items": ("diamond", "diamond_ore", "deepslate_diamond_ore"),
        "default_target_y": -54,
        "min_tier": 3,
    },
    "emerald": {
        "target_blocks": ("emerald_ore", "deepslate_emerald_ore"),
        "expected_items": ("emerald", "emerald_ore", "deepslate_emerald_ore"),
        "default_target_y": 16,
        "min_tier": 3,
    },
}

DEFAULT_MAX_DURABILITY: dict[str, int] = {
    "wooden_pickaxe": 59,
    "golden_pickaxe": 32,
    "stone_pickaxe": 131,
    "iron_pickaxe": 250,
    "diamond_pickaxe": 1561,
    "netherite_pickaxe": 2031,
}

RADAR_SCAN_RADIUS = 64
VEIN_HOOK_RADIUS = 12
EXHAUSTED_COLLAR_MIN_SEP = 14.0


@dataclasses.dataclass
class BranchSpec:
    kind: str  # "stair_down" | "spine_forward" | "branch_leaf" | "offset_retry"
    direction: str
    steps: int
    status: str = "pending"  # "pending" | "active" | "completed" | "pruned"
    prune_reason: str | None = None
    completed_waypoints: list[tuple[int, int, int]] = dataclasses.field(default_factory=list)
    offset_tried: bool = False
    sidestep_dir: str | None = None
    resume_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "steps": self.steps,
            "status": self.status,
            "prune_reason": self.prune_reason,
            "completed_waypoints": [list(p) for p in self.completed_waypoints],
            "offset_tried": self.offset_tried,
            "sidestep_dir": self.sidestep_dir,
            "resume_kind": self.resume_kind,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BranchSpec:
        return cls(
            kind=str(raw["kind"]),
            direction=str(raw["direction"]),
            steps=int(raw["steps"]),
            status=str(raw.get("status", "pending")),
            prune_reason=raw.get("prune_reason"),
            completed_waypoints=[(int(p[0]), int(p[1]), int(p[2])) for p in (raw.get("completed_waypoints") or [])],
            offset_tried=bool(raw.get("offset_tried", False)),
            sidestep_dir=raw.get("sidestep_dir"),
            resume_kind=raw.get("resume_kind"),
        )


@dataclasses.dataclass
class ShaftEdge:
    edge_kind: str  # "stair_down" | "spine_forward" | "branch_leaf" | "offset_sidestep"
    direction: str
    waypoints: list[tuple[int, int, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_kind": self.edge_kind,
            "direction": self.direction,
            "waypoints": [list(p) for p in self.waypoints],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShaftEdge:
        return cls(
            edge_kind=str(raw["edge_kind"]),
            direction=str(raw["direction"]),
            waypoints=[(int(p[0]), int(p[1]), int(p[2])) for p in (raw.get("waypoints") or [])],
        )


@dataclasses.dataclass
class ShaftNode:
    node_id: int
    kind: str  # "collar" | "switchback" | "spine"
    anchor: tuple[int, int, int]
    parent_id: int | None
    edge_from_parent: ShaftEdge | None
    branches: list[BranchSpec]
    torch_placed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "anchor": list(self.anchor),
            "parent_id": self.parent_id,
            "edge_from_parent": self.edge_from_parent.to_dict() if self.edge_from_parent else None,
            "branches": [b.to_dict() for b in self.branches],
            "torch_placed": self.torch_placed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShaftNode:
        anc = raw["anchor"]
        efp = raw.get("edge_from_parent")
        return cls(
            node_id=int(raw["node_id"]),
            kind=str(raw["kind"]),
            anchor=(int(anc[0]), int(anc[1]), int(anc[2])),
            parent_id=int(raw["parent_id"]) if raw.get("parent_id") is not None else None,
            edge_from_parent=ShaftEdge.from_dict(efp) if isinstance(efp, dict) else None,
            branches=[BranchSpec.from_dict(b) for b in (raw.get("branches") or [])],
            torch_placed=bool(raw.get("torch_placed", False)),
        )


@dataclasses.dataclass
class DeferredOreRecord:
    pos: tuple[int, int, int]
    block_name: str
    required_tier: int
    nearest_node_id: int


class WorldLedger:
    """Spatial memory of excavated corridors, safe shafts, discovered ores, and utility stations."""

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        self.path: pathlib.Path | None = pathlib.Path(path) if path is not None else None
        self.excavated_positions: set[tuple[int, int, int]] = set()
        self.known_corridor_positions: set[tuple[int, int, int]] = set()
        self.exhausted_collars: list[tuple[int, int, int]] = []
        self.discovered_ores: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.utility_blocks: dict[str, list[tuple[int, int, int]]] = {
            "crafting_table": [],
            "furnace": [],
        }
        self.saved_shafts: list[dict[str, Any]] = []
        if self.path is not None:
            self.load()

    def record_excavated(self, pt: tuple[int, int, int]) -> None:
        self.excavated_positions.add((int(pt[0]), int(pt[1]), int(pt[2])))
        self.discovered_ores.pop((int(pt[0]), int(pt[1]), int(pt[2])), None)

    def record_corridor(self, pt: tuple[int, int, int]) -> None:
        bx, by, bz = int(pt[0]), int(pt[1]), int(pt[2])
        self.known_corridor_positions.add((bx, by, bz))
        self.known_corridor_positions.add((bx, by + 1, bz))

    def record_exhausted_collar(self, anchor: tuple[int, int, int]) -> None:
        pt = (int(anchor[0]), int(anchor[1]), int(anchor[2]))
        if pt not in self.exhausted_collars:
            self.exhausted_collars.append(pt)
        self.saved_shafts = [
            s for s in self.saved_shafts if tuple(s.get("collar_anchor") or ()) != pt
        ]

    def record_ore(self, pt: tuple[int, int, int], block_name: str, required_tier: int) -> None:
        pos = (int(pt[0]), int(pt[1]), int(pt[2]))
        if pos in self.excavated_positions:
            return
        self.discovered_ores[pos] = {
            "pos": list(pos),
            "block": str(block_name),
            "required_tier": int(required_tier),
        }

    def remove_ore(self, pt: tuple[int, int, int]) -> None:
        self.discovered_ores.pop((int(pt[0]), int(pt[1]), int(pt[2])), None)

    def record_utility(self, block_name: str, pos: tuple[int, int, int] | list[int]) -> None:
        key = str(block_name).removeprefix("minecraft:")
        if len(pos) < 3:
            return
        pt = (int(pos[0]), int(pos[1]), int(pos[2]))
        bucket = self.utility_blocks.setdefault(key, [])
        if pt not in bucket:
            bucket.append(pt)
            if len(bucket) > 24:
                del bucket[:-24]

    def nearest_utility(
        self,
        block_name: str,
        origin: tuple[int, int, int],
        *,
        max_dist: float = 48.0,
    ) -> tuple[int, int, int] | None:
        key = str(block_name).removeprefix("minecraft:")
        cands = self.utility_blocks.get(key) or []
        best: tuple[int, int, int] | None = None
        best_d = max_dist
        for pt in cands:
            d = math.dist(origin, pt)
            if d <= best_d:
                best_d = d
                best = pt
        return best

    def upsert_shaft(self, shaft_data: dict[str, Any]) -> None:
        anc = tuple(int(v) for v in shaft_data["collar_anchor"])
        if anc in self.exhausted_collars:
            return
        filtered = [
            s for s in self.saved_shafts if tuple(s.get("collar_anchor") or ()) != anc
        ]
        entry = dict(shaft_data)
        entry["collar_anchor"] = list(anc)
        entry["from_disk"] = False
        filtered.append(entry)
        self.saved_shafts = filtered[-8:]

    def load(self) -> bool:
        if self.path is None or not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        for p in raw.get("excavated_positions") or []:
            if isinstance(p, list) and len(p) >= 3:
                self.excavated_positions.add((int(p[0]), int(p[1]), int(p[2])))
        for p in raw.get("known_corridor_positions") or []:
            if isinstance(p, list) and len(p) >= 3:
                self.known_corridor_positions.add((int(p[0]), int(p[1]), int(p[2])))
        for p in raw.get("exhausted_collars") or []:
            if isinstance(p, list) and len(p) >= 3:
                pt = (int(p[0]), int(p[1]), int(p[2]))
                if pt not in self.exhausted_collars:
                    self.exhausted_collars.append(pt)
        for item in raw.get("discovered_ores") or []:
            if isinstance(item, dict) and isinstance(item.get("pos"), list) and len(item["pos"]) >= 3:
                pos = (int(item["pos"][0]), int(item["pos"][1]), int(item["pos"][2]))
                self.discovered_ores[pos] = {
                    "pos": list(pos),
                    "block": str(item.get("block") or ""),
                    "required_tier": int(item.get("required_tier", 2)),
                    "from_disk": True,
                }
        utils = raw.get("utility_blocks")
        if isinstance(utils, dict):
            for k, arr in utils.items():
                if isinstance(arr, list):
                    self.utility_blocks[str(k)] = [
                        (int(p[0]), int(p[1]), int(p[2]))
                        for p in arr
                        if isinstance(p, list) and len(p) >= 3
                    ]
        for sh in raw.get("saved_shafts") or []:
            if isinstance(sh, dict) and isinstance(sh.get("collar_anchor"), list) and len(sh["collar_anchor"]) >= 3:
                sh_copy = dict(sh)
                sh_copy["from_disk"] = True
                self.saved_shafts.append(sh_copy)
        return True

    def save(self) -> bool:
        if self.path is None:
            return False
        payload = {
            "version": 1,
            "updated_at": round(time.time(), 2),
            "excavated_positions": [list(p) for p in list(self.excavated_positions)[-4000:]],
            "known_corridor_positions": [list(p) for p in list(self.known_corridor_positions)[-4000:]],
            "exhausted_collars": [list(p) for p in self.exhausted_collars[-32:]],
            "discovered_ores": list(self.discovered_ores.values())[-200:],
            "utility_blocks": {
                k: [list(p) for p in v[-24:]] for k, v in self.utility_blocks.items()
            },
            "saved_shafts": self.saved_shafts[-8:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
            return True
        except OSError:
            return False


def _default_ledger_path_for_bot(bot: Any) -> pathlib.Path | None:
    client = getattr(bot, "_client", None)
    transport = getattr(client, "_transport", None)
    if transport is not None and type(transport).__name__ == "MockTransport":
        return None
    raw_user = str(getattr(bot, "bot_username", None) or "default")
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_user).strip("_") or "default"
    return pathlib.Path(".state") / f"world_ledger_{safe_user}.json"


def _resolve_target_profile(target: str, target_y: int | None) -> dict[str, Any]:
    raw = str(target or "iron").strip().lower().removeprefix("minecraft:")
    aliases = {
        "raw_iron": "iron",
        "iron_ingot": "iron",
        "iron_ore": "iron",
        "deepslate_iron_ore": "iron",
        "diamond_ore": "diamond",
        "deepslate_diamond_ore": "diamond",
        "coal_ore": "coal",
        "deepslate_coal_ore": "coal",
        "copper_ore": "copper",
        "deepslate_copper_ore": "copper",
        "raw_copper": "copper",
        "gold_ore": "gold",
        "deepslate_gold_ore": "gold",
        "raw_gold": "gold",
        "lapis_lazuli": "lapis",
        "lapis_ore": "lapis",
        "deepslate_lapis_ore": "lapis",
        "redstone_ore": "redstone",
        "deepslate_redstone_ore": "redstone",
        "emerald_ore": "emerald",
        "deepslate_emerald_ore": "emerald",
    }
    key = aliases.get(raw, raw)
    prof = ORE_DFS_PROFILES.get(key)
    if prof is not None:
        resolved_y = int(target_y) if target_y is not None else int(prof["default_target_y"])
        return {
            "profile_key": key,
            "target_blocks": tuple(prof["target_blocks"]),
            "expected_items": tuple(prof["expected_items"]),
            "target_y": resolved_y,
            "min_tier": int(prof["min_tier"]),
        }
    req_tier = max(1, int(BLOCK_MIN_PICKAXE_TIER.get(raw, 2)))
    expected = tuple(sorted(_expected_drops_for_blocks([raw])))
    resolved_y = int(target_y) if target_y is not None else (-54 if req_tier >= 3 else 16)
    return {
        "profile_key": raw,
        "target_blocks": (raw,),
        "expected_items": expected or (raw,),
        "target_y": resolved_y,
        "min_tier": req_tier,
    }


def _extract_item_remaining_durability(item: dict[str, Any]) -> int:
    name = str(item.get("name") or "").removeprefix("minecraft:")
    max_d = int(DEFAULT_MAX_DURABILITY.get(name, 100))
    dur = item.get("durability")
    if isinstance(dur, (list, tuple)) and len(dur) >= 1:
        try:
            return max(0, int(dur[0]))
        except (TypeError, ValueError):
            pass
    rem = item.get("durability_remaining")
    if isinstance(rem, (int, float)):
        return max(0, int(rem))
    used = item.get("durability_used", item.get("damage"))
    item_max = int(item.get("max_durability") or max_d)
    if isinstance(used, (int, float)) and item_max > 0:
        return max(0, item_max - int(used))
    if isinstance(dur, (int, float)) and item_max > 0:
        if 0.0 <= float(dur) <= 1.0 and item_max > 1:
            return max(0, int(round(float(dur) * item_max)))
        return max(0, int(dur))
    return max_d


def _pickaxe_inventory_summary(inv: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Return (usable_pickaxes, max_tier, total_remaining_durability)."""
    picks: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    rows: list[tuple[dict[str, Any], bool]] = [
        (row, False) for row in (inv.get("items") or []) if isinstance(row, dict)
    ]
    held = inv.get("held")
    if isinstance(held, dict) and held.get("name"):
        rows.append((held, False))
    offhand = inv.get("offhand")
    if isinstance(offhand, dict) and offhand.get("name"):
        rows.append((offhand, True))
    for row, from_offhand in rows:
        name = str(row.get("name") or "").removeprefix("minecraft:")
        if name not in PICKAXE_TIERS:
            continue
        raw_slot = row.get("slot")
        slot = 45 if (from_offhand and raw_slot is None) else raw_slot
        if isinstance(slot, int):
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
        rem = _extract_item_remaining_durability(row)
        if rem <= 0:
            continue
        picks.append({
            "name": name,
            "tier": int(PICKAXE_TIERS[name]),
            "remaining": rem,
            "slot": slot,
            "in_offhand": bool(from_offhand or slot == 45),
        })
    max_tier = max((int(p["tier"]) for p in picks), default=0)
    total_rem = sum(int(p["remaining"]) for p in picks)
    return picks, max_tier, total_rem


def _choose_pickaxe_for_tier(picks: list[dict[str, Any]], required_tier: int) -> dict[str, Any] | None:
    """Dual-pickaxe discipline: choose the lowest-tier main-inv pickaxe that meets required_tier."""
    eligible = [p for p in picks if int(p["tier"]) >= max(1, int(required_tier)) and int(p["remaining"]) >= 1]
    if not eligible:
        return None
    eligible.sort(key=lambda p: (int(p["tier"]), 1 if p.get("in_offhand") else 0, int(p["remaining"])))
    return eligible[0]


def _seal_reserve(counts: dict[str, int]) -> int:
    return sum(int(counts.get(name, 0)) for name in SHAFT_SEAL_BLOCKS)


def _pick_seal_material(counts: dict[str, int]) -> str | None:
    for cand in SHAFT_SEAL_BLOCKS:
        if counts.get(cand, 0) >= 1:
            return cand
    return None


def _has_collar_resupply_materials(counts: dict[str, int]) -> bool:
    has_wood = (
        counts.get("stick", 0) >= 2
        or any(v >= 2 and k.endswith("_planks") for k, v in counts.items())
        or any(v >= 1 and k.endswith(("_log", "_stem")) for k, v in counts.items())
    )
    has_head_mat = (
        counts.get("cobblestone", 0) + counts.get("cobbled_deepslate", 0) >= 3
        or counts.get("iron_ingot", 0) >= 3
        or counts.get("diamond", 0) >= 3
    )
    has_smeltable = counts.get("raw_iron", 0) >= 1
    return (has_wood and has_head_mat) or has_smeltable


def _floor_pos(raw: Any) -> tuple[int, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise ValueError(f"Invalid pos: {raw!r}")
    return (math.floor(float(raw[0])), math.floor(float(raw[1])), math.floor(float(raw[2])))


class MiningDFSEngine:
    """Stateful path-compressed underground mining tree with lossless waypoint backtracking."""

    def __init__(self, skills: Any, *, ledger_path: pathlib.Path | str | None = None) -> None:
        self.skills = skills
        self.bot = skills.bot
        resolved_ledger = ledger_path if ledger_path is not None else _default_ledger_path_for_bot(self.bot)
        self.ledger = WorldLedger(resolved_ledger)
        self.nodes: dict[int, ShaftNode] = {}
        self.next_node_id: int = 1
        self.collar_node_id: int | None = None
        self.active_node_id: int | None = None
        self.target_y: int = 16
        self.deferred_ores: dict[tuple[int, int, int], DeferredOreRecord] = {}
        self.mined_ore_positions: set[tuple[int, int, int]] = set()
        self.excavated_positions: set[tuple[int, int, int]] = set(self.ledger.excavated_positions)
        self.known_corridor_positions: set[tuple[int, int, int]] = set(self.ledger.known_corridor_positions)
        self.exhausted_collars: list[tuple[int, int, int]] = list(self.ledger.exhausted_collars)
        self.radar_targets: list[tuple[int, int, int]] = []
        self._last_radar_anchor: tuple[int, int, int] | None = None
        self.sealed_positions: set[tuple[int, int, int]] = set()
        self.catch_blocks_placed: list[list[int]] = []
        self.bridge_blocks_placed: list[list[int]] = []
        self.stone_pick_rock_equips: int = 0
        self.ore_pick_equips: int = 0
        self.dug_during_return: int = 0
        self.relocations_count: int = 0

    def _clear_active_tree_only(self) -> None:
        self.nodes.clear()
        self.next_node_id = 1
        self.collar_node_id = None
        self.active_node_id = None
        self.sealed_positions.clear()
        self._last_radar_anchor = None

    def _archive_current_shaft_to_ledger(self, *, mark_exhausted: bool = False) -> None:
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            return
        collar_anc = self.nodes[self.collar_node_id].anchor
        if mark_exhausted or not self._tree_has_pending_branches():
            if collar_anc not in self.exhausted_collars:
                self.exhausted_collars.append(collar_anc)
            self.ledger.record_exhausted_collar(collar_anc)
        else:
            self.ledger.upsert_shaft(
                {
                    "collar_anchor": list(collar_anc),
                    "target_y": self.target_y,
                    "collar_node_id": self.collar_node_id,
                    "active_node_id": self.active_node_id,
                    "next_node_id": self.next_node_id,
                    "nodes": [n.to_dict() for n in self.nodes.values()],
                }
            )
        self.ledger.save()

    def _tree_has_pending_branches(self) -> bool:
        for n in self.nodes.values():
            if any(b.status == "pending" for b in n.branches):
                return True
            if n.kind == "spine" and any(
                b.kind == "branch_leaf"
                and b.status == "completed"
                and b.prune_reason != "promoted_to_spine"
                and len(b.completed_waypoints) >= 3
                for b in n.branches
            ):
                return True
        return False

    def reset(self, *, geofence: Any = None) -> dict[str, Any]:
        had_collar: tuple[int, int, int] | None = None
        if self.collar_node_id is not None and self.collar_node_id in self.nodes:
            had_collar = self.nodes[self.collar_node_id].anchor
            if had_collar not in self.exhausted_collars:
                self.exhausted_collars.append(had_collar)
            self.ledger.record_exhausted_collar(had_collar)

        self._clear_active_tree_only()
        self.deferred_ores.clear()
        self.radar_targets.clear()
        self.catch_blocks_placed.clear()
        self.bridge_blocks_placed.clear()
        self.stone_pick_rock_equips = 0
        self.ore_pick_equips = 0
        self.dug_during_return = 0

        relocated_from: list[int] | None = None
        relocated_to: list[int] | None = None
        if had_collar is not None:
            try:
                st = self.bot.status()
                if st.get("alive") is not False and st.get("ready") is not False and st.get("pos"):
                    cur_pos = _floor_pos(st["pos"])
                    if self._is_near_exhausted_collar(cur_pos):
                        relocated_from = list(cur_pos)
                        new_pos = self._relocate_to_fresh_collar_site(cur_pos, geofence=geofence)
                        if new_pos != cur_pos:
                            relocated_to = list(new_pos)
            except (ApiError, ValueError, KeyError):
                pass
        self.ledger.save()
        out: dict[str, Any] = {
            "ok": True,
            "action": "reset",
            "message": (
                f"MiningDFS 矿井空间树已重置并自动换点至 {relocated_to}。"
                if relocated_to is not None
                else "MiningDFS 矿井空间树已重置（如需换井请确保已离开旧井口 >=14 格）。"
            ),
        }
        if relocated_from is not None:
            out["relocated_from"] = relocated_from
        if relocated_to is not None:
            out["relocated_to"] = relocated_to
        return out

    def snapshot(self) -> dict[str, Any]:
        node_summaries = [
            {
                "node_id": n.node_id,
                "kind": n.kind,
                "anchor": list(n.anchor),
                "parent_id": n.parent_id,
                "pending_branches": [
                    {"kind": b.kind, "dir": b.direction, "steps": b.steps}
                    for b in n.branches
                    if b.status == "pending"
                ],
                "pruned_branches": [
                    {"kind": b.kind, "dir": b.direction, "reason": b.prune_reason}
                    for b in n.branches
                    if b.status == "pruned"
                ],
            }
            for n in self.nodes.values()
        ]
        return {
            "collar_node_id": self.collar_node_id,
            "active_node_id": self.active_node_id,
            "target_y": self.target_y,
            "node_count": len(self.nodes),
            "nodes": node_summaries[-12:],
            "deferred_ores": [
                {
                    "pos": list(rec.pos),
                    "block": rec.block_name,
                    "required_tier": rec.required_tier,
                    "nearest_node_id": rec.nearest_node_id,
                }
                for rec in self.deferred_ores.values()
            ],
            "radar_targets": [list(p) for p in self.radar_targets[:8]],
            "exhausted_collars": [list(p) for p in self.exhausted_collars[-6:]],
            "sealed_count": len(self.sealed_positions),
            "catch_blocks_placed": len(self.catch_blocks_placed),
            "bridge_blocks_placed": len(self.bridge_blocks_placed),
            "stone_pick_rock_equips": self.stone_pick_rock_equips,
            "ore_pick_equips": self.ore_pick_equips,
            "relocations_count": self.relocations_count,
        }

    # ---- Low-level Safe World Helpers ----

    def _record_excavated(self, pt: tuple[int, int, int]) -> None:
        self.excavated_positions.add(pt)
        self.ledger.record_excavated(pt)

    def _record_corridor(self, pt: tuple[int, int, int]) -> None:
        self.known_corridor_positions.add(pt)
        self.known_corridor_positions.add((pt[0], pt[1] + 1, pt[2]))
        self.ledger.record_corridor(pt)

    def _is_self_cavity(self, pts: list[tuple[int, int, int]] | tuple[tuple[int, int, int], ...]) -> bool:
        return any(p in self.excavated_positions or p in self.known_corridor_positions for p in pts)

    def _is_near_exhausted_collar(self, pos: tuple[int, int, int], *, min_dist: float = 12.0) -> bool:
        for ex in self.exhausted_collars:
            if math.hypot(pos[0] - ex[0], pos[2] - ex[2]) < min_dist and abs(pos[1] - ex[1]) <= 18:
                return True
        return False

    def _get_block_name(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str] | None = None) -> str:
        if cache is not None and pt in cache:
            return cache[pt]
        res = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
        raw = res.get("name") or res.get("block")
        if not raw:
            raise ApiError("unknown_block", f"无法确认方块 {list(pt)}")
        name = str(raw).removeprefix("minecraft:")
        if cache is not None:
            cache[pt] = name
        if name in {"crafting_table", "furnace"}:
            self.ledger.record_utility(name, pt)
        return name

    def _find_offhand_swap_item(self, inv: dict[str, Any], *, max_exclude_tier: int = 3) -> str | None:
        items = [
            r
            for r in (inv.get("items") or [])
            if isinstance(r, dict) and r.get("name") and int(r.get("count") or 1) > 0
        ]
        counts: dict[str, int] = {}
        for r in items:
            n = str(r["name"]).removeprefix("minecraft:")
            counts[n] = counts.get(n, 0) + int(r.get("count") or 1)
        seal = _pick_seal_material(counts)
        if seal:
            return seal
        non_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if str(r["name"]).removeprefix("minecraft:") not in PICKAXE_TIERS
        ]
        if non_pick:
            return non_pick[0]
        lower_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if PICKAXE_TIERS.get(str(r["name"]).removeprefix("minecraft:"), 99) < max_exclude_tier
        ]
        return lower_pick[0] if lower_pick else None

    def _equip_for_block_tier(
        self,
        required_tier: int,
        *,
        for_ore: bool = False,
        inv_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, int, int]:
        """Select & equip best pickaxe per Dual-Pickaxe Discipline. Returns (chosen, held_name, max_tier, total_rem)."""
        inv = inv_snapshot if isinstance(inv_snapshot, dict) else self.bot.inventory()
        picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
        held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None

        # Protect one high-tier pickaxe in offhand while mining rock with lower-tier pickaxes
        # so mineflayer bestHarvestTool cannot burn the last iron pickaxe before stone pickaxes run out.
        if required_tier <= 2:
            low_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) <= 2]
            high_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) >= 3]
            high_off = [p for p in picks if p.get("in_offhand") and int(p["tier"]) >= 3]
            if low_main and high_main and not high_off:
                high_keep = max(high_main, key=lambda p: (int(p["tier"]), int(p["remaining"])))
                try:
                    self.bot.call("equip", {"item": high_keep["name"], "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                except ApiError:
                    pass

        chosen = _choose_pickaxe_for_tier(picks, required_tier)
        if chosen is not None and chosen.get("in_offhand"):
            swap_item = self._find_offhand_swap_item(inv, max_exclude_tier=int(chosen["tier"]))
            if swap_item:
                try:
                    self.bot.call("equip", {"item": swap_item, "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                    chosen = _choose_pickaxe_for_tier(picks, required_tier)
                except ApiError:
                    pass

        if chosen is not None and (chosen.get("in_offhand") or chosen["name"] != held_name):
            try:
                self.bot.call("equip", {"item": chosen["name"], "slot": "hand"})
                held_name = chosen["name"]
                if for_ore:
                    self.ore_pick_equips += 1
                elif chosen["name"] in {"stone_pickaxe", "wooden_pickaxe"}:
                    self.stone_pick_rock_equips += 1
            except ApiError:
                pass
        return chosen, held_name, max_tier, total_rem

    def _geofence_blocked(self, geofence: Any, x: float, z: float, *, action: str) -> dict[str, Any] | None:
        if geofence is None:
            return None
        return geofence.check_point(x + 0.5, z + 0.5, action=action)

    def _inspect_step_hazards(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        *,
        forward_delta: tuple[int, int] | None = None,
    ) -> tuple[tuple[str, dict[str, Any]] | None, list[tuple[int, int, int]]]:
        """Distinguish in-path fluid/bedrock hazards from sealable lateral/overhead fluids."""
        if not targets:
            return None, []
        target_set = set(targets)
        sealable_fluids: list[tuple[int, int, int]] = []
        seen_fluids: set[tuple[int, int, int]] = set()
        top_target = targets[0]

        for p in targets:
            b = self._get_block_name(p, cache)
            if b in FLUID_BLOCKS or b == "bedrock":
                return ("fluid_or_bedrock", {"pos": list(p), "block": b, "relation": "in_path"}), []
            # Omit (0, -1, 0): fluid strictly beneath a cell cannot flow upward into it.
            for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (p[0] + ax, p[1] + ay, p[2] + az)
                if nb in target_set:
                    continue
                nb_name = self._get_block_name(nb, cache)
                if nb_name in FLUID_BLOCKS:
                    if forward_delta is not None and (ax, az) == forward_delta and nb_name in LAVA_BLOCKS:
                        return (
                            "fluid_or_bedrock",
                            {"pos": list(nb), "block": nb_name, "relation": "forward_lava", "target": list(p)},
                        ), []
                    if nb not in seen_fluids:
                        seen_fluids.add(nb)
                        sealable_fluids.append(nb)

        above1 = (top_target[0], top_target[1] + 1, top_target[2])
        above2 = (top_target[0], top_target[1] + 2, top_target[2])
        a1_name = self._get_block_name(above1, cache)
        if a1_name in FALLING_BLOCKS and self._get_block_name(above2, cache) in FALLING_BLOCKS:
            return (
                "falling_material_adjacent",
                {
                    "pos": list(above1),
                    "block": a1_name,
                    "relation": "overhead_falling_column",
                    "target": list(top_target),
                },
            ), sealable_fluids
        return None, sealable_fluids

    def _adjacent_fluid_or_falling(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[str, dict[str, Any]] | None:
        """Strict check used when digging optional side ore cells without sealing."""
        hazard, adj_fluids = self._inspect_step_hazards(targets, cache)
        if hazard is not None:
            return hazard
        if adj_fluids:
            nb = adj_fluids[0]
            return "fluid_or_bedrock", {"pos": list(nb), "block": cache.get(nb, "fluid"), "relation": "adjacent_fluid"}
        return None

    def _is_solid_floor(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str]) -> bool:
        b = self._get_block_name(pt, cache)
        if b in PASSABLE_BLOCKS | FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS | FLIMSY_FLOOR_BLOCKS:
            return False
        if b.endswith(FLIMSY_FLOOR_SUFFIXES):
            return False
        if b in FALLING_BLOCKS:
            below = self._get_block_name((pt[0], pt[1] - 1, pt[2]), cache)
            if below in PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS or below.endswith(FLIMSY_FLOOR_SUFFIXES):
                return False
        return True

    def _dig_cell_safely(
        self,
        dpos: tuple[int, int, int],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[bool, str | None, bool, int]:
        """Dig one target cell safely, handling passables, tool breaks, and single overhead falling blocks."""
        deferred_added = 0
        mined_ore = False
        stale_passable = cache.get(dpos) in PASSABLE_BLOCKS | FALLING_BLOCKS
        bname = self._get_block_name(dpos, None if stale_passable else cache)
        if bname in PASSABLE_BLOCKS:
            cache[dpos] = bname
            return True, None, False, 0

        for _ in range(5):
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1))
            if req_t > 1:
                chosen_o, _, max_t, _ = self._equip_for_block_tier(req_t, for_ore=(bname in TRACKED_ORES))
                if chosen_o is None or max_t < req_t:
                    if bname in TRACKED_ORES and dpos not in self.deferred_ores:
                        self.deferred_ores[dpos] = DeferredOreRecord(dpos, bname, req_t, parent_node_id)
                        self.ledger.record_ore(dpos, bname, req_t)
                        deferred_added += 1
                    return False, "insufficient_pickaxe_tier", mined_ore, deferred_added

            is_tracked = bname in TRACKED_ORES
            should_collect = bool(is_tracked or collect_rock)
            retried_tool = False
            while True:
                try:
                    self.bot.call("dig", {"pos": [dpos[0], dpos[1], dpos[2]], "collect": should_collect, "stop_on": ["hurt", "death"]})
                    cache[dpos] = "air"
                    self._record_excavated(dpos)
                    if is_tracked:
                        self.mined_ore_positions.add(dpos)
                        self.ledger.remove_ore(dpos)
                        mined_ore = True
                    break
                except ApiError as err:
                    if err.code == "nothing_to_dig":
                        cache[dpos] = "air"
                        self._record_excavated(dpos)
                        return True, None, mined_ore, deferred_added
                    if err.code in {"hurt", "death", "interrupted", "protected_region"}:
                        return False, ("hurt" if err.code in {"hurt", "death"} else err.code), mined_ore, deferred_added
                    if not retried_tool:
                        retried_tool = True
                        st = self.bot.status()
                        if st.get("alive") is False:
                            return False, "not_ready", mined_ore, deferred_added
                        cache.pop(dpos, None)
                        live_blk = self._get_block_name(dpos, cache)
                        if live_blk in PASSABLE_BLOCKS:
                            return True, None, mined_ore, deferred_added
                        bname = live_blk
                        self._equip_for_block_tier(int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1)), for_ore=(bname in TRACKED_ORES))
                        continue
                    return False, "dig_failed", mined_ore, deferred_added

            above_pt = (dpos[0], dpos[1] + 1, dpos[2])
            if bname in FALLING_BLOCKS or self._get_block_name(above_pt, cache) in FALLING_BLOCKS:
                time.sleep(0.14)
                cache.pop(dpos, None)
                cache.pop(above_pt, None)
                refill = self._get_block_name(dpos, cache)
                if refill in PASSABLE_BLOCKS:
                    return True, None, mined_ore, deferred_added
                if refill in FLUID_BLOCKS:
                    return False, "fluid_or_bedrock", mined_ore, deferred_added
                bname = refill
                continue
            return True, None, mined_ore, deferred_added

        return False, "falling_material_adjacent", mined_ore, deferred_added

    def _seal_breach(
        self,
        seal_coords: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
    ) -> list[list[int]]:
        """Place solid seal blocks at exposed air or fluid openings to wall off caves/fluids."""
        placed: list[list[int]] = []
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return placed
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS) - {"torch", "wall_torch"}
        for pt in seal_coords:
            if pt in self.sealed_positions:
                continue
            cache.pop(pt, None)
            try:
                cur = self._get_block_name(pt, cache)
            except ApiError:
                continue
            if cur not in replaceable:
                continue
            try:
                st = self.bot.status()
                bpos = _floor_pos(st.get("pos") or [0, 0, 0])
                if pt in {bpos, (bpos[0], bpos[1] + 1, bpos[2])}:
                    continue
            except (ApiError, ValueError):
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": [pt[0], pt[1], pt[2]]})
                cache[pt] = filler
                self.sealed_positions.add(pt)
                placed.append([pt[0], pt[1], pt[2]])
            except ApiError:
                counts = _inventory_counts(self.bot)
                filler = _pick_seal_material(counts)
                if not filler:
                    break
        return placed

    def _place_bridge_block(
        self,
        support_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        """Place a solid floor/step block to bridge across self-dug cavities or shallow gaps."""
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return False
        cache.pop(support_pt, None)
        try:
            cur = self._get_block_name(support_pt, cache)
        except ApiError:
            return False
        if self._is_solid_floor(support_pt, cache):
            return True
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS) - {"torch", "wall_torch"}
        if cur.endswith(FLIMSY_FLOOR_SUFFIXES) and cur not in replaceable:
            try:
                self.bot.call("dig", {"pos": [support_pt[0], support_pt[1], support_pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                cache[support_pt] = "air"
            except ApiError:
                return False
        elif cur not in replaceable:
            return False
        try:
            self.bot.call("place", {"item": filler, "pos": [support_pt[0], support_pt[1], support_pt[2]]})
            cache[support_pt] = filler
            self.bridge_blocks_placed.append([support_pt[0], support_pt[1], support_pt[2]])
            return True
        except ApiError:
            return False

    def _settled_position(self, cur: list[float] | None = None) -> tuple[list[float], bool]:
        """Wait briefly until gravity settles on ground after stepping onto a target block."""
        pos = list(cur) if cur else [0.0, 64.0, 0.0]
        on_ground = True
        for _ in range(6):
            st = self.bot.status()
            if st.get("pos"):
                pos = [float(v) for v in st["pos"][:3]]
            on_ground = bool(st.get("on_ground", True))
            if on_ground:
                break
            time.sleep(0.08)
        return pos, on_ground

    def _move_exact(self, pt: tuple[int, int, int], *, timeout_ms: int = 4500) -> bool:
        fx, fy, fz = pt[0] + 0.5, float(pt[1]), pt[2] + 0.5
        try:
            self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.35, "timeout": timeout_ms, "stop_on": ["hurt", "death"]})
        except ApiError:
            pass
        cur, on_ground = self._settled_position()
        if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.78 and on_ground:
            self._record_corridor(pt)
            return True

        # Fallback: directly look toward target and nudge across block edge (both step-down & step-up)
        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            need_jump = fy > cur[1] + 0.25
            try:
                self.bot.call("look", {"x": fx, "y": fy + 0.4, "z": fz})
                ctrl_body: dict[str, Any] = {
                    "forward": True,
                    "ms": 540 if need_jump else 420,
                    "stop_on": ["hurt", "death"],
                }
                if need_jump:
                    ctrl_body["jump"] = True
                self.bot.call("control", ctrl_body)
            except ApiError:
                pass
            time.sleep(0.08)
            cur, on_ground = self._settled_position(cur)
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85 and on_ground:
                self._record_corridor(pt)
                return True

        # Second move_to attempt after nudge cleared ledge/step boundary
        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            try:
                self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.3, "timeout": 2500, "stop_on": ["hurt", "death"]})
            except ApiError:
                pass
            cur, on_ground = self._settled_position(cur)
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85:
                self._record_corridor(pt)
                return True
        return False

    def _clear_incidental_obstruction_only(self, target_wp: tuple[int, int, int]) -> int:
        """During lossless backtrack, clear only loose falling debris at the waypoint itself."""
        dug = 0
        for dy in (1, 0):
            pt = (target_wp[0], target_wp[1] + dy, target_wp[2])
            try:
                b = self._get_block_name(pt)
            except ApiError:
                continue
            if b in FALLING_BLOCKS:
                try:
                    self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                    dug += 1
                except ApiError:
                    pass
        return dug

    def _walk_waypoints(
        self,
        waypoints: list[tuple[int, int, int]],
        *,
        reverse: bool = False,
        min_hp_ref: list[float],
    ) -> tuple[bool, str | None]:
        ordered = list(reversed(waypoints)) if reverse else list(waypoints)
        for wp in ordered:
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                return False, "not_ready"
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                return False, "hurt"
            cur = _floor_pos(st["pos"])
            if abs(cur[0] - wp[0]) == 0 and abs(cur[1] - wp[1]) == 0 and abs(cur[2] - wp[2]) == 0:
                continue
            if self._move_exact(wp):
                continue
            dx_w = wp[0] - cur[0]
            dy_w = wp[1] - cur[1]
            dz_w = wp[2] - cur[2]
            if max(abs(dx_w), abs(dz_w)) == 2 and abs(dy_w) in (0, 2):
                mid_wp = (cur[0] + dx_w // 2, cur[1] + (dy_w // 2), cur[2] + dz_w // 2)
                if self._move_exact(mid_wp) and self._move_exact(wp):
                    continue
            cleared = self._clear_incidental_obstruction_only(wp)
            self.dug_during_return += cleared
            if cleared > 0 and self._move_exact(wp):
                continue
            return False, "backtrack_blocked"
        return True, None

    def _find_node_path(self, start_id: int, goal_id: int) -> list[tuple[list[tuple[int, int, int]], bool, int]]:
        """Compute compressed tree path as segments of (waypoints, reverse_flag, dest_node_id)."""
        if start_id == goal_id:
            return []
        ancestors_a: list[int] = []
        cur: int | None = start_id
        while cur is not None and cur in self.nodes:
            ancestors_a.append(cur)
            cur = self.nodes[cur].parent_id
        set_a = set(ancestors_a)

        ancestors_b: list[int] = []
        cur_b: int | None = goal_id
        lca: int | None = None
        while cur_b is not None and cur_b in self.nodes:
            if cur_b in set_a:
                lca = cur_b
                break
            ancestors_b.append(cur_b)
            cur_b = self.nodes[cur_b].parent_id

        if lca is None:
            return []

        segments: list[tuple[list[tuple[int, int, int]], bool, int]] = []
        walk = start_id
        while walk != lca:
            node = self.nodes[walk]
            if node.edge_from_parent is None or node.parent_id is None:
                break
            segments.append((node.edge_from_parent.waypoints, True, node.parent_id))
            walk = node.parent_id
        for child_id in reversed(ancestors_b):
            node = self.nodes[child_id]
            if node.edge_from_parent is not None:
                segments.append((node.edge_from_parent.waypoints, False, child_id))
        return segments

    def _navigate_to_node(
        self,
        goal_node_id: int,
        *,
        min_hp_ref: list[float],
    ) -> tuple[bool, str | None]:
        if self.active_node_id is None or self.active_node_id not in self.nodes or goal_node_id not in self.nodes:
            return False, "no_active_tree"
        st = self.bot.status()
        cur_pos = _floor_pos(st["pos"])
        anchor_now = self.nodes[self.active_node_id].anchor
        if cur_pos != anchor_now:
            best_near_id: int | None = None
            best_near_d = float("inf")
            for nid, nd in self.nodes.items():
                d = math.dist(cur_pos, nd.anchor)
                if d < best_near_d:
                    best_near_d = d
                    best_near_id = nid
            if best_near_id is not None and best_near_d <= 16.0 and (
                best_near_id == self.collar_node_id or best_near_d <= math.dist(cur_pos, anchor_now)
            ):
                if not self._move_exact(self.nodes[best_near_id].anchor):
                    return False, "cannot_reach_nearest_node"
                self.active_node_id = best_near_id
            elif self.collar_node_id in self.nodes and math.hypot(
                cur_pos[0] - self.nodes[self.collar_node_id].anchor[0],
                cur_pos[2] - self.nodes[self.collar_node_id].anchor[2],
            ) <= 48.0:
                collar_anc = self.nodes[self.collar_node_id].anchor
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": collar_anc[0] + 0.5,
                            "z": collar_anc[2] + 0.5,
                            "range": 1.4,
                            "timeout": 9000,
                            "stop_on": ["hurt", "death"],
                        },
                    )
                except ApiError:
                    pass
                if not self._move_exact(collar_anc):
                    return False, "cannot_reach_collar"
                self.active_node_id = self.collar_node_id
            elif not self._move_exact(anchor_now):
                return False, "cannot_reach_anchor"

        if self.active_node_id == goal_node_id:
            return True, None

        segments = self._find_node_path(self.active_node_id, goal_node_id)
        for waypoints, rev, dest_id in segments:
            ok, err = self._walk_waypoints(waypoints, reverse=rev, min_hp_ref=min_hp_ref)
            if not ok:
                return False, err
            self.active_node_id = dest_id
        return True, None

    # ---- Long-Range 64-Block Ore Radar & Directional Steering ----

    def _scan_ore_radar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        max_tier: int,
        geofence: Any,
        force: bool = False,
    ) -> list[tuple[int, int, int]]:
        """Use 64-block /find_blocks radar to detect exposed target ore veins in nearby caves/shafts."""
        self.radar_targets = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions and not self.skills._is_block_avoided(pt)
        ]
        if (
            not force
            and self._last_radar_anchor is not None
            and math.dist(anchor, self._last_radar_anchor) < 10.0
            and self.radar_targets
        ):
            self.radar_targets.sort(key=lambda p: math.dist(anchor, p))
            return self.radar_targets

        self._last_radar_anchor = anchor
        target_set = set(target_blocks)
        found_pts: list[tuple[int, int, int]] = []
        seen: set[tuple[int, int, int]] = set()

        try:
            res = self.bot.call(
                "find_blocks",
                {"radius": RADAR_SCAN_RADIUS, "types": list(target_blocks), "limit": 32},
            )
            blocks = res.get("blocks") or []
        except ApiError:
            blocks = []

        nearest_nid = self.active_node_id or self.collar_node_id or 1
        for blk in blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if bname not in target_set or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if pt in seen or pt in self.mined_ore_positions or self.skills._is_block_avoided(pt):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            seen.add(pt)
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            self.ledger.record_ore(pt, bname, req_t)
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, nearest_nid)
                continue
            found_pts.append(pt)

        # Also pull verified ledger ores matching target_set within 64 blocks
        for pt, meta in list(self.ledger.discovered_ores.items()):
            if pt in seen or pt in self.mined_ore_positions or self.skills._is_block_avoided(pt):
                continue
            if meta.get("block") not in target_set or int(meta.get("required_tier", 2)) > max_tier:
                continue
            if math.dist(anchor, pt) > RADAR_SCAN_RADIUS + 4:
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            if meta.get("from_disk"):
                try:
                    live_name = self._get_block_name(pt)
                except ApiError:
                    continue
                if live_name != meta.get("block"):
                    self.ledger.remove_ore(pt)
                    continue
                meta["from_disk"] = False
            seen.add(pt)
            found_pts.append(pt)

        found_pts.sort(key=lambda p: (abs(p[1] - self.target_y) // 12, math.dist(anchor, p)))
        self.radar_targets = found_pts[:24]
        return self.radar_targets

    def _best_radar_target_for_anchor(
        self,
        anchor: tuple[int, int, int],
        *,
        at_spine: bool = False,
    ) -> tuple[int, int, int] | None:
        valid = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions and not self.skills._is_block_avoided(pt)
        ]
        if not valid:
            return None
        if at_spine:
            same_band = [pt for pt in valid if abs(pt[1] - anchor[1]) <= 10]
            if same_band:
                return min(same_band, key=lambda p: math.dist(anchor, p))
        return min(valid, key=lambda p: math.dist(anchor, p))

    def _radar_direction_projection(
        self,
        anchor: tuple[int, int, int],
        direction: str,
        *,
        at_spine: bool = False,
    ) -> float:
        rt = self._best_radar_target_for_anchor(anchor, at_spine=at_spine)
        if rt is None:
            return 0.0
        dx, dz = DIRECTIONS[direction]
        vx, vz = rt[0] - anchor[0], rt[2] - anchor[2]
        return float(vx * dx + vz * dz)

    # ---- Canopy Escape & Solid Collar Site Selection ----

    def _probe_collar_dirs(
        self,
        anchor: tuple[int, int, int],
        *,
        geofence: Any,
        cache: dict[tuple[int, int, int], str] | None = None,
        counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Return viable stair_down directions at anchor, ordered by radar alignment and dryness."""
        blk_cache = cache if cache is not None else {}
        inv_counts = counts if counts is not None else _inventory_counts(self.bot)
        has_seal = _seal_reserve(inv_counts) >= 1
        canonical = {"north": 0, "east": 1, "south": 2, "west": 3}
        scored: list[tuple[int, float, int, str]] = []

        for d in ("north", "east", "south", "west"):
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                continue
            nx, nz = anchor[0] + dx, anchor[2] + dz
            dig_cells = [(nx, anchor[1] + 1, nz), (nx, anchor[1], nz), (nx, anchor[1] - 1, nz)]
            try:
                hazard, adj_fluids = self._inspect_step_hazards(dig_cells, blk_cache, forward_delta=(dx, dz))
                if hazard is not None:
                    continue
                if adj_fluids and not has_seal and any(blk_cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    continue
                support_pt = (nx, anchor[1] - 2, nz)
                support_blk = self._get_block_name(support_pt, blk_cache)
                if support_blk in FLUID_BLOCKS | {"bedrock"}:
                    continue
                support_ok = self._is_solid_floor(support_pt, blk_cache)
                if not support_ok:
                    can_bridge = has_seal and (
                        self._is_self_cavity([support_pt, *dig_cells])
                        or self._is_solid_floor((nx, anchor[1] - 3, nz), blk_cache)
                    )
                    if not can_bridge:
                        continue
            except ApiError:
                continue

            proj = self._radar_direction_projection(anchor, d, at_spine=False)
            fluid_penalty = 1 if adj_fluids else 0
            radar_rank = -proj if proj > 1.0 else 0.0
            scored.append((fluid_penalty, radar_rank, canonical[d], d))

        scored.sort(key=lambda r: (r[0], r[1], r[2]))
        return [r[3] for r in scored]

    def _descend_tree_canopy_if_needed(self, cur_pos: tuple[int, int, int]) -> tuple[int, int, int]:
        """If spawned on *_leaves canopy, safely dig leaves underfoot (hand-dig, 0 pickaxe wear) down to terrain."""
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        for _ in range(10):
            under_pt = (pos[0], pos[1] - 1, pos[2])
            try:
                under_blk = self._get_block_name(under_pt, None)
            except ApiError:
                break
            if not under_blk.endswith("_leaves"):
                break
            # Check vertical drop safety before breaking leaf underfoot
            safe_drop = False
            contiguous_air = 0
            for dy in range(2, 16):
                probe_pt = (pos[0], pos[1] - dy, pos[2])
                try:
                    b = self._get_block_name(probe_pt, cache)
                except ApiError:
                    break
                if b in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                    safe_drop = False
                    break
                if b in PASSABLE_BLOCKS:
                    contiguous_air += 1
                    if contiguous_air > 4:
                        safe_drop = False
                        break
                    continue
                if b.endswith("_leaves"):
                    contiguous_air = 0
                    safe_drop = True
                    break
                # Hit non-leaf solid block within <= 4 air blocks
                safe_drop = True
                break
            if not safe_drop:
                break
            try:
                self.bot.call("dig", {"pos": [under_pt[0], under_pt[1], under_pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
            except ApiError:
                break
            settled, _ = self._settled_position()
            new_pos = _floor_pos(settled)
            if new_pos == pos:
                break
            pos = new_pos
        return pos

    def _find_solid_collar_candidates(
        self,
        origin: tuple[int, int, int],
        *,
        geofence: Any,
        avoid_exhausted: bool,
        radius: int = 20,
    ) -> list[tuple[int, int, int]]:
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": min(64, max(8, radius)), "types": list(SURFACE_GROUND_BLOCKS), "limit": 48},
            )
            blocks = found.get("blocks") or []
        except ApiError:
            blocks = []

        cache: dict[tuple[int, int, int], str] = {}
        counts = _inventory_counts(self.bot)
        scored: list[tuple[int, float, tuple[int, int, int]]] = []
        seen: set[tuple[int, int, int]] = set()

        for blk in blocks:
            bpos = blk.get("pos")
            if not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            gx, gy, gz = int(bpos[0]), int(bpos[1]), int(bpos[2])
            stand = (gx, gy + 1, gz)
            if stand in seen:
                continue
            seen.add(stand)
            if self._geofence_blocked(geofence, gx, gz, action="collar_seek") is not None:
                continue
            if avoid_exhausted and self._is_near_exhausted_collar(stand, min_dist=EXHAUSTED_COLLAR_MIN_SEP):
                continue
            hdist = math.hypot(stand[0] - origin[0], stand[2] - origin[2])
            if avoid_exhausted and hdist < 10.0:
                continue
            try:
                if not self._is_solid_floor((gx, gy, gz), cache):
                    continue
                if self._get_block_name(stand, cache) not in PASSABLE_BLOCKS:
                    continue
                if self._get_block_name((gx, gy + 2, gz), cache) not in PASSABLE_BLOCKS:
                    continue
                dirs = self._probe_collar_dirs(stand, geofence=geofence, cache=cache, counts=counts)
            except ApiError:
                continue
            if not dirs:
                continue
            dir_tier = 0 if len(dirs) >= 2 else 1
            scored.append((dir_tier, math.dist(origin, stand), stand))

        scored.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in scored[:8]]

    def _relocate_to_fresh_collar_site(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any = None,
        deadline: float | None = None,
        min_hp_ref: list[float] | None = None,
    ) -> tuple[int, int, int]:
        """Deterministically relocate >=14 blocks away from exhausted Collar sites onto viable ground."""
        cands = self._find_solid_collar_candidates(
            cur_pos,
            geofence=geofence,
            avoid_exhausted=True,
            radius=28,
        )
        for stand in cands[:5]:
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 8000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass
            self._move_exact(stand, timeout_ms=2500)
            st = self.bot.status()
            now_pos = _floor_pos(st["pos"])
            if not self._is_near_exhausted_collar(now_pos, min_dist=10.0):
                if self._probe_collar_dirs(now_pos, geofence=geofence):
                    self.relocations_count += 1
                    return now_pos

        # Underground fallback: dig a 12-block horizontal relocation drift away from nearest exhausted collar
        ex_ref = self.exhausted_collars[-1] if self.exhausted_collars else cur_pos
        dir_order = sorted(
            ("north", "east", "south", "west"),
            key=lambda d: -(
                (cur_pos[0] + DIRECTIONS[d][0] * 12 - ex_ref[0]) ** 2
                + (cur_pos[2] + DIRECTIONS[d][1] * 12 - ex_ref[2]) ** 2
            ),
        )
        hp_ref = min_hp_ref if min_hp_ref is not None else [20.0]
        dl = deadline if deadline is not None else (time.monotonic() + 18.0)
        for d in dir_order:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, cur_pos[0] + dx * 12, cur_pos[2] + dz * 12, action="collar_relocate_drift") is not None:
                continue
            drift = self._dig_level_edge(
                direction=d,
                steps=12,
                target_blocks=(),
                parent_node_id=0,
                geofence=geofence,
                deadline=dl,
                min_hp_ref=hp_ref,
                min_target_tier=1,
            )
            if int(drift["steps_done"]) >= 4:
                self.relocations_count += 1
                return tuple(drift["end_anchor"])

        return cur_pos

    def _ensure_solid_collar_anchor(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
    ) -> tuple[tuple[int, int, int], list[str], str | None]:
        """Ensure Collar starts on solid terrain with >=1 viable stair direction (fixes tree-canopy trap)."""
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        near_ex = self._is_near_exhausted_collar(pos)
        under_pt = (pos[0], pos[1] - 1, pos[2])
        try:
            on_solid = self._is_solid_floor(under_pt, cache)
        except ApiError:
            on_solid = False

        if on_solid and not near_ex:
            dirs0 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs0:
                return pos, dirs0, None

        # Step 1: If standing on leaves canopy, safely descend through leaves to ground
        pos = self._descend_tree_canopy_if_needed(pos)
        cache.clear()
        near_ex = self._is_near_exhausted_collar(pos)
        try:
            on_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), cache)
        except ApiError:
            on_solid = False
        if on_solid and not near_ex:
            dirs1 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs1:
                return pos, dirs1, None

        # Step 2: Relocate if near exhausted collar, or horizontally seek solid ground anchor nearby
        if near_ex:
            pos = self._relocate_to_fresh_collar_site(
                pos,
                geofence=geofence,
                deadline=deadline,
                min_hp_ref=min_hp_ref,
            )
            dirs_rel = self._probe_collar_dirs(pos, geofence=geofence)
            if dirs_rel and self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                return pos, dirs_rel, None

        cands = self._find_solid_collar_candidates(
            pos,
            geofence=geofence,
            avoid_exhausted=near_ex,
            radius=20,
        )
        for stand in cands[:5]:
            if time.monotonic() >= deadline:
                break
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 7000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass
            self._move_exact(stand, timeout_ms=2200)
            st = self.bot.status()
            pos = _floor_pos(st["pos"])
            if self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                dirs_c = self._probe_collar_dirs(pos, geofence=geofence)
                if dirs_c:
                    return pos, dirs_c, None

        # Final check: never fall back to blind 4-way digging while standing on leaves/flimsy air!
        try:
            still_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {})
        except ApiError:
            still_solid = False
        if not still_solid:
            return pos, [], "no_solid_collar_ground"

        dirs_final = self._probe_collar_dirs(pos, geofence=geofence)
        return pos, dirs_final, (None if dirs_final else "no_viable_collar_direction")

    def _try_restore_saved_shaft(
        self,
        cur_pos: tuple[int, int, int],
        *,
        min_hp_ref: list[float],
    ) -> bool:
        """If near a previously excavated non-exhausted shaft in WorldLedger, verify via get_block and resume."""
        best_sh: dict[str, Any] | None = None
        best_d = 36.0
        for sh in self.ledger.saved_shafts:
            anc_raw = sh.get("collar_anchor")
            if not isinstance(anc_raw, list) or len(anc_raw) < 3:
                continue
            anc = (int(anc_raw[0]), int(anc_raw[1]), int(anc_raw[2]))
            if anc in self.exhausted_collars:
                continue
            hd = math.hypot(cur_pos[0] - anc[0], cur_pos[2] - anc[2])
            if hd <= best_d and abs(cur_pos[1] - anc[1]) <= 20:
                best_d = hd
                best_sh = sh

        if best_sh is None:
            return False

        raw_nodes = best_sh.get("nodes") or []
        if not raw_nodes:
            return False
        restored: dict[int, ShaftNode] = {}
        for rn in raw_nodes:
            if isinstance(rn, dict):
                nd = ShaftNode.from_dict(rn)
                restored[nd.node_id] = nd
        collar_id = int(best_sh.get("collar_node_id", 1))
        if collar_id not in restored:
            return False

        # Verify world ground truth if loaded from disk (reject stale shafts after chunk regen)
        if best_sh.get("from_disk"):
            collar_anc = restored[collar_id].anchor
            sample_wp: tuple[int, int, int] | None = None
            for nd in restored.values():
                if nd.edge_from_parent:
                    for wp in nd.edge_from_parent.waypoints:
                        if wp != collar_anc:
                            sample_wp = wp
                            break
                if sample_wp is not None:
                    break
            if sample_wp is not None:
                try:
                    if self._get_block_name(sample_wp) not in PASSABLE_BLOCKS:
                        self.ledger.saved_shafts = [s for s in self.ledger.saved_shafts if s is not best_sh]
                        return False
                except ApiError:
                    return False

        self.nodes = restored
        self.collar_node_id = collar_id
        self.next_node_id = max(restored.keys(), default=1) + 1
        active_id = int(best_sh.get("active_node_id") or collar_id)
        self.active_node_id = active_id if active_id in restored else collar_id
        if not self._tree_has_pending_branches():
            self._clear_active_tree_only()
            return False

        for o_pt, meta in list(self.ledger.discovered_ores.items()):
            if o_pt in self.mined_ore_positions or o_pt in self.deferred_ores:
                continue
            bname = str(meta.get("block") or "")
            if not bname:
                continue
            if meta.get("from_disk"):
                try:
                    if self._get_block_name(o_pt) != bname:
                        self.ledger.remove_ore(o_pt)
                        continue
                    meta["from_disk"] = False
                except ApiError:
                    continue
            near_nid = min(self.nodes.keys(), key=lambda nid: math.dist(o_pt, self.nodes[nid].anchor))
            self.deferred_ores[o_pt] = DeferredOreRecord(
                pos=o_pt,
                block_name=bname,
                required_tier=int(meta.get("required_tier", 2)),
                nearest_node_id=near_nid,
            )

        ok_nav, _ = self._navigate_to_node(self.active_node_id, min_hp_ref=min_hp_ref)
        if not ok_nav:
            self._clear_active_tree_only()
            return False
        return True

    # ---- Inline Vein Hook & Catch-Block ----

    def _find_peel_blockers(
        self,
        anchor: tuple[int, int, int],
        ore_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> list[tuple[int, int, int]]:
        """Find 1-2 safe solid blocks between anchor and recessed ore to peel for reachability."""
        eye = (anchor[0], anchor[1] + 1, anchor[2])
        cands: list[tuple[float, tuple[int, int, int]]] = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = (ore_pt[0] + ax, ore_pt[1] + ay, ore_pt[2] + az)
            if nb in {anchor, eye, ore_pt}:
                continue
            if abs(nb[1] - anchor[1]) > 3 or (abs(nb[0] - anchor[0]) + abs(nb[2] - anchor[2])) > 4:
                continue
            try:
                b = self._get_block_name(nb, cache)
            except ApiError:
                continue
            if b in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"}:
                continue
            if self._adjacent_fluid_or_falling([nb], cache) is not None:
                continue
            cands.append((math.dist(eye, nb), nb))
        cands.sort(key=lambda r: r[0])
        if not cands:
            return []
        first = cands[0][1]
        if abs(first[0] - anchor[0]) + abs(first[2] - anchor[2]) <= 2:
            return [first]
        # Peel intermediate block closer to eye first, then first
        step_x = first[0] + (1 if anchor[0] > first[0] else (-1 if anchor[0] < first[0] else 0))
        step_z = first[2] + (1 if anchor[2] > first[2] else (-1 if anchor[2] < first[2] else 0))
        mid = (step_x, first[1], step_z)
        if mid not in {anchor, eye, ore_pt, first}:
            try:
                mb = self._get_block_name(mid, cache)
                if mb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([mid], cache) is None:
                    return [mid, first]
            except ApiError:
                pass
        return [first]

    def _mine_vein_ore_cell(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        anchor: tuple[int, int, int],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
        pending_seal_pts: list[tuple[int, int, int]],
    ) -> tuple[bool, int]:
        """Verify fluid safety, place Catch-Block tray if hanging, peel 1-2 blocks if needed, and dig ore."""
        catch_placed = 0
        if pt in self.mined_ore_positions or self.skills._is_block_avoided(pt):
            return False, 0
        try:
            if self._get_block_name(pt, cache) != bname:
                return False, 0
        except ApiError:
            return False, 0

        hazard, adj_fluids = self._inspect_step_hazards([pt], cache)
        if hazard is not None:
            return False, 0
        if adj_fluids:
            sealed_f = self._seal_breach(adj_fluids, cache)
            if len(sealed_f) < len(adj_fluids) and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                return False, 0

        below_pt = (pt[0], pt[1] - 1, pt[2])
        try:
            below_name = self._get_block_name(below_pt, cache)
        except ApiError:
            below_name = "stone"
        if below_name in PASSABLE_BLOCKS and below_pt != anchor and below_pt != (anchor[0], anchor[1] + 1, anchor[2]):
            counts = _inventory_counts(self.bot)
            filler = _pick_seal_material(counts)
            if filler:
                try:
                    self.bot.call("place", {"item": filler, "pos": [below_pt[0], below_pt[1], below_pt[2]]})
                    cache[below_pt] = filler
                    self.catch_blocks_placed.append([below_pt[0], below_pt[1], below_pt[2]])
                    catch_placed += 1
                except ApiError:
                    pass

        req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
        chosen, _, _, total_rem = self._equip_for_block_tier(req_t, for_ore=True)
        if chosen is None or total_rem < 1:
            return False, catch_placed

        dug = False
        try:
            self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": True, "stop_on": ["hurt", "death"]})
            dug = True
        except ApiError as err:
            if (
                err.code == "out_of_reach"
                and bname in target_set
                and abs(pt[1] - anchor[1]) <= 3
                and (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) <= 5
            ):
                peel_pts = self._find_peel_blockers(anchor, pt, cache)
                if peel_pts:
                    peeled_ok = True
                    for pp in peel_pts:
                        ok_p, _, _, _ = self._dig_cell_safely(pp, collect_rock=False, parent_node_id=0, cache=cache)
                        if not ok_p:
                            peeled_ok = False
                            break
                    if peeled_ok:
                        chosen2, _, _, rem2 = self._equip_for_block_tier(req_t, for_ore=True)
                        if chosen2 is not None and rem2 >= 1:
                            try:
                                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": True, "stop_on": ["hurt", "death"]})
                                dug = True
                            except ApiError:
                                pass
        if not dug:
            return False, catch_placed

        cache[pt] = "air"
        self._record_excavated(pt)
        self.mined_ore_positions.add(pt)
        self.deferred_ores.pop(pt, None)
        self.ledger.remove_ore(pt)
        if adj_fluids:
            self._seal_breach(adj_fluids, cache)

        open_neighbors = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
            if abs(nb[0] - anchor[0]) <= 1 and abs(nb[2] - anchor[2]) <= 1:
                continue
            try:
                if self._get_block_name(nb, cache) in AIR_BLOCKS:
                    open_neighbors.append(nb)
            except ApiError:
                pass
        if open_neighbors:
            pending_seal_pts.append(pt)
        return True, catch_placed

    def _run_vein_hook(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        current_node_id: int,
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
        min_target_tier: int = 1,
    ) -> dict[str, Any]:
        """Inspect nearby exposed ores (radius=12): mine reachable veins & feed farther ores into radar."""
        target_set = set(target_blocks)
        inv_raw, _ = _inventory_snapshot_and_counts(self.bot)
        _, max_tier, _ = _pickaxe_inventory_summary(inv_raw)
        focus_target_only = min_target_tier >= 3 and max_tier >= min_target_tier
        query_types = list(target_blocks) if focus_target_only else list(TRACKED_ORES)

        try:
            found = self.bot.call("find_blocks", {"radius": VEIN_HOOK_RADIUS, "types": query_types, "limit": 24})
        except ApiError:
            return {"mined_coords": [], "deferred_added": 0, "catch_placed": 0, "breaches_sealed": 0}

        raw_blocks = found.get("blocks") or []
        mined_coords: list[list[int]] = []
        pending_seal_pts: list[tuple[int, int, int]] = []
        mined_block_names: set[str] = set()
        deferred_added = 0
        catch_placed = 0
        breaches_sealed = 0

        candidates: list[tuple[int, float, tuple[int, int, int], str]] = []
        for blk in raw_blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if not bname or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if pt in self.mined_ore_positions or self.skills._is_block_avoided(pt):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="vein_hook") is not None:
                continue
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            if bname in target_set:
                self.ledger.record_ore(pt, bname, req_t)
            max_y_diff = 4 if bname in target_set else 3
            max_xz_diff = 7 if bname in target_set else 5
            if abs(pt[1] - anchor[1]) > max_y_diff or (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) > max_xz_diff:
                if bname in target_set and req_t <= max_tier and pt not in self.radar_targets:
                    self.radar_targets.append(pt)
                elif req_t > max_tier and pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, current_node_id)
                    deferred_added += 1
                continue
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(
                        pos=pt,
                        block_name=bname,
                        required_tier=req_t,
                        nearest_node_id=current_node_id,
                    )
                    deferred_added += 1
                continue
            if bname not in target_set:
                if focus_target_only:
                    continue
                if bname not in {"coal_ore", "deepslate_coal_ore", "iron_ore", "deepslate_iron_ore", "diamond_ore", "deepslate_diamond_ore"}:
                    continue
            prio = 0 if bname in target_set else 1
            dist = math.dist(anchor, pt)
            candidates.append((prio, dist, pt, bname))

        candidates.sort(key=lambda r: (r[0], r[1]))
        vein_queue: deque[tuple[tuple[int, int, int], str]] = deque()
        queued_pts: set[tuple[int, int, int]] = set()
        for _, _, pt, bname in candidates[:6]:
            if pt not in queued_pts:
                vein_queue.append((pt, bname))
                queued_pts.add(pt)

        vein_limit = 16
        while vein_queue and len(mined_coords) < vein_limit:
            pt, bname = vein_queue.popleft()
            ok_ore, catch_delta = self._mine_vein_ore_cell(
                pt,
                bname,
                anchor=anchor,
                target_set=target_set,
                cache=cache,
                pending_seal_pts=pending_seal_pts,
            )
            catch_placed += catch_delta
            if not ok_ore:
                continue
            mined_coords.append([pt[0], pt[1], pt[2]])
            mined_block_names.add(bname)
            if bname in target_set:
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
                    if nb in queued_pts or nb in self.mined_ore_positions or self.skills._is_block_avoided(nb):
                        continue
                    if geofence is not None and geofence.check_point(nb[0] + 0.5, nb[2] + 0.5, action="vein_hook") is not None:
                        continue
                    if abs(nb[0] - anchor[0]) + abs(nb[1] - anchor[1]) + abs(nb[2] - anchor[2]) > 7:
                        continue
                    try:
                        nb_name = self._get_block_name(nb, cache)
                    except ApiError:
                        continue
                    if nb_name in target_set:
                        vein_queue.append((nb, nb_name))
                        queued_pts.add(nb)

        if mined_coords:
            mined_pt_set = {(c[0], c[1], c[2]) for c in mined_coords}
            filtered_seal_pts = [pt for pt in pending_seal_pts if pt not in mined_pt_set or not any(
                (pt[0] + ax, pt[1] + ay, pt[2] + az) in mined_pt_set
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
            )]
            expected = _expected_drops_for_blocks(list(mined_block_names))
            if expected:
                try:
                    self.skills._collect_nearby_items(
                        bot_y=float(anchor[1]),
                        radius=5,
                        max_pickups=4,
                        target_items=expected,
                    )
                except ApiError:
                    pass
            if filtered_seal_pts:
                sealed = self._seal_breach(filtered_seal_pts, cache)
                breaches_sealed += len(sealed)
            self._move_exact(anchor)
            self._equip_for_block_tier(1, for_ore=False)

        return {
            "mined_coords": mined_coords,
            "deferred_added": deferred_added,
            "catch_placed": catch_placed,
            "breaches_sealed": breaches_sealed,
        }

    # ---- Edge Excavators ----

    def _dig_step_cells(
        self,
        cells: list[tuple[int, int, int]],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[bool, str | None, list[list[int]], int]:
        mined_ores: list[list[int]] = []
        deferred_added = 0
        for dpos in cells:
            ok_d, err_d, was_ore, def_delta = self._dig_cell_safely(
                dpos,
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
            )
            deferred_added += def_delta
            if was_ore:
                mined_ores.append([dpos[0], dpos[1], dpos[2]])
            if not ok_d:
                return False, err_d or "dig_failed", mined_ores, deferred_added
        return True, None, mined_ores, deferred_added

    def _move_step_with_falling_recovery(
        self,
        step_pt: tuple[int, int, int],
        *,
        head_cells: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        if self._move_exact(step_pt):
            return True
        cleared_any = False
        for cpos in head_cells:
            cache.pop(cpos, None)
            try:
                cur_b = self._get_block_name(cpos, cache)
            except ApiError:
                continue
            if cur_b in FALLING_BLOCKS:
                ok_c, _, _, _ = self._dig_cell_safely(cpos, collect_rock=False, parent_node_id=0, cache=cache)
                if ok_c:
                    cleared_any = True
        return self._move_exact(step_pt) if cleared_any else False

    def _pickaxe_redline_threshold(self, cur_y: int, counts_now: dict[str, int]) -> int:
        if self.collar_node_id in self.nodes:
            collar_y = self.nodes[self.collar_node_id].anchor[1]
            if collar_y - cur_y >= 10 and _has_collar_resupply_materials(counts_now):
                return 12
        return 6

    def _dig_stair_down_edge(
        self,
        *,
        direction: str,
        max_steps: int,
        target_y: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        waypoints: list[tuple[int, int, int]] = []
        cache: dict[tuple[int, int, int], str] = {}
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints.append(start_anchor)

        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            if by <= target_y:
                stop_reason = "reached_target_y"
                break

            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_stair") is not None:
                stop_reason = "geofence_blocked"
                break

            cur_head = (bx, by + 1, bz)
            head_ok, head_err, head_ores, head_def = self._dig_step_cells(
                [cur_head],
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
            )
            deferred_added += head_def
            mined_ores.extend(head_ores)
            if not head_ok:
                stop_reason = head_err or "dig_failed"
                break

            support_pt = (nx, by - 2, nz)
            try:
                support_blk = self._get_block_name(support_pt, cache)
                support_ok = self._is_solid_floor(support_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            down_targets = [(nx, by + 1, nz), (nx, by, nz), (nx, by - 1, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(down_targets, cache, forward_delta=(dx, dz))
            if hazard is not None or (support_blk == "bedrock" and by - 1 > target_y) or support_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                breach_pts = [(nx, by, nz), (nx, by + 1, nz)]
                sealed = self._seal_breach(breach_pts, cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else ("fluid_or_bedrock" if support_blk in FLUID_BLOCKS | {"bedrock"} else "cave_or_drop_ahead")
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not support_ok:
                is_self_cav = self._is_self_cavity([support_pt, *down_targets])
                try:
                    below_sup_solid = self._is_solid_floor((nx, by - 3, nz), cache)
                except ApiError:
                    below_sup_solid = False
                can_bridge = is_self_cav or (below_sup_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break
                need_bridge = True

            if adj_fluids:
                # If no seal material yet and side fluid is lava while target cells are solid, don't unleash lava
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                pre_names = [self._get_block_name(p, cache) for p in down_targets]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if (
                pre_names[1] in PASSABLE_BLOCKS
                and pre_names[2] in PASSABLE_BLOCKS
                and hostile_near
                and not self._is_self_cavity(down_targets[1:])
            ):
                sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = "hostile_in_cave_ahead"
                break

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                down_targets,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                # Ensure corridor itself did not flood with unsealed fluid
                flooded = False
                for dp in down_targets:
                    cache.pop(dp, None)
                    if self._get_block_name(dp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([dp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(support_pt, cache) or not self._is_solid_floor(support_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break

            step_pt = (nx, by - 1, nz)
            if not self._move_step_with_falling_recovery(step_pt, head_cells=down_targets[:2], cache=cache):
                stop_reason = "movement_not_confirmed"
                break
            steps_done += 1
            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }

    def _dig_level_edge(
        self,
        *,
        direction: str,
        steps: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        cache: dict[tuple[int, int, int], str] = {}
        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints: list[tuple[int, int, int]] = [start_anchor]
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        for _ in range(steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_tunnel") is not None:
                stop_reason = "geofence_blocked"
                break

            floor_pt = (nx, by - 1, nz)
            try:
                floor_blk = self._get_block_name(floor_pt, cache)
                floor_ok = self._is_solid_floor(floor_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            faces = [(nx, by + 1, nz), (nx, by, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(faces, cache, forward_delta=(dx, dz))
            if hazard is not None or floor_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else "fluid_or_bedrock"
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not floor_ok:
                is_self_cav = self._is_self_cavity([floor_pt, *faces])
                try:
                    below_fl_solid = self._is_solid_floor((nx, by - 2, nz), cache)
                except ApiError:
                    below_fl_solid = False
                can_bridge = is_self_cav or (below_fl_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break
                need_bridge = True

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                face_names = [self._get_block_name(p, cache) for p in faces]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if face_names[0] in PASSABLE_BLOCKS and face_names[1] in PASSABLE_BLOCKS:
                far_floor = (nx + dx, by - 1, nz + dz)
                is_known_intersection = self._is_self_cavity([*faces, far_floor])
                if hostile_near and not is_known_intersection:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "hostile_in_cave_ahead"
                    break
                if not is_known_intersection:
                    try:
                        if not self._is_solid_floor(far_floor, cache):
                            sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                            breaches_sealed += len(sealed)
                            stop_reason = "cave_breach_sealed"
                            break
                    except ApiError:
                        pass

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                faces,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                flooded = False
                for fp in faces:
                    cache.pop(fp, None)
                    if self._get_block_name(fp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([fp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(floor_pt, cache) or not self._is_solid_floor(floor_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break

            step_pt = (nx, by, nz)
            if not self._move_step_with_falling_recovery(step_pt, head_cells=faces, cache=cache):
                stop_reason = "movement_not_confirmed"
                break
            steps_done += 1
            if steps_done % 3 == 0:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if target_blocks:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }

    # ---- Tree Construction, Radar Steering & Offset Retry ----

    def _create_node(
        self,
        *,
        kind: str,
        anchor: tuple[int, int, int],
        parent_id: int | None,
        edge_from_parent: ShaftEdge | None,
        branches: list[BranchSpec],
    ) -> ShaftNode:
        nid = self.next_node_id
        self.next_node_id += 1
        node = ShaftNode(
            node_id=nid,
            kind=kind,
            anchor=anchor,
            parent_id=parent_id,
            edge_from_parent=edge_from_parent,
            branches=branches,
        )
        self.nodes[nid] = node
        self.active_node_id = nid
        self._record_corridor(anchor)
        for rec in self.deferred_ores.values():
            old_node = self.nodes.get(rec.nearest_node_id)
            if old_node is None or math.dist(rec.pos, anchor) < math.dist(rec.pos, old_node.anchor):
                rec.nearest_node_id = nid
        return node

    def _init_collar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_y: int,
        geofence: Any,
        viable_dirs: list[str] | None = None,
    ) -> ShaftNode:
        cands = list(viable_dirs) if viable_dirs else self._probe_collar_dirs(anchor, geofence=geofence)
        if not cands:
            cands = ["north", "east", "south", "west"]
        branches: list[BranchSpec] = []
        depth = max(4, anchor[1] - target_y)
        for d in cands:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                continue
            branches.append(BranchSpec(kind="stair_down", direction=d, steps=depth))
        if not branches:
            branches.append(BranchSpec(kind="stair_down", direction="north", steps=depth))
        node = self._create_node(kind="collar", anchor=anchor, parent_id=None, edge_from_parent=None, branches=branches)
        self.collar_node_id = node.node_id
        return node

    def _spine_branches(self, forward_dir: str, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[forward_dir]
        raw_specs: list[tuple[str, str, int]] = [
            ("branch_leaf", left_d, 8),
            ("branch_leaf", right_d, 8),
            ("spine_forward", forward_dir, 4),
        ]

        rt = self._best_radar_target_for_anchor(anchor, at_spine=True)
        extra_descent: BranchSpec | None = None
        if rt is not None:
            projs = {d: self._radar_direction_projection(anchor, d, at_spine=True) for d in (left_d, right_d, forward_dir)}
            best_d, best_proj = max(projs.items(), key=lambda kv: kv[1])
            if best_proj > 2.0:
                # If target ore is far along a lateral leaf direction, steer the Spine directly toward it
                if best_d != forward_dir and best_proj > 7.0:
                    other_lat = right_d if best_d == left_d else left_d
                    steer_steps = min(8, max(4, int(best_proj)))
                    raw_specs = [
                        ("spine_forward", best_d, steer_steps),
                        ("branch_leaf", forward_dir, 6),
                        ("branch_leaf", other_lat, 8),
                    ]
                else:
                    raw_specs.sort(key=lambda item: (-projs.get(item[1], 0.0)))
                if rt[1] < anchor[1] - 4:
                    dx_r, dz_r = DIRECTIONS[best_d]
                    if self._geofence_blocked(geofence, anchor[0] + dx_r * 3, anchor[2] + dz_r * 3, action="mine_dfs_spine_drop") is None:
                        extra_descent = BranchSpec(
                            kind="stair_down",
                            direction=best_d,
                            steps=min(16, max(3, anchor[1] - rt[1])),
                        )

        out: list[BranchSpec] = []
        if extra_descent is not None:
            out.append(extra_descent)
        for kind, d, ln in raw_specs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                continue
            out.append(BranchSpec(kind=kind, direction=d, steps=ln))
        return out

    def _switchback_branches(self, prev_dir: str, cur_y: int, target_y: int, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[prev_dir]
        cand_dirs = [left_d, right_d]
        cand_dirs.sort(key=lambda d: -self._radar_direction_projection(anchor, d, at_spine=False))
        depth = max(3, cur_y - target_y)
        out: list[BranchSpec] = []
        for d in cand_dirs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_switchback") is not None:
                continue
            out.append(BranchSpec(kind="stair_down", direction=d, steps=depth))
        return out

    def _enqueue_offset_retry_if_eligible(
        self,
        node: ShaftNode,
        failed_branch: BranchSpec,
        prune_reason: str,
        geofence: Any,
    ) -> bool:
        """Instead of permanently killing a blocked stair descent on 1st strike, enqueue a 3-block lateral sidestep retry."""
        if failed_branch.offset_tried or failed_branch.kind != "stair_down" or node.kind == "spine":
            return False
        if prune_reason not in {
            "cave_or_drop_ahead",
            "unsafe_floor_or_cave",
            "cave_breach_sealed",
            "fluid_or_bedrock",
            "falling_material_adjacent",
        }:
            return False

        left_d, right_d = LEFT_RIGHT_DIRS[failed_branch.direction]
        for lat_d in (left_d, right_d):
            lx, lz = DIRECTIONS[lat_d]
            if self._geofence_blocked(geofence, node.anchor[0] + lx * 3, node.anchor[2] + lz * 3, action="mine_dfs_offset") is not None:
                continue
            already = any(
                b.kind == "offset_retry" and b.direction == failed_branch.direction and b.sidestep_dir == lat_d
                for b in node.branches
            )
            if already:
                continue
            node.branches.append(
                BranchSpec(
                    kind="offset_retry",
                    direction=failed_branch.direction,
                    steps=failed_branch.steps,
                    offset_tried=True,
                    sidestep_dir=lat_d,
                    resume_kind=failed_branch.kind,
                )
            )
            return True
        return False

    def _maybe_place_torch_at_node(self, node: ShaftNode) -> bool:
        if node.torch_placed:
            return False
        counts = _inventory_counts(self.bot)
        if counts.get("torch", 0) <= 0:
            return False
        tpos = [node.anchor[0], node.anchor[1] + 1, node.anchor[2]]
        try:
            self.bot.call("place", {"item": "torch", "pos": tpos})
            node.torch_placed = True
            return True
        except ApiError:
            return False

    def _maybe_return_collar_for_resupply(
        self,
        counts_now: dict[str, int],
        *,
        min_hp_ref: list[float],
    ) -> bool:
        """When pickaxe_low hits deep underground and player lacks a portable table, backtrack 0-dig to Collar."""
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            return False
        if counts_now.get("crafting_table", 0) >= 1:
            return False
        total_planks = sum(v for k, v in counts_now.items() if k.endswith("_planks"))
        total_logs = sum(v for k, v in counts_now.items() if k.endswith(("_log", "_stem")))
        if total_planks >= 4 or total_logs >= 1:
            return False
        if not _has_collar_resupply_materials(counts_now) and self.ledger.nearest_utility("crafting_table", self.nodes[self.collar_node_id].anchor, max_dist=16.0) is None:
            return False
        ok_ret, _ = self._navigate_to_node(self.collar_node_id, min_hp_ref=min_hp_ref)
        return ok_ret

    # ---- Public Entrypoint ----

    def run(
        self,
        *,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        max_duration_s: float = 32.0,
    ) -> dict[str, Any]:
        act = str(action or "explore").strip().lower()
        if act == "reset":
            return self.reset(geofence=geofence)
        if act == "status":
            return {"ok": True, "action": "status", "tree": self.snapshot()}

        profile = _resolve_target_profile(target, target_y)
        self.target_y = profile["target_y"]
        target_blocks = profile["target_blocks"]
        expected_items = profile["expected_items"]
        goal_count = max(1, min(64, int(goal_count)))
        max_duration_s = max(5.0, min(90.0, float(max_duration_s)))

        st0 = self.bot.status()
        if st0.get("alive") is False:
            return {"ok": False, "requires_replan": True, "stop_reason": "dead", "tree": self.snapshot()}
        if st0.get("in_water"):
            esc = self.skills.escape_water()
            if esc.get("in_water"):
                return {"ok": False, "requires_replan": True, "stop_reason": "in_water", "water_escape": esc}
            st0 = self.bot.status()

        cur_pos = _floor_pos(st0.get("pos") or [0, 64, 0])
        min_hp_ref = [float(st0.get("health") if isinstance(st0.get("health"), (int, float)) else 20.0)]

        if act == "return_collar":
            if self.collar_node_id is None or self.collar_node_id not in self.nodes:
                return {"ok": False, "requires_replan": True, "stop_reason": "no_collar_recorded", "tree": self.snapshot()}
            dug_before = self.dug_during_return
            ok_ret, err_ret = self._navigate_to_node(self.collar_node_id, min_hp_ref=min_hp_ref)
            st_after = self.bot.status()
            return {
                "ok": ok_ret,
                "requires_replan": not ok_ret,
                "action": "return_collar",
                "stop_reason": "returned_to_collar" if ok_ret else (err_ret or "return_failed"),
                "pos_before": st0.get("pos"),
                "pos_after": st_after.get("pos"),
                "collar_anchor": list(self.nodes[self.collar_node_id].anchor),
                "dug_during_return": self.dug_during_return - dug_before,
                "tree": self.snapshot(),
            }

        if act != "explore":
            return {"ok": False, "error": "bad_args", "message": f"未知 action={action!r}，支持 explore / return_collar / reset / status。"}

        inv0, counts0 = _inventory_snapshot_and_counts(self.bot)
        picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        if not picks0 or total_rem0 <= 6:
            return {
                "ok": False,
                "requires_replan": True,
                "stop_reason": "pickaxe_low",
                "message": "可用镐缺失或总剩余耐久过低，请先补充石镐/铁镐再执行 MiningDFS。",
                "tree": self.snapshot(),
            }
        if max_tier0 < int(profile["min_tier"]):
            return {
                "ok": False,
                "requires_replan": True,
                "stop_reason": "insufficient_pickaxe_tier",
                "required_tier": int(profile["min_tier"]),
                "current_max_tier": max_tier0,
                "message": f"MiningDFS 目标 {profile['profile_key']} 需要至少等级 {profile['min_tier']} 的镐，当前背包最高镐等级为 {max_tier0}，请先升镐或改采低阶矿。",
                "tree": self.snapshot(),
            }

        deadline = time.monotonic() + max_duration_s
        # Run 64-block ore radar up front so Collar & Spine steer toward known veins
        self._scan_ore_radar(cur_pos, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence, force=True)

        # Tree initialization, WorldLedger shaft restoration, or resume from Collar / current node
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            if not self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref):
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if anchor_err is not None and not viable_dirs:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "stop_reason": anchor_err,
                        "message": (
                            "当前脚下为树冠/悬空地形，DFS 无法开井（周围 20 格无安全实心地面）。"
                            "请先脱离树冠：用 mc_skill_staircase(vertical='down') 或 mc_skill_unstick 向下挖到实心地面，"
                            "必要时用 mc_move_to 平移到开阔实心陆地，确认脚下为草地/石头后再调用 mc_skill_mine_dfs 重新开井。"
                        ),
                        "pos_before": st0.get("pos"),
                        "pos_after": self.bot.status().get("pos"),
                        "tree": self.snapshot(),
                    }
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
        else:
            collar = self.nodes[self.collar_node_id]
            active = self.nodes.get(self.active_node_id or -1)
            if not self._tree_has_pending_branches():
                # Previous tree already exhausted -> archive, physically relocate, and start a fresh Collar!
                self._archive_current_shaft_to_ledger(mark_exhausted=True)
                self._clear_active_tree_only()
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if anchor_err is not None and not viable_dirs:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "stop_reason": "shaft_exhausted",
                        "message": "当前矿井已枯竭且附近无安全新井落点，请先用 mc_move_to 移动 20 格以上再调用 explore。",
                        "tree": self.snapshot(),
                    }
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            elif active is None:
                self.active_node_id = collar.node_id
            elif math.dist(cur_pos, active.anchor) > 6.0 and math.hypot(cur_pos[0] - collar.anchor[0], cur_pos[2] - collar.anchor[2]) > 48.0:
                # Far relocation (e.g. respawned at distant spawn): save old shaft to WorldLedger instead of losing it
                self._archive_current_shaft_to_ledger(mark_exhausted=False)
                self._clear_active_tree_only()
                if not self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref):
                    solid_pos, viable_dirs, _ = self._ensure_solid_collar_anchor(
                        cur_pos,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                    )
                    self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            elif math.dist(cur_pos, active.anchor) > 3.5:
                # Within 48 blocks of Collar (e.g. respawned nearby or walked to smelt/craft): walk back & descend 0-dig!
                ok_nav, err_nav = self._navigate_to_node(active.node_id, min_hp_ref=min_hp_ref)
                if not ok_nav:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "stop_reason": err_nav or "resume_navigation_failed",
                        "tree": self.snapshot(),
                    }

        mined_ores: list[list[int]] = []
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        stop_reason = "time_slice_progress"
        auto_relocations_this_slice = 0

        def _gained_target_count(after_counts: dict[str, int]) -> int:
            delta = _positive_inv_delta(counts0, after_counts)
            return sum(int(delta.get(item_n, 0)) for item_n in expected_items)

        min_target_tier = int(profile["min_tier"])

        def _quota_met() -> bool:
            return _gained_target_count(_inventory_counts(self.bot)) >= goal_count

        # Priority 1: harvest unlocked deferred ores whose required tier is now satisfied!
        unlockable = [
            rec for rec in sorted(self.deferred_ores.values(), key=lambda r: (r.required_tier, r.nearest_node_id))
            if rec.block_name in target_blocks and rec.required_tier <= max_tier0
        ]
        for rec in unlockable:
            if time.monotonic() >= deadline:
                break
            if rec.nearest_node_id not in self.nodes:
                continue
            ok_nav, err_nav = self._navigate_to_node(rec.nearest_node_id, min_hp_ref=min_hp_ref)
            if not ok_nav:
                stop_reason = err_nav or "deferred_nav_failed"
                break
            hook = self._run_vein_hook(
                self.nodes[rec.nearest_node_id].anchor,
                target_blocks=target_blocks,
                current_node_id=rec.nearest_node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
            )
            mined_ores.extend(hook["mined_coords"])
            catch_placed += hook["catch_placed"]
            breaches_sealed += hook["breaches_sealed"]
            if rec.pos in self.deferred_ores:
                chosen_d, _, _, _ = self._equip_for_block_tier(rec.required_tier, for_ore=True)
                if chosen_d is not None:
                    try:
                        bname_now = self._get_block_name(rec.pos)
                        if bname_now == rec.block_name:
                            self.bot.call("dig", {"pos": list(rec.pos), "collect": True, "stop_on": ["hurt", "death"]})
                            self._record_excavated(rec.pos)
                            self.mined_ore_positions.add(rec.pos)
                            self.deferred_ores.pop(rec.pos, None)
                            self.ledger.remove_ore(rec.pos)
                            mined_ores.append(list(rec.pos))
                            self._move_exact(self.nodes[rec.nearest_node_id].anchor)
                            self._equip_for_block_tier(1, for_ore=False)
                    except ApiError:
                        pass
            if _quota_met():
                stop_reason = "quota_reached"
                break

        # Priority 2: main DFS loop
        while stop_reason != "quota_reached" and time.monotonic() < deadline:
            if self.active_node_id is None or self.active_node_id not in self.nodes:
                stop_reason = "shaft_exhausted"
                break
            node = self.nodes[self.active_node_id]

            # Refresh 64-block ore radar periodically as we move deeper or horizontally
            self._scan_ore_radar(node.anchor, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence)

            hook0 = self._run_vein_hook(
                node.anchor,
                target_blocks=target_blocks,
                current_node_id=node.node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
            )
            mined_ores.extend(hook0["mined_coords"])
            deferred_added += hook0["deferred_added"]
            catch_placed += hook0["catch_placed"]
            breaches_sealed += hook0["breaches_sealed"]
            if _quota_met():
                stop_reason = "quota_reached"
                break

            next_branch = next((b for b in node.branches if b.status == "pending"), None)
            if next_branch is None:
                spine_blocked = any(b.kind == "spine_forward" and b.status == "pruned" for b in node.branches)
                if node.kind == "spine" and node.anchor[1] <= self.target_y + 2 and spine_blocked:
                    promo = max(
                        (
                            b
                            for b in node.branches
                            if b.kind == "branch_leaf"
                            and b.status == "completed"
                            and b.prune_reason != "promoted_to_spine"
                            and len(b.completed_waypoints) >= 3
                        ),
                        key=lambda b: (
                            self._radar_direction_projection(node.anchor, b.direction, at_spine=True),
                            len(b.completed_waypoints),
                        ),
                        default=None,
                    )
                    if promo is not None:
                        promo.prune_reason = "promoted_to_spine"
                        ok_pr, err_pr = self._walk_waypoints(
                            promo.completed_waypoints,
                            reverse=False,
                            min_hp_ref=min_hp_ref,
                        )
                        if not ok_pr:
                            stop_reason = err_pr or "promote_walk_failed"
                            break
                        end_tip = promo.completed_waypoints[-1]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_tip,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("branch_leaf", promo.direction, list(promo.completed_waypoints)),
                            branches=self._spine_branches(promo.direction, end_tip, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        continue

                # All branches at this node completed or pruned -> Backtrack to parent!
                if node.parent_id is None or node.edge_from_parent is None:
                    self._archive_current_shaft_to_ledger(mark_exhausted=True)
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        self._clear_active_tree_only()
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now,
                            geofence=geofence,
                            deadline=deadline,
                            min_hp_ref=min_hp_ref,
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = "shaft_exhausted"
                    break
                ok_bt, err_bt = self._walk_waypoints(node.edge_from_parent.waypoints, reverse=True, min_hp_ref=min_hp_ref)
                if not ok_bt:
                    # Backtrack path blocked by self-placed seal/bridge or cave-in. Instead of a
                    # hard stop (which used to spin for the whole slice on repeated calls), archive
                    # this shaft and try one fresh Collar relocation. Mirrors the shaft_exhausted
                    # recovery above so a single blocked waypoint cannot deadlock the whole mine.
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        self._clear_active_tree_only()
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = err_bt or "backtrack_failed"
                    break
                self.active_node_id = node.parent_id
                continue

            next_branch.status = "active"
            if next_branch.kind == "offset_retry":
                lat_dir = next_branch.sidestep_dir or LEFT_RIGHT_DIRS[next_branch.direction][0]
                resume_k = next_branch.resume_kind or "stair_down"
                edge_res = self._dig_level_edge(
                    direction=lat_dir,
                    steps=3,
                    target_blocks=target_blocks,
                    parent_node_id=node.node_id,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                    min_target_tier=min_target_tier,
                    quota_met=_quota_met,
                )
                steps_done = int(edge_res["steps_done"])
                er = str(edge_res["stop_reason"])
                breaches_sealed += int(edge_res["breaches_sealed"])
                catch_placed += int(edge_res["catch_placed"])
                deferred_added += int(edge_res["deferred_added"])
                mined_ores.extend(edge_res["mined_ores"])

                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done >= 1:
                        end_anchor_o = tuple(edge_res["end_anchor"])
                        self._create_node(
                            kind="spine" if resume_k == "spine_forward" else "switchback",
                            anchor=end_anchor_o,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("offset_sidestep", lat_dir, edge_res["waypoints"]),
                            branches=[
                                BranchSpec(
                                    kind=resume_k,
                                    direction=next_branch.direction,
                                    steps=next_branch.steps,
                                    offset_tried=True,
                                )
                            ],
                        )
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    stop_reason = er
                    break
                if er == "time_slice" and steps_done == 0:
                    next_branch.status = "pending"
                    stop_reason = "time_slice_progress"
                    break
                if steps_done >= 2:
                    next_branch.status = "completed"
                    end_anchor = tuple(edge_res["end_anchor"])
                    retry_branches = [
                        BranchSpec(
                            kind=resume_k,
                            direction=next_branch.direction,
                            steps=next_branch.steps,
                            offset_tried=True,
                        )
                    ]
                    if resume_k == "spine_forward":
                        l_d, r_d = LEFT_RIGHT_DIRS[next_branch.direction]
                        for ld in (l_d, r_d):
                            ldx, ldz = DIRECTIONS[ld]
                            if self._geofence_blocked(geofence, end_anchor[0] + ldx * 6, end_anchor[2] + ldz * 6, action="mine_dfs_spine") is None:
                                retry_branches.append(BranchSpec(kind="branch_leaf", direction=ld, steps=6))
                    child_kind = "spine" if resume_k == "spine_forward" else "switchback"
                    sh_node = self._create_node(
                        kind=child_kind,
                        anchor=end_anchor,
                        parent_id=node.node_id,
                        edge_from_parent=ShaftEdge("offset_sidestep", lat_dir, edge_res["waypoints"]),
                        branches=retry_branches,
                    )
                    self._maybe_place_torch_at_node(sh_node)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)

            elif next_branch.kind == "stair_down":
                edge_res = self._dig_stair_down_edge(
                    direction=next_branch.direction,
                    max_steps=next_branch.steps,
                    target_y=self.target_y,
                    target_blocks=target_blocks,
                    parent_node_id=node.node_id,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                    min_target_tier=min_target_tier,
                    quota_met=_quota_met,
                )
                steps_done = int(edge_res["steps_done"])
                er = str(edge_res["stop_reason"])
                breaches_sealed += int(edge_res["breaches_sealed"])
                catch_placed += int(edge_res["catch_placed"])
                deferred_added += int(edge_res["deferred_added"])
                mined_ores.extend(edge_res["mined_ores"])

                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done > 0:
                        end_anchor_q = tuple(edge_res["end_anchor"])
                        rem_dq = max(1, end_anchor_q[1] - self.target_y)
                        q_node = self._create_node(
                            kind="spine" if end_anchor_q[1] <= self.target_y + 2 else "switchback",
                            anchor=end_anchor_q,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor_q, geofence)
                                if end_anchor_q[1] <= self.target_y + 2
                                else [BranchSpec("stair_down", next_branch.direction, rem_dq)]
                                + self._switchback_branches(next_branch.direction, end_anchor_q[1], self.target_y, end_anchor_q, geofence)
                            ),
                        )
                        self._maybe_place_torch_at_node(q_node)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done > 0:
                        end_anchor_p = tuple(edge_res["end_anchor"])
                        rem_d = max(1, end_anchor_p[1] - self.target_y)
                        next_branch.status = "completed"
                        self._create_node(
                            kind="spine" if end_anchor_p[1] <= self.target_y + 2 else "switchback",
                            anchor=end_anchor_p,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor_p, geofence)
                                if end_anchor_p[1] <= self.target_y + 2
                                else [BranchSpec("stair_down", next_branch.direction, rem_d)]
                                + self._switchback_branches(next_branch.direction, end_anchor_p[1], self.target_y, end_anchor_p, geofence)
                            ),
                        )
                    stop_reason = er
                    break

                end_anchor = tuple(edge_res["end_anchor"])
                if er in {"reached_target_y", "completed"} and end_anchor[1] <= self.target_y + 2:
                    next_branch.status = "completed"
                    spine_node = self._create_node(
                        kind="spine",
                        anchor=end_anchor,
                        parent_id=node.node_id,
                        edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                        branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                    )
                    self._maybe_place_torch_at_node(spine_node)
                elif er == "time_slice" and steps_done == 0:
                    next_branch.status = "pending"
                    stop_reason = "time_slice_progress"
                    break
                elif er in {"time_slice", "completed"} and steps_done > 0:
                    next_branch.status = "completed"
                    if end_anchor[1] > self.target_y:
                        rem_depth = max(1, end_anchor[1] - self.target_y)
                        cont_branches = [BranchSpec("stair_down", next_branch.direction, rem_depth)]
                        cont_branches.extend(
                            self._switchback_branches(next_branch.direction, end_anchor[1], self.target_y, end_anchor, geofence)
                        )
                        self._create_node(
                            kind="switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                            branches=cont_branches,
                        )
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done >= 2 and end_anchor[1] <= self.target_y + 2:
                        left_d, _ = LEFT_RIGHT_DIRS[next_branch.direction]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(left_d, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                    elif steps_done >= 2 and end_anchor[1] > self.target_y:
                        sw_branches = self._switchback_branches(next_branch.direction, end_anchor[1], self.target_y, end_anchor, geofence)
                        if sw_branches:
                            sw_node = self._create_node(
                                kind="switchback",
                                anchor=end_anchor,
                                parent_id=node.node_id,
                                edge_from_parent=ShaftEdge("stair_down", next_branch.direction, edge_res["waypoints"]),
                                branches=sw_branches,
                            )
                            self._maybe_place_torch_at_node(sw_node)
                        else:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    else:
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                        else:
                            self._move_exact(node.anchor)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)

            else:
                # "branch_leaf" or "spine_forward"
                edge_res = self._dig_level_edge(
                    direction=next_branch.direction,
                    steps=next_branch.steps,
                    target_blocks=target_blocks,
                    parent_node_id=node.node_id,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                    min_target_tier=min_target_tier,
                    quota_met=_quota_met,
                )
                steps_done = int(edge_res["steps_done"])
                er = str(edge_res["stop_reason"])
                breaches_sealed += int(edge_res["breaches_sealed"])
                catch_placed += int(edge_res["catch_placed"])
                deferred_added += int(edge_res["deferred_added"])
                mined_ores.extend(edge_res["mined_ores"])

                if er == "quota_reached":
                    next_branch.status = "completed"
                    if next_branch.kind == "spine_forward" and steps_done >= 1:
                        end_anchor_q = tuple(edge_res["end_anchor"])
                        q_sp = self._create_node(
                            kind="spine",
                            anchor=end_anchor_q,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_q, geofence),
                        )
                        self._maybe_place_torch_at_node(q_sp)
                    elif next_branch.kind == "branch_leaf" and steps_done > 0:
                        if steps_done >= 2:
                            next_branch.completed_waypoints = list(edge_res["waypoints"])
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done >= 2 and next_branch.kind == "spine_forward":
                        next_branch.status = "completed"
                        end_anchor_s = tuple(edge_res["end_anchor"])
                        self._create_node(
                            kind="spine",
                            anchor=end_anchor_s,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_s, geofence),
                        )
                    elif steps_done > 0 and next_branch.kind == "branch_leaf":
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = er
                    break

                if next_branch.kind == "branch_leaf":
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    next_branch.status = "completed" if er in {"completed", "time_slice"} else "pruned"
                    if er in {"completed", "time_slice"} and steps_done >= 2:
                        next_branch.completed_waypoints = list(edge_res["waypoints"])
                    if er not in {"completed", "time_slice"}:
                        next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    # "spine_forward": advance main Spine anchor!
                    end_anchor = tuple(edge_res["end_anchor"])
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    if er in {"completed", "time_slice"} and (steps_done >= 2 or (er == "time_slice" and steps_done >= 1)):
                        next_branch.status = "completed"
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        if er == "time_slice":
                            stop_reason = "time_slice_progress"
                            break
                    else:
                        next_branch.status = "pruned"
                        next_branch.prune_reason = er
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)

            if _quota_met():
                stop_reason = "quota_reached"
                break

        inv_after, counts_after = _inventory_snapshot_and_counts(self.bot)
        gained_delta = _positive_inv_delta(counts0, counts_after)
        target_gained = _gained_target_count(counts_after)
        if target_gained >= goal_count:
            stop_reason = "quota_reached"

        returned_collar_on_low = False
        if stop_reason == "pickaxe_low":
            returned_collar_on_low = self._maybe_return_collar_for_resupply(counts_after, min_hp_ref=min_hp_ref)

        # Persist shaft progress and WorldLedger state
        self._archive_current_shaft_to_ledger(mark_exhausted=(stop_reason == "shaft_exhausted"))

        # Restore rock pickaxe on completion so tool readiness stays clean
        self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_after)
        st_end = self.bot.status()

        ok = stop_reason in {"quota_reached", "time_slice_progress"}
        out_res: dict[str, Any] = {
            "ok": ok,
            "requires_replan": not ok or stop_reason == "hurt",
            "action": act,
            "target": profile["profile_key"],
            "target_y": self.target_y,
            "goal_count": goal_count,
            "target_gained": target_gained,
            "gained": gained_delta,
            "stop_reason": stop_reason,
            "pos_before": st0.get("pos"),
            "pos_after": st_end.get("pos"),
            "mined_ores": mined_ores,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "tree": self.snapshot(),
        }
        if returned_collar_on_low:
            out_res["returned_to_collar_for_resupply"] = True
        if stop_reason == "shaft_exhausted":
            out_res["message"] = "当前矿井分支与偏移重试均已穷尽；下次调用 reset 或 explore 将换点重开，建议先用 mc_move_to 移动 16 格以上到新区域。"
        return out_res
