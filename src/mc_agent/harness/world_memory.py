"""Single-bot spatial & logistics memory: anchors, storage chests, non-ore POIs, biomes/habitats, and chunk footprints."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
import dataclasses
import json
import math
import os
import pathlib
import re
import time
from typing import Any

from ..kb import normalize_dimension


def _cardinal_bearing(dx: float, dz: float) -> str:
    if abs(dx) < 0.5 and abs(dz) < 0.5:
        return "脚下"
    if abs(dx) >= abs(dz):
        return "东(+X)" if dx > 0 else "西(-X)"
    return "南(+Z)" if dz > 0 else "北(-Z)"

# Strictly exclude underground `*_ore` veins so WorldMemory stays 100% decoupled from MiningDFS!
TRACKED_POI_BLOCKS: frozenset[str] = frozenset(
    {
        # High-value surface / structure blocks & portals
        "gold_block",
        "raw_gold_block",
        "iron_block",
        "raw_iron_block",
        "diamond_block",
        "emerald_block",
        "obsidian",
        "crying_obsidian",
        "nether_portal",
        "end_portal_frame",
        "spawner",
        "amethyst_cluster",
        "budding_amethyst",
        "sponge",
        "wet_sponge",
        "bookshelf",
        "hay_block",
        "bell",
        # Fluids
        "lava",
        "water",
        # Base & village utility stations
        "crafting_table",
        "furnace",
        "blast_furnace",
        "smoker",
        "chest",
        "trapped_chest",
        "barrel",
        "enchanting_table",
        "anvil",
        "chipped_anvil",
        "damaged_anvil",
        "grindstone",
        "smithing_table",
        "brewing_stand",
        "lectern",
        "stonecutter",
        "loom",
        "beacon",
        "lodestone",
        "respawn_anchor",
        # Scarce / agricultural surface plants
        "sugar_cane",
        "bamboo",
        "cactus",
        "pumpkin",
        "carved_pumpkin",
        "melon",
        "sweet_berry_bush",
        "nether_wart",
        "bee_nest",
        "beehive",
    }
)

CONTAINER_BLOCKS: frozenset[str] = frozenset({"chest", "trapped_chest", "barrel"})
FLUID_CLUSTER_BLOCKS: frozenset[str] = frozenset({"water", "lava"})
EXPLORATION_CELL_SHIFT: int = 6  # 64x64 blocks = 4x4 chunks (aligned with 64m API scan radius)
DEFAULT_MAX_EXPLORATION_CELLS: int = 2048
EXPLORATION_DIRS_8: tuple[tuple[str, int, int], ...] = (
    ("east", 1, 0),
    ("southeast", 1, 1),
    ("south", 0, 1),
    ("southwest", -1, 1),
    ("west", -1, 0),
    ("northwest", -1, -1),
    ("north", 0, -1),
    ("northeast", 1, -1),
)
VILLAGE_ELIGIBLE_BIOME_TOKENS: tuple[str, ...] = (
    "plains",
    "meadow",
    "desert",
    "savanna",
    "taiga",
    "snowy_plains",
    "sunflower_plains",
)
NON_VILLAGE_BIOME_TOKENS: tuple[str, ...] = (
    "ocean",
    "river",
    "beach",
    "birch",
    "jungle",
    "swamp",
    "mangrove",
    "badlands",
    "dark_forest",
    "cherry",
    "pale_garden",
    "mushroom",
    "peaks",
    "stony",
)


def _classify_village_eligible_biome(biome: str) -> bool | None:
    b = _clean_name(biome)
    if not b or b == "?":
        return None
    if any(tok in b for tok in NON_VILLAGE_BIOME_TOKENS):
        return False
    if any(tok in b for tok in VILLAGE_ELIGIBLE_BIOME_TOKENS):
        return True
    return None
HABITAT_MOB_TYPES: frozenset[str] = frozenset(
    {
        "villager",
        "wandering_trader",
        "iron_golem",
        "sheep",
        "cow",
        "mooshroom",
        "pig",
        "chicken",
        "horse",
        "donkey",
        "wolf",
        "cat",
        "bee",
        "allay",
        "strider",
        "piglin",
        "blaze",
    }
)

# Upstream block equivalents for crafting/goal shortage hints (item_needed -> memory POI blocks)
ITEM_TO_MEMORY_POI_SOURCES: dict[str, tuple[str, ...]] = {
    "gold_ingot": ("gold_block", "raw_gold_block"),
    "gold_nugget": ("gold_block",),
    "iron_ingot": ("iron_block", "raw_iron_block"),
    "diamond": ("diamond_block",),
    "emerald": ("emerald_block",),
    "obsidian": ("obsidian", "lava"),
    "lava_bucket": ("lava",),
    "water_bucket": ("water",),
    "paper": ("sugar_cane",),
    "book": ("bookshelf", "sugar_cane"),
    "bread": ("hay_block",),
    "wheat": ("hay_block",),
}

DEFAULT_MAX_POIS_PER_TYPE = 32
DEFAULT_MAX_TOTAL_POIS = 512
DEFAULT_UNREACHABLE_COOLDOWN_S = 300.0
DEFAULT_HABITAT_TTL_S = 900.0
DEFAULT_MAX_VISITED_CHUNKS = 4096


def is_tracked_poi_block(block_name: str) -> bool:
    clean = str(block_name or "").removeprefix("minecraft:").strip().lower()
    if not clean or clean.endswith("_ore") or clean == "ancient_debris":
        return False
    if clean.endswith("_bed"):
        return True
    return clean in TRACKED_POI_BLOCKS


def _clean_name(raw: str) -> str:
    return str(raw or "").removeprefix("minecraft:").strip().lower()


def _matches_requested_block(entry_name: str, req_set: set[str]) -> bool:
    if entry_name in req_set:
        return True
    if "bed" in req_set and entry_name.endswith("_bed"):
        return True
    return False


def default_memory_path_for_bot(bot: Any) -> pathlib.Path | None:
    """Resolve `.state/world_memory_<user>.json` for live BotSession while keeping Mock/Fake unit tests in-memory."""
    env_p = os.environ.get("MC_WORLD_MEMORY_PATH", "").strip()
    if env_p:
        return pathlib.Path(env_p)
    client = getattr(bot, "_client", None)
    if client is None:
        return None
    transport = getattr(client, "_transport", None)
    if transport is not None and type(transport).__name__ == "MockTransport":
        return None
    raw_user = str(getattr(bot, "bot_username", None) or "").strip()
    if not raw_user:
        return None
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_user).strip("_") or "default"
    return pathlib.Path(".state") / f"world_memory_{safe_user}.json"


@dataclasses.dataclass
class POIEntry:
    name: str
    dim: str
    pos: tuple[int, int, int]
    seen_at: float
    cooldown_until: float = 0.0
    fail_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dim": self.dim,
            "pos": list(self.pos),
            "seen_at": round(self.seen_at, 2),
        }


class WorldMemory:
    """Lightweight 5-layer single-bot spatial memory with lazy read-time verification and 0 MiningDFS coupling."""

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        env_path = os.environ.get("MC_WORLD_MEMORY_PATH", "").strip()
        resolved = path if path is not None else (env_path if env_path else None)
        self.path: pathlib.Path | None = pathlib.Path(resolved) if resolved else None

        # Layer 1: Core Anchors (spawn, bed, home, portals, last_death)
        self.spawn_points: dict[str, list[int]] = {}
        self.bed_spawn: dict[str, Any] | None = None  # {"dim": str, "pos": [x, y, z]}
        self.home_bases: dict[str, list[int]] = {}  # dim -> [x, y, z]
        self.portals: dict[str, list[list[int]]] = {}  # dim -> [[x, y, z], ...]
        self.last_death: dict[str, Any] | None = None  # {"dim": str, "pos": [x, y, z], "t": float}

        # Layer 2: Storage Containers ((dim, x, y, z) -> {"block": str, "items": {item: count}, "updated_at": float})
        self.containers: dict[tuple[str, int, int, int], dict[str, Any]] = {}

        # Layer 2b: In-flight furnace conversion jobs (wall-clock persisted so a restart keeps the ledger)
        self.conversion_jobs: dict[str, dict[str, Any]] = {}

        # Layer 3: Non-Ore Scarce POIs (LRU OrderedDict keyed by (dim, x, y, z))
        self.pois: OrderedDict[tuple[str, int, int, int], POIEntry] = OrderedDict()

        # Layer 4: Biomes & Mob Habitats
        self.biomes: dict[tuple[str, str], list[list[int]]] = {}  # (dim, biome) -> representative [x, y, z] list
        self.habitats: dict[tuple[str, str], list[dict[str, Any]]] = {}  # (dim, mob) -> [{"pos": [x,y,z], "seen_at": float}]

        # Layer 5: Visited Chunk Footprints for Broad Exploration (16x16 fine grid)
        self.visited_chunks: OrderedDict[tuple[str, int, int], None] = OrderedDict()

        # Layer 6: Coarse 64x64 Exploration Region Map (4x4 chunks, aligned with 64m API scan radius)
        # Key: (dim, rx, rz) -> {"terrain": "land"|"water"|"void_edge", "biome": str,
        #                        "village_eligible": bool|None, "scanned_village": bool, "visits": int}
        self.exploration_cells: OrderedDict[tuple[str, int, int], dict[str, Any]] = OrderedDict()

        self._dirty = False
        self._last_save_ts = 0.0
        self._status_death_primed = False
        self._status_death_sig: tuple[Any, ...] | None = None
        if self.path is not None:
            self.load()

    # ---- Persistence ----

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return

        now_m = time.monotonic()
        anchors = raw.get("anchors") or {}
        if isinstance(anchors, dict):
            for d, p in (anchors.get("spawn_points") or {}).items():
                if isinstance(p, list) and len(p) >= 3:
                    self.spawn_points[str(d)] = [int(p[0]), int(p[1]), int(p[2])]
            bed = anchors.get("bed_spawn")
            if isinstance(bed, dict) and isinstance(bed.get("pos"), list) and len(bed["pos"]) >= 3:
                self.bed_spawn = {
                    "dim": str(bed.get("dim") or "overworld"),
                    "pos": [int(bed["pos"][0]), int(bed["pos"][1]), int(bed["pos"][2])],
                }
            for d, p in (anchors.get("home_bases") or {}).items():
                if isinstance(p, list) and len(p) >= 3:
                    self.home_bases[str(d)] = [int(p[0]), int(p[1]), int(p[2])]
            for d, plist in (anchors.get("portals") or {}).items():
                if isinstance(plist, list):
                    self.portals[str(d)] = [
                        [int(p[0]), int(p[1]), int(p[2])]
                        for p in plist
                        if isinstance(p, list) and len(p) >= 3
                    ][:8]
            ld = anchors.get("last_death")
            if isinstance(ld, dict) and isinstance(ld.get("pos"), list) and len(ld["pos"]) >= 3:
                dp_loaded = [int(ld["pos"][0]), int(ld["pos"][1]), int(ld["pos"][2])]
                self.last_death = {
                    "dim": str(ld.get("dim") or "overworld"),
                    "pos": dp_loaded,
                    "t": now_m,
                }
                self._status_death_primed = True
                self._status_death_sig = (tuple(dp_loaded), None)

        for row in raw.get("containers") or []:
            if not isinstance(row, dict):
                continue
            p = row.get("pos")
            if isinstance(p, list) and len(p) >= 3:
                dim = str(row.get("dim") or "overworld")
                key = (dim, int(p[0]), int(p[1]), int(p[2]))
                items_clean = {
                    _clean_name(k): int(v)
                    for k, v in (row.get("items") or {}).items()
                    if _clean_name(k) and int(v or 0) > 0
                }
                self.containers[key] = {
                    "block": _clean_name(row.get("block") or "chest"),
                    "items": items_clean,
                    "updated_at": now_m,
                }

        wall_now = time.time()
        for row in raw.get("conversion_jobs") or []:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("id") or "").strip()
            p = row.get("pos")
            if not job_id or not isinstance(p, list) or len(p) < 3:
                continue
            try:
                ready_at = float(row.get("ready_at") or 0.0)
            except (TypeError, ValueError):
                ready_at = 0.0
            remaining = int(row.get("remaining", row.get("count") or 0) or 0)
            if remaining <= 0 or ready_at + 1800.0 < wall_now:
                continue
            self.conversion_jobs[job_id] = {
                "id": job_id,
                "kind": str(row.get("kind") or "furnace"),
                "dim": str(row.get("dim") or "overworld"),
                "pos": [int(p[0]), int(p[1]), int(p[2])],
                "input_item": _clean_name(row.get("input_item") or ""),
                "output_item": _clean_name(row.get("output_item") or ""),
                "count": int(row.get("count") or remaining),
                "remaining": remaining,
                "furnace_count": int(row.get("furnace_count") or 1),
                "per_item_s": float(row.get("per_item_s") or 10.0),
                "started_at": float(row.get("started_at") or wall_now),
                "ready_at": ready_at,
            }

        for row in raw.get("pois") or []:
            if not isinstance(row, dict):
                continue
            p = row.get("pos")
            bname = _clean_name(row.get("name") or "")
            if isinstance(p, list) and len(p) >= 3 and is_tracked_poi_block(bname):
                dim = str(row.get("dim") or "overworld")
                pt = (int(p[0]), int(p[1]), int(p[2]))
                self.pois[(dim, pt[0], pt[1], pt[2])] = POIEntry(
                    name=bname,
                    dim=dim,
                    pos=pt,
                    seen_at=now_m,
                )

        for row in raw.get("biomes") or []:
            if isinstance(row, dict) and row.get("biome") and isinstance(row.get("points"), list):
                dim = str(row.get("dim") or "overworld")
                bname = _clean_name(row["biome"])
                pts = [
                    [int(p[0]), int(p[1]), int(p[2])]
                    for p in row["points"]
                    if isinstance(p, list) and len(p) >= 3
                ][:4]
                if bname and pts:
                    self.biomes[(dim, bname)] = pts

        for row in raw.get("habitats") or []:
            if not isinstance(row, dict):
                continue
            dim = normalize_dimension(row.get("dim") or "overworld")
            mob = _clean_name(row.get("mob") or "")
            if mob not in HABITAT_MOB_TYPES:
                continue
            clusters: list[dict[str, Any]] = []
            for c in row.get("clusters") or []:
                if not isinstance(c, dict):
                    continue
                p = c.get("pos")
                if isinstance(p, list) and len(p) >= 3:
                    try:
                        clusters.append(
                            {
                                "pos": [int(p[0]), int(p[1]), int(p[2])],
                                "seen_at": now_m,  # monotonic timestamps are process-local
                            }
                        )
                    except (TypeError, ValueError):
                        continue
            if clusters:
                self.habitats[(dim, mob)] = clusters[-6:]

        for row in raw.get("visited_chunks") or []:
            if isinstance(row, list) and len(row) >= 3:
                self.visited_chunks[(str(row[0]), int(row[1]), int(row[2]))] = None

        for item in raw.get("exploration_cells") or []:
            if not isinstance(item, dict):
                continue
            try:
                dim = normalize_dimension(item.get("dim"))
                rx = int(item["rx"])
                rz = int(item["rz"])
            except (KeyError, TypeError, ValueError):
                continue
            terrain = str(item.get("terrain") or "land")
            if terrain not in {"land", "water", "void_edge"}:
                terrain = "land"
            ve = item.get("village_eligible")
            self.exploration_cells[(dim, rx, rz)] = {
                "terrain": terrain,
                "biome": _clean_name(item.get("biome") or ""),
                "village_eligible": ve if isinstance(ve, bool) else None,
                "scanned_village": bool(item.get("scanned_village")),
                "visits": max(0, int(item.get("visits") or 0)),
            }

    def save(self, *, force: bool = False) -> None:
        if self.path is None or (not self._dirty and not force):
            return
        now = time.monotonic()
        if not force and (now - self._last_save_ts) < 2.0:
            return
        payload = {
            "version": 1,
            "anchors": {
                "spawn_points": self.spawn_points,
                "bed_spawn": self.bed_spawn,
                "home_bases": self.home_bases,
                "portals": self.portals,
                "last_death": (
                    {"dim": self.last_death["dim"], "pos": self.last_death["pos"]}
                    if isinstance(self.last_death, dict)
                    else None
                ),
            },
            "containers": [
                {
                    "dim": k[0],
                    "pos": [k[1], k[2], k[3]],
                    "block": v.get("block", "chest"),
                    "items": v.get("items", {}),
                }
                for k, v in self.containers.items()
            ],
            "pois": [e.to_dict() for e in self.pois.values()],
            "conversion_jobs": [
                {
                    "id": j["id"],
                    "kind": j.get("kind", "furnace"),
                    "dim": j.get("dim", "overworld"),
                    "pos": list(j.get("pos") or []),
                    "input_item": j.get("input_item", ""),
                    "output_item": j.get("output_item", ""),
                    "count": int(j.get("count") or 0),
                    "remaining": int(j.get("remaining") or 0),
                    "furnace_count": int(j.get("furnace_count") or 1),
                    "per_item_s": float(j.get("per_item_s") or 10.0),
                    "started_at": float(j.get("started_at") or 0.0),
                    "ready_at": float(j.get("ready_at") or 0.0),
                }
                for j in self.conversion_jobs.values()
            ],
            "biomes": [
                {"dim": dim, "biome": b, "points": pts}
                for (dim, b), pts in self.biomes.items()
            ],
            "habitats": [
                {
                    "dim": dim,
                    "mob": mob,
                    "clusters": [{"pos": list(c.get("pos") or [])} for c in clusters[-6:]],
                }
                for (dim, mob), clusters in self.habitats.items()
            ],
            "visited_chunks": [[d, cx, cz] for (d, cx, cz) in list(self.visited_chunks.keys())[-DEFAULT_MAX_VISITED_CHUNKS:]],
            "exploration_cells": [
                {
                    "dim": d,
                    "rx": rx,
                    "rz": rz,
                    "terrain": cell.get("terrain", "land"),
                    "biome": cell.get("biome", ""),
                    "village_eligible": cell.get("village_eligible"),
                    "scanned_village": bool(cell.get("scanned_village")),
                    "visits": int(cell.get("visits") or 0),
                }
                for (d, rx, rz), cell in list(self.exploration_cells.items())[-DEFAULT_MAX_EXPLORATION_CELLS:]
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
            self._dirty = False
            self._last_save_ts = now
        except OSError:
            pass

    # ---- Passive Observation Hooks ----

    def observe_status(self, st: dict[str, Any]) -> None:
        if not isinstance(st, dict):
            return
        pos = st.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        try:
            bx = math.floor(float(pos[0]))
            by = math.floor(float(pos[1]))
            bz = math.floor(float(pos[2]))
        except (TypeError, ValueError):
            return
        dim = normalize_dimension(st.get("dimension"))

        # 1) Initial world spawn point
        if dim not in self.spawn_points:
            self.spawn_points[dim] = [bx, by, bz]
            self._dirty = True

        # 2) Visited chunk footprint
        ck = (dim, bx >> 4, bz >> 4)
        if ck in self.visited_chunks:
            self.visited_chunks.move_to_end(ck)
        else:
            self.visited_chunks[ck] = None
            while len(self.visited_chunks) > DEFAULT_MAX_VISITED_CHUNKS:
                self.visited_chunks.popitem(last=False)
            self._dirty = True

        # 3) Biome atlas (deduplicate within 48m)
        raw_biome = _clean_name(st.get("biome") or "")
        if raw_biome and raw_biome != "?":
            bkey = (dim, raw_biome)
            pts = self.biomes.setdefault(bkey, [])
            if all(math.hypot(p[0] - bx, p[2] - bz) >= 48.0 for p in pts):
                pts.append([bx, by, bz])
                if len(pts) > 4:
                    pts.pop(0)
                self._dirty = True

        # 3b) Coarse 64x64 exploration region cell (always record silently)
        self._touch_exploration_cell(
            dim=dim,
            rx=bx >> EXPLORATION_CELL_SHIFT,
            rz=bz >> EXPLORATION_CELL_SHIFT,
            terrain=("water" if (st.get("in_water") or st.get("submerged")) else "land")
            if (st.get("on_ground") is not False or st.get("in_water") or st.get("submerged"))
            else None,
            biome=raw_biome if (raw_biome and raw_biome != "?") else "",
            increment_visit=True,
        )

        # 4) Last death from status if newly occurred in this session (ignore pre-existing stale status.last_death on first alive poll)
        ld = st.get("last_death")
        if isinstance(ld, dict) and isinstance(ld.get("pos"), (list, tuple)) and len(ld["pos"]) >= 3:
            try:
                dp = [math.floor(float(ld["pos"][0])), math.floor(float(ld["pos"][1])), math.floor(float(ld["pos"][2]))]
                ld_stamp = ld.get("seq") or ld.get("t") or ld.get("time") or ld.get("at")
                sig = (tuple(dp), ld_stamp)
                if not self._status_death_primed:
                    self._status_death_primed = True
                    self._status_death_sig = sig
                    if st.get("alive") is False:
                        self.last_death = {"dim": dim, "pos": dp, "t": time.monotonic()}
                        self._dirty = True
                elif sig != self._status_death_sig:
                    self._status_death_sig = sig
                    self.last_death = {"dim": dim, "pos": dp, "t": time.monotonic()}
                    self._dirty = True
            except (TypeError, ValueError):
                pass
        elif not self._status_death_primed:
            self._status_death_primed = True
            self._status_death_sig = None
        self.save()

    def observe_blocks(self, blocks: Iterable[dict[str, Any]], *, dim: str = "overworld") -> int:
        norm_dim = normalize_dimension(dim)
        added = 0
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            bname = _clean_name(blk.get("name") or "")
            bpos = blk.get("pos")
            if not bname or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            # Passive coarse biome/village-eligibility inference even when server biome is empty
            if norm_dim == "overworld":
                try:
                    rx = math.floor(float(bpos[0])) >> EXPLORATION_CELL_SHIFT
                    rz = math.floor(float(bpos[2])) >> EXPLORATION_CELL_SHIFT
                except (TypeError, ValueError):
                    rx = rz = None
                if rx is not None and rz is not None:
                    if bname in {"birch_log", "jungle_log", "dark_oak_log", "mangrove_log", "cherry_log"}:
                        self._touch_exploration_cell(dim=norm_dim, rx=rx, rz=rz, village_eligible=False)
                    elif bname in {"spruce_log", "acacia_log", "sandstone", "hay_block", "bell", "dirt_path"}:
                        self._touch_exploration_cell(dim=norm_dim, rx=rx, rz=rz, village_eligible=True)
            if not is_tracked_poi_block(bname):
                continue
            try:
                pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            except (TypeError, ValueError):
                continue
            if self.record_poi(bname, pt, dim=norm_dim, auto_save=False):
                added += 1
        if added > 0:
            self._maybe_promote_home_base(norm_dim)
            self.save()
        return added

    def record_poi(
        self,
        block_name: str,
        pos: tuple[int, int, int] | list[int],
        *,
        dim: str = "overworld",
        auto_save: bool = True,
    ) -> bool:
        bname = _clean_name(block_name)
        if not is_tracked_poi_block(bname):
            return False
        norm_dim = normalize_dimension(dim)
        pt = (int(pos[0]), int(pos[1]), int(pos[2]))
        now = time.monotonic()

        # Spatial deduplication for contiguous water/lava pools (keep 1 representative per 6-block radius)
        if bname in FLUID_CLUSTER_BLOCKS:
            for (edim, ex, ey, ez), entry in self.pois.items():
                if edim == norm_dim and entry.name == bname and abs(ex - pt[0]) <= 6 and abs(ez - pt[2]) <= 6 and abs(ey - pt[1]) <= 3:
                    entry.seen_at = now
                    self.pois.move_to_end((edim, ex, ey, ez))
                    return False

        key = (norm_dim, pt[0], pt[1], pt[2])
        existing = self.pois.get(key)
        if existing is not None:
            existing.name = bname
            existing.seen_at = now
            self.pois.move_to_end(key)
        else:
            self.pois[key] = POIEntry(name=bname, dim=norm_dim, pos=pt, seen_at=now)
            self._enforce_poi_caps(norm_dim, bname)

        if bname == "nether_portal":
            plist = self.portals.setdefault(norm_dim, [])
            if all(math.dist(p, pt) >= 6.0 for p in plist):
                plist.append(list(pt))
                if len(plist) > 8:
                    plist.pop(0)

        self._dirty = True
        if bname in {"crafting_table", "furnace", "blast_furnace", *CONTAINER_BLOCKS} or bname.endswith("_bed"):
            self._maybe_promote_home_base(norm_dim)
        if auto_save:
            self.save()
        return True

    def _enforce_poi_caps(self, dim: str, bname: str) -> None:
        same_type_keys = [k for k, v in self.pois.items() if k[0] == dim and v.name == bname]
        while len(same_type_keys) > DEFAULT_MAX_POIS_PER_TYPE:
            drop_k = same_type_keys.pop(0)
            self.pois.pop(drop_k, None)
        while len(self.pois) > DEFAULT_MAX_TOTAL_POIS:
            self.pois.popitem(last=False)

    def _maybe_promote_home_base(self, dim: str) -> None:
        """Auto-detect a Home Base when crafting_table + furnace + (chest or bed) cluster within 12m."""
        tables: list[tuple[int, int, int]] = []
        furnaces: list[tuple[int, int, int]] = []
        anchors_3rd: list[tuple[int, int, int]] = []
        for (edim, _, _, _), e in self.pois.items():
            if edim != dim:
                continue
            if e.name == "crafting_table":
                tables.append(e.pos)
            elif e.name in {"furnace", "blast_furnace", "smoker"}:
                furnaces.append(e.pos)
            elif e.name in CONTAINER_BLOCKS or e.name.endswith("_bed"):
                anchors_3rd.append(e.pos)
        for (cdim, cx, cy, cz) in self.containers:
            if cdim == dim and (cx, cy, cz) not in anchors_3rd:
                anchors_3rd.append((cx, cy, cz))
        if self.bed_spawn and self.bed_spawn.get("dim") == dim:
            bp = tuple(int(v) for v in self.bed_spawn["pos"])
            if bp not in anchors_3rd:
                anchors_3rd.append(bp)

        for tpos in tables:
            has_f = any(math.dist(tpos, fpos) <= 12.0 for fpos in furnaces)
            has_c = any(math.dist(tpos, cpos) <= 12.0 for cpos in anchors_3rd)
            if has_f and has_c:
                if dim not in self.home_bases:
                    self.home_bases[dim] = list(tpos)
                    self._dirty = True
                return

    def set_home(self, pos: list[int] | tuple[int, int, int], *, dim: str = "overworld") -> None:
        norm_dim = normalize_dimension(dim)
        self.home_bases[norm_dim] = [int(pos[0]), int(pos[1]), int(pos[2])]
        self._dirty = True
        self.save()

    def record_bed_spawn(self, pos: list[int] | tuple[int, int, int], *, dim: str = "overworld") -> None:
        norm_dim = normalize_dimension(dim)
        pt = [int(pos[0]), int(pos[1]), int(pos[2])]
        self.bed_spawn = {"dim": norm_dim, "pos": pt}
        self.record_poi("white_bed", pt, dim=norm_dim, auto_save=False)
        self._maybe_promote_home_base(norm_dim)
        self._dirty = True
        self.save()

    def record_death(self, pos: list[int] | tuple[int, int, int] | None, *, dim: str = "overworld") -> None:
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        norm_dim = normalize_dimension(dim)
        self.last_death = {
            "dim": norm_dim,
            "pos": [math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))],
            "t": time.monotonic(),
        }
        self._dirty = True
        self.save()

    def update_container(
        self,
        pos: list[int] | tuple[int, int, int],
        items: dict[str, int],
        *,
        block_name: str = "chest",
        dim: str = "overworld",
    ) -> None:
        norm_dim = normalize_dimension(dim)
        pt = (int(pos[0]), int(pos[1]), int(pos[2]))
        clean_items = {_clean_name(k): int(v) for k, v in items.items() if _clean_name(k) and int(v or 0) > 0}
        bname = _clean_name(block_name or "chest")
        self.containers[(norm_dim, pt[0], pt[1], pt[2])] = {
            "block": bname if bname in CONTAINER_BLOCKS else "chest",
            "items": clean_items,
            "updated_at": time.monotonic(),
        }
        self.record_poi(bname if bname in CONTAINER_BLOCKS else "chest", pt, dim=norm_dim, auto_save=False)
        self._maybe_promote_home_base(norm_dim)
        self._dirty = True
        self.save()

    def update_container_from_window(
        self,
        window_result: dict[str, Any],
        *,
        fallback_pos: list[int] | tuple[int, int, int] | None = None,
        fallback_block: str | None = None,
        dim: str = "overworld",
    ) -> bool:
        """Extract chest/barrel slot contents from `/window/view` or `/window/act` response."""
        if not isinstance(window_result, dict):
            return False
        target_info = window_result.get("target") if isinstance(window_result.get("target"), dict) else {}
        wtype = _clean_name(
            window_result.get("window")
            or window_result.get("block")
            or target_info.get("name")
            or target_info.get("block")
            or window_result.get("type")
            or fallback_block
            or ""
        )
        pos = window_result.get("pos") or target_info.get("pos") or fallback_pos
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return False
        is_chest_like = any(k in wtype for k in ("chest", "barrel", "shulker", "generic_9x", "generic_3x3", "hopper"))
        if not is_chest_like and _clean_name(fallback_block or "") not in CONTAINER_BLOCKS:
            return False

        sections = window_result.get("sections") if isinstance(window_result.get("sections"), dict) else {}
        sec_container = sections.get("container") if isinstance(sections.get("container"), list) else None
        raw_slots = (
            sec_container
            if sec_container is not None
            else (
                window_result.get("container_items")
                or window_result.get("chest_items")
                or window_result.get("slots")
                or window_result.get("items")
            )
        )
        if not isinstance(raw_slots, list):
            return False

        counts: dict[str, int] = {}
        container_slot_limit = int(window_result.get("container_slots") or 27)
        for row in raw_slots:
            if not isinstance(row, dict):
                continue
            slot_idx = row.get("slot")
            if (
                sec_container is None
                and isinstance(slot_idx, int)
                and "container_items" not in window_result
                and slot_idx >= container_slot_limit
            ):
                continue
            iname = _clean_name(row.get("name") or row.get("item") or "")
            icnt = int(row.get("count") or 0)
            if iname and icnt > 0:
                counts[iname] = counts.get(iname, 0) + icnt

        blk = _clean_name(fallback_block or "")
        self.update_container(pos, counts, block_name=(blk if blk in CONTAINER_BLOCKS else "chest"), dim=dim)
        return True

    def invalidate_container(
        self,
        pos: list[int] | tuple[int, int, int],
        *,
        dim: str = "overworld",
        block_name: str | None = None,
        auto_save: bool = True,
    ) -> dict[str, Any]:
        """Remove a broken container from both the `containers` and `pois` tables."""
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return {"container_removed": False, "poi_removed": False}
        norm_dim = normalize_dimension(dim)
        key = (norm_dim, int(pos[0]), int(pos[1]), int(pos[2]))
        container_removed = self.containers.pop(key, None) is not None
        poi_removed = self.pois.pop(key, None) is not None
        # In-flight jobs pointing at a destroyed furnace are dead weight.
        dead_jobs = [
            jid
            for jid, job in self.conversion_jobs.items()
            if tuple(job.get("pos") or ()) == (key[1], key[2], key[3]) and str(job.get("dim") or "overworld") == norm_dim
        ]
        for jid in dead_jobs:
            self.conversion_jobs.pop(jid, None)
        if container_removed or poi_removed or dead_jobs:
            self._dirty = True
            if auto_save:
                self.save()
        return {
            "container_removed": container_removed,
            "poi_removed": poi_removed,
            "jobs_removed": dead_jobs,
            "pos": [key[1], key[2], key[3]],
            "dim": norm_dim,
            "block": block_name,
        }

    # ---- In-flight conversion (furnace) ledger ----

    def register_conversion_job(
        self,
        *,
        pos: list[int] | tuple[int, int, int],
        input_item: str,
        output_item: str,
        count: int,
        dim: str = "overworld",
        furnaces: int = 1,
        per_item_s: float = 10.0,
        ready_at: float | None = None,
        kind: str = "furnace",
        auto_save: bool = True,
    ) -> dict[str, Any]:
        """Register one non-blocking conversion job (multi-furnace aware) and persist it."""
        norm_dim = normalize_dimension(dim)
        pt = [int(pos[0]), int(pos[1]), int(pos[2])]
        wall_now = time.time()
        n = max(1, int(count))
        job_id = f"{kind}-{int(wall_now * 1000)}-{len(self.conversion_jobs) + 1}"
        eta = float(ready_at) if ready_at is not None else wall_now + per_item_s * math.ceil(n / max(1, int(furnaces))) + 5.0
        job = {
            "id": job_id,
            "kind": kind,
            "dim": norm_dim,
            "pos": pt,
            "input_item": _clean_name(input_item),
            "output_item": _clean_name(output_item),
            "count": n,
            "remaining": n,
            "furnace_count": max(1, int(furnaces)),
            "per_item_s": float(per_item_s),
            "started_at": wall_now,
            "ready_at": eta,
        }
        self.conversion_jobs[job_id] = job
        self._dirty = True
        if auto_save:
            self.save()
        return dict(job)

    def get_conversion_jobs(self, *, dim: str | None = None, include_expired: bool = False) -> list[dict[str, Any]]:
        wall_now = time.time()
        out: list[dict[str, Any]] = []
        expired: list[str] = []
        for jid, job in self.conversion_jobs.items():
            if dim is not None and str(job.get("dim") or "overworld") != normalize_dimension(dim):
                continue
            if not include_expired and float(job.get("ready_at") or 0.0) + 1800.0 < wall_now:
                expired.append(jid)
                continue
            out.append(dict(job))
        for jid in expired:
            self.conversion_jobs.pop(jid, None)
            self._dirty = True
        return out

    def get_in_conversion_counts(self, *, dim: str | None = None) -> dict[str, int]:
        """Output-item counts currently sitting inside furnaces (not yet in the bag)."""
        counts: dict[str, int] = {}
        for job in self.get_conversion_jobs(dim=dim):
            out_name = _clean_name(job.get("output_item") or "")
            remaining = int(job.get("remaining") or 0)
            if out_name and remaining > 0:
                counts[out_name] = counts.get(out_name, 0) + remaining
        return counts

    def complete_conversion_job(
        self,
        job_id: str | None = None,
        *,
        pos: list[int] | tuple[int, int, int] | None = None,
        dim: str | None = None,
        collected: int | None = None,
        auto_save: bool = True,
    ) -> dict[str, Any]:
        """Book the collected output; drop the job when nothing remains."""
        target: dict[str, Any] | None = None
        if job_id:
            target = self.conversion_jobs.get(str(job_id))
        elif pos is not None:
            norm_dim = normalize_dimension(dim or "overworld")
            for job in self.conversion_jobs.values():
                if tuple(job.get("pos") or ()) == tuple(int(v) for v in pos[:3]) and str(job.get("dim")) == norm_dim:
                    target = job
                    break
        if target is None:
            return {"ok": False, "reason": "job_not_found", "job_id": job_id, "pos": list(pos) if pos else None}
        take = int(collected if collected is not None else target.get("remaining") or 0)
        target["remaining"] = max(0, int(target.get("remaining") or 0) - max(0, take))
        finished = target["remaining"] <= 0
        if finished:
            self.conversion_jobs.pop(target["id"], None)
        self._dirty = True
        if auto_save:
            self.save()
        return {
            "ok": True,
            "job_id": target["id"],
            "collected": take,
            "remaining": target["remaining"],
            "output_item": target.get("output_item"),
            "pos": target.get("pos"),
            "finished": finished,
        }

    def observe_entities(self, entities: Iterable[dict[str, Any]], *, dim: str = "overworld") -> int:
        norm_dim = normalize_dimension(dim)
        now = time.monotonic()
        recorded = 0
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            ename = _clean_name(ent.get("name") or "")
            epos = ent.get("pos")
            if ename not in HABITAT_MOB_TYPES or not isinstance(epos, (list, tuple)) or len(epos) < 3:
                continue
            try:
                pt = [math.floor(float(epos[0])), math.floor(float(epos[1])), math.floor(float(epos[2]))]
            except (TypeError, ValueError):
                continue
            hkey = (norm_dim, ename)
            clusters = [c for c in self.habitats.get(hkey, []) if (now - float(c.get("seen_at", 0.0))) <= DEFAULT_HABITAT_TTL_S]
            merged = False
            for c in clusters:
                cp = c["pos"]
                if math.hypot(cp[0] - pt[0], cp[2] - pt[2]) <= 32.0:
                    c["pos"] = pt
                    c["seen_at"] = now
                    merged = True
                    break
            if not merged:
                clusters.append({"pos": pt, "seen_at": now})
                if len(clusters) > 6:
                    clusters.pop(0)
                recorded += 1
            self.habitats[hkey] = clusters
        return recorded

    # ---- Lazy Removal & Unreachable Cooldown ----

    def remove_poi(self, pos: tuple[int, int, int] | list[int], *, dim: str = "overworld") -> bool:
        norm_dim = normalize_dimension(dim)
        key = (norm_dim, int(pos[0]), int(pos[1]), int(pos[2]))
        removed_poi = self.pois.pop(key, None) is not None
        removed_cnt = self.containers.pop(key, None) is not None
        if self.bed_spawn and self.bed_spawn.get("dim") == norm_dim and self.bed_spawn.get("pos") == [key[1], key[2], key[3]]:
            self.bed_spawn = None
        if removed_poi or removed_cnt:
            self._dirty = True
            self.save()
            return True
        return False

    def mark_unreachable(
        self,
        pos: tuple[int, int, int] | list[int],
        *,
        dim: str = "overworld",
        reason: str = "path_not_found",
        cooldown_s: float = DEFAULT_UNREACHABLE_COOLDOWN_S,
    ) -> bool:
        norm_dim = normalize_dimension(dim)
        key = (norm_dim, int(pos[0]), int(pos[1]), int(pos[2]))
        entry = self.pois.get(key)
        if entry is None:
            return False
        entry.cooldown_until = time.monotonic() + max(5.0, float(cooldown_s))
        entry.fail_reason = reason
        return True

    # ---- Retrieval, Lazy Verification & Exploration Compass ----

    def query_pois(
        self,
        names: Iterable[str],
        *,
        origin: tuple[float, float, float] | list[float],
        dim: str = "overworld",
        limit: int = 5,
        verify_fn: Callable[[tuple[int, int, int]], str | None] | None = None,
        verify_radius: float = 32.0,
    ) -> list[dict[str, Any]]:
        """Return nearest non-cooled POIs in `dim`, lazily pruning stale entries within `verify_radius`."""
        req_set = {_clean_name(n) for n in names if _clean_name(n)}
        if not req_set:
            return []
        norm_dim = normalize_dimension(dim)
        now = time.monotonic()
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])

        cands: list[tuple[float, tuple[str, int, int, int], POIEntry]] = []
        for key, entry in self.pois.items():
            if key[0] != norm_dim:
                continue
            if entry.cooldown_until > now:
                continue
            if not _matches_requested_block(entry.name, req_set):
                continue
            d = math.dist((ox, oy, oz), (entry.pos[0] + 0.5, entry.pos[1] + 0.5, entry.pos[2] + 0.5))
            cands.append((d, key, entry))

        cands.sort(key=lambda r: r[0])
        results: list[dict[str, Any]] = []
        for dist, key, entry in cands:
            if verify_fn is not None and dist <= verify_radius:
                live_blk = verify_fn(entry.pos)
                if live_blk is not None:
                    clean_live = _clean_name(live_blk)
                    # Flowing water/lava or matching block keeps it valid; air/other removes stale entry!
                    still_valid = (
                        clean_live == entry.name
                        or (entry.name == "lava" and clean_live in {"lava", "flowing_lava"})
                        or (entry.name == "water" and clean_live in {"water", "flowing_water"})
                        or (entry.name.endswith("_bed") and clean_live.endswith("_bed"))
                    )
                    if not still_valid:
                        self.remove_poi(entry.pos, dim=norm_dim)
                        continue
            dx, dz = entry.pos[0] - ox, entry.pos[2] - oz
            results.append(
                {
                    "name": entry.name,
                    "pos": list(entry.pos),
                    "dist": round(dist, 1),
                    "bearing": _cardinal_bearing(dx, dz),
                }
            )
            if len(results) >= limit:
                break
        return results

    def query_habitats(
        self,
        mob_names: Iterable[str],
        *,
        origin: tuple[float, float, float] | list[float],
        dim: str = "overworld",
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        req_set = {_clean_name(m) for m in mob_names if _clean_name(m)}
        if "animal" in req_set:
            req_set.update({"sheep", "cow", "pig", "chicken"})
        norm_dim = normalize_dimension(dim)
        now = time.monotonic()
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        hits: list[tuple[float, str, list[int], float]] = []
        for (hdim, mname), clusters in self.habitats.items():
            if hdim != norm_dim or mname not in req_set:
                continue
            for c in clusters:
                age = now - float(c.get("seen_at", 0.0))
                if age > DEFAULT_HABITAT_TTL_S:
                    continue
                cp = c["pos"]
                d = math.dist((ox, oy, oz), (cp[0] + 0.5, cp[1], cp[2] + 0.5))
                hits.append((d, mname, cp, age))
        hits.sort(key=lambda r: r[0])
        return [
            {
                "name": mname,
                "approx_pos": list(cp),
                "dist": round(d, 1),
                "bearing": _cardinal_bearing(cp[0] - ox, cp[2] - oz),
                "seen_s_ago": int(age),
            }
            for d, mname, cp, age in hits[:limit]
        ]

    def query_container_item(
        self,
        item_name: str,
        *,
        origin: tuple[float, float, float] | list[float],
        dim: str = "overworld",
    ) -> list[dict[str, Any]]:
        clean = _clean_name(item_name)
        norm_dim = normalize_dimension(dim)
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        out: list[tuple[float, list[int], int, str]] = []
        for (cdim, cx, cy, cz), meta in self.containers.items():
            if cdim != norm_dim:
                continue
            cnt = int((meta.get("items") or {}).get(clean, 0))
            if cnt > 0:
                d = math.dist((ox, oy, oz), (cx + 0.5, cy + 0.5, cz + 0.5))
                out.append((d, [cx, cy, cz], cnt, str(meta.get("block") or "chest")))
        out.sort(key=lambda r: r[0])
        return [
            {"block": blk, "pos": pos, "count": cnt, "dist": round(d, 1)}
            for d, pos, cnt, blk in out
        ]

    def shortage_memory_hint(
        self,
        missing_items: Iterable[str],
        *,
        origin: tuple[float, float, float] | list[float] | None,
        dim: str = "overworld",
    ) -> str:
        """Check storage chests & upstream POI blocks (e.g. gold_ingot -> gold_block) for shortage hints."""
        if origin is None or len(origin) < 3:
            return ""
        tips: list[str] = []
        for raw_it in missing_items:
            it = _clean_name(raw_it)
            chest_hits = self.query_container_item(it, origin=origin, dim=dim)
            if chest_hits:
                h0 = chest_hits[0]
                tips.append(f"仓储{h0['block']}@{h0['pos']}({h0['dist']:.0f}m)存有{it}×{h0['count']}")
                continue
            poi_sources = ITEM_TO_MEMORY_POI_SOURCES.get(it)
            if poi_sources:
                poi_hits = self.query_pois(poi_sources, origin=origin, dim=dim, limit=1)
                if poi_hits:
                    p0 = poi_hits[0]
                    tips.append(f"记忆库已知{p0['name']}@{p0['pos']}({p0['dist']:.0f}m,{p0['bearing']})")
            if len(tips) >= 2:
                break
        return "；".join(tips)

    def unexplored_directions(
        self,
        pos: tuple[float, float, float] | list[float],
        *,
        dim: str = "overworld",
    ) -> list[str]:
        norm_dim = normalize_dimension(dim)
        cx0 = math.floor(float(pos[0])) >> 4
        cz0 = math.floor(float(pos[2])) >> 4
        dirs = (("north", 0, -1), ("east", 1, 0), ("south", 0, 1), ("west", -1, 0))
        scored: list[tuple[int, str]] = []
        for name, dx, dz in dirs:
            unvisited = sum(
                1
                for step_chunks in range(1, 7)
                if (norm_dim, cx0 + dx * step_chunks, cz0 + dz * step_chunks) not in self.visited_chunks
            )
            if unvisited > 0:
                scored.append((-unvisited, name))
        scored.sort()
        return [d for _, d in scored]

    def _touch_exploration_cell(
        self,
        *,
        dim: str,
        rx: int,
        rz: int,
        terrain: str | None = None,
        biome: str = "",
        village_eligible: bool | None = None,
        scanned_village: bool | None = None,
        increment_visit: bool = False,
    ) -> dict[str, Any]:
        norm_dim = normalize_dimension(dim)
        key = (norm_dim, int(rx), int(rz))
        cell = self.exploration_cells.get(key)
        if cell is None:
            cell = {
                "terrain": terrain or "land",
                "biome": "",
                "village_eligible": None,
                "scanned_village": False,
                "visits": 0,
            }
            self.exploration_cells[key] = cell
            while len(self.exploration_cells) > DEFAULT_MAX_EXPLORATION_CELLS:
                self.exploration_cells.popitem(last=False)
            self._dirty = True
        else:
            self.exploration_cells.move_to_end(key)

        if terrain in {"land", "water", "void_edge"}:
            # Priority: void_edge > land (if previously visited on dry land, brief water step doesn't erase land) > water
            cur_t = cell.get("terrain", "land")
            if terrain == "void_edge":
                if cur_t != "void_edge":
                    cell["terrain"] = "void_edge"
                    cell["village_eligible"] = False
                    self._dirty = True
            elif terrain == "land":
                if cur_t != "land" and cur_t != "void_edge":
                    cell["terrain"] = "land"
                    self._dirty = True
            elif terrain == "water" and cur_t != "void_edge" and int(cell.get("visits") or 0) == 0:
                if cur_t != "water":
                    cell["terrain"] = "water"
                    self._dirty = True

        clean_b = _clean_name(biome)
        if clean_b and clean_b != "?" and cell.get("biome") != clean_b:
            cell["biome"] = clean_b
            inferred = _classify_village_eligible_biome(clean_b)
            if inferred is not None:
                cell["village_eligible"] = inferred
            self._dirty = True

        if village_eligible is not None and cell.get("village_eligible") is None:
            cell["village_eligible"] = bool(village_eligible)
            self._dirty = True

        if scanned_village is True and not cell.get("scanned_village"):
            cell["scanned_village"] = True
            self._dirty = True

        if increment_visit:
            cell["visits"] = int(cell.get("visits") or 0) + 1
            if cell["visits"] <= 3:
                self._dirty = True
        return cell

    def record_exploration_edge(
        self,
        pos: tuple[float, float, float] | list[float],
        direction: str,
        kind: str,
        *,
        dim: str = "overworld",
    ) -> None:
        """Record a water coast or void edge encountered in the adjacent 64x64 cell along `direction`."""
        dir_map = {name: (dx, dz) for name, dx, dz in EXPLORATION_DIRS_8}
        vec = dir_map.get(direction.strip().lower())
        if vec is None or len(pos) < 3:
            return
        dx, dz = vec
        rx0 = math.floor(float(pos[0])) >> EXPLORATION_CELL_SHIFT
        rz0 = math.floor(float(pos[2])) >> EXPLORATION_CELL_SHIFT
        terrain = "void_edge" if kind in {"void_edge", "void_geofence_blocked"} else "water"
        self._touch_exploration_cell(
            dim=dim,
            rx=rx0 + dx,
            rz=rz0 + dz,
            terrain=terrain,
            village_eligible=False,
        )

    def mark_village_scanned(
        self,
        pos: tuple[float, float, float] | list[float],
        *,
        dim: str = "overworld",
    ) -> None:
        if len(pos) < 3:
            return
        bx = math.floor(float(pos[0]))
        bz = math.floor(float(pos[2]))
        self._touch_exploration_cell(
            dim=dim,
            rx=bx >> EXPLORATION_CELL_SHIFT,
            rz=bz >> EXPLORATION_CELL_SHIFT,
            scanned_village=True,
        )

    def score_direction_novelty(
        self,
        pos: tuple[float, float, float] | list[float],
        direction: str,
        *,
        dim: str = "overworld",
        intent: str = "village",
    ) -> tuple[int, int, int]:
        """Return sort key (blocked_rank, -unvisited_fine_chunks, -coarse_bonus) for a heading.

        Lower tuple = better direction to explore. Blocked water/void edges are deprioritized
        rather than hard-banned so dead-ends/peninsulas can still backtrack cleanly.
        """
        norm_dim = normalize_dimension(dim)
        dir_map = {name: (dx, dz) for name, dx, dz in EXPLORATION_DIRS_8}
        vec = dir_map.get(direction.strip().lower())
        if vec is None or len(pos) < 3:
            return (1, 0, 0)
        dx, dz = vec
        cx0 = math.floor(float(pos[0])) >> 4
        cz0 = math.floor(float(pos[2])) >> 4
        rx0 = math.floor(float(pos[0])) >> EXPLORATION_CELL_SHIFT
        rz0 = math.floor(float(pos[2])) >> EXPLORATION_CELL_SHIFT

        # Fine 16x16 chunk novelty over next 6 chunks (~96m)
        unvisited_chunks = sum(
            1
            for step in range(1, 7)
            if (norm_dim, cx0 + dx * step, cz0 + dz * step) not in self.visited_chunks
        )

        # Coarse 64x64 cell lookahead (1..3 cells = 64..192m)
        blocked_rank = 0
        coarse_bonus = 0
        for step in (1, 2, 3):
            cell = self.exploration_cells.get((norm_dim, rx0 + dx * step, rz0 + dz * step))
            if cell is None:
                coarse_bonus += 2
                continue
            terrain = cell.get("terrain", "land")
            if terrain == "void_edge":
                if step == 1:
                    blocked_rank = max(blocked_rank, 3)
                coarse_bonus -= 4
            elif terrain == "water":
                if step == 1:
                    blocked_rank = max(blocked_rank, 2)
                coarse_bonus -= 2
            else:
                if intent == "village":
                    ve = cell.get("village_eligible")
                    if ve is True and not cell.get("scanned_village"):
                        coarse_bonus += 3
                    elif ve is False:
                        coarse_bonus -= 1
                if int(cell.get("visits") or 0) == 0:
                    coarse_bonus += 1
        return (blocked_rank, -unvisited_chunks, -coarse_bonus)

    def unexplored_directions_8(
        self,
        pos: tuple[float, float, float] | list[float],
        *,
        dim: str = "overworld",
        intent: str = "village",
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Rank all 8 compass headings by fine+coarse exploration novelty (cardinals preferred on tie)."""
        excluded = {d.strip().lower() for d in (exclude or set())}
        cardinal_order = {"east": 0, "north": 1, "south": 2, "west": 3}
        scored = [
            (
                self.score_direction_novelty(pos, name, dim=dim, intent=intent),
                0 if name in cardinal_order else 1,
                cardinal_order.get(name, 10 + idx),
                name,
            )
            for idx, (name, _, _) in enumerate(EXPLORATION_DIRS_8)
            if name not in excluded
        ]
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [name for _, _, _, name in scored]

    def summarize_exploration(
        self,
        pos: tuple[float, float, float] | list[float],
        *,
        dim: str = "overworld",
        intent: str = "village",
        half_size: int = 1024,
    ) -> dict[str, Any]:
        """Aggregate Layer-6 64x64 cells into 4 quadrants + intent-based recommendations (~50-70 tokens)."""
        norm_dim = normalize_dimension(dim)
        origin = self.spawn_points.get(norm_dim) or [int(pos[0]), int(pos[1]), int(pos[2])]
        ox, oz = int(origin[0]), int(origin[2])
        orx = ox >> EXPLORATION_CELL_SHIFT
        orz = oz >> EXPLORATION_CELL_SHIFT
        quad_total_cells = max(1, (max(64, int(half_size)) >> EXPLORATION_CELL_SHIFT) ** 2)

        quads: dict[str, dict[str, Any]] = {
            "NE": {"cells": 0, "land": 0, "water": 0, "void": 0, "village_eligible": 0, "biomes": {}},
            "NW": {"cells": 0, "land": 0, "water": 0, "void": 0, "village_eligible": 0, "biomes": {}},
            "SE": {"cells": 0, "land": 0, "water": 0, "void": 0, "village_eligible": 0, "biomes": {}},
            "SW": {"cells": 0, "land": 0, "water": 0, "void": 0, "village_eligible": 0, "biomes": {}},
        }
        blocked_edges: list[str] = []
        total_cells = 0

        for (cdim, rx, rz), cell in self.exploration_cells.items():
            if cdim != norm_dim:
                continue
            total_cells += 1
            qname = ("N" if rz < orz else "S") + ("E" if rx >= orx else "W")
            q = quads[qname]
            q["cells"] += 1
            t = cell.get("terrain", "land")
            if t == "void_edge":
                q["void"] += 1
            elif t == "water":
                q["water"] += 1
            else:
                q["land"] += 1
            if cell.get("village_eligible") is True:
                q["village_eligible"] += 1
            b = cell.get("biome") or ""
            if b:
                q["biomes"][b] = q["biomes"].get(b, 0) + 1

        # Identify blocked immediate directions around current pos
        rx0 = math.floor(float(pos[0])) >> EXPLORATION_CELL_SHIFT
        rz0 = math.floor(float(pos[2])) >> EXPLORATION_CELL_SHIFT
        for name, dx, dz in EXPLORATION_DIRS_8:
            c1 = self.exploration_cells.get((norm_dim, rx0 + dx, rz0 + dz))
            if c1 is not None and c1.get("terrain") in {"water", "void_edge"}:
                tag = "虚空" if c1.get("terrain") == "void_edge" else "海"
                blocked_edges.append(f"{name}({tag})")

        ranked_dirs = self.unexplored_directions_8(pos, dim=norm_dim, intent=intent)
        recommended = ranked_dirs[:3]

        quad_parts: list[str] = []
        quad_stats: dict[str, dict[str, Any]] = {}
        for qname in ("NE", "NW", "SE", "SW"):
            q = quads[qname]
            pct = min(100, round(q["cells"] * 100.0 / quad_total_cells))
            top_b = max(q["biomes"].items(), key=lambda kv: kv[1])[0] if q["biomes"] else ""
            tag = f"{top_b}" if top_b else ("水" if q["water"] > q["land"] else ("未探" if q["cells"] == 0 else "陆"))
            quad_stats[qname] = {
                "cells": q["cells"],
                "coverage_pct": pct,
                "land": q["land"],
                "water": q["water"],
                "void": q["void"],
                "village_eligible": q["village_eligible"],
                "top_biome": top_b,
            }
            quad_parts.append(f"{qname}:{pct}%({tag})")

        blocked_str = f" | 已阻={','.join(blocked_edges[:3])}" if blocked_edges else ""
        brief = (
            f"[探索罗盘({intent})] 已探{total_cells}格 "
            f"{' '.join(quad_parts)} | 推荐方向={'/' .join(recommended)}{blocked_str}"
        )
        return {
            "dim": norm_dim,
            "intent": intent,
            "total_cells": total_cells,
            "quadrants": quad_stats,
            "recommended_directions": recommended,
            "blocked_edges": blocked_edges,
            "brief": brief,
        }

    # ---- Compact 1-Line HUD Catalog Formatter (<= 84 chars) ----

    def format_hud_line(
        self,
        *,
        dim: str,
        pos: tuple[float, float, float] | list[float],
        current_biome: str = "",
        local_visible_names: set[str] | None = None,
        max_chars: int = 84,
    ) -> str:
        """Render a single compact catalog line of off-screen anchors, chest items, POIs, habitats & biomes."""
        norm_dim = normalize_dimension(dim)
        ox, oy, oz = float(pos[0]), float(pos[1]), float(pos[2])
        visible = {_clean_name(n) for n in (local_visible_names or set())}
        now = time.monotonic()
        segments: list[str] = []

        # 1) Home / Storage summary (if > 10m away or has stored items)
        home = self.home_bases.get(norm_dim)
        chest_totals: dict[str, int] = {}
        nearest_chest_dist: float | None = None
        for (cdim, cx, cy, cz), meta in self.containers.items():
            if cdim != norm_dim:
                continue
            d_c = math.dist((ox, oy, oz), (cx + 0.5, cy + 0.5, cz + 0.5))
            if nearest_chest_dist is None or d_c < nearest_chest_dist:
                nearest_chest_dist = d_c
            for ik, iv in (meta.get("items") or {}).items():
                chest_totals[ik] = chest_totals.get(ik, 0) + int(iv)

        top_stored = sorted(chest_totals.items(), key=lambda kv: -kv[1])[:2]
        stored_tag = ",".join(f"{k}:{v}" for k, v in top_stored)
        if home is not None:
            d_home = math.dist((ox, oy, oz), (home[0] + 0.5, home[1], home[2] + 0.5))
            if d_home > 12.0 or stored_tag:
                h_str = f"家@{home}({d_home:.0f}m" + (f",箱:{stored_tag})" if stored_tag else ")")
                segments.append(h_str)
        elif stored_tag and nearest_chest_dist is not None:
            segments.append(f"仓箱({nearest_chest_dist:.0f}m,{stored_tag})")

        # 2) Off-screen Scarce POIs (catalog of nearest distance per distinct type, skipping 16m visible)
        poi_priority = {
            "gold_block": 0,
            "diamond_block": 0,
            "obsidian": 1,
            "nether_portal": 1,
            "enchanting_table": 1,
            "lava": 2,
            "spawner": 2,
            "lectern": 3,
            "brewing_stand": 3,
            "anvil": 3,
            "sugar_cane": 4,
            "hay_block": 4,
            "water": 6,
            "furnace": 7,
            "crafting_table": 8,
        }
        best_by_name: dict[str, float] = {}
        for (edim, _, _, _), entry in self.pois.items():
            if edim != norm_dim or entry.cooldown_until > now:
                continue
            label = "bed" if entry.name.endswith("_bed") else entry.name
            if label in visible or entry.name in visible:
                continue
            d_p = math.dist((ox, oy, oz), (entry.pos[0] + 0.5, entry.pos[1] + 0.5, entry.pos[2] + 0.5))
            if d_p <= 15.5:
                continue
            prev_d = best_by_name.get(label)
            if prev_d is None or d_p < prev_d:
                best_by_name[label] = d_p

        sorted_pois = sorted(
            best_by_name.items(),
            key=lambda kv: (poi_priority.get(kv[0], 5), kv[1]),
        )
        for name, d_p in sorted_pois[:5]:
            segments.append(f"{name}({d_p:.0f}m)")

        # 3) Off-screen Habitats (villager / sheep / cow > 18m away)
        best_hab: dict[str, float] = {}
        for (hdim, mname), clusters in self.habitats.items():
            if hdim != norm_dim:
                continue
            for c in clusters:
                if (now - float(c.get("seen_at", 0.0))) > DEFAULT_HABITAT_TTL_S:
                    continue
                cp = c["pos"]
                d_h = math.dist((ox, oy, oz), (cp[0] + 0.5, cp[1], cp[2] + 0.5))
                if d_h > 18.0:
                    prev_h = best_hab.get(mname)
                    if prev_h is None or d_h < prev_h:
                        best_hab[mname] = d_h
        for mname in ("villager", "sheep", "cow", "blaze", "piglin"):
            if mname in best_hab and len(segments) < 6:
                segments.append(f"{mname}({best_hab[mname]:.0f}m)")

        # 4) Other discovered Biomes (different from current biome)
        cur_b = _clean_name(current_biome)
        other_biomes: list[tuple[float, str]] = []
        for (bdim, bname), pts in self.biomes.items():
            if bdim != norm_dim or bname == cur_b or not pts:
                continue
            d_b = min(math.dist((ox, oy, oz), (p[0], p[1], p[2])) for p in pts)
            if d_b > 24.0:
                other_biomes.append((d_b, bname))
        other_biomes.sort(key=lambda r: r[0])
        if other_biomes and len(segments) < 6:
            d_b0, bn0 = other_biomes[0]
            segments.append(f"群系:{bn0}({d_b0:.0f}m)")

        if not segments:
            return ""

        prefix = "记忆库: "
        out = prefix
        for idx, seg in enumerate(segments):
            sep = "" if idx == 0 else ", "
            if len(out) + len(sep) + len(seg) > max_chars:
                break
            out += sep + seg
        return out if out != prefix else ""
