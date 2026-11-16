"""Minecraft Agent API v1.3 声明式工具集（对接 nano-agent ToolRegistry / ToolScheduler）。

观察类工具标记为 `concurrency="shared"`（可单轮并发查询）；
动作类工具标记为 `concurrency="exclusive"`（自动串行执行，避免触发 API `busy` 锁冲突）。
业务错误（如缺材料、寻路失败、受伤打断）序列化为 JSON 字符串回填给模型纠错；认证失效或致命冷却抛 RuntimeError 终止。
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

import httpx

from ..bot import BotSession
from ..errors import ApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ..kb import build_jit_error_hint, lookup_kb
from ..skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier
from ..skills.library import BLOCK_MIN_PICKAXE_TIER, _best_pickaxe_in_inv
from .tool_registry import Parameter, Tool, ToolRegistry

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
    def __init__(self, bot: BotSession) -> None:
        self.bot = bot
        self._state_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._last_event_seq: int = int(getattr(bot, "_last_event_seq", 0) or 0)
        self._in_water: bool = False
        self.skills = MacroSkills(bot)

    def _set_last_event_seq(self, seq: int) -> None:
        with self._state_lock:
            if seq > self._last_event_seq:
                self._last_event_seq = seq
            try:
                setattr(self.bot, "_last_event_seq", self._last_event_seq)
            except Exception:
                pass

    def _reconnect_once(self) -> None:
        with self._reconnect_lock:
            self.bot.wait_until_ready(timeout_s=30.0, poll_interval_s=1.0)

    def _update_status_cache(self, st: dict[str, Any]) -> None:
        with self._state_lock:
            self._in_water = bool(st.get("in_water") or st.get("submerged"))

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
            if bname not in _PASSABLE_SIGHT_BLOCKS:
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

    def _call_json(self, path: str, body: dict[str, Any] | None = None, *, method: str = "POST") -> str:
        try:
            result = self.bot.call(path, body, method=method)
        except (AuthError, DeathLoopCooldownError) as exc:
            raise RuntimeError(f"MC API 会话不可继续：{exc}") from None
        except ApiError as exc:
            if exc.code != "not_connected":
                return self._format_api_error(path, body or {}, exc)
            try:
                self._reconnect_once()
                result = self.bot.call(path, body, method=method)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"MC API 网络请求异常（{type(exc).__name__}）：{exc}") from None

        if path == "status" and isinstance(result, dict):
            self._update_status_cache(result)
            self._enrich_status_awareness(result)
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)

    @staticmethod
    def _format_api_error(action: str, params: dict[str, Any], exc: ApiError) -> str:
        err_obj: dict[str, Any] = {"code": exc.code, "message": exc.message, **exc.context}
        hint = build_jit_error_hint(action, params, exc.code, exc.message)
        if hint:
            err_obj["kb_hint"] = hint
        return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)

    def _run_skill(self, fn) -> str:
        try:
            result = fn()
        except (AuthError, DeathLoopCooldownError, SafetyEmergency) as exc:
            raise RuntimeError(f"MC 技能触发紧急停机：{exc}") from None
        except ApiError as exc:
            if exc.code != "not_connected":
                return self._format_api_error("skill", {}, exc)
            try:
                self._reconnect_once()
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

        return json.dumps(result, ensure_ascii=False)

    # ---- Shared Read-Only Observations ----

    def mc_status(self) -> str:
        return self._call_json("status", method="GET")

    def mc_inventory(self) -> str:
        return self._call_json("inventory", method="GET")

    def mc_find_blocks(
        self,
        radius: int = 16,
        types: list[str] | None = None,
        limit: int = 20,
        mature: bool | None = None,
    ) -> str:
        radius = max(1, min(64, radius))
        limit = max(1, min(60, limit))
        payload: dict[str, Any] = {"radius": radius, "limit": limit}
        if types:
            payload["types"] = types
        if mature is not None:
            payload["mature"] = mature
        return self._call_json("find_blocks", payload)

    def mc_get_block(self, x: int, y: int, z: int) -> str:
        return self._call_json("get_block", {"pos": [x, y, z]})

    def mc_get_entities(self, radius: int = 32, types: list[str] | None = None) -> str:
        radius = max(1, min(64, radius))
        payload: dict[str, Any] = {"radius": radius}
        if types:
            payload["types"] = types
        raw = self._call_json("get_entities", payload)
        try:
            parsed = json.loads(raw)
            ents = (parsed.get("result") or {}).get("entities") or []
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
            except (AuthError, DeathLoopCooldownError):
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
            except (AuthError, DeathLoopCooldownError):
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
                    cause = ev.get("cause") or ev.get("message") or ""
                    pos = ev.get("pos")
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

    def mc_move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        entity_id: int | None = None,
        range: float = 2.0,
        ignore_hurt: bool = False,
    ) -> str:
        payload: dict[str, Any] = {"range": range}
        if entity_id is not None:
            payload["entity_id"] = entity_id
        else:
            if x is None or z is None:
                return json.dumps(
                    {"ok": False, "error": {"code": "bad_args", "message": "需要提供 (x, z) 坐标或 entity_id"}},
                    ensure_ascii=False,
                )
            payload["x"] = x
            payload["z"] = z
            if y is not None:
                payload["y"] = y
        # 水中自动保护：防止溺水扣血打断自己的游上岸动作
        if ignore_hurt or self._is_in_water():
            payload["stop_on"] = ["death"]
        return self._call_json("move_to", payload)

    def mc_dig(self, x: int, y: int, z: int, collect: bool = True) -> str:
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
        return self._call_json("dig", {"pos": [x, y, z], "collect": collect})

    def mc_place(
        self,
        item: str,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"item": item}
        if x is not None and y is not None and z is not None:
            payload["pos"] = [x, y, z]
        elif any(v is not None for v in (x, y, z)):
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "放置坐标需同时提供 x, y, z，或全部省略自动选落点"}},
                ensure_ascii=False,
            )
        return self._call_json("place", payload)

    def mc_craft(self, item: str, count: int = 1) -> str:
        return self._call_json("craft", {"item": item, "count": max(1, count)})

    def mc_equip(self, item: str, slot: str = "hand") -> str:
        return self._call_json("equip", {"item": item, "slot": slot})

    def mc_interact(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        entity_id: int | None = None,
        item: str | None = None,
    ) -> str:
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
        return self._call_json("interact", payload)

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
            payload["ms"] = ms
        return self._call_json("use_item", payload)

    def mc_attack(
        self,
        entity_id: int,
        max_hits: int | None = None,
        interval_ms: int | None = None,
        timeout_ms: int = 20000,
    ) -> str:
        payload = {"entity_id": entity_id, "timeout": timeout_ms}
        if max_hits is not None:
            payload["max_hits"] = max(1, max_hits)
        if interval_ms is not None:
            payload["interval_ms"] = max(50, min(5000, interval_ms))
        return self._call_json("attack", payload)

    def mc_shoot(
        self,
        weapon: str = "bow",
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
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
        return self._call_json("window/view", {"target": target})

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
        return self._call_json("window/act", {"target": target, "ops": ops})

    def mc_posture(self, sneak: bool = False, shield: bool = False, jump: bool = False) -> str:
        return self._call_json("posture", {"sneak": sneak, "shield": shield, "jump": jump})

    def mc_dismount(self) -> str:
        return self._call_json("dismount", {})

    def mc_drop(self, item: str, count: int = 1) -> str:
        return self._call_json("drop", {"item": item, "count": max(1, count)})

    def mc_chat(self, message: str) -> str:
        return self._call_json("chat", {"message": message[:100]})

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
        stop_on: list[str] | None = None,
    ) -> str:
        payload = {}
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
        return self._call_json("look", payload)

    # ---- L0 / L1 Macro Skills ----

    def mc_advancement_frontier(self, limit: int = 12) -> str:
        return self._run_skill(lambda: compute_advancement_frontier(self.bot, limit=limit))

    def mc_skill_stabilize(self) -> str:
        return self._run_skill(lambda: check_and_stabilize(self.bot))

    def mc_skill_harvest_logs(self, target_logs: int = 4, radius: int = 32) -> str:
        return self._run_skill(lambda: self.skills.harvest_logs(target_logs=target_logs, radius=radius))

    def mc_skill_craft_gear(self, item: str, count: int = 1, equip_slot: str | None = None) -> str:
        return self._run_skill(lambda: self.skills.craft_gear(item=item, count=count, equip_slot=equip_slot))

    def mc_skill_mine_blocks(self, block_types: list[str], count: int = 3, radius: int = 32) -> str:
        return self._run_skill(lambda: self.skills.mine_blocks(block_types=block_types, count=count, radius=radius))

    def mc_skill_smelt(self, input_item: str, count: int = 1, fuel_item: str | None = None) -> str:
        return self._run_skill(
            lambda: self.skills.smelt_items(input_item=input_item, count=count, fuel_item=fuel_item)
        )

    def mc_skill_collect_drops(self, radius: int = 12, max_pickups: int = 8) -> str:
        return self._run_skill(lambda: self.skills.collect_drops(radius=radius, max_pickups=max_pickups))

    def mc_skill_bind_bed(self, x: int, y: int, z: int) -> str:
        return self._run_skill(lambda: self.skills.bind_bed_spawn(x=x, y=y, z=z))

    def mc_skill_unstick(self) -> str:
        return self._run_skill(lambda: self.skills.unstick())

    def mc_skill_bunker(self) -> str:
        return self._run_skill(lambda: self.skills.dig_safety_bunker())

    def mc_skill_staircase(
        self,
        direction: str = "north",
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool = True,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.dig_staircase(
                direction=direction,
                steps=steps,
                vertical=vertical,
                stop_on_ore=stop_on_ore,
            )
        )

    def mc_kb_lookup(self, query: str, category: str = "all") -> str:
        return json.dumps(lookup_kb(query, category=category), ensure_ascii=False)

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            [
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
                    description="只读查表：检索1.21.11合成配方与物品ID纠偏、矿脉Y层分布、怪物对策与API物理规则（支持中英文关键词）。",
                    parameters={
                        "query": Parameter("检索词，如'iron_ore'/'铁矿'/'shield'/'高炉'/'水桶'/'寻路'"),
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
                    name="mc_skill_staircase",
                    description="安全阶梯升降宏技能：沿 north/south/east/west 挖掘3格高阶梯向下下矿(vertical='down'，探测到铁矿自动停下)或向上修台阶重返地表(vertical='up')，严禁在坑里逐格手挖阶梯。",
                    parameters={
                        "direction": Parameter("前进方向：north(-Z)/south(+Z)/east(+X)/west(-X)", required=False),
                        "steps": Parameter("阶梯步数(1-25)，每步改变1格Y高度", kind="integer", required=False),
                        "vertical": Parameter("'down' 向下探矿 或 'up' 向上返回地表", required=False),
                        "stop_on_ore": Parameter("遇到暴露 iron_ore/deepslate_iron_ore 是否立刻停下（默认true）", kind="boolean", required=False),
                    },
                    handler=self.mc_skill_staircase,
                    concurrency="exclusive",
                ),
            ]
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
