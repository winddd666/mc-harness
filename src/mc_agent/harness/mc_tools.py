"""Minecraft Agent API v1.3 声明式工具集（对接 nano-agent ToolRegistry / ToolScheduler）。

观察类工具标记为 `concurrency="shared"`（可单轮并发查询）；
动作类工具标记为 `concurrency="exclusive"`（自动串行执行，避免触发 API `busy` 锁冲突）。
业务错误（如缺材料、寻路失败、受伤打断）序列化为 JSON 字符串回填给模型纠错；认证失效或致命冷却抛 RuntimeError 终止。
"""

from __future__ import annotations

from copy import copy
import json
import math
import re
import threading
import time
from typing import Any, Callable

import httpx

from ..bot import BotSession
from ..errors import ApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ..kb import assess_world_target, build_jit_error_hint, lookup_kb, normalize_dimension, resolve_item_alias
from ..skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier
from ..skills.combat_adapter import run_combat_skill
from ..skills.library import (
    BLOCK_MIN_PICKAXE_TIER,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    TABLE_RECIPES,
    _StairDeadline,
    _StairSession,
    _best_pickaxe_in_inv,
    _can_fit_item_in_inventory,
    _compute_empty_slots,
    _inventory_counts,
    best_melee_weapon_in_inv,
    explain_material_craft_error,
    is_passable_sight_block,
)
from ..skills.safety import SAFE_FOOD_PRIORITY
from .geofence import VoidGeofence
from .tool_registry import Parameter, Tool, ToolRegistry
from .world_memory import WorldMemory, default_memory_path_for_bot

_BED_BLOCK_TYPES: tuple[str, ...] = tuple(
    f"{color}_bed"
    for color in (
        "white", "orange", "magenta", "light_blue", "yellow", "lime",
        "pink", "gray", "light_gray", "cyan", "purple", "blue",
        "brown", "green", "red", "black",
    )
)


_BLOCK_COLORS: tuple[str, ...] = tuple(name.removesuffix("_bed") for name in _BED_BLOCK_TYPES)
_BLOCK_WOODS: tuple[str, ...] = tuple(name.removesuffix("_log") for name in LOG_TYPES)
_FIND_BLOCK_ALIASES: dict[str, tuple[str, ...]] = {
    "bed": _BED_BLOCK_TYPES, "beds": _BED_BLOCK_TYPES,
    "log": LOG_TYPES, "logs": LOG_TYPES,
    "planks": tuple(f"{wood}_planks" for wood in (*_BLOCK_WOODS, "crimson", "warped", "bamboo")),
    "wool": tuple(f"{color}_wool" for color in _BLOCK_COLORS),
    "grass": ("short_grass", "tall_grass"),
    "portal": ("nether_portal",), "nether_rack": ("netherrack",),
    "cave": ("cave_air",), "flowing_water": ("water",), "flowing_lava": ("lava",),
    "cobble": ("cobblestone",),
}


# These r11 inputs are item/entity registry IDs, never block IDs. Other
# unfamiliar names remain version-authoritative and are checked by the API.
_NON_BLOCK_FIND_TYPES = frozenset({"shield", "leather", "sheep"})


def _invalid_find_block_query(ignored: list[str]) -> str:
    return json.dumps({
        "ok": False, "requires_replan": True, "ignored_types": ignored,
        "error": {"code": "bad_args", "message": "没有有效方块类型。示例：mc_find_blocks(types=['oak_log','stone'])；查实体请用 mc_get_entities。"},
    }, ensure_ascii=False)


def _normalize_find_block_types(
    types: list[str] | tuple[str, ...], *, dimension: str | None = None
) -> list[str]:
    """Expand block families; leave registry validation to the server version."""
    nether = str(dimension or "").lower() == "the_nether"
    out: list[str] = []
    for raw in types:
        name = str(raw).strip().lower().removeprefix("minecraft:")
        if not name or name in _NON_BLOCK_FIND_TYPES:
            continue
        if nether and name in {"log", "logs"}:
            # Generic "log" in the nether means the local wood family. Expanding
            # to overworld logs only makes the server reject/empty the query.
            out.extend(NETHER_STEM_TYPES)
        elif nether and name == "planks":
            out.extend(("crimson_planks", "warped_planks"))
        else:
            out.extend(_FIND_BLOCK_ALIASES.get(name, (name,)))
    return list(dict.fromkeys(out))


COMPACT_CORE_TOOLS: tuple[str, ...] = (
    "mc_find_blocks",
    "mc_get_entities",
    "mc_move_to",
    "mc_dig",
    "mc_place",
    "mc_attack",
    "mc_skill_harvest_logs",
    "mc_skill_craft_gear",
    "mc_skill_mine_blocks",
    "mc_skill_smelt",
    "mc_skill_staircase",
    "mc_skill_tunnel",
    "mc_skill_mine_dfs",
    "mc_skill_flee",
    "mc_skill_unstick",
)

_PASSABLE_SIGHT_BLOCKS: set[str] = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "flowing_water",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "torch",
    "wall_torch",
    "vine",
    "snow",
    "leaf_litter",
    "sugar_cane",
    "dandelion",
    "poppy",
    "dead_bush",
    "glass",
    "glass_pane",
}


class McToolset:
    def __init__(
        self,
        bot: BotSession,
        *,
        geofence: VoidGeofence | None = None,
        world_memory: WorldMemory | None = None,
        wait_for_cooldown: bool = False,
        stop_check: Callable[[], bool] | None = None,
        on_cooldown_wait: Callable[[str, float], None] | None = None,
        episode_deadline_mono: float | None = None,
    ) -> None:
        self.bot = bot
        self._state_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._wait_for_cooldown = bool(wait_for_cooldown)
        self._stop_check = stop_check
        self._on_cooldown_wait = on_cooldown_wait
        self._episode_deadline_mono: float | None = episode_deadline_mono
        self._last_event_seq: int = int(getattr(bot, "_last_event_seq", 0) or 0)
        self._in_water: bool = False
        self._last_block_pos: list[int] | None = None
        self._last_dimension: str = "overworld"
        self._last_biome: str = ""
        self._entity_observations: dict[int, dict[str, Any]] = {}
        self._entity_observed_mono: float = 0.0
        self._entity_observed_dimension: str = "overworld"
        self._geofence_blocked_count: int = 0
        self._path_not_found_targets: list[tuple[float, float]] = []
        self._empty_max_scans: dict[tuple[str, tuple[str, ...]], tuple[int, tuple[float, float] | None]] = {}
        self.geofence = geofence if geofence is not None else VoidGeofence.from_env()
        self.world_memory = (
            world_memory
            if world_memory is not None
            else WorldMemory(default_memory_path_for_bot(bot))
        )
        self.skills = MacroSkills(bot, geofence=self.geofence)
        self.skills.world_memory = self.world_memory

    def rebind_runtime_hooks(
        self,
        *,
        geofence: VoidGeofence | None = None,
        wait_for_cooldown: bool | None = None,
        stop_check: Callable[[], bool] | None = None,
        on_cooldown_wait: Callable[[str, float], None] | None = None,
        episode_deadline_mono: float | None = None,
    ) -> None:
        """Rebind per-episode timeout/cooldown callbacks on a persistent shared McToolset."""
        if geofence is not None:
            self.geofence = geofence
            self.skills.geofence = geofence
        if wait_for_cooldown is not None:
            self._wait_for_cooldown = bool(wait_for_cooldown)
        self._stop_check = stop_check
        self._on_cooldown_wait = on_cooldown_wait
        self._episode_deadline_mono = episode_deadline_mono
        self._geofence_blocked_count = 0
        self._path_not_found_targets.clear()
        self._empty_max_scans.clear()

    def _annotate_scan_ceiling(
        self,
        parsed: dict[str, Any],
        res_obj: dict[str, Any] | None,
        *,
        tool_name: str,
        requested_radius: int,
        effective_radius: int,
        query_types: list[str] | None,
        empty: bool,
        cur_pos: tuple[float, float, float] | list[float] | None,
    ) -> bool:
        """Surface the 64m scan ceiling and break futile radius-doubling loops (long-run Bottleneck #1)."""
        changed = False
        if requested_radius > effective_radius and isinstance(res_obj, dict):
            res_obj["radius_clamped_from"] = requested_radius
            res_obj["radius_effective"] = effective_radius
            res_obj["hint"] = (
                f"{tool_name} 最大扫描半径硬上限为 {effective_radius}m（已将 radius={requested_radius} 钳制为 {effective_radius}m）。"
                "原地继续放大 radius 不会扫描到更远区块；若当前 64m 内无目标，请先移动 >=40 格换区，或直接输出阻断结论切换目标。"
            )
            changed = True
        if not empty or (effective_radius < 48 and requested_radius <= 64):
            return changed
        norm_key = (
            tool_name,
            tuple(sorted(str(t).strip().lower().removeprefix("minecraft:") for t in (query_types or []))),
        )
        pos_xz = (float(cur_pos[0]), float(cur_pos[2])) if cur_pos is not None and len(cur_pos) >= 3 else None
        with self._state_lock:
            prev_count, prev_xz = self._empty_max_scans.get(norm_key, (0, None))
            moved_far = (
                pos_xz is not None
                and prev_xz is not None
                and math.hypot(pos_xz[0] - prev_xz[0], pos_xz[1] - prev_xz[1]) >= 24.0
            )
            count = 1 if moved_far else prev_count + 1
            self._empty_max_scans[norm_key] = (count, pos_xz if pos_xz is not None else prev_xz)
        if count >= 2:
            parsed["requires_replan"] = True
            parsed["scan_exhausted"] = True
            parsed["empty_max_scan_count"] = count
            msg = (
                f"你在当前位置附近已连续 {count} 次执行大半径({effective_radius}m)扫描且结果为 0。"
                "服务端仅加载玩家周围约 64m 区块，严禁继续放大 radius 或原地重复扫描！"
                "请立即移动 >=40 格换区，或直接结束本集汇报目标阻断。"
            )
            parsed["hint"] = msg
            if isinstance(res_obj, dict):
                res_obj["hint"] = msg
            changed = True
        return changed

    def _clamp_tool_budget(
        self,
        requested_s: float,
        *,
        max_s: float = 55.0,
        min_s: float = 5.0,
        reserve_s: float = 4.0,
    ) -> tuple[float, bool]:
        """Clamp long-running skill budgets to [min_s, max_s] and remaining episode deadline."""
        eff = max(float(min_s), min(float(max_s), float(requested_s)))
        if self._episode_deadline_mono is not None:
            rem = self._episode_deadline_mono - time.monotonic() - float(reserve_s)
            if rem <= 2.0:
                return (0.0, True)
            eff = min(eff, max(2.0, rem))
        return (eff, False)

    def _record_geofence_block(
        self,
        blocked: dict[str, Any],
        *,
        safe_directions: list[str] | None = None,
    ) -> str:
        """BUG-048: track per-episode geofence blocks and escalate to requires_replan after 3 hits."""
        with self._state_lock:
            self._geofence_blocked_count += 1
            count = self._geofence_blocked_count
        out = dict(blocked)
        out["geofence_blocked_count"] = count
        if safe_directions is not None:
            out["suggested_safe_directions"] = list(safe_directions)
        if count >= 3:
            out["requires_replan"] = True
            out["hint"] = (
                f"本集已连续 {count} 次触发安全围栏拦截，请放弃当前越界方向/目标，"
                "先调用 mc_status 确认当前位置并向安全中心方向换新目标，不要继续微调越界坐标。"
            )
        return json.dumps(out, ensure_ascii=False)

    def _set_last_event_seq(self, seq: int) -> None:
        with self._state_lock:
            if seq > self._last_event_seq:
                self._last_event_seq = seq
            try:
                setattr(self.bot, "_last_event_seq", self._last_event_seq)
            except Exception:
                pass

    def _reconnect_once(self, *, quarantined: bool = False) -> None:
        with self._reconnect_lock:
            if quarantined and hasattr(self.bot, "recover_quarantine"):
                try:
                    self.bot.recover_quarantine(timeout_s=30.0)
                    return
                except DeathLoopCooldownError:
                    if not (self._wait_for_cooldown and hasattr(self.bot, "ensure_connected_and_ready")):
                        raise
            if self._wait_for_cooldown and hasattr(self.bot, "ensure_connected_and_ready"):
                self.bot.ensure_connected_and_ready(
                    timeout_s=45.0,
                    poll_interval_s=1.0,
                    wait_for_cooldown=True,
                    stop_check=self._stop_check,
                    on_wait=self._on_cooldown_wait,
                )
            else:
                self.bot.wait_until_ready(timeout_s=30.0, poll_interval_s=1.0)

    def _update_status_cache(self, st: dict[str, Any]) -> None:
        pos = st.get("pos")
        with self._state_lock:
            self._in_water = bool(st.get("in_water") or st.get("submerged"))
            if st.get("dimension"):
                self._last_dimension = normalize_dimension(st.get("dimension"))
            if "biome" in st and st.get("biome") is not None:
                self._last_biome = str(st.get("biome") or "")
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                try:
                    self._last_block_pos = [
                        math.floor(float(pos[0])),
                        math.floor(float(pos[1])),
                        math.floor(float(pos[2])),
                    ]
                except (TypeError, ValueError):
                    pass
        try:
            self.world_memory.observe_status(st)
        except Exception:
            pass

    def _cached_env(self, *, fetch_if_missing: bool = False) -> tuple[str, list[int] | None, str]:
        with self._state_lock:
            dim = self._last_dimension
            pos = list(self._last_block_pos) if self._last_block_pos is not None else None
            biome = self._last_biome
        if fetch_if_missing and pos is None and callable(getattr(self.bot, "status", None)):
            try:
                st = self.bot.status()
                if isinstance(st, dict):
                    self._update_status_cache(st)
                    with self._state_lock:
                        dim = self._last_dimension
                        pos = list(self._last_block_pos) if self._last_block_pos is not None else None
                        biome = self._last_biome
            except (ApiError, httpx.HTTPError, RuntimeError, ValueError):
                pass
        return dim, pos, biome

    def assess_target(
        self,
        target: str | list[str] | tuple[str, ...],
        *,
        mode: str = "auto",
        radius: int = 16,
    ) -> dict[str, Any]:
        """评估当前维度、高度与群系下寻找/挖掘或放置/交互指定目标的可行性。"""
        dim, pos, biome = self._cached_env(fetch_if_missing=True)
        return assess_world_target(
            target,
            dimension=dim,
            y=(pos[1] if pos is not None else None),
            biome=biome,
            radius=radius,
            mode=mode,
        )

    def last_block_pos(self) -> list[int] | None:
        with self._state_lock:
            return list(self._last_block_pos) if self._last_block_pos is not None else None

    def update_block_pos(self, pos: Any) -> None:
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        try:
            blk = [math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))]
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._last_block_pos = blk

    def _is_in_water(self) -> bool:
        with self._state_lock:
            return self._in_water

    def _get_block_name(self, x: int, y: int, z: int) -> str:
        res = self.bot.call("get_block", {"pos": [x, y, z]})
        raw = res.get("name") or res.get("block") or "air"
        return str(raw).removeprefix("minecraft:")

    def _ray_wall_blocker(
        self,
        start_xyz: tuple[float, float, float],
        end_xyz: tuple[float, float, float],
    ) -> dict[str, Any] | None:
        sx, sy, sz = start_xyz[0], start_xyz[1] + 1.62, start_xyz[2]
        ex, ey, ez = end_xyz[0], end_xyz[1] + 1.0, end_xyz[2]
        dist = math.sqrt((ex - sx) ** 2 + (ey - sy) ** 2 + (ez - sz) ** 2)
        if dist < 1.0:
            return None

        bx, by, bz = math.floor(start_xyz[0]), math.floor(start_xyz[1]), math.floor(start_xyz[2])
        hx, hy, hz = math.floor(end_xyz[0]), math.floor(end_xyz[1]), math.floor(end_xyz[2])
        skip_cells = {(bx, by, bz), (bx, by + 1, bz), (hx, hy, hz), (hx, hy + 1, hz)}

        steps = max(2, int(math.ceil(dist * 2.0)))
        visited: set[tuple[int, int, int]] = set()
        probes: list[tuple[int, int, int]] = []
        for idx in range(1, steps):
            t = idx / steps
            cell = (
                math.floor(sx + (ex - sx) * t),
                math.floor(sy + (ey - sy) * t),
                math.floor(sz + (ez - sz) * t),
            )
            if cell in skip_cells or cell in visited:
                continue
            visited.add(cell)
            probes.append(cell)
            if len(probes) >= 18:
                break

        for vx, vy, vz in probes:
            try:
                bname = self._get_block_name(vx, vy, vz)
            except (ApiError, httpx.HTTPError):
                return None
            if not is_passable_sight_block(bname):
                return {"name": bname, "pos": [vx, vy, vz]}
        return None

    def _enrich_status_awareness(self, st: dict[str, Any]) -> None:
        nh = st.get("nearest_hostile")
        pos = st.get("pos")
        if not isinstance(nh, dict) or not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

        if not st.get("in_water"):
            try:
                for dy in (2, 3):
                    roof_name = self._get_block_name(bx, by + dy, bz)
                    if roof_name not in _PASSABLE_SIGHT_BLOCKS:
                        st["enclosure"] = {
                            "roofed_pocket": True,
                            "ceiling_block": roof_name,
                            "note": "上方有遮盖，但不代表四周封闭；确认无新受伤和无遮挡近敌后，可在掩体内就地合成/熔炼。",
                        }
                        break
            except (ApiError, httpx.HTTPError):
                pass

        # Occlusion is an observation of one ray at one instant, never proof of safety.
        assessment: dict[str, Any] = {
            "hostile_blocked_by_wall": None,
            "safe_in_place": False,
            "assessment_complete": False,
            "nearby_hostiles": [],
            "note": "附近敌情尚未确认；不要把未知实体视为隔墙安全。",
        }
        st["threat_assessment"] = assessment
        try:
            ents = self.bot.call("get_entities", {"radius": 28, "types": ["hostile"]})
        except (ApiError, httpx.HTTPError):
            return
        elist = ents.get("entities") or []
        candidates = []
        for ent in elist:
            if not isinstance(ent, dict):
                continue
            epos = ent.get("pos")
            if not isinstance(epos, (list, tuple)) or len(epos) < 3:
                continue
            try:
                xyz = tuple(float(v) for v in epos[:3])
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in xyz):
                continue
            candidates.append((math.dist((px, py, pz), xyz), ent, xyz))
        candidates.sort(key=lambda row: row[0])

        for distance, ent, xyz in candidates[:5]:
            blocker = self._ray_wall_blocker((px, py, pz), xyz)
            row = {
                "id": ent.get("id"), "name": ent.get("name"),
                "dist": round(distance, 1), "blocked_by_wall": blocker is not None,
            }
            if blocker is not None:
                row["blocking_block"] = blocker
            assessment["nearby_hostiles"].append(row)
            if nh.get("id") is not None and ent.get("id") == nh["id"]:
                assessment["hostile_blocked_by_wall"] = blocker is not None
                assessment["dy"] = round(xyz[1] - py, 1)
                if blocker is not None:
                    assessment["blocking_block"] = blocker

        checked = assessment["nearby_hostiles"]
        assessment["hostiles_observed"] = len(elist)
        assessment["hostiles_checked"] = len(checked)
        assessment["assessment_complete"] = (
            len(checked) == len(elist) and (not nh or assessment["hostile_blocked_by_wall"] is not None)
        )
        assessment["all_checked_hostiles_occluded"] = bool(checked) and all(r["blocked_by_wall"] for r in checked)
        if any(not r["blocked_by_wall"] for r in checked):
            assessment["note"] = "附近有无遮挡或遮挡未确认的怪物；最近一只隔墙不能代表整体安全。优先保持掩体、举盾或封堵，受伤时暂停采集/合成。"
        elif checked:
            assessment["note"] = "已检查怪物的当前射线均有遮挡，可保持现有掩体；仍需留意未检查实体、绕路和新受伤，勿据此认定绝对安全或盲目冲向地表。"

    def _call_json(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout_s: float | None = None,
    ) -> str:
        call_kw: dict[str, Any] = {"method": method}
        if timeout_s is not None:
            call_kw["timeout_s"] = float(timeout_s)
        try:
            result = self.bot.call(path, body, **call_kw)
        except AuthError as exc:
            raise RuntimeError(f"MC API 会话不可继续：{exc}") from None
        except DeathLoopCooldownError as exc:
            if not self._wait_for_cooldown:
                raise RuntimeError(f"MC API 会话不可继续：{exc}") from None
            try:
                self._reconnect_once(quarantined=False)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except ApiError as exc:
            self.update_block_pos(exc.context.get("pos") or exc.context.get("from"))
            if exc.code not in ("not_connected", "action_quarantined"):
                return self._format_api_error(path, body or {}, exc)
            try:
                self._reconnect_once(quarantined=(exc.code == "action_quarantined"))
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.TimeoutException:
            if timeout_s is not None and path == "move_to":
                try:
                    self.bot.call("stop", {}, timeout_s=2.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
                return json.dumps(
                    {
                        "ok": False,
                        "requires_replan": True,
                        "error": {
                            "code": "move_timeout",
                            "message": f"move_to 超过 {round(float(timeout_s), 1)}s 预算已中止移动，请缩短步长或更换路径。",
                        },
                    },
                    ensure_ascii=False,
                )
            try:
                self._reconnect_once(quarantined=True)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError as retry_not_ready:
                return self._format_api_error(path, body or {}, retry_not_ready)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.HTTPError as exc:
            # A timed-out action may still be running server-side. Recover the
            # quarantined session and retry the same request once.
            try:
                self._reconnect_once(quarantined=True)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(
                    f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}"
                ) from None

        if path == "status" and isinstance(result, dict):
            self._update_status_cache(result)
            self._enrich_status_awareness(result)
        elif isinstance(result, dict):
            self.update_block_pos(result.get("pos_after") or result.get("pos"))
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)

    @staticmethod
    def _format_api_error(action: str, params: dict[str, Any], exc: ApiError) -> str:
        err_obj: dict[str, Any] = {"code": exc.code, "message": exc.message, **exc.context}
        if action == "dig" and str(exc.code).lower() in ("out_of_reach", "path_not_found"):
            target_pos = params.get("pos")
            bot_pos = err_obj.get("pos") or err_obj.get("from")
            if (
                isinstance(target_pos, (list, tuple))
                and len(target_pos) >= 3
                and isinstance(bot_pos, (list, tuple))
                and len(bot_pos) >= 3
            ):
                try:
                    err_obj["dist"] = round(
                        math.dist(
                            [float(target_pos[0]), float(target_pos[1]), float(target_pos[2])],
                            [float(bot_pos[0]), float(bot_pos[1]), float(bot_pos[2])],
                        ),
                        1,
                    )
                except (TypeError, ValueError):
                    pass
        if action == "place" and exc.code in {"no_support", "blocked_by_self", "place_failed", "occupied"}:
            target = params.get("pos")
            if exc.code == "blocked_by_self":
                err_obj["recovery_hint"] = "先用 mc_move_to 到至少 3 格外，设 range=0.5，让出目标格，再放置；也可省略 x/y/z 自动选落点。"
            elif exc.code == "no_support":
                err_obj["recovery_hint"] = (
                    "目标需要相邻实心方块支撑；先用 mc_get_block 检查目标与下方，选择贴着实心面的空格。"
                    "若四周全空，先在旁边 mc_dig 一格侧袋制造参考面，或换一个贴着实心块的坐标，"
                    "也可省略 x/y/z 自动选落点；不要重复同一坐标。"
                )
            else:
                err_obj["recovery_hint"] = (f"先用 mc_get_block 检查目标 {target}；若仍是 sugar_cane、草或其他可清理障碍，先 mc_dig 清空目标格，再 mc_place。不要重复向同一占用格放置。")
        if action == "craft":
            err_obj = explain_material_craft_error(params.get("item", ""), params.get("count", 1), err_obj)
        if action == "use_item" and str(exc.code).lower() == "missing_item" and isinstance(err_obj.get("inventory"), dict):
            inv_map = err_obj["inventory"]
            avail_foods = [
                f
                for f in (*SAFE_FOOD_PRIORITY, "golden_apple", "enchanted_golden_apple")
                if int(inv_map.get(f) or 0) > 0
            ]
            if avail_foods:
                err_obj["kb_hint"] = (
                    f"背包中没有 {params.get('item')}，但已有可食用食物: {avail_foods}；"
                    f"请改传 mc_use_item(item='{avail_foods[0]}')！"
                )
            else:
                err_obj["no_food_in_inventory"] = True
                err_obj["kb_hint"] = (
                    "当前背包内没有任何可食用食物(0食物)！严禁继续调用 mc_use_item 空吃 bread/apple/golden_apple 等不存在的食物！"
                    "请立即停止进食尝试，继续执行挖矿/任务，或待地表安全时猎杀动物获取食物。"
                )
            return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)
        hint = build_jit_error_hint(action, params, exc.code, exc.message)
        if (
            action == "craft"
            and str(exc.code).lower() == "missing_ingredients"
            and params.get("item") in ("stick", "crafting_table")
            and isinstance(err_obj.get("inventory"), dict)
        ):
            inv_map = err_obj["inventory"]
            has_planks = any(k.endswith("_planks") and int(v or 0) > 0 for k, v in inv_map.items())
            logs = [k for k, v in inv_map.items() if k.endswith("_log") and int(v or 0) > 0]
            if not has_planks and logs:
                extra = f"背包有原木({','.join(logs)})但木板为0，请先将 {logs[0]} 合成为木板(*_planks)，或直接调 mc_skill_craft_gear！"
                hint = f"{hint} {extra}" if hint else extra
        if hint:
            err_obj["kb_hint"] = hint
        return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)

    def _run_skill(self, fn) -> str:
        try:
            result = fn()
        except (AuthError, SafetyEmergency) as exc:
            raise RuntimeError(f"MC 技能触发紧急停机：{exc}") from None
        except DeathLoopCooldownError as exc:
            if not self._wait_for_cooldown:
                raise RuntimeError(f"MC 技能触发紧急停机：{exc}") from None
            try:
                self._reconnect_once(quarantined=False)
                result = fn()
            except (AuthError, DeathLoopCooldownError, SafetyEmergency) as re_exc:
                raise RuntimeError(f"MC 技能触发紧急停机：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error("skill", {}, exc)
            except ApiError as retry_exc:
                return self._format_api_error("skill", {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except ApiError as exc:
            if exc.code not in ("not_connected", "action_quarantined"):
                return self._format_api_error("skill", {}, exc)
            try:
                self._reconnect_once(quarantined=(exc.code == "action_quarantined"))
                result = fn()
            except (AuthError, DeathLoopCooldownError, SafetyEmergency) as re_exc:
                raise RuntimeError(f"MC 技能触发紧急停机：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error("skill", {}, exc)
            except ApiError as retry_exc:
                return self._format_api_error("skill", {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"MC API 网络请求异常（{type(exc).__name__}）：{exc}") from None

        if isinstance(result, dict):
            self.update_block_pos(result.get("pos_after") or result.get("pos"))
        return json.dumps(result, ensure_ascii=False)

    # ---- Shared Read-Only Observations ----

    def mc_status(self) -> str:
        return self._call_json("status", method="GET")

    def mc_inventory(self) -> str:
        raw = self._call_json("inventory", method="GET")
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(parsed.get("result"), dict):
                self.bot._last_inventory = parsed["result"]
        except ValueError:
            pass
        return raw

    _CROP_BLOCK_TYPES = frozenset(
        {
            "wheat",
            "carrots",
            "potatoes",
            "beetroots",
            "nether_wart",
            "cocoa",
            "sweet_berry_bush",
            "torchflower_crop",
            "pitcher_crop",
            "melon_stem",
            "pumpkin_stem",
        }
    )

    def mc_find_blocks(
        self,
        radius: int = 16,
        types: list[str] | None = None,
        limit: int = 20,
        mature: bool | None = None,
    ) -> str:
        requested_radius = int(radius)
        radius = max(1, min(64, requested_radius))
        limit = max(1, min(60, limit))
        payload: dict[str, Any] = {"radius": radius, "limit": limit}
        ignored: list[str] = []
        if types:
            requested = [str(name).strip().lower().removeprefix("minecraft:") for name in types]
            ignored = list(dict.fromkeys(name for name in requested if name in _NON_BLOCK_FIND_TYPES))
            dim_alias, _, _ = self._cached_env(fetch_if_missing=False)
            types = _normalize_find_block_types(types, dimension=dim_alias)
            if not types:
                return _invalid_find_block_query(ignored or requested)
            payload["types"] = types
        if mature is not None:
            norm_types = [str(t).removeprefix("minecraft:").strip().lower() for t in (types or [])]
            if not norm_types or any(t in self._CROP_BLOCK_TYPES for t in norm_types):
                payload["mature"] = mature
        while True:
            raw = self._call_json("find_blocks", payload)
            try:
                parsed = json.loads(raw)
            except ValueError:
                return raw
            err = parsed.get("error") if isinstance(parsed, dict) else None
            unknown = (re.search(r"\bunknown block\s+([a-z0-9_:]+)", str(err.get("message") or ""), re.IGNORECASE)
                       if isinstance(err, dict) and err.get("code") == "bad_args" else None)
            bad_type = unknown.group(1).lower().removeprefix("minecraft:") if unknown else None
            if not bad_type or not types or bad_type not in types:
                break
            ignored.append(bad_type)
            types = [name for name in types if name != bad_type]
            if not types:
                return _invalid_find_block_query(ignored)
            payload["types"] = types
        if ignored and isinstance(parsed, dict):
            parsed["ignored_types"] = ignored
            raw = json.dumps(parsed, ensure_ascii=False)
        res_obj = parsed.get("result") if isinstance(parsed, dict) else None
        blocks = res_obj.get("blocks") if isinstance(res_obj, dict) else None
        if (parsed.get("ok") is True and isinstance(res_obj, dict)
                and isinstance(res_obj.get("total"), int) and isinstance(blocks, list)):
            # total is the backend scan count, not the number of returned rows.
            # Do not rewrite it; expose the truncation explicitly (P2-9).
            returned = len(blocks)
            res_obj["returned"] = returned
            if res_obj["total"] > returned:
                res_obj["truncated"] = True
                res_obj["total_note"] = (
                    "total 为后端在半径内扫描到的匹配总数；blocks 仅返回前 limit 条。"
                    "请缩小 radius/types 或提高 limit（上限 60），不要把 total 当作可访问方块数。"
                )
            raw = json.dumps(parsed, ensure_ascii=False)
        if parsed.get("ok") is True and isinstance(blocks, list) and blocks:
            dim_obs, _, _ = self._cached_env(fetch_if_missing=False)
            self.world_memory.observe_blocks(blocks, dim=dim_obs)
        if types:
            if parsed.get("ok") is True and isinstance(blocks, list) and len(blocks) == 0:
                dim, pos, biome = self._cached_env(fetch_if_missing=True)
                if pos is not None:
                    def _verify_live(pt: tuple[int, int, int]) -> str | None:
                        try:
                            return self._get_block_name(pt[0], pt[1], pt[2])
                        except Exception:
                            return None

                    mem_hits = self.world_memory.query_pois(
                        types,
                        origin=pos,
                        dim=dim,
                        limit=3,
                        verify_fn=_verify_live,
                        verify_radius=32.0,
                    )
                    if mem_hits:
                        res_obj["memory_blocks"] = mem_hits
                        res_obj["memory_hint"] = (
                            f"当前 {radius}m 内未扫到目标，但记忆库已探明最近 {mem_hits[0]['name']} "
                            f"位于 {mem_hits[0]['pos']}（距此 {mem_hits[0]['dist']}m，{mem_hits[0]['bearing']}），"
                            "可直接调 mc_skill_mine_blocks 或 mc_move_to(x, z) 分段前往。"
                        )
                        return json.dumps(parsed, ensure_ascii=False)
                check = assess_world_target(
                    types,
                    dimension=dim,
                    y=(pos[1] if pos is not None else None),
                    biome=biome,
                    radius=radius,
                    mode="find",
                )
                if check.get("available") is False:
                    res_obj["ecology_check"] = check
                    res_obj["hint"] = check["hint"]
                    return json.dumps(parsed, ensure_ascii=False)
        if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(res_obj, dict):
            _, cur_pos, _ = self._cached_env(fetch_if_missing=False)
            if self._annotate_scan_ceiling(
                parsed,
                res_obj,
                tool_name="mc_find_blocks",
                requested_radius=requested_radius,
                effective_radius=radius,
                query_types=types,
                empty=(isinstance(blocks, list) and len(blocks) == 0),
                cur_pos=cur_pos,
            ):
                return json.dumps(parsed, ensure_ascii=False)
        return raw

    def mc_get_block(self, x: int, y: int, z: int) -> str:
        return self._call_json("get_block", {"pos": [x, y, z]})

    def mc_get_entities(self, radius: int = 32, types: list[str] | None = None) -> str:
        requested_radius = int(radius)
        radius = max(1, min(64, requested_radius))
        payload: dict[str, Any] = {"radius": radius}
        if types:
            payload["types"] = types
        raw = self._call_json("get_entities", payload)
        try:
            parsed = json.loads(raw)
            res_ent = parsed.get("result") if isinstance(parsed, dict) else None
            ents = (res_ent or {}).get("entities") or []
            dim_ent, pos_ent, _ = self._cached_env(fetch_if_missing=False)
            if isinstance(parsed, dict) and parsed.get("ok") is True:
                self._entity_observations = {int(ent["id"]): ent for ent in ents if isinstance(ent, dict) and isinstance(ent.get("id"), int)}
                self._entity_observed_mono = time.monotonic()
                self._entity_observed_dimension = dim_ent
            if ents:
                self.world_memory.observe_entities(ents, dim=dim_ent)
            elif types and isinstance(res_ent, dict) and pos_ent is not None:
                hab_hits = self.world_memory.query_habitats(types, origin=pos_ent, dim=dim_ent, limit=3)
                if hab_hits:
                    res_ent["memory_habitats"] = hab_hits
                    return json.dumps(parsed, ensure_ascii=False)
            near_hostiles = [
                ent
                for ent in ents
                if isinstance(ent, dict)
                and ent.get("type") == "hostile"
                and float(ent.get("dist", 999)) <= 24.0
                and isinstance(ent.get("pos"), (list, tuple))
                and len(ent["pos"]) >= 3
            ][:5]
            if near_hostiles:
                st = self.bot.status()
                spos = st.get("pos") or []
                if isinstance(spos, (list, tuple)) and len(spos) >= 3:
                    start_xyz = (float(spos[0]), float(spos[1]), float(spos[2]))
                    for ent in near_hostiles:
                        epos = ent["pos"]
                        blocker = self._ray_wall_blocker(
                            start_xyz,
                            (float(epos[0]), float(epos[1]), float(epos[2])),
                        )
                        ent["blocked_by_wall"] = blocker is not None
                        if blocker is not None:
                            ent["blocking_block"] = blocker
                    return json.dumps(parsed, ensure_ascii=False)
            if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(res_ent, dict):
                if self._annotate_scan_ceiling(
                    parsed,
                    res_ent,
                    tool_name="mc_get_entities",
                    requested_radius=requested_radius,
                    effective_radius=radius,
                    query_types=types,
                    empty=(len(ents) == 0),
                    cur_pos=pos_ent,
                ):
                    return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass
        return raw

    def mc_events(self, limit: int = 50) -> str:
        limit = max(1, min(200, limit))
        with self._state_lock:
            since = self._last_event_seq
        raw = self._call_json("events", {"since": since, "limit": limit})
        try:
            parsed = json.loads(raw)
            if parsed.get("ok") and isinstance(parsed.get("result"), dict):
                res = parsed["result"]
                evs = res.get("events") or []
                page_max = max(
                    [int(res.get("last_seq") or 0)]
                    + [int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)],
                    default=0,
                )
                if page_max > 0:
                    self._set_last_event_seq(page_max)
        except ValueError:
            pass
        return raw

    def sync_event_cursor(self) -> int:
        """翻页消费全部开局前历史事件，避免把旧死亡/受伤当成本轮新事件重放。"""
        for _ in range(50):
            with self._state_lock:
                since = self._last_event_seq
            try:
                res = self.bot.events(since=since, limit=50)
            except AuthError:
                raise
            except DeathLoopCooldownError:
                if self._wait_for_cooldown:
                    break
                raise
            except (ApiError, httpx.HTTPError):
                break
            if not isinstance(res, dict):
                break
            evs = res.get("events") or []
            evs_max = max(
                (int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)),
                default=0,
            )
            api_last = int(res.get("last_seq") or 0)
            # Only jump to server-reported last_seq when the current page is final (<50 items);
            # otherwise advance only to the highest seq actually seen on this page so next page isn't skipped.
            page_max = evs_max if len(evs) >= 50 and evs_max > since else max(evs_max, api_last)
            self._set_last_event_seq(page_max)
            if len(evs) < 50 or page_max <= since:
                break
        with self._state_lock:
            return self._last_event_seq

    def drain_notable_events(self) -> str:
        """增量拉取最近事件并提取他人聊天、新解锁进度与受伤/死亡/重生提醒。"""
        notes: list[str] = []
        bot_name = (self.bot.bot_username or "").strip()
        for _ in range(10):
            with self._state_lock:
                since = self._last_event_seq
            try:
                res = self.bot.events(since=since, limit=50)
            except AuthError:
                raise
            except DeathLoopCooldownError:
                if self._wait_for_cooldown:
                    break
                raise
            except (ApiError, httpx.HTTPError):
                break
            if not isinstance(res, dict):
                break
            evs = res.get("events") or []
            evs_max = max(
                (int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)),
                default=0,
            )
            api_last = int(res.get("last_seq") or 0)
            # Only jump to server-reported last_seq when the current page is final (<50 items);
            # otherwise advance only to the highest seq actually seen on this page so next page isn't skipped.
            page_max = evs_max if len(evs) >= 50 and evs_max > since else max(evs_max, api_last)
            self._set_last_event_seq(page_max)
            for ev in evs:
                if not isinstance(ev, dict):
                    continue
                ev_seq = ev.get("seq")
                if isinstance(ev_seq, int) and ev_seq <= since:
                    continue
                kind = str(ev.get("type") or ev.get("event") or "").lower()
                if kind == "chat":
                    sender = str(ev.get("username") or ev.get("from") or ev.get("sender") or "")
                    msg = str(ev.get("message") or ev.get("text") or "").strip()
                    if not msg:
                        continue
                    if sender and bot_name and sender == bot_name:
                        continue
                    if not sender and bot_name and msg.startswith(f"{bot_name} "):
                        continue
                    # Drop multiplayer server broadcast noise for other bots/players (advancements, deaths, joins)
                    if not sender or sender.lower() == "unknown":
                        low_msg = msg.lower()
                        if any(
                            m in low_msg
                            for m in (
                                "has made the advancement",
                                "has completed the challenge",
                                "has reached the goal",
                                "was slain by",
                                "was shot by",
                                "was blown up by",
                                "drowned",
                                "fell from a high place",
                                "hit the ground too hard",
                                "tried to swim in lava",
                                "suffocated in a wall",
                                "burned to death",
                                "joined the game",
                                "left the game",
                            )
                        ):
                            continue
                    notes.append(f"[不可信玩家公屏消息 {sender or 'unknown'}: {msg[:120]}]")
                elif kind in ("advancement", "advancements"):
                    adv = ev.get("advancement") or ev.get("id") or ev.get("title") or ev.get("name") or ""
                    if adv:
                        notes.append(f"[新解锁进度: {adv}]")
                elif kind == "hurt":
                    hp = ev.get("health")
                    src = ev.get("source") or ev.get("by") or ""
                    notes.append(f"[受伤 health={hp}{(' by ' + str(src)) if src else ''}]")
                elif kind == "death":
                    rec_fn = getattr(self.bot, "record_death_event", None)
                    if callable(rec_fn):
                        rec_fn(ev_seq if ev_seq is not None else ev.get("t"))
                    cause = ev.get("cause") or ev.get("message") or ""
                    pos = ev.get("pos")
                    drop_pos = ev.get("dropped_items_at") or pos
                    try:
                        self.world_memory.record_death(drop_pos, dim=self._last_dimension)
                    except Exception:
                        pass
                    notes.append(f"[死亡 pos={pos} {cause}]")
                elif kind in ("respawn", "spawn"):
                    notes.append(f"[已重生 pos={ev.get('pos')}]")
            if len(evs) < 50 or page_max <= since:
                break
        return "; ".join(notes)

    def mc_advancements(self, status: str | None = None) -> str:
        payload: dict[str, Any] = {"include_recipes": False}
        if status:
            payload["status"] = status
        return self._call_json("advancements", payload)

    # ---- Exclusive Actions ----

    @staticmethod
    def _annotate_stale_entity(raw: str, entity_id: int | None) -> str:
        if entity_id is None:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict) or parsed.get("ok") is not False:
            return raw
        err = parsed.get("error") if isinstance(parsed.get("error"), dict) else None
        if err is None or err.get("code") != "no_such_entity":
            return raw
        parsed["requires_replan"] = True
        err["message"] = (
            f"实体 {entity_id} 已不可见或消失；请重新调 mc_get_entities 获取最新 id/位置，"
            "不要重复使用旧 id。"
        )
        return json.dumps(parsed, ensure_ascii=False)

    def mc_move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        entity_id: int | None = None,
        range: float = 2.0,
        ignore_hurt: bool = False,
    ) -> str:
        range = max(0.2, min(16.0, float(range)))
        payload: dict[str, Any] = {"range": range}
        geofence_clamped_info: dict[str, Any] | None = None
        if x is not None and z is not None:
            orig_x, orig_z = float(x), float(z)
            clamped = self.geofence.clamped_move_target(orig_x, orig_z)
            if clamped is not None:
                x, z = clamped[0], clamped[1]
                geofence_clamped_info = {
                    "geofence_clamped": True,
                    "geofence_original_xz": [round(orig_x, 1), round(orig_z, 1)],
                    "geofence_clamped_xz": [round(float(x), 1), round(float(z), 1)],
                }
            else:
                blocked = self.geofence.check_point(orig_x, orig_z, action="mc_move_to")
                if blocked is not None:
                    return self._record_geofence_block(blocked)
            payload["x"] = x
            payload["z"] = z
            if y is not None:
                yf = float(y)
                last_y = float(self._last_block_pos[1]) if self._last_block_pos and len(self._last_block_pos) >= 3 else None
                bogus_zero_y = (yf == 0.0) and (last_y is None or abs(last_y) >= 16.0)
                if not (-64.0 <= yf <= 320.0):
                    return json.dumps(
                        {
                            "ok": False,
                            "requires_replan": True,
                            "error": {
                                "code": "bad_args",
                                "message": (
                                    f"y={yf:g} 超出主世界/下界合法高度(-64..320)，疑似把 z 填进了 y。"
                                    "请检查坐标后重传 x/z（y 可省略让寻路决定），不要用同一参数重试。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                if not bogus_zero_y:
                    payload["y"] = yf
        elif entity_id is not None:
            payload["entity_id"] = entity_id
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "需要提供 (x, z) 坐标或 entity_id"}},
                ensure_ascii=False,
            )
        # 水中自动保护：防止溺水扣血打断自己的游上岸动作
        if ignore_hurt or self._is_in_water():
            payload["stop_on"] = ["death"]
        # r12: a stale action lease made /move_to block ~101s before returning
        # busy. /status already exposes the lease owner, so probe it cheaply and
        # fail fast instead of queueing a long path behind an unrelated action.
        origin_pos = None
        try:
            pre = self.bot.call("status", timeout_s=2.0)
            if isinstance(pre, dict):
                origin_pos = pre.get("pos")
                self._update_status_cache(pre)
                busy_action = str(pre.get("busy") or "")
                if busy_action:
                    return json.dumps(
                        {
                            "ok": False,
                            "requires_replan": True,
                            "error": {
                                "code": "action_busy",
                                "action": busy_action,
                                "message": (
                                    f"动作锁仍被 {busy_action} 占用，未提交 move_to，避免阻塞等待。"
                                    "请先处理当前动作（受伤先脱险/等待其完成），1~2 秒后重试坐标；"
                                    "不要用同一参数连续重试。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
        except (ApiError, httpx.HTTPError, RuntimeError, ValueError):
            pass
        eff_move_timeout, move_deadline_imminent = self._clamp_tool_budget(
            25.0, max_s=25.0, min_s=3.0, reserve_s=3.0
        )
        if move_deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": False,
                    "error": {
                        "code": "episode_deadline_imminent",
                        "message": "本集剩余时间不足 5s，已跳过 move_to 以避免超时越界。",
                    },
                },
                ensure_ascii=False,
            )
        waypoint = None
        # Budgeted episodes return after one hop so the actor can reassess terrain.
        if (
            self._episode_deadline_mono is not None
            and entity_id is None and x is not None and z is not None
            and isinstance(origin_pos, (list, tuple)) and len(origin_pos) >= 3
            and all(isinstance(origin_pos[i], (int, float)) and math.isfinite(origin_pos[i]) for i in (0, 2))
        ):
            dx, dz = float(x) - origin_pos[0], float(z) - origin_pos[2]
            distance = math.hypot(dx, dz)
            if math.isfinite(distance) and distance > 36.0:
                waypoint = [origin_pos[0] + 32.0 * dx / distance, origin_pos[2] + 32.0 * dz / distance]
                blocked = self.geofence.check_point(*waypoint, action="mc_move_to")
                if blocked is not None:
                    return self._record_geofence_block(blocked)
                payload["x"], payload["z"] = waypoint
                payload.pop("y", None)

        raw = self._call_json("move_to", payload, timeout_s=eff_move_timeout)
        try:
            first = json.loads(raw)
            if first.get("ok") is False and (first.get("error") or {}).get("code") == "busy":
                # /stop waits for the preceding lease to finish. Retry only
                # after the server confirms release; never spin on busy.
                stopped = self.bot.call("stop", {}, timeout_s=5.5)
                if stopped.get("released") is True or (stopped.get("stopped") is True and stopped.get("action") is None):
                    retry_timeout, retry_imminent = self._clamp_tool_budget(
                        15.0, max_s=15.0, min_s=3.0, reserve_s=3.0
                    )
                    if not retry_imminent:
                        raw = self._call_json("move_to", payload, timeout_s=retry_timeout)
        except (ValueError, ApiError, httpx.HTTPError):
            pass
        if waypoint is not None:
            return self._move_waypoint_receipt(raw, waypoint, [float(x), float(z)], geofence_clamped_info)
        if x is not None and z is not None:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                err_code = str(err_obj.get("code") or "")
                if err_code == "busy":
                    parsed["requires_replan"] = True
                    err_obj["message"] = (
                        f"动作锁仍被 {err_obj.get('action') or '其他动作'} 占用，move_to 未执行；"
                        "请先脱困或等待 1~2 秒后重试，不要连续提交同一路径。"
                    )
                    raw = json.dumps(parsed, ensure_ascii=False)
                st_mv: dict[str, Any] | None = None
                try:
                    st_mv = self.bot.status()
                    self._update_status_cache(st_mv)
                except Exception:
                    pass
                if err_code == "movement_not_confirmed":
                    pos_now = st_mv.get("pos") if isinstance(st_mv, dict) else None
                    err_msg = str(err_obj.get("message") or "")
                    server_says_in_range = ("已在目标" in err_msg) or ("within range" in err_msg.lower())
                    dist_now: float | None = None
                    if isinstance(pos_now, (list, tuple)) and len(pos_now) >= 3:
                        if y is not None:
                            dist_now = math.dist(
                                [float(x), float(y), float(z)],
                                [float(pos_now[0]), float(pos_now[1]), float(pos_now[2])],
                            )
                        else:
                            dist_now = math.hypot(float(x) - float(pos_now[0]), float(z) - float(pos_now[2]))
                    if server_says_in_range or (dist_now is not None and dist_now <= float(range) + 0.05):
                        if pos_now is None:
                            pos_now = [x, y, z]
                        if dist_now is None:
                            dist_now = 0.0
                        return json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "already_in_range": True,
                                    "pos": list(pos_now[:3]),
                                    "range": range,
                                    "distance": round(dist_now, 2),
                                    "hint": (
                                        "已在目标 range 内，本次移动视为完成。"
                                        "若要挪动站位，请指定至少 3 格外的坐标并设 range=0.5，"
                                        "或直接调 mc_place 不传坐标。"
                                    ),
                                },
                            },
                            ensure_ascii=False,
                        )
                dim_m, cur_p, _ = self._cached_env(fetch_if_missing=True)
                dist_m = math.hypot(float(x) - cur_p[0], float(z) - cur_p[2]) if cur_p is not None else 0.0
                if dist_m > 36.0 and err_code in {"path_not_found", "no_path", "timeout", "action_timeout", "stalled"}:
                    try:
                        seg_res = self.skills.navigate_segmented(
                            float(x),
                            float(z),
                            y=y,
                            range_m=range,
                            geofence=self.geofence,
                            ignore_hurt=(ignore_hurt or self._is_in_water()),
                            skip_direct=True,
                        )
                        if seg_res.get("ok"):
                            self.update_block_pos(seg_res.get("pos") or (seg_res.get("result") or {}).get("pos"))
                            return json.dumps({"ok": True, "result": seg_res}, ensure_ascii=False)
                    except ApiError:
                        pass
                # If moving onto a remembered POI failed with path_not_found, cool down that POI for 300s
                if err_code in {"path_not_found", "no_path"}:
                    tx_f, tz_f = float(x), float(z)
                    tx_i, tz_i = math.floor(tx_f), math.floor(tz_f)
                    for (edim, ex, ey, ez) in list(self.world_memory.pois.keys()):
                        if edim == dim_m and abs(ex - tx_i) <= 1 and abs(ez - tz_i) <= 1 and (y is None or abs(ey - math.floor(float(y))) <= 2):
                            self.world_memory.mark_unreachable((ex, ey, ez), dim=dim_m, reason=err_code, cooldown_s=300.0)
                    # BUG-058: guide short-range dig_toward and escalate repeated path_not_found to requires_replan
                    with self._state_lock:
                        near_hits = 1 + sum(
                            1 for (px_f, pz_f) in self._path_not_found_targets
                            if math.hypot(tx_f - px_f, tz_f - pz_f) <= 8.0
                        )
                        self._path_not_found_targets.append((tx_f, tz_f))
                        total_hits = len(self._path_not_found_targets)
                    parsed["path_not_found_count"] = total_hits
                    if cur_p is not None and dist_m <= 12.0:
                        parsed["suggested"] = {
                            "mode": "dig_toward",
                            "target": [
                                round(tx_f, 1),
                                round(float(y), 1) if y is not None else None,
                                round(tz_f, 1),
                            ],
                            "distance": round(dist_m, 1),
                        }
                        parsed["hint"] = (
                            f"目标距当前位置仅 {round(dist_m, 1)}m 但寻路不通（可能隔墙/悬崖/门框阻挡），"
                            "请改用 mc_dig / mc_skill_tunnel / mc_skill_staircase 挖通或垫高，不要重复调用 mc_move_to。"
                        )
                    if near_hits >= 2 or total_hits >= 3:
                        parsed["requires_replan"] = True
                        parsed["hint"] = (
                            f"本集已 {total_hits} 次遇到 path_not_found（近邻目标重复 {near_hits} 次），"
                            "该路径不可达。请立即放弃此坐标，改挖通路径（mc_skill_tunnel/mc_dig）或换可达目标。"
                        )
                    raw = json.dumps(parsed, ensure_ascii=False)
        if entity_id is not None:
            try:
                parsed_ent = json.loads(raw)
                err_ent = parsed_ent.get("error") if isinstance(parsed_ent.get("error"), dict) else {}
                msg_ent = str(err_ent.get("message") or "")
                if (isinstance(parsed_ent, dict) and parsed_ent.get("ok") is False
                        and err_ent.get("code") == "movement_not_confirmed"
                        and (("已在目标" in msg_ent) or ("within range" in msg_ent.lower()))):
                    pos_ent = parsed_ent.get("pos_after") or err_ent.get("pos") or [None, None, None]
                    return json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "already_in_range": True,
                                "entity_id": entity_id,
                                "pos": pos_ent,
                                "range": range,
                                "hint": "服务端确认已在目标 range 内，本次移动视为完成。",
                            },
                        },
                        ensure_ascii=False,
                    )
            except (TypeError, ValueError):
                pass
        out_raw = self._annotate_stale_entity(raw, entity_id)
        if geofence_clamped_info is not None:
            try:
                parsed_out = json.loads(out_raw)
                if isinstance(parsed_out, dict):
                    parsed_out.update(geofence_clamped_info)
                    out_raw = json.dumps(parsed_out, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        return out_raw

    def _move_waypoint_receipt(
        self, raw: str, waypoint: list[float], target: list[float], geofence_info: dict[str, Any] | None
    ) -> str:
        receipt = json.loads(raw)
        result = receipt.get("result") or {}
        error = receipt.get("error") or {}
        pos = result.get("pos") or result.get("pos_after") or error.get("pos")
        if receipt.get("ok") is False:
            receipt["requires_replan"] = True
        if pos is None or receipt.get("ok") is False:
            try:
                status = self.bot.call("status", method="GET", timeout_s=1.0)
                self._update_status_cache(status)
                pos = status.get("pos") or pos
            except (ApiError, httpx.HTTPError):
                pass
        remaining = None
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            remaining = round(math.hypot(target[0] - float(pos[0]), target[1] - float(pos[2])), 1)
        receipt["result"] = {
            **result,
            "partial_move": True,
            "waypoint_xz": waypoint,
            "target_xz": target,
            "pos": pos,
            "remaining_dist": remaining,
            "hint": "本次执行途中航点。请根据当前位置和剩余距离继续分段移动或换方向。",
        }
        if geofence_info:
            receipt.update(geofence_info)
        return json.dumps(receipt, ensure_ascii=False)

    def mc_dig(self, x: int, y: int, z: int, collect: bool = True) -> str:
        blocked = self.geofence.check_point(float(x), float(z), action="mc_dig")
        if blocked is not None:
            return self._record_geofence_block(blocked)
        try:
            bname = self._get_block_name(x, y, z)
        except Exception:
            bname = ""
        req_tier = BLOCK_MIN_PICKAXE_TIER.get(bname, 0)
        if req_tier > 0:
            try:
                inv = self.bot.inventory()
                counts: dict[str, int] = {}
                for row in inv.get("items") or []:
                    iname = row.get("name")
                    icnt = int(row.get("count") or 0)
                    if iname and icnt > 0:
                        counts[iname] = counts.get(iname, 0) + icnt
                best_pick, best_tier = _best_pickaxe_in_inv(counts)
                held_name = (inv.get("held") or {}).get("name") if isinstance(inv.get("held"), dict) else None
            except Exception:
                best_pick, best_tier, held_name = None, 0, None
            if best_tier < req_tier:
                return json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "tool_tier_insufficient",
                            "message": (
                                f"禁止用当前工具强挖 {bname}（会直接碎裂无掉落物）！"
                                f"至少需要等级 {req_tier} 的镐，当前背包最高仅 {best_pick or '无镐'}，请先合成对应镐。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            if best_pick and held_name != best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass
        raw_dig = self._call_json("dig", {"pos": [x, y, z], "collect": collect})
        try:
            parsed_dig = json.loads(raw_dig)
            if isinstance(parsed_dig, dict) and parsed_dig.get("ok") is True:
                # Broken block is now air: drop both the container cache and its POI entry.
                self.world_memory.invalidate_container(
                    [x, y, z], dim=self._last_dimension, block_name=bname or None
                )
                self.world_memory.remove_poi([x, y, z], dim=self._last_dimension)
            elif isinstance(parsed_dig, dict) and parsed_dig.get("ok") is False:
                err = parsed_dig.get("error") if isinstance(parsed_dig.get("error"), dict) else {}
                if err.get("code") == "timeout":
                    # P2-21/r15: a timed-out cell was immediately retried by the
                    # model. Avoid it briefly so the next scan picks another.
                    try:
                        self.skills._avoid_temporarily((x, y, z), "dig_timeout", 15.0)
                    except Exception:
                        pass
                    parsed_dig["requires_replan"] = True
                    err["message"] = (
                        f"挖掘 {[x, y, z]} 在预算内未确认；已临时避开该格 15 秒。"
                        "请换相邻格、先 mc_skill_unstick/靠近，或改用 mc_skill_mine_blocks 重新扫描；"
                        "不要立即重试同一坐标。"
                    )
                    raw_dig = json.dumps(parsed_dig, ensure_ascii=False)
        except Exception:
            pass
        return raw_dig

    def _check_dimension_physics(self, item: str | None) -> str | None:
        if not item:
            return None
        dim, pos, biome = self._cached_env(fetch_if_missing=False)
        probe = assess_world_target(item, dimension=dim, y=(pos[1] if pos is not None else None), biome=biome, mode="place")
        if probe.get("known") is True and pos is None:
            dim, pos, biome = self._cached_env(fetch_if_missing=True)
            probe = assess_world_target(item, dimension=dim, y=(pos[1] if pos is not None else None), biome=biome, mode="place")
        if probe.get("available") is False:
            return json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_dimension_physics",
                        "message": probe["hint"],
                        "ecology_check": probe,
                    },
                },
                ensure_ascii=False,
            )
        return None

    def mc_place(
        self,
        item: str,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        physics_block = self._check_dimension_physics(item)
        if physics_block is not None:
            return physics_block
        payload: dict[str, Any] = {"item": item}
        if x is not None and y is not None and z is not None:
            blocked = self.geofence.check_point(float(x), float(z), action="mc_place")
            if blocked is not None:
                return self._record_geofence_block(blocked)
            payload["pos"] = [x, y, z]
        elif any(v is not None for v in (x, y, z)):
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "放置坐标需同时提供 x, y, z，或全部省略自动选落点"}},
                ensure_ascii=False,
            )
        raw_place = self._call_json("place", payload)
        try:
            parsed_pl = json.loads(raw_place)
            if isinstance(parsed_pl, dict) and parsed_pl.get("ok") is True:
                res_pl = parsed_pl.get("result") if isinstance(parsed_pl.get("result"), dict) else {}
                p_xyz = res_pl.get("pos") or ([x, y, z] if (x is not None and y is not None and z is not None) else None)
                if isinstance(p_xyz, (list, tuple)) and len(p_xyz) >= 3:
                    self.world_memory.record_poi(item, [int(p_xyz[0]), int(p_xyz[1]), int(p_xyz[2])], dim=self._last_dimension)
        except Exception:
            pass
        return raw_place

    def mc_craft(self, item: str, count: int = 1) -> str:
        cnt = max(1, min(64, int(count)))
        resolved = resolve_item_alias(item)
        cached_inv = getattr(self.bot, "_last_inventory", None)
        if isinstance(cached_inv, dict) and _compute_empty_slots(cached_inv) < 2:
            inv_now, _ = self.skills._ensure_free_slots(min_free=2)
            if _compute_empty_slots(inv_now) == 0 and not _can_fit_item_in_inventory(inv_now, item, cnt):
                return json.dumps(
                    {
                        "ok": False,
                        "requires_replan": True,
                        "error": {
                            "code": "inventory_full",
                            "item": item,
                            "count": cnt,
                            "empty_slots": 0,
                            "message": (
                                f"背包 36 格已满(empty_slots=0)且无法自动腾出空槽容纳 {item} x{cnt}；"
                                "直接调 craft 会触发 30s 超时卡死，请先消耗、放置方块或丢弃不需要的物品后再合成。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
        raw = self._call_json("craft", {"item": item, "count": cnt})
        if resolved in TABLE_RECIPES:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and not parsed.get("ok"):
                err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                err_code = str(err_obj.get("code") or "")
                if err_code in {"need_crafting_table", "no_window", "path_not_found", "out_of_reach"}:
                    try:
                        counts_now = _inventory_counts(self.bot)
                    except ApiError:
                        counts_now = {}
                    if counts_now.get("crafting_table", 0) >= 1:
                        tbl_res = self.skills.ensure_crafting_table(
                            force_recarve=(err_code in {"no_window", "path_not_found", "out_of_reach"}),
                            target_item=resolved,
                        )
                        if tbl_res.get("ok"):
                            return self._call_json("craft", {"item": item, "count": cnt})
                    elif err_code in {"no_window", "path_not_found"} and isinstance(err_obj.get("table"), list) and len(err_obj["table"]) >= 3:
                        tpos = [int(err_obj["table"][0]), int(err_obj["table"][1]), int(err_obj["table"][2])]
                        self.skills._record_utility_in_ledger("crafting_table", tpos)
                        if self.skills._navigate_to_distant_table(tpos):
                            return self._call_json("craft", {"item": item, "count": cnt})
        return raw

    def mc_equip(self, item: str, slot: str = "hand") -> str:
        norm_item = str(item or "").removeprefix("minecraft:").strip()
        norm_slot = "torso" if slot == "chest" else str(slot or "hand")
        raw = self._call_json("equip", {"item": norm_item, "slot": norm_slot})
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            err = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            if err.get("code") == "missing_item":
                try:
                    inv = self.bot.inventory()
                    offhand = inv.get("offhand") if isinstance(inv, dict) else None
                    off_name = str(offhand.get("name") or "").removeprefix("minecraft:") if isinstance(offhand, dict) else ""
                    if off_name and off_name == norm_item:
                        if norm_slot == "off-hand":
                            return json.dumps(
                                {"ok": True, "already_equipped": True, "item": norm_item, "slot": "off-hand"},
                                ensure_ascii=False,
                            )
                        parsed["hint"] = (
                            f"{norm_item} 当前已装备在副手(off-hand slot 45)，服务端 /equip 仅查主背包槽位；"
                            "请先将盾牌/方块等其他物品装备到 off-hand 腾出该物品，或改用背包内其他工具。"
                        )
                        return json.dumps(parsed, ensure_ascii=False)
                except Exception:
                    pass
        return raw

    def mc_interact(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        entity_id: int | None = None,
        item: str | None = None,
    ) -> str:
        item_name = str(item or "").removeprefix("minecraft:")
        if entity_id is None and item_name == "fishing_rod":
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": True,
                    "error": {
                        "code": "use_item_api",
                        "message": (
                            "钓鱼需要从岸边向开阔水面抛竿。请选择水面坐标，"
                            "调用 mc_use_item(item='fishing_rod', x=..., y=..., z=...) 抛竿并等待收竿。"
                        ),
                    },
                },
                ensure_ascii=False,
            )
        physics_block = self._check_dimension_physics(item)
        if physics_block is not None:
            return physics_block
        payload: dict[str, Any] = {}
        if entity_id is not None:
            payload["entity_id"] = entity_id
        elif x is not None and y is not None and z is not None:
            payload["pos"] = [x, y, z]
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "interact 必须提供 (x, y, z) 方块坐标或 entity_id"}},
                ensure_ascii=False,
            )
        if item:
            payload["item"] = item
        is_bucket = item in {"bucket", "water_bucket", "lava_bucket"}
        if is_bucket and x is not None and y is not None and z is not None:
            try:
                blk = self.bot.call("get_block", {"pos": [x, y, z]}, timeout_s=2.0)
                if isinstance(blk, dict):
                    bname = str(blk.get("name") or "").removeprefix("minecraft:")
                    level = (blk.get("properties") or {}).get("level")
                    if bname in {"lava", "water", "flowing_lava", "flowing_water"} and level is not None:
                        if int(level) > 0:
                            return json.dumps(
                                {
                                    "ok": False,
                                    "requires_replan": True,
                                    "error": {
                                        "code": "interact_flowing_fluid",
                                        "target": [x, y, z],
                                        "block": bname,
                                        "level": int(level),
                                        "message": (
                                            f"目标 {bname} 是流动流体(level={int(level)})，桶只能舀源头(level=0)。"
                                            "请沿流体上游/中心寻找源头，或先用方块封堵流向；不要对同一格重复右键。"
                                        ),
                                    },
                                },
                                ensure_ascii=False,
                            )
            except (ApiError, httpx.HTTPError, TypeError, ValueError):
                pass
            try:
                st = getattr(self, "_last_status", None)
                if not isinstance(st, dict) or not st.get("pos"):
                    st = self.bot.status()
                pos = st.get("pos") if isinstance(st, dict) else None
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    bot_y = float(pos[1])
                    if abs(float(y) - bot_y) >= 5.0:
                        return json.dumps(
                            {
                                "ok": False,
                                "requires_replan": True,
                                "error": {
                                    "code": "out_of_reach_vertical",
                                    "message": (
                                        f"目标方块 y={y} 与当前高度 y={bot_y:.1f} 相差 {abs(float(y) - bot_y):.1f} 格，"
                                        "直接右键桶会寻路卡死；请先用 mc_skill_staircase 移动到接近目标高度后再调 mc_interact。"
                                    ),
                                },
                            },
                            ensure_ascii=False,
                        )
            except Exception:
                pass
        raw = self._call_json("interact", payload)
        is_bottle = "pos" in payload and item_name == "glass_bottle"
        if not (is_bucket or is_bottle):
            return raw
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return raw
        res_obj = parsed.get("result")
        if not isinstance(res_obj, dict):
            return raw

        used = res_obj.get("used")
        gained = res_obj.get("gained")
        if is_bucket and res_obj.get("changed") is False and not used and not gained:
            message = "交互未产生方块变化(changed=false)：目标可能是流动流体而非静止源头(level=0)，或视线/距离受阻，桶内状态未变化！请靠近静止源头(level=0) 3 格内重试。"
        elif is_bottle and str(res_obj.get("block") or "").removeprefix("minecraft:") in {"water", "flowing_water"}:
            if (
                isinstance(used, dict) and used.get("glass_bottle")
                and isinstance(gained, dict) and (gained.get("potion") or gained.get("water_bottle"))
            ):
                return raw
            message = (
                "交互回执未确认玻璃瓶装水。请用 mc_inventory 确认水药瓶是否入包。"
                "若没有，换一个 3 格内可达的静止水源（level=0）。"
            )
        else:
            return raw

        parsed["ok"] = False
        parsed["requires_replan"] = True
        parsed["error"] = {"code": "interact_no_effect", "message": message}
        return json.dumps(parsed, ensure_ascii=False)

    def mc_use_item(
        self,
        item: str,
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        ms: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"item": item}
        if entity_id is not None:
            payload["target"] = {"entity_id": entity_id}
        elif x is not None and y is not None and z is not None:
            payload["target"] = {"pos": [x, y, z]}
        if ms is not None:
            payload["ms"] = max(50, min(10000, int(ms)))
        return self._call_json("use_item", payload)

    def mc_attack(
        self,
        entity_id: int,
        max_hits: int | None = None,
        interval_ms: int | None = None,
        timeout_ms: int = 20000,
    ) -> str:
        seen = self._entity_observations.get(entity_id)
        if (seen is not None and time.monotonic() - self._entity_observed_mono <= 3.0
                and self._entity_observed_dimension == self._last_dimension
                and isinstance(seen.get("dist"), (int, float)) and seen["dist"] > 32.0):
            try:
                live = self.bot.call("get_entities", {"radius": 64}, timeout_s=2.0).get("entities") or []
                target = next((ent for ent in live if isinstance(ent, dict) and ent.get("id") == entity_id), None)
                if target is None or (isinstance(target.get("dist"), (int, float)) and target["dist"] > 32.0):
                    return json.dumps({
                        "ok": False, "requires_replan": True,
                        "error": {"code": "target_out_of_reach" if target else "target_not_visible", "entity_id": entity_id,
                                  "dist": target.get("dist") if target else None,
                                  "message": "目标已超出近战追击范围或不再可见；请放弃空挥，先确认安全路线并靠近目标，或选择附近目标。"},
                    }, ensure_ascii=False)
            except (ApiError, httpx.HTTPError):
                pass
        try:
            inv = self.bot.inventory()
            counts: dict[str, int] = {}
            for row in inv.get("items") or []:
                iname = row.get("name")
                icnt = int(row.get("count") or 0)
                if iname and icnt > 0:
                    counts[iname] = counts.get(iname, 0) + icnt
            best_w = best_melee_weapon_in_inv(counts)
            held_name = (inv.get("held") or {}).get("name") if isinstance(inv.get("held"), dict) else None
            if best_w and held_name != best_w:
                self.bot.call("equip", {"item": best_w, "slot": "hand"})
        except (ApiError, KeyError, TypeError):
            pass
        timeout_ms = max(1000, min(60000, int(timeout_ms)))
        payload = {"entity_id": entity_id, "timeout": timeout_ms}
        if max_hits is not None:
            payload["max_hits"] = max(1, min(100, int(max_hits)))
        if interval_ms is not None:
            payload["interval_ms"] = max(50, min(5000, int(interval_ms)))
        raw = self._call_json("attack", payload)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        res = parsed.get("result") if isinstance(parsed, dict) else None
        if (
            isinstance(parsed, dict)
            and parsed.get("ok") is True
            and isinstance(res, dict)
            and "hits" in res
            and int(res.get("hits") or 0) == 0
            and not res.get("killed")
        ):
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": True,
                    "error": {
                        "code": "attack_zero_hits",
                        "entity_id": entity_id,
                        "ms": res.get("ms"),
                        "message": "攻击超时且 0 次命中(hits=0)：目标可能隔墙不可达、距离过远或寻路受阻，请勿原地重复空挥。",
                    },
                    "result": res,
                },
                ensure_ascii=False,
            )
        return raw

    def mc_shoot(
        self,
        weapon: str = "bow",
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        charge_ms: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"weapon": weapon}
        if entity_id is not None:
            payload["entity_id"] = entity_id
        elif x is not None and y is not None and z is not None:
            payload["pos"] = [x, y, z]
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "shoot 需要提供 entity_id 或 (x, y, z)"}},
                ensure_ascii=False,
            )
        if charge_ms is not None:
            payload["charge_ms"] = max(200, min(5000, int(charge_ms)))
        return self._call_json("shoot", payload)

    def mc_window_view(
        self,
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        target = _build_window_target(block, entity_id, x, y, z)
        if isinstance(target, str):
            return target
        if entity_id is not None:
            # BUG-027: the backend's goNear can silently fail, then activate
            # from 12m away and report a misleading no_window. Pre-check here.
            try:
                ents = self.bot.call("get_entities", {"radius": 32}, timeout_s=2.0).get("entities") or []
                ent = next((e for e in ents if isinstance(e, dict) and e.get("id") == entity_id), None)
                if ent is None:
                    return json.dumps(
                        {
                            "ok": False, "requires_replan": True,
                            "error": {
                                "code": "target_not_visible", "entity_id": entity_id,
                                "message": (
                                    f"实体 {entity_id} 在 32m 内不可见；请重新 mc_get_entities 获取最新 id，"
                                    "或先 mc_move_to 到目标附近再开窗。不要重复使用旧 id。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                dist = ent.get("dist")
                if isinstance(dist, (int, float)) and float(dist) > 4.0:
                    epos = ent.get("pos")
                    move_hint = ""
                    if isinstance(epos, (list, tuple)) and len(epos) >= 3:
                        move_hint = f"先调 mc_move_to(x={epos[0]}, z={epos[2]}, range=2) 靠近，再 mc_window_view。"
                    return json.dumps(
                        {
                            "ok": False, "requires_replan": True,
                            "error": {
                                "code": "out_of_reach", "entity_id": entity_id,
                                "dist": round(float(dist), 1), "pos": epos,
                                "message": (
                                    f"目标距离 {float(dist):.1f}m，超过开窗交互范围(<=4m)。"
                                    f"{move_hint}不要在 12m 外重复开窗，后端不会先走近。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
            except (ApiError, httpx.HTTPError, TypeError, ValueError):
                pass
        raw = self._call_json("window/view", {"target": target})
        self._sync_window_payload(
            raw,
            block=block,
            pos=[x, y, z] if (x is not None and y is not None and z is not None) else None,
            entity_id=entity_id,
        )
        return raw

    def mc_window_act(
        self,
        ops: list[dict[str, Any]],
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        target = _build_window_target(block, entity_id, x, y, z)
        if isinstance(target, str):
            return target
        raw = self._call_json("window/act", {"target": target, "ops": ops})
        self._sync_window_payload(
            raw,
            block=block,
            pos=[x, y, z] if (x is not None and y is not None and z is not None) else None,
            entity_id=entity_id,
        )
        return raw

    def _sync_window_payload(
        self,
        raw: str,
        *,
        block: str | None = None,
        pos: list[int] | None = None,
        entity_id: int | None = None,
    ) -> None:
        """Sync WorldMemory.containers from window payloads on BOTH ok and error branches."""
        if block == "furnace" or entity_id is not None:
            return
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return
        res = parsed.get("result") if isinstance(parsed.get("result"), dict) else None
        err = parsed.get("error") if isinstance(parsed.get("error"), dict) else None
        payload: dict[str, Any] | None = None
        if res is not None:
            inner = res.get("window")
            if isinstance(inner, dict):
                payload = inner
                if not payload.get("pos") and isinstance(res.get("pos"), (list, tuple)):
                    payload = {**payload, "pos": res["pos"]}
            elif isinstance(inner, str):
                payload = res
        elif err is not None and isinstance(err.get("window"), dict):
            payload = err["window"]
        if payload is None:
            if err is not None and pos is not None and err.get("code") in {"no_window", "not_found", "window_closed"}:
                try:
                    live = str(self._get_block_name(*pos) or "").removeprefix("minecraft:")
                except Exception:
                    live = ""
                if live in {"air", "cave_air", "void_air", ""}:
                    self.world_memory.invalidate_container(pos, dim=self._last_dimension, block_name=block)
            return
        fb_pos = pos
        if fb_pos is None:
            target_info = payload.get("target") if isinstance(payload.get("target"), dict) else {}
            raw_pos = payload.get("pos") or target_info.get("pos")
            if isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 3:
                fb_pos = [int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2])]
            else:
                fb_pos = self.skills._resolve_nearby_container_pos(block)
        try:
            self.world_memory.update_container_from_window(
                payload,
                fallback_pos=fb_pos,
                fallback_block=block,
                dim=self._last_dimension,
            )
        except Exception:
            pass
        if fb_pos is not None:
            try:
                live = str(self._get_block_name(*fb_pos) or "").removeprefix("minecraft:")
            except Exception:
                live = ""
            if live in {"air", "cave_air", "void_air"}:
                self.world_memory.invalidate_container(fb_pos, dim=self._last_dimension, block_name=block)

    def mc_skill_container(
        self,
        items: list[dict[str, Any]],
        action: str = "put",
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.container_batch(
                items,
                action=action,
                block=block,
                x=x,
                y=y,
                z=z,
                dim=self._last_dimension,
            )
        )

    def mc_skill_build_portal(self, enter: bool = True, wait_s: float = 8.0) -> str:
        return self._run_skill(
            lambda: self.skills.build_nether_portal(enter=bool(enter), wait_s=float(wait_s))
        )

    def mc_skill_enchant(
        self,
        item: str,
        option: int,
        lapis_count: int | None = None,
        take: bool = True,
        block: str | None = "enchanting_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        verify: bool = True,
        reconnect_on_stale: bool = True,
    ) -> str:
        def _do_enchant() -> dict[str, Any]:
            before = self._item_enchant_state(item) if (verify and take) else None
            result = self.skills.enchant_item_once(
                item,
                option,
                lapis_count=lapis_count,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
            if verify and take and result.get("ok") and before is not None:
                time.sleep(0.5)
                after = self._item_enchant_state(item)
                verified = bool(after is not None and after[1] > before[1])
                result["inventory_verified"] = verified
                if not verified and reconnect_on_stale:
                    try:
                        self._reset_window_session()
                    except Exception as exc:  # noqa: BLE001
                        result["window_reset_error"] = f"{type(exc).__name__}: {exc}"
                        result["requires_replan"] = True
                    else:
                        refreshed = self._item_enchant_state(item)
                        if refreshed is not None and refreshed[1] > before[1]:
                            # The enchant did land; only the client window was stale.
                            result["inventory_verified"] = True
                            result["reconnected_for_sync"] = True
                        else:
                            # Genuine failure: retry exactly once on the fresh session.
                            retry = self.skills.enchant_item_once(
                                item,
                                option,
                                lapis_count=lapis_count,
                                take=take,
                                block=block,
                                x=x,
                                y=y,
                                z=z,
                            )
                            time.sleep(0.5)
                            after_retry = self._item_enchant_state(item)
                            retry_ok = bool(
                                retry.get("ok") and after_retry is not None and after_retry[1] > before[1]
                            )
                            retry["inventory_verified"] = retry_ok
                            retry["retried_after_reconnect"] = True
                            if not retry_ok:
                                retry["stale_window"] = True
                                retry["requires_replan"] = True
                            result = retry
            return result

        return self._run_skill(_do_enchant)

    def _item_enchant_state(self, item: str) -> tuple[int, int] | None:
        """Return (total_count, enchanted_count) for one item, or None if unreadable."""
        try:
            inv = self.bot.call("inventory", {})
        except (ApiError, httpx.HTTPError):
            return None
        want = resolve_item_alias(item)
        total = 0
        enchanted = 0
        for row in inv.get("items") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").removeprefix("minecraft:")
            if name != want:
                continue
            count = int(row.get("count") or 0)
            total += count
            if row.get("enchantments"):
                enchanted += count
        return total, enchanted

    def _reset_window_session(self) -> None:
        """Disconnect + reconnect to clear a stale mineflayer window/inventory snapshot."""
        try:
            self.bot.disconnect()
        except ApiError as exc:
            if exc.code != "not_connected":
                raise
        time.sleep(2.0)
        try:
            self.bot.connect(respect_reconnect_interval=False)
        except ApiError:
            # Strict competition server enforces >=30s reconnect interval; fall back gracefully.
            self.bot.connect(respect_reconnect_interval=True)
        st = self.bot.wait_until_ready(timeout_s=35.0)
        if isinstance(st, dict):
            self._update_status_cache(st)

    def mc_skill_trade(
        self,
        entity_id: int,
        option: int | None = None,
        get_item: str | None = None,
        times: int = 1,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.merchant_trade(
                entity_id=entity_id,
                option=option,
                get_item=get_item,
                times=times,
            )
        )

    def mc_skill_brew(
        self,
        ingredient_item: str,
        bottles: list[dict[str, Any]] | None = None,
        fuel_item: str | None = None,
        wait: bool = True,
        block: str | None = "brewing_stand",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.brew_once(
                ingredient_item,
                bottles=bottles,
                fuel_item=fuel_item,
                wait=wait,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )

    def mc_skill_anvil(
        self,
        left_item: str,
        right_item: str | None = None,
        left_enchanted: bool | None = None,
        right_enchanted: bool | None = None,
        text: str | None = None,
        take: bool = True,
        block: str | None = "anvil",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.anvil_operate(
                left_item,
                right_item=right_item,
                left_enchanted=left_enchanted,
                right_enchanted=right_enchanted,
                text=text,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )

    def mc_skill_smithing(
        self,
        template_item: str,
        base_item: str,
        addition_item: str,
        take: bool = True,
        block: str | None = "smithing_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.smithing_apply(
                template_item,
                base_item,
                addition_item,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )

    def mc_skill_stonecutter(
        self,
        input_item: str,
        option: int,
        count: int | None = None,
        take: bool = True,
        block: str | None = "stonecutter",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.stonecutter_cut(
                input_item,
                option,
                count=count,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )

    def mc_posture(self, sneak: bool = False, shield: bool = False, jump: bool = False) -> str:
        return self._call_json("posture", {"sneak": sneak, "shield": shield, "jump": jump})

    def mc_dismount(self) -> str:
        return self._call_json("dismount", {})

    def mc_drop(self, item: str, count: int = 1) -> str:
        return self._call_json("drop", {"item": item, "count": max(1, min(64, int(count)))})

    def mc_chat(self, message: str) -> str:
        return self._call_json("chat", {"message": str(message).lstrip("/")[:100]})

    def mc_stop(self) -> str:
        return self._call_json("stop", {})

    def mc_control(
        self,
        forward: bool = False,
        back: bool = False,
        left: bool = False,
        right: bool = False,
        jump: bool = False,
        sneak: bool = False,
        sprint: bool = False,
        yaw: float | None = None,
        pitch: float | None = None,
        hit: int | None = None,
        hit_interval_ms: int | None = None,
        ms: int | None = None,
        wait: bool | None = None,
        stop_on: list[str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {}
        for key, enabled in (
            ("forward", forward),
            ("back", back),
            ("left", left),
            ("right", right),
            ("jump", jump),
            ("sneak", sneak),
            ("sprint", sprint),
        ):
            if enabled:
                payload[key] = True
        if yaw is not None:
            payload["yaw"] = yaw
        if pitch is not None:
            payload["pitch"] = pitch
        if hit is not None:
            payload["hit"] = max(1, min(20, hit))
        if hit_interval_ms is not None:
            payload["hit_interval_ms"] = max(50, min(5000, hit_interval_ms))
        if ms is not None:
            payload["ms"] = max(50, min(3000, ms))
        if wait is not None:
            payload["wait"] = bool(wait)
        if stop_on is not None:
            payload["stop_on"] = stop_on
        return self._call_json("control", payload)

    def mc_look(
        self,
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> str:
        payload = {}
        for key, val in (
            ("entity_id", entity_id),
            ("x", x),
            ("y", y),
            ("z", z),
            ("yaw", yaw),
            ("pitch", pitch),
        ):
            if val is not None:
                payload[key] = val
        return self._annotate_stale_entity(self._call_json("look", payload), entity_id)

    # ---- L0 / L1 Macro Skills ----

    def mc_advancement_frontier(self, limit: int = 12) -> str:
        limit = max(1, min(50, int(limit)))
        return self._run_skill(lambda: compute_advancement_frontier(self.bot, limit=limit))

    def mc_skill_stabilize(self) -> str:
        return self._run_skill(lambda: check_and_stabilize(self.bot))

    def _cleanup_inventory_after(self, res: Any) -> Any:
        """BUG-029: enforce retention caps after every mining-style skill."""
        if not isinstance(res, dict):
            return res
        try:
            _, cleaned = self.skills._ensure_free_slots(min_free=3)
        except Exception:
            cleaned = []
        if cleaned:
            res["inventory_cleaned"] = [
                {"item": d.get("item"), "count": d.get("count")} for d in cleaned
            ]
        return res

    def mc_skill_harvest_logs(self, target_logs: int = 4, radius: int = 32) -> str:
        target_logs = max(1, min(32, int(target_logs)))
        radius = max(4, min(64, int(radius)))
        return self._run_skill(
            lambda: self._cleanup_inventory_after(
                self.skills.harvest_logs(target_logs=target_logs, radius=radius)
            )
        )

    def mc_skill_scout_surface(
        self,
        direction: str = "auto",
        target_types: list[str] | None = None,
        max_duration_s: float = 45.0,
        step_dist: float = 28.0,
    ) -> str:
        if not math.isfinite(max_duration_s):
            return self._run_skill(lambda: self.skills.scout_surface(max_duration_s=max_duration_s))
        budget_s, deadline_imminent = self._clamp_tool_budget(max_duration_s, max_s=50.0, min_s=0.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "distance_traveled": 0.0,
                "segments_completed": 0,
                "final_pos": None,
                "detected_pois": [],
                "detected_entities": [],
                "stop_reason": "episode_deadline_imminent",
                "requires_replan": True,
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过地表斥候。"},
            }, ensure_ascii=False)
        return self._run_skill(
            lambda: self.skills.scout_surface(
                direction=direction, target_types=target_types, max_duration_s=budget_s, step_dist=step_dist,
            )
        )

    def mc_skill_craft_gear(self, item: str, count: int = 1, equip_slot: str | None = None) -> str:
        count = max(1, min(64, int(count)))
        return self._run_skill(lambda: self.skills.craft_gear(item=item, count=count, equip_slot=equip_slot))

    def mc_skill_ensure_items(
        self,
        items: list[dict[str, Any]],
        max_duration_s: int = 150,
    ) -> str:
        dur = max(10, min(600, int(max_duration_s)))
        return self._run_skill(
            lambda: self.skills.ensure_items(
                items,
                max_duration_s=float(dur),
                geofence=self.geofence,
                stop_check=self._stop_check,
            )
        )

    def mc_skill_mine_blocks(self, block_types: list[str], count: int = 3, radius: int = 32) -> str:
        count = max(1, min(32, int(count)))
        radius = max(4, min(64, int(radius)))
        return self._run_skill(
            lambda: self._cleanup_inventory_after(
                self.skills.mine_blocks(block_types=block_types, count=count, radius=radius)
            )
        )

    def mc_skill_smelt(self, input_item: str, count: int = 1, fuel_item: str | None = None) -> str:
        count = max(1, min(64, int(count)))
        budget_s, deadline_imminent = self._clamp_tool_budget(55.0)
        if deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过熔炼。"},
                },
                ensure_ascii=False,
            )
        return self._run_skill(
            lambda: self.skills.smelt_items(
                input_item=input_item, count=count, fuel_item=fuel_item, wait_timeout_s=budget_s
            )
        )

    def mc_skill_collect_drops(self, radius: int = 12, max_pickups: int = 8) -> str:
        radius = max(2, min(32, int(radius)))
        max_pickups = max(1, min(20, int(max_pickups)))
        return self._run_skill(lambda: self.skills.collect_drops(radius=radius, max_pickups=max_pickups))

    def mc_skill_bind_bed(self, x: int, y: int, z: int) -> str:
        return self._run_skill(lambda: self.skills.bind_bed_spawn(x=x, y=y, z=z))

    def mc_skill_unstick(self) -> str:
        return self._run_skill(lambda: self.skills.unstick())

    def mc_skill_bunker(self) -> str:
        return self._run_skill(lambda: self.skills.dig_safety_bunker())

    def mc_skill_flee(self, distance: int = 14) -> str:
        distance = max(8, min(22, int(distance)))
        budget_s, deadline_imminent = self._clamp_tool_budget(35.0, max_s=35.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "stop_reason": "episode_deadline_imminent",
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，未发起长距离撤退。"},
            }, ensure_ascii=False)
        return self._run_skill(
            lambda: self.skills.flee_hostiles(
                distance=float(distance), geofence=self.geofence, max_duration_s=budget_s,
            )
        )

    def mc_skill_combat(
        self,
        mode: str = "auto",
        target_name: str | None = None,
        max_time_s: float = 20.0,
        pillar_height: int = 3,
    ) -> str:
        """Tactical active combat macro backed by `combat_v1` via `BotSessionCombatAdapter`."""
        return self._run_skill(
            lambda: run_combat_skill(
                self.bot,
                mode=mode,
                target_name=target_name,
                max_time_s=float(max_time_s),
                pillar_height=int(pillar_height),
                geofence=self.geofence,
                episode_deadline_mono=self._episode_deadline_mono,
            )
        )

    def _check_direction_geofence(
        self, direction: str, steps: int, action: str, *, status: Callable[[], dict[str, Any]] | None = None
    ) -> str | None:
        if not self.geofence.enabled:
            return None
        try:
            st = (status or self.bot.status)()
        except _StairDeadline:
            raise
        except Exception:
            return None
        pos = st.get("pos") or []
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return None
        if direction == "pillar":
            blocked = self.geofence.check_point(float(pos[0]), float(pos[2]), action=action)
            return self._record_geofence_block(blocked) if blocked is not None else None
        blocked = self.geofence.check_ray(float(pos[0]), float(pos[2]), direction, steps, action=action)
        if blocked is None:
            return None
        safe_dirs = [
            cand
            for cand in ("north", "east", "south", "west")
            if self.geofence.check_ray(float(pos[0]), float(pos[2]), cand, steps, action=action) is None
        ]
        return self._record_geofence_block(blocked, safe_directions=safe_dirs)

    def _pick_staircase_direction(self, steps: int, *, vertical: str, skills: MacroSkills) -> str:
        try:
            st = skills.bot.status() or {}
        except _StairDeadline:
            raise
        except Exception:
            return "north"
        pos = st.get("pos") or []
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return "north"
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)
        candidates: list[str] = ["north", "east", "south", "west"]
        try:
            probed = (
                skills._probe_viable_up_stair_dirs(bx, by, bz)
                if vertical == "up"
                else skills._probe_viable_down_stair_dirs(bx, by, bz)
            )
            if probed:
                candidates = probed
        except _StairDeadline:
            raise
        except Exception:
            pass
        if self.geofence.enabled:
            for cand in candidates:
                if self.geofence.check_ray(px, pz, cand, steps, action="mc_skill_staircase") is None:
                    return cand
        return candidates[0] if candidates else "north"

    def mc_skill_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
    ) -> str:
        started = time.monotonic()
        budget_s, deadline_imminent = self._clamp_tool_budget(45.0, max_s=45.0, reserve_s=3.0)
        deadline = started + budget_s
        episode_budget = {"deadline": deadline} if self._episode_deadline_mono is not None else {}
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "steps_done": 0,
                "stop_reason": "episode_deadline_imminent",
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过阶梯施工。"},
            }, ensure_ascii=False)

        d_norm = str(direction or "").strip().lower()
        v_norm = str(vertical or "down").strip().lower()
        is_pillar = d_norm == "pillar" or v_norm == "pillar"
        original_steps = max(1, int(steps))
        requested_steps = min(25, original_steps)
        # BUG-037: a 45s call cannot finish long requests (models often ask
        # for 40); cap at 10 so the model gets progress instead of another
        # zero/low-progress time_budget.
        steps = min(requested_steps, 12 if is_pillar else 10)
        if is_pillar:
            d_norm = v_norm = "pillar"
        elif d_norm in ("down", "up"):
            if v_norm not in ("down", "up") or (v_norm == "down" and d_norm == "up"):
                v_norm = d_norm
            d_norm = ""
        resolved_stop = (v_norm == "down") if stop_on_ore is None else bool(stop_on_ore)
        preflight = copy(self.skills)
        preflight.bot = _StairSession(self.bot, 45.0, deadline=deadline)

        def _invoke(dir_key: str) -> str:
            blocked = self._check_direction_geofence(
                dir_key, steps, "mc_skill_staircase", status=preflight.bot.status
            )
            if blocked is not None:
                return blocked
            return self._run_skill(
                lambda: self.skills.dig_staircase(
                    direction=dir_key, steps=steps, vertical=v_norm, stop_on_ore=resolved_stop, **episode_budget,
                )
            )

        try:
            direction = d_norm or self._pick_staircase_direction(steps, vertical=v_norm, skills=preflight)
            raw = _invoke(direction)
        except _StairDeadline:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "direction": d_norm or None,
                "vertical": v_norm,
                "steps_done": 0,
                "stop_reason": "time_budget",
                "error": {"code": "time_budget", "message": "阶梯方向或围栏预检已用完预算。"},
            }, ensure_ascii=False)
        if is_pillar:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict):
            return raw
        if requested_steps != steps:
            parsed["steps_capped_from"] = original_steps
            parsed["steps_clamped"] = True
        # BUG-037 third mode (r21): first step stuck but viable directions
        # are already known; retry once automatically instead of making the
        # model try the opposite direction.
        if (
            parsed.get("stop_reason") == "movement_not_confirmed"
            and int(parsed.get("steps_done") or 0) == 0
            and int(parsed.get("dug_count") or 0) == 0
        ):
            viable = [d for d in (parsed.get("viable_directions") or []) if isinstance(d, str)]
            alt = next((d for d in viable if d != direction), None)
            if alt and time.monotonic() < deadline:
                try:
                    retry_raw = _invoke(alt)
                except _StairDeadline:
                    return json.dumps(parsed, ensure_ascii=False)
                try:
                    retry_parsed = json.loads(retry_raw)
                except (TypeError, ValueError):
                    return retry_raw
                if isinstance(retry_parsed, dict):
                    retry_parsed["auto_retry_direction"] = alt
                    if requested_steps != steps:
                        retry_parsed["steps_capped_from"] = original_steps
                        retry_parsed["steps_clamped"] = True
                    return json.dumps(retry_parsed, ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False)

    def mc_skill_tunnel(self, direction: str = "north", steps: int = 4, stop_on_ore: bool = True) -> str:
        direction = str(direction).strip().lower()
        steps = max(1, min(8, int(steps)))
        blocked = self._check_direction_geofence(direction, steps, "mc_skill_tunnel")
        if blocked is not None:
            return blocked
        return self._run_skill(
            lambda: self._cleanup_inventory_after(
                self.skills.dig_level_tunnel(direction, steps, stop_on_ore)
            )
        )

    def mc_skill_mine_dfs(
        self,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        max_duration_s: float = 32.0,
    ) -> str:
        norm_target = str(target or "").strip().lower().removeprefix("minecraft:")
        eff_target_y = target_y
        clamped_from: int | None = None
        if norm_target in {"diamond", "diamond_ore", "deepslate_diamond_ore"} and target_y is not None:
            ty_int = int(target_y)
            if -30 < ty_int <= 16:
                clamped_from = ty_int
                eff_target_y = -54

        eff_duration_s, deadline_imminent = self._clamp_tool_budget(
            max_duration_s, max_s=55.0, min_s=20.0, reserve_s=4.0
        )
        if deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": False,
                    "error": {
                        "code": "episode_deadline_imminent",
                        "message": "本集剩余时间不足 6s，已跳过长耗时挖矿以避免超时越界。",
                    },
                },
                ensure_ascii=False,
            )

        def _invoke() -> dict[str, Any]:
            res = self.skills.mine_dfs(
                target=target,
                goal_count=goal_count,
                target_y=eff_target_y,
                action=action,
                geofence=self.geofence,
                max_duration_s=eff_duration_s,
            )
            if clamped_from is not None and isinstance(res, dict):
                res["clamped_shallow_diamond_y_from"] = clamped_from
            if isinstance(res, dict):
                # BUG-029: mine_dfs is the main long-mining path and does not
                # call _ensure_free_slots; enforce retention caps here.
                try:
                    _, cleaned = self.skills._ensure_free_slots(min_free=3)
                except Exception:
                    cleaned = []
                if cleaned:
                    res["inventory_cleaned"] = [
                        {"item": d.get("item"), "count": d.get("count")} for d in cleaned
                    ]
            return res

        return self._run_skill(_invoke)

    def mc_kb_lookup(self, query: str, category: str = "all") -> str:
        res = lookup_kb(query, category=category)
        dim, pos, biome = self._cached_env(fetch_if_missing=False)
        eco = assess_world_target(
            query,
            dimension=dim,
            y=(pos[1] if pos is not None else None),
            biome=biome,
            mode="auto",
        )
        if eco.get("known") is True and (pos is not None or eco.get("available") is False or eco.get("verdict") == "ok"):
            res["ecology_check"] = eco
        return json.dumps(res, ensure_ascii=False)

    def registry(
        self,
        *,
        profile: str = "full",
        extra_tools: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> ToolRegistry:
        all_tools = [
                # ---- 观察工具 (shared: 可单轮并行调用) ----
                Tool(
                    name="mc_status",
                    description="查询坐标pos、速度speed/velocity、血量health、饱食food、昼夜、手持与最近敌对生物。",
                    parameters={},
                    handler=self.mc_status,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_inventory",
                    description="查询背包items、护甲armor、副手offhand与空槽数。",
                    parameters={},
                    handler=self.mc_inventory,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_find_blocks",
                    description="扫描暴露方块。省略types返回附近各方块种类数量与最近坐标；传types返回匹配坐标列表。",
                    parameters={
                        "radius": Parameter("半径1-64，默认16", kind="integer", required=False),
                        "types": Parameter("方块英文ID列表，如['oak_log','stone']", kind="string_array", required=False),
                        "limit": Parameter("条数上限1-60，默认20", kind="integer", required=False),
                        "mature": Parameter("仅搜成熟作物", kind="boolean", required=False),
                    },
                    handler=self.mc_find_blocks,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_get_block",
                    description="查单格方块详情及harvest_tool_ok。",
                    parameters={
                        "x": Parameter("X", kind="integer"),
                        "y": Parameter("Y", kind="integer"),
                        "z": Parameter("Z", kind="integer"),
                    },
                    handler=self.mc_get_block,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_get_entities",
                    description="扫描附近玩家、生物、掉落物（id,name,pos,dist,health）。",
                    parameters={
                        "radius": Parameter("半径1-64，默认32", kind="integer", required=False),
                        "types": Parameter("过滤'hostile'/'animal'/'player'/'item'或生物名", kind="string_array", required=False),
                    },
                    handler=self.mc_get_entities,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_events",
                    description="增量拉取最新游戏事件（聊天、受伤、拾取、死亡坐标）。",
                    parameters={
                        "limit": Parameter("上限，默认50", kind="integer", required=False),
                    },
                    handler=self.mc_events,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_advancements",
                    description="查原版进度状态。",
                    parameters={
                        "status": Parameter("'completed'/'in_progress'/'not_started'", required=False),
                    },
                    handler=self.mc_advancements,
                    concurrency="shared",
                ),
                # ---- 动作工具 (exclusive: 独占串行执行) ----
                Tool(
                    name="mc_move_to",
                    description="寻路移动到(x,z,可选y)或entity_id。",
                    parameters={
                        "x": Parameter("X", kind="number", required=False),
                        "y": Parameter("Y", kind="number", required=False),
                        "z": Parameter("Z", kind="number", required=False),
                        "entity_id": Parameter("实体ID", kind="integer", required=False),
                        "range": Parameter("到达距离，默认2.0", kind="number", required=False),
                        "ignore_hurt": Parameter("受伤不中断", kind="boolean", required=False),
                    },
                    handler=self.mc_move_to,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_dig",
                    description="自动走近目标方块、切换最佳工具挖掘并拾取掉落物。",
                    parameters={
                        "x": Parameter("X", kind="integer"),
                        "y": Parameter("Y", kind="integer"),
                        "z": Parameter("Z", kind="integer"),
                        "collect": Parameter("自动拾取，默认true", kind="boolean", required=False),
                    },
                    handler=self.mc_dig,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_place",
                    description="放置方块；省略x,y,z时自动放在身旁地面。",
                    parameters={
                        "item": Parameter("方块名，如'crafting_table'"),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_place,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_craft",
                    description="合成物品（count为目标产物总数量，如要12块木板传count=12；3x3配方需附近已放置crafting_table）。",
                    parameters={
                        "item": Parameter("产物英文名，如'oak_planks','crafting_table','wooden_pickaxe'"),
                        "count": Parameter("目标产物总数量，默认1", kind="integer", required=False),
                    },
                    handler=self.mc_craft,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_equip",
                    description="装备物品到槽位：'hand'/'off-hand'/'head'/'torso'/'legs'/'feet'。",
                    parameters={
                        "item": Parameter("物品名"),
                        "slot": Parameter("槽位，默认'hand'", required=False),
                    },
                    handler=self.mc_equip,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_interact",
                    description="右键交互方块(x,y,z)或实体(entity_id)：门、床、播种、装倒水、上船、喂养。",
                    parameters={
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                        "entity_id": Parameter("实体ID", kind="integer", required=False),
                        "item": Parameter("交互前手持物品", required=False),
                    },
                    handler=self.mc_interact,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_use_item",
                    description="使用物品：吃食物、喝药水、扔珍珠、放船、钓鱼。",
                    parameters={
                        "item": Parameter("物品名"),
                        "entity_id": Parameter("对准实体ID", kind="integer", required=False),
                        "x": Parameter("对准X", kind="number", required=False),
                        "y": Parameter("对准Y", kind="number", required=False),
                        "z": Parameter("对准Z", kind="number", required=False),
                        "ms": Parameter("保持毫秒", kind="integer", required=False),
                    },
                    handler=self.mc_use_item,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_attack",
                    description="自动换近战武器追击entity_id（默认打死并拾取掉落；传max_hits可风筝单敲）。",
                    parameters={
                        "entity_id": Parameter("目标实体ID", kind="integer"),
                        "max_hits": Parameter("最多攻击次数，省略打至死", kind="integer", required=False),
                        "interval_ms": Parameter("挥击间隔ms(50-5000)", kind="integer", required=False),
                        "timeout_ms": Parameter("超时ms，默认20000", kind="integer", required=False),
                    },
                    handler=self.mc_attack,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_shoot",
                    description="远程射击目标（bow/crossbow/trident）。",
                    parameters={
                        "weapon": Parameter("武器名，默认'bow'", required=False),
                        "entity_id": Parameter("目标实体ID", kind="integer", required=False),
                        "x": Parameter("X", kind="number", required=False),
                        "y": Parameter("Y", kind="number", required=False),
                        "z": Parameter("Z", kind="number", required=False),
                        "charge_ms": Parameter("蓄力时长ms(200-5000)", kind="integer", required=False),
                    },
                    handler=self.mc_shoot,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_window_view",
                    description="查看附近容器(furnace/chest)或村民交易界面。",
                    parameters={
                        "block": Parameter("方块类型名，如'furnace'", required=False),
                        "entity_id": Parameter("实体ID", kind="integer", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_window_view,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_window_act",
                    description="操作容器/熔炉/交易，如[{'op':'put','item':'raw_iron','count':3,'to':'input'},{'op':'put','item':'coal','count':1,'to':'fuel'}]或[{'op':'take','from':'output'}]。",
                    parameters={
                        "ops": Parameter("操作数组", kind="object_array"),
                        "block": Parameter("方块名，如'furnace'", required=False),
                        "entity_id": Parameter("实体ID", kind="integer", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_window_act,
                    concurrency="exclusive",
                ),
                # ---- L2 窗口原子封装（无策略；给定参数即执行） ----
                Tool(
                    name="mc_skill_container",
                    description="原子容器批量存取：对箱子/木桶一次 put 或 take 多组物品，并同步 WorldMemory 容器快照。",
                    parameters={
                        "items": Parameter("物品数组，如[{'item':'cobblestone','count':64}]", kind="object_array"),
                        "action": Parameter("put 存入 / take 取出，默认put", required=False),
                        "block": Parameter("方块名，如'chest'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_skill_container,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_build_portal",
                    description="一键清地、搭建 4x5 黑曜石下界传送门、用打火石点火并走入传送门进入下界（需背包有 obsidian>=10 与 flint_and_steel>=1）。",
                    parameters={
                        "enter": Parameter("点火后是否自动走入传送门进入下界，默认true", kind="boolean", required=False),
                        "wait_s": Parameter("站入传送门等待维度切换的最长秒数，默认8", kind="number", required=False),
                    },
                    handler=self.mc_skill_build_portal,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_enchant",
                    description="附魔台单次连招：同一次window/act放物品+青金石+选option并取回；默认校验落包，窗口状态陈旧时自动重连刷新。",
                    parameters={
                        "item": Parameter("待附魔物品英文ID"),
                        "option": Parameter("附魔选项索引(0-2)", kind="integer"),
                        "lapis_count": Parameter("青金石数量，省略=option+1", kind="integer", required=False),
                        "take": Parameter("是否取回附魔物品，默认true", kind="boolean", required=False),
                        "block": Parameter("方块名，默认'enchanting_table'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                        "verify": Parameter("校验附魔物品是否真的落包，默认true", kind="boolean", required=False),
                        "reconnect_on_stale": Parameter("落包校验失败时自动重连刷新窗口状态，默认true", kind="boolean", required=False),
                    },
                    handler=self.mc_skill_enchant,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_trade",
                    description="村民/流浪商人交易：先读options，再按option索引或get_item匹配执行select(times)。",
                    parameters={
                        "entity_id": Parameter("村民/流浪商人实体ID", kind="integer"),
                        "option": Parameter("交易选项索引，省略则按get_item匹配", kind="integer", required=False),
                        "get_item": Parameter("想要的产物英文ID", required=False),
                        "times": Parameter("连续交易次数，默认1", kind="integer", required=False),
                    },
                    handler=self.mc_skill_trade,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_brew",
                    description="酿造台单次投料：bottles/ingredient/fuel，必要时轮询brew_time_left至0。",
                    parameters={
                        "ingredient_item": Parameter("酿造原料英文ID，如'nether_wart'"),
                        "bottles": Parameter("瓶子数组，如[{'item':'potion','potion':'water'}]", kind="object_array", required=False),
                        "fuel_item": Parameter("燃料英文ID，如'blaze_powder'", required=False),
                        "wait": Parameter("是否等待酿造完成，默认true", kind="boolean", required=False),
                        "block": Parameter("方块名，默认'brewing_stand'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_skill_brew,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_anvil",
                    description="铁砧单次连招：放left/right、可选改名text、取output。",
                    parameters={
                        "left_item": Parameter("左槽物品英文ID"),
                        "right_item": Parameter("右槽材料英文ID", required=False),
                        "text": Parameter("改名文本(<=50字符)", required=False),
                        "take": Parameter("是否取回产物，默认true", kind="boolean", required=False),
                        "block": Parameter("方块名，默认'anvil'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_skill_anvil,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_smithing",
                    description="锻造台单次连招：template/base/addition三槽投料并取output。",
                    parameters={
                        "template_item": Parameter("锻造模板英文ID"),
                        "base_item": Parameter("基础物品英文ID"),
                        "addition_item": Parameter("追加材料英文ID"),
                        "take": Parameter("是否取回产物，默认true", kind="boolean", required=False),
                        "block": Parameter("方块名，默认'smithing_table'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_skill_smithing,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_stonecutter",
                    description="切石机单次连招：放input、select选项并取output。",
                    parameters={
                        "input_item": Parameter("原料英文ID，如'stone'"),
                        "option": Parameter("切石选项索引", kind="integer"),
                        "count": Parameter("投入原料数量，省略=全部", kind="integer", required=False),
                        "take": Parameter("是否取回产物，默认true", kind="boolean", required=False),
                        "block": Parameter("方块名，默认'stonecutter'", required=False),
                        "x": Parameter("X", kind="integer", required=False),
                        "y": Parameter("Y", kind="integer", required=False),
                        "z": Parameter("Z", kind="integer", required=False),
                    },
                    handler=self.mc_skill_stonecutter,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_posture",
                    description="持久潜行sneak、举盾shield或持续跳跃jump（水里浮面）全量开关，不占动作锁。",
                    parameters={
                        "sneak": Parameter("潜行", kind="boolean", required=False),
                        "shield": Parameter("举盾", kind="boolean", required=False),
                        "jump": Parameter("持续跳跃/水中浮面", kind="boolean", required=False),
                    },
                    handler=self.mc_posture,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_dismount",
                    description="下船/下马（开船需先贴岸<=1格）。",
                    parameters={},
                    handler=self.mc_dismount,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_drop",
                    description="丢弃背包物品。",
                    parameters={
                        "item": Parameter("物品名"),
                        "count": Parameter("数量，默认1", kind="integer", required=False),
                    },
                    handler=self.mc_drop,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_chat",
                    description="发送公屏消息（<=100字，禁/开头）。",
                    parameters={
                        "message": Parameter("消息文本"),
                    },
                    handler=self.mc_chat,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_stop",
                    description="刹停当前动作。",
                    parameters={},
                    handler=self.mc_stop,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_control",
                    description="绕过寻路底层键位与视线左键挥砍（stalled脱困/跳劈/微调站位）。",
                    parameters={
                        "forward": Parameter("前进", kind="boolean", required=False),
                        "back": Parameter("后退", kind="boolean", required=False),
                        "left": Parameter("左移", kind="boolean", required=False),
                        "right": Parameter("右移", kind="boolean", required=False),
                        "jump": Parameter("跳跃", kind="boolean", required=False),
                        "sneak": Parameter("潜行", kind="boolean", required=False),
                        "sprint": Parameter("疾跑", kind="boolean", required=False),
                        "yaw": Parameter("朝向偏航弧度", kind="number", required=False),
                        "pitch": Parameter("朝向俯仰弧度", kind="number", required=False),
                        "hit": Parameter("左键挥砍次数(1-20)", kind="integer", required=False),
                        "hit_interval_ms": Parameter("连挥间隔ms(50-5000)", kind="integer", required=False),
                        "ms": Parameter("按住时长ms(50-3000)", kind="integer", required=False),
                        "wait": Parameter("实时操控：false按下即返且后台保持ms，空参立即松开", kind="boolean", required=False),
                        "stop_on": Parameter("打断条件列表，传[]防受击中断", kind="string_array", required=False),
                    },
                    handler=self.mc_control,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_look",
                    description="主动转向（弧度yaw/pitch、坐标x/y/z或entity_id），不占动作锁。",
                    parameters={
                        "entity_id": Parameter("目标实体ID", kind="integer", required=False),
                        "x": Parameter("目标X坐标", kind="number", required=False),
                        "y": Parameter("目标Y坐标", kind="number", required=False),
                        "z": Parameter("目标Z坐标", kind="number", required=False),
                        "yaw": Parameter("偏航弧度", kind="number", required=False),
                        "pitch": Parameter("俯仰弧度", kind="number", required=False),
                    },
                    handler=self.mc_look,
                    concurrency="shared",
                ),
                # ---- L0/L1 确定性宏技能与进度规划 ----
                Tool(
                    name="mc_advancement_frontier",
                    description="结合1.21.11进度树计算当前立即可解锁的Frontier进度列表（优先级排序）。",
                    parameters={
                        "limit": Parameter("返回条数上限，默认12", kind="integer", required=False),
                    },
                    handler=self.mc_advancement_frontier,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_skill_stabilize",
                    description="L0保命自检：水下踩水浮面、自动吃背包最优食物、低血量/岩浆急救。",
                    parameters={},
                    handler=self.mc_skill_stabilize,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_scout_surface",
                    description="地表寻村/探索优先用。连续推进最多5段，每段扫描64格，命中目标或遇近敌/受伤就停。",
                    parameters={
                        "direction": Parameter("auto优先未探索方向，也可指定north/east/south/west或对角方向", required=False),
                        "target_types": Parameter(
                            "目标方块ID或villager/iron_golem。省略时搜村庄方块和村民/铁傀儡",
                            kind="string_array", required=False,
                        ),
                        "max_duration_s": Parameter("本次预算秒数，默认45，最多50", kind="number", required=False),
                        "step_dist": Parameter("每段距离28至32格，默认28", kind="number", required=False),
                    },
                    handler=self.mc_skill_scout_surface,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_harvest_logs",
                    description="宏技能：自动换斧砍树集原木，内置保护区(protected_region)坐标拉黑。",
                    parameters={
                        "target_logs": Parameter("目标原木数，默认4", kind="integer", required=False),
                        "radius": Parameter("搜索半径4-64，默认32", kind="integer", required=False),
                    },
                    handler=self.mc_skill_harvest_logs,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_craft_gear",
                    description="宏技能：自动从原木合木板/木棍、自动摆工作台并合成工具/护甲/盾/工作站且自动装备。",
                    parameters={
                        "item": Parameter("目标物品英文ID，如stone_pickaxe/shield/furnace"),
                        "count": Parameter("目标产物总数量，默认1", kind="integer", required=False),
                        "equip_slot": Parameter("装备槽hand/off-hand/head/torso/legs/feet", required=False),
                    },
                    handler=self.mc_skill_craft_gear,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_mine_blocks",
                    description="宏技能：自动校验镐等级并装备最高镐，屏蔽保护区坐标后连挖目标方块/矿石。",
                    parameters={
                        "block_types": Parameter("目标方块ID列表，如['stone']或['iron_ore','deepslate_iron_ore']", kind="string_array"),
                        "count": Parameter("目标数量，默认3", kind="integer", required=False),
                        "radius": Parameter("搜索半径4-64，默认32", kind="integer", required=False),
                    },
                    handler=self.mc_skill_mine_blocks,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_smelt",
                    description="宏技能：自动建放熔炉、精确配ceil(n/8)燃料并轮询守卫直到取出烧制产物。",
                    parameters={
                        "input_item": Parameter("原料英文ID，如raw_iron/cobblestone/beef"),
                        "count": Parameter("烧制数量，默认1", kind="integer", required=False),
                        "fuel_item": Parameter("指定燃料英文ID（省略自动选煤/木炭/木头）", required=False),
                    },
                    handler=self.mc_skill_smelt,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_collect_drops",
                    description="宏技能：扫描附近item掉落物并走近拾取；仅真实背包增加才成功，无入包返回失败，附尝试/跳过原因和当前背包。",
                    parameters={
                        "radius": Parameter("扫描半径2-32，默认12", kind="integer", required=False),
                        "max_pickups": Parameter("最多尝试拾取个数，默认8", kind="integer", required=False),
                    },
                    handler=self.mc_skill_collect_drops,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_bind_bed",
                    description="宏技能：走近指定坐标床并安全绑定重生点（白天或夜晚均可靠生效）。",
                    parameters={
                        "x": Parameter("床X坐标", kind="integer"),
                        "y": Parameter("床Y坐标", kind="integer"),
                        "z": Parameter("床Z坐标", kind="integer"),
                    },
                    handler=self.mc_skill_bind_bed,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_unstick",
                    description="宏技能：按math.floor精确清理身体0.6x1.8碰撞箱占位与头顶卡死方块并前跳脱困。",
                    parameters={},
                    handler=self.mc_skill_unstick,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_kb_lookup",
                    description="只读查表：检索1.21.11全部125项成就(含RULES分值与攻略)、合成配方、矿脉Y层分布、怪物对策与API物理规则（支持中英文）。",
                    parameters={
                        "query": Parameter("检索词，如'trident'/'throw_trident'/'抖包袱'/'lodestone'/'铁矿'/'shield'"),
                        "category": Parameter("分类过滤：all/recipe/ore/mob/mechanic/advancement，默认all", required=False),
                    },
                    handler=self.mc_kb_lookup,
                    concurrency="shared",
                ),
                Tool(
                    name="mc_skill_bunker",
                    description="紧急地下掩体宏技能：原地向下挖1x3竖井并用实体方块封顶，隔绝夜间怪物，同时自动校验脚底[bx,by-4,bz]实体地板与流体/基岩安全。",
                    parameters={},
                    handler=self.mc_skill_bunker,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_flee",
                    description="紧急逃跑宏技能：免疫受击打断(stop_on=['death'])，自动背离最近敌对生物冲刺跳跃并寻路拉开安全距离（遇苦力怕、群怪、无剑斧或残血时优先调用）。",
                    parameters={"distance": Parameter("逃离距离(8-22)，默认14", kind="integer", required=False)},
                    handler=self.mc_skill_flee,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_combat",
                    description="主动战术战斗宏技能（combat_v1）：自动选武器、控攻速与动态刹车接战。mode='auto' 按敌怪与装备自动路由（苦力怕风筝、远程/持盾走剑盾突进 rush、无盾近战 melee）；也可显式指定 rush/melee/creeper/skeleton_shield/skeleton_bow/pillar/crit/wtap/gap_foot/gap_head。受围栏、30s 硬预算与小脑低血抢占保护。",
                    parameters={
                        "mode": Parameter("战斗模式：auto(推荐)/melee/rush/creeper/skeleton_shield/skeleton_bow/pillar/crit/mcrit/wtap/gap_foot/gap_head", required=False),
                        "target_name": Parameter("可选英文实体名，如 zombie/skeleton/blaze/creeper", required=False),
                        "max_time_s": Parameter("最大接战时长(3-30s)，默认20", kind="number", required=False),
                        "pillar_height": Parameter("仅 mode='pillar' 有效，搭高格数(1-4)，默认3", kind="integer", required=False),
                    },
                    handler=self.mc_skill_combat,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_staircase",
                    description="阶梯升降辅助技能：主要用于向上修台阶重返地表(vertical='up'/'pillar')或短距地形升降；正规地下下井探铁/探钻请优先调用 mc_skill_mine_dfs。",
                    parameters={
                        "direction": Parameter("前进方向：north(-Z)/south(+Z)/east(+X)/west(-X)", required=False),
                        "steps": Parameter("阶梯步数(1-25)，每步改变1格Y高度", kind="integer", required=False),
                        "vertical": Parameter("'up' 斜向阶梯返地表、'pillar' 原地垫脚直上 或 'down' 短距下行", required=False),
                        "stop_on_ore": Parameter("遇到暴露 iron_ore/deepslate_iron_ore 是否立刻停下（默认true）", kind="boolean", required=False),
                    },
                    handler=self.mc_skill_staircase,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_tunnel",
                    description="沿同一Y层开凿两格高短矿道并逐格走入，解决隔墙矿石无法寻路；每次最多8格，检查实体地板、邻接流体/落沙、镐等级和实际到达位置。遇矿停在矿面，再用采矿技能；遇危险或受伤立即返回重规划。",
                    parameters={
                        "direction": Parameter("north(-Z)/south(+Z)/east(+X)/west(-X)", required=False),
                        "steps": Parameter("同层前进格数1–8，默认4", kind="integer", required=False),
                        "stop_on_ore": Parameter("前方矿面停下，默认true", kind="boolean", required=False),
                    },
                    handler=self.mc_skill_tunnel,
                    concurrency="exclusive",
                ),
                Tool(
                    name="mc_skill_mine_dfs",
                    description="路径压缩地下探矿DFS（实心地面开井Collar->Switchback转角->64格矿脉雷达引导Spine鱼骨开巷）：自动双镐分流、悬空矿Catch-Block托盘、邻域流体与破口封堵、自挖空腔垫石穿越及零挖掘航点回溯。",
                    parameters={
                        "target": Parameter("目标矿种:iron/diamond/coal/gold/copper/redstone/lapis", required=False),
                        "goal_count": Parameter("目标入包数量，默认6", kind="integer", required=False),
                        "target_y": Parameter("指定开采Y层，省略按矿种预设", kind="integer", required=False),
                        "action": Parameter("explore/return_collar/climb_out/reset/status，默认explore（climb_out=一次调用内自动探测方向并连续爬回地表，深井废弃后用它，别一格一格叫staircase；reset会重置当前矿井并尝试换点；遇shaft_exhausted建议先mc_move_to平移16格以上再explore）", required=False),
                        "max_duration_s": Parameter("单次调用最长作业秒数（20-90，默认32）。保持默认即可；提效调参留待后续专项", kind="number", required=False),
                    },
                    handler=self.mc_skill_mine_dfs,
                    concurrency="exclusive",
                ),
        ]
        if profile == "compact":
            allowed = set(COMPACT_CORE_TOOLS)
            if extra_tools:
                allowed.update(str(t) for t in extra_tools)
            return ToolRegistry([_compactify_tool(tool) for tool in all_tools if tool.name in allowed])
        return ToolRegistry(all_tools)


_COMPACT_TOOL_DESC: dict[str, str] = {
    "mc_status": "查位置/血/食/昼夜",
    "mc_inventory": "查背包/护甲/副手",
    "mc_find_blocks": "扫暴露方块",
    "mc_get_block": "查单格方块",
    "mc_get_entities": "扫生物/物品",
    "mc_events": "拉取新事件",
    "mc_advancements": "查进度状态",
    "mc_move_to": "寻路(x,z;禁y)",
    "mc_skill_scout_surface": "地表寻村/探索，连走最多5段，命中或遇险即停",
    "mc_dig": "挖格拾取",
    "mc_place": "放方块",
    "mc_craft": "合成(3x3需桌)",
    "mc_equip": "装备(hand/off-hand/head/torso/legs/feet)",
    "mc_interact": "右键方块/实体",
    "mc_use_item": "吃/用物品",
    "mc_attack": "近战攻击",
    "mc_shoot": "射箭/弩/三叉戟",
    "mc_window_view": "看容器或交易窗",
    "mc_window_act": "操作容器/炉/附魔/交易",
    "mc_skill_container": "容器批量存取",
    "mc_skill_build_portal": "一键搭建并进入下界传送门",
    "mc_skill_enchant": "附魔台单次连招",
    "mc_skill_trade": "村民按选项交易",
    "mc_skill_brew": "酿造台投料并等待",
    "mc_skill_anvil": "铁砧合成/改名",
    "mc_skill_smithing": "锻造台升级",
    "mc_skill_stonecutter": "切石机切料",
    "mc_posture": "切换sneak/shield/jump",
    "mc_dismount": "下船/下马",
    "mc_drop": "丢弃物品",
    "mc_chat": "发公屏消息",
    "mc_stop": "停当前动作",
    "mc_control": "底层按键挥砍",
    "mc_look": "转向坐标/实体",
    "mc_advancement_frontier": "查可解锁进度",
    "mc_skill_stabilize": "保命进食浮水",
    "mc_skill_harvest_logs": "自动换斧砍树",
    "mc_skill_craft_gear": "合成任意物品/工具/护甲(自动木板木棍工作台)",
    "mc_skill_mine_blocks": "自动换镐挖露头矿/石",
    "mc_skill_smelt": "自动放炉配燃料烧制",
    "mc_skill_collect_drops": "拾掉落",
    "mc_skill_bind_bed": "走近床设重生点",
    "mc_skill_unstick": "卡死/水体脱困",
    "mc_kb_lookup": "查配方/矿/成就",
    "mc_skill_bunker": "竖井避怪",
    "mc_skill_flee": "无遮挡遇险时封堵矿道或冲刺逃离",
    "mc_skill_combat": "主动战术接战(auto/rush/melee/creeper/pillar/crit/wtap/gap)，受围栏与30s预算保护",
    "mc_skill_staircase": "爬出天坑/垫高(up/pillar)；下井探矿用mine_dfs",
    "mc_skill_tunnel": "定向凿墙平推(隔墙目标/绕水/下界穿墙；盲探矿用mine_dfs)",
    "mc_skill_mine_dfs": "路径压缩探矿DFS/返井口",
}

_SELF_EVIDENT_PARAMS: frozenset[str] = frozenset(
    {
        "x", "y", "z", "entity_id", "radius", "limit", "steps", "query",
        "message", "sneak", "shield", "jump", "forward", "back", "left",
        "right", "sprint", "yaw", "pitch", "ms", "collect", "mature",
        "ignore_hurt", "max_pickups", "target_logs", "stop_on_ore",
        "interval_ms", "timeout_ms", "hit", "hit_interval_ms", "stop_on",
        "wait", "charge_ms", "goal_count", "target_y",
        "item", "count", "range", "max_hits", "block", "block_types",
        "input_item", "fuel_item", "slot", "equip_slot", "types", "distance",
        "action", "option", "get_item", "times", "left_item", "right_item",
        "text", "take", "template_item", "base_item", "addition_item",
        "bottles", "wait", "ingredient_item",
    }
)

_COMPACT_PARAM_DESC: dict[tuple[str, str], str] = {
    ("mc_get_entities", "types"): "hostile/animal/player/item",
    ("mc_advancements", "status"): "completed/not_started",
    ("mc_shoot", "weapon"): "bow/crossbow/trident",
    ("mc_window_act", "ops"): "[{op,item,count,to/from}]",
    ("mc_kb_lookup", "category"): "all/recipe/ore/mob/advancement",
    ("mc_skill_staircase", "direction"): "north/south/east/west",
    ("mc_skill_staircase", "vertical"): "down/up/pillar",
    ("mc_skill_tunnel", "direction"): "north/south/east/west",
    ("mc_skill_mine_dfs", "target"): "iron/diamond/coal/gold",
    ("mc_skill_mine_dfs", "action"): "explore/return_collar/climb_out(自动爬回地表)/reset(换点重开)",
}


def _compactify_tool(tool: Tool) -> Tool:
    new_params: dict[str, Parameter] = {}
    for pname, p in tool.parameters.items():
        if (tool.name, pname) in _COMPACT_PARAM_DESC:
            pdesc = _COMPACT_PARAM_DESC[(tool.name, pname)]
        elif pname in _SELF_EVIDENT_PARAMS:
            pdesc = ""
        else:
            pdesc = p.description[:26]
        new_params[pname] = Parameter(description=pdesc, kind=p.kind, required=p.required)
    return Tool(
        name=tool.name,
        description=_COMPACT_TOOL_DESC.get(tool.name, tool.description[:40]),
        parameters=new_params,
        handler=tool.handler,
        concurrency=tool.concurrency,
        interrupt=tool.interrupt,
        compact_schema=True,
    )


def _build_window_target(
    block: str | None,
    entity_id: int | None,
    x: int | None,
    y: int | None,
    z: int | None,
) -> dict[str, Any] | str:
    if entity_id is not None:
        return {"entity_id": entity_id}
    if x is not None and y is not None and z is not None:
        return {"pos": [x, y, z]}
    if block:
        return {"block": block}
    return json.dumps(
        {"ok": False, "error": {"code": "bad_args", "message": "需要提供 block、entity_id 或 (x, y, z) 之一"}},
        ensure_ascii=False,
    )
