#!/usr/bin/env python3
"""mc-agent-api v1.3.14 独立微操与战斗实测工具包（100% Python 标准库，零第三方依赖）。

支持两种用法：
1. --interactive 联机实操模式（无需 RCON）：自己开 MC 1.21.11 客户端进测试服站在 Bot 旁或放怪，
   直接在命令行发 crit / wtap / creeper / skel / pillar / gap 观察物理效果并调参；
2. RCON 全自动高空靶场测伤模式（exp1 ~ exp7 或 --all）：自动建隔离石室、用服务端权威 Health 测跳劈 1.5x 暴击与各策略掉血。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

# ==============================================================================
# 可调核心微操参数（同学实测时可直接改这里的默认值，再观察手感与伤害）
# ==============================================================================
DEFAULT_CRIT_DELAY_S = 0.38     # 双阶段跳劈：起跳后等待多久进入下落期出刀 (秒，推荐扫描 0.32 ~ 0.44)
DEFAULT_PILLAR_WAIT_S = 0.21    # 原地跳垫高：起跳后等待多久脚底净空 /place（黄金窗口 0.18 ~ 0.28s）
WTAP_HIT_MS = 190               # 疾跑击退斩持续按下前进+疾跑时长 (ms)
CREEPER_ENGAGE_DIST = 3.3       # 苦力怕进入出刀打断距离
CREEPER_RETREAT_MS = 650        # 苦力怕一刀后反向 sprint+jump 拉开距离持续时间 (ms)
ZIGZAG_ANGLE_RAD = 0.56         # 躲骷髅箭 Z 字折线斜角 (~32 度)
ZIGZAG_STEP_MS = 420            # Z 字变向单步按键时长 (ms)
ZIGZAG_SLEEP_S = 0.36           # Z 字变向实际切换周期 (s)
GAP_STAND_WAIT_S = 0.60         # 隔缝挥砍攻击冷却 (剑 625ms)

HERE = pathlib.Path(__file__).resolve().parent
TOKEN_CACHE_PATH = HERE / ".fightbot_token.json"
REPORT_PATH = HERE / "combat_test_report.json"

ARENA_MIN_X = 790
ARENA_MAX_X = 830
ARENA_MIN_Y = 117
ARENA_FLOOR_Y = 119
ARENA_MAX_Y = 125
ARENA_MIN_Z = 790
ARENA_MAX_Z = 830
CENTER_X = 810.5
CENTER_Y = 120.0
CENTER_Z = 810.5
ARENA_SEL = "x=786,y=115,z=786,dx=48,dy=16,dz=48"


# ==============================================================================
# 1. 环境配置与零依赖 HTTP / RCON 基础客户端（自动绕过本地系统代理）
# ==============================================================================

def load_env_file(env_path: pathlib.Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("MC_API_URL", "MC_BOT_USER", "MC_BOT_PASS", "RCON_HOST", "RCON_PORT", "RCON_PASS"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.extra = extra or {}


class SimpleBotClient:
    """Zero-dependency HTTP client with cached Bearer token reuse (safe against login rate limits)."""

    def __init__(self, base_url: str, bot_user: str, bot_pass: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot_user = bot_user
        self.bot_pass = bot_pass
        # Explicit empty ProxyHandler avoids Clash/system proxies hijacking 127.0.0.1 / LAN / Tailscale
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token: str | None = None

    def _raw_request(self, path: str, body: dict[str, Any] | None = None, *, method: str = "POST", use_auth: bool = True, timeout: float = 45.0) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None if method == "GET" else json.dumps(body if body is not None else {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if use_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, {"ok": False, "error": {"code": f"http_{err.code}", "message": raw[:200]}}

    def call(self, path: str, body: dict[str, Any] | None = None, *, method: str = "POST", timeout: float = 45.0) -> dict[str, Any]:
        status_code, payload = self._raw_request(path, body, method=method, timeout=timeout)
        if status_code == 200 and payload.get("ok") is True:
            res = payload.get("result")
            return res if isinstance(res, dict) else payload
        err = payload.get("error") or {}
        raise ApiError(str(err.get("code") or f"http_{status_code}"), str(err.get("message") or payload), err)

    def _load_cached_token(self) -> str | None:
        if not TOKEN_CACHE_PATH.is_file():
            return None
        try:
            data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        entry = (data.get("tokens") or {}).get(f"{self.base_url}|{self.bot_user.lower()}")
        if isinstance(entry, dict) and entry.get("token"):
            return str(entry["token"])
        return None

    def _save_cached_token(self, token: str, expires_at: str | None) -> None:
        data: dict[str, Any] = {"tokens": {}}
        if TOKEN_CACHE_PATH.is_file():
            try:
                old = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("tokens"), dict):
                    data["tokens"] = old["tokens"]
            except (OSError, json.JSONDecodeError):
                pass
        data["tokens"][f"{self.base_url}|{self.bot_user.lower()}"] = {
            "bot_username": self.bot_user,
            "token": token,
            "expires_at": expires_at,
        }
        TOKEN_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(TOKEN_CACHE_PATH, 0o600)
        except OSError:
            pass

    def ensure_ready(self, *, timeout_s: float = 45.0) -> dict[str, Any]:
        cached = self._load_cached_token()
        if cached:
            self.token = cached
            try:
                st = self.call("status", method="GET")
                if st.get("ready"):
                    return st
            except ApiError as exc:
                if exc.code in ("unauthorized", "invalid_token"):
                    self.token = None
                elif exc.code != "not_connected":
                    raise

        if not self.token:
            if not self.bot_pass or "请填入" in self.bot_pass:
                raise SystemExit("未找到有效缓存 Token，请先在 testkit.env 中填写 MC_BOT_USER 与 MC_BOT_PASS。")
            code, payload = self._raw_request(
                "auth/login",
                {"username": self.bot_user, "password": self.bot_pass},
                method="POST",
                use_auth=False,
                timeout=15.0,
            )
            if code != 200 or not payload.get("ok") or not payload.get("token"):
                err = payload.get("error") or payload
                raise SystemExit(f"登录失败: {err}")
            self.token = str(payload["token"])
            self._save_cached_token(self.token, payload.get("expires_at"))

        deadline = time.monotonic() + timeout_s
        last_connect = 0.0
        while time.monotonic() < deadline:
            try:
                st = self.call("status", method="GET")
                if st.get("ready"):
                    return st
                if time.monotonic() - last_connect > 8.0:
                    self.call("connect", {})
                    last_connect = time.monotonic()
            except ApiError as exc:
                if exc.code == "not_connected":
                    if time.monotonic() - last_connect >= 3.0 or last_connect == 0.0:
                        self.call("connect", {})
                        last_connect = time.monotonic()
                elif exc.code == "not_ready":
                    pass
                else:
                    raise
            time.sleep(0.8)
        raise SystemExit(f"等待 {self.bot_user} 就绪超时 ({timeout_s}s)")


class SimpleRconClient:
    """30-line pure-stdlib Source RCON TCP client (works on Windows/macOS/Linux)."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self.host = host
        self.port = port
        self.password = password

    @staticmethod
    def _pack(rid: int, ptype: int, body: str) -> bytes:
        payload = struct.pack("<ii", rid, ptype) + body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    @staticmethod
    def _recv(sock: socket.socket) -> tuple[int, str]:
        raw_len = b""
        while len(raw_len) < 4:
            chunk = sock.recv(4 - len(raw_len))
            if not chunk:
                raise OSError("RCON connection closed")
            raw_len += chunk
        (length,) = struct.unpack("<i", raw_len)
        buf = b""
        while len(buf) < length:
            chunk = sock.recv(length - len(buf))
            if not chunk:
                raise OSError("RCON connection closed mid-packet")
            buf += chunk
        rid, _ptype = struct.unpack("<ii", buf[:8])
        return rid, buf[8:].rstrip(b"\x00").decode("utf-8", errors="replace")

    def batch(self, commands: list[str], *, timeout: float = 20.0) -> list[dict[str, str]]:
        if not self.password:
            raise RuntimeError("RCON_PASS 未配置；无需 RCON 请改用 --interactive 模式。")
        out: list[dict[str, str]] = []
        with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
            sock.sendall(self._pack(1, 3, self.password))
            rid, _ = self._recv(sock)
            if rid == -1:
                raise RuntimeError("RCON 认证失败（密码错误）。")
            for idx, cmd in enumerate(commands, start=2):
                sock.sendall(self._pack(idx, 2, cmd))
                _, reply = self._recv(sock)
                out.append({"cmd": cmd, "reply": reply.strip()})
        return out

    def one(self, cmd: str) -> str:
        return self.batch([cmd])[0]["reply"]


# ==============================================================================
# 2. 独立微操 & 战斗策略原语（可直接用于交互模式，也可嵌入二阶段脚本）
# ==============================================================================

def aim_angles(
    bot_pos: list[float],
    target_pos: list[float],
    height_offset: float = 0.85,
    *,
    eye_height: float = 1.62,
) -> tuple[float, float]:
    dx = target_pos[0] - bot_pos[0]
    dy = (target_pos[1] + height_offset) - (bot_pos[1] + eye_height)
    dz = target_pos[2] - bot_pos[2]
    yaw = math.atan2(-dx, -dz)
    pitch = math.atan2(dy, math.hypot(dx, dz))
    return yaw, pitch


def safe_stop(bot: SimpleBotClient) -> None:
    for fn in (
        lambda: bot.call("stop", {}),
        lambda: bot.call("control", {"wait": False}),
        lambda: bot.call("posture", {}),
    ):
        try:
            fn()
        except ApiError:
            pass


def nearest_hostile_entity(bot: SimpleBotClient, *, radius: int = 16, name_filter: str | None = None) -> dict[str, Any] | None:
    types = [name_filter] if name_filter else ["hostile"]
    ents = bot.call("get_entities", {"radius": radius, "types": types}).get("entities") or []
    return ents[0] if ents else None


def equip_best_melee(bot: SimpleBotClient, *, prefer: str = "sword") -> str | None:
    inv = bot.call("inventory", method="GET")
    items = [r.get("name") for r in (inv.get("items") or []) if r.get("name")]
    if prefer == "axe":
        order = ("netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "wooden_axe", "golden_axe")
    else:
        order = (
            "netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "wooden_sword", "golden_sword",
            "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "wooden_axe",
        )
    for w in order:
        if w in items:
            held = (inv.get("held") or {}).get("name")
            if held != w:
                bot.call("equip", {"item": w, "slot": "hand"})
            return w
    return None


def action_jump_crit(
    bot: SimpleBotClient,
    *,
    entity_id: int | None = None,
    delay_s: float = DEFAULT_CRIT_DELAY_S,
    moving: bool = False,
) -> dict[str, Any]:
    """双阶段 wait=false 跳劈暴击：起跳不带 sprint，越过最高点下落期挥刀。"""
    if entity_id is None:
        ent = nearest_hostile_entity(bot, radius=8)
        if not ent:
            return {"ok": False, "reason": "8格内未找到敌对生物"}
        entity_id = int(ent["id"])
    bot.call("look", {"entity_id": entity_id})
    jump_payload: dict[str, Any] = {"jump": True, "ms": 550, "wait": False}
    if moving:
        jump_payload["forward"] = True
    bot.call("control", jump_payload)
    time.sleep(delay_s)
    if moving:
        bot.call("look", {"entity_id": entity_id})
    st_pre = bot.call("status", method="GET")
    hit_res = bot.call("control", {"hit": 1, "ms": 60, "wait": False, "stop_on": []})
    return {
        "ok": True,
        "delay_s": delay_s,
        "pre_hit_on_ground": st_pre.get("on_ground"),
        "pre_hit_vy": (st_pre.get("velocity") or [None, None, None])[1],
        "hits": hit_res.get("hits"),
    }


def action_wtap_knockback(bot: SimpleBotClient, *, entity_id: int | None = None) -> dict[str, Any]:
    """W-Tap 疾跑强击退斩：向前冲刺挥出一刀，把贴脸目标推开 2~3+ 格。"""
    if entity_id is None:
        ent = nearest_hostile_entity(bot, radius=6)
        if not ent:
            return {"ok": False, "reason": "6格内未找到敌对生物"}
        entity_id = int(ent["id"])
    bot.call("look", {"entity_id": entity_id})
    res = bot.call("control", {"forward": True, "sprint": True, "hit": 1, "ms": WTAP_HIT_MS, "stop_on": []})
    return {"ok": True, "hits": res.get("hits"), "moved": res.get("moved")}


def action_kite_creeper(bot: SimpleBotClient, *, max_time_s: float = 25.0) -> dict[str, Any]:
    """苦力怕无伤风筝循环（API 无 fuse 字段，依赖距离阈值 + 强击退打断 + sprint+jump 后撤）。"""
    equip_best_melee(bot)
    t0 = time.monotonic()
    strikes = 0
    killed = False
    while time.monotonic() - t0 < max_time_s:
        ent = nearest_hostile_entity(bot, radius=24, name_filter="creeper")
        if not ent:
            killed = True
            break
        dist = float(ent["dist"])
        if dist > CREEPER_ENGAGE_DIST:
            try:
                bot.call("move_to", {"pos": ent["pos"], "range": 2.4, "timeout": 5000, "stop_on": ["hurt"]})
            except ApiError:
                pass
            ent = nearest_hostile_entity(bot, radius=24, name_filter="creeper")
            if not ent:
                killed = True
                break
            dist = float(ent["dist"])
        if dist <= CREEPER_ENGAGE_DIST:
            bot.call("look", {"entity_id": ent["id"]})
            bot.call("control", {"forward": True, "sprint": True, "hit": 1, "ms": WTAP_HIT_MS, "stop_on": []})
            strikes += 1
            st_now = bot.call("status", method="GET")
            away_yaw = math.atan2(-(st_now["pos"][0] - ent["pos"][0]), -(st_now["pos"][2] - ent["pos"][2]))
            bot.call("control", {"forward": True, "sprint": True, "jump": True, "yaw": away_yaw, "pitch": 0.0, "ms": CREEPER_RETREAT_MS, "wait": False})
            time.sleep((CREEPER_RETREAT_MS / 1000.0) + 0.1)
            bot.call("control", {"wait": False})
        else:
            time.sleep(0.1)
    safe_stop(bot)
    return {"killed": killed, "strikes": strikes, "elapsed_s": round(time.monotonic() - t0, 2)}


def action_skeleton_duel(bot: SimpleBotClient, *, strategy: str, max_time_s: float = 22.0) -> dict[str, Any]:
    """对比 5 种打小白策略：naive / shield / zigzag / pillar / bow。"""
    equip_best_melee(bot)
    t0 = time.monotonic()
    killed = False

    if strategy == "naive":
        ent = nearest_hostile_entity(bot, radius=24, name_filter="skeleton")
        if not ent:
            return {"ok": False, "reason": "24格内未找到skeleton"}
        res = bot.call("attack", {"entity_id": ent["id"], "timeout": int(max_time_s * 1000)})
        return {"strategy": strategy, "killed": res.get("killed"), "hits": res.get("hits"), "elapsed_s": round(time.monotonic() - t0, 2)}

    if strategy == "shield":
        bot.call("posture", {"shield": True})
        try:
            while time.monotonic() - t0 < max_time_s:
                ent = nearest_hostile_entity(bot, radius=24, name_filter="skeleton")
                if not ent:
                    killed = True
                    break
                if float(ent["dist"]) > 3.1:
                    bot.call("look", {"entity_id": ent["id"]})
                    bot.call("control", {"forward": True, "ms": 380, "wait": False})
                    time.sleep(0.28)
                else:
                    bot.call("control", {"wait": False})
                    bot.call("posture", {})
                    try:
                        bot.call("attack", {"entity_id": ent["id"], "max_hits": 2, "timeout": 3500, "stop_on": []})
                    except ApiError:
                        pass
                    if not nearest_hostile_entity(bot, radius=24, name_filter="skeleton"):
                        killed = True
                        break
                    bot.call("posture", {"shield": True})
        finally:
            safe_stop(bot)
        return {"strategy": strategy, "killed": killed, "elapsed_s": round(time.monotonic() - t0, 2)}

    if strategy == "zigzag":
        sign = 1
        try:
            while time.monotonic() - t0 < max_time_s:
                ent = nearest_hostile_entity(bot, radius=24, name_filter="skeleton")
                if not ent:
                    killed = True
                    break
                if float(ent["dist"]) > 3.2:
                    st = bot.call("status", method="GET")
                    yaw_to, _ = aim_angles(st["pos"], ent["pos"])
                    bot.call("control", {"forward": True, "sprint": True, "yaw": yaw_to + sign * ZIGZAG_ANGLE_RAD, "pitch": 0.0, "ms": ZIGZAG_STEP_MS, "wait": False})
                    sign *= -1
                    time.sleep(ZIGZAG_SLEEP_S)
                else:
                    bot.call("control", {"wait": False})
                    try:
                        res = bot.call("attack", {"entity_id": ent["id"], "max_hits": 3, "timeout": 5000, "stop_on": []})
                        if res.get("killed"):
                            killed = True
                            break
                    except ApiError:
                        pass
        finally:
            safe_stop(bot)
        return {"strategy": strategy, "killed": killed, "elapsed_s": round(time.monotonic() - t0, 2)}

    if strategy == "pillar":
        # Wait behind cover until skeleton comes within 3.6m, then execute melee
        try:
            while time.monotonic() - t0 < max_time_s:
                ent = nearest_hostile_entity(bot, radius=24, name_filter="skeleton")
                if not ent:
                    killed = True
                    break
                if float(ent["dist"]) <= 3.6:
                    try:
                        res = bot.call("attack", {"entity_id": ent["id"], "timeout": 8000, "stop_on": []})
                        if res.get("killed"):
                            killed = True
                            break
                    except ApiError:
                        pass
                time.sleep(0.35)
        finally:
            safe_stop(bot)
        return {"strategy": strategy, "killed": killed, "elapsed_s": round(time.monotonic() - t0, 2)}

    if strategy == "bow":
        shots = 0
        hits_landed = 0
        try:
            while time.monotonic() - t0 < max_time_s and shots < 8:
                ent = nearest_hostile_entity(bot, radius=28, name_filter="skeleton")
                if not ent:
                    killed = True
                    break
                shots += 1
                sres = bot.call("shoot", {"entity_id": ent["id"], "weapon": "bow", "charge_ms": 1000, "stop_on": []})
                hb = sres.get("target_health_before")
                ha = sres.get("target_health_after")
                if hb is not None and (ha is None or ha < hb):
                    hits_landed += 1
                if ha is None:
                    time.sleep(0.25)
                    if not nearest_hostile_entity(bot, radius=28, name_filter="skeleton"):
                        killed = True
                        break
        finally:
            safe_stop(bot)
        return {"strategy": strategy, "killed": killed, "shots": shots, "hits_landed": hits_landed, "elapsed_s": round(time.monotonic() - t0, 2)}

    raise ValueError(f"Unknown skeleton strategy: {strategy}")


def action_pillar_up_and_clear(bot: SimpleBotClient, *, height: int = 3, block_item: str = "cobblestone") -> dict[str, Any]:
    """原地起跳后在 180~280ms 黄金窗口脚下放方块垫高，再低头挥砍脚下怪物。"""
    st0 = bot.call("status", method="GET")
    bx, by, bz = math.floor(st0["pos"][0]), math.floor(st0["pos"][1]), math.floor(st0["pos"][2])
    pillar_ok = True
    for step in range(height):
        bot.call("control", {"jump": True, "ms": 480, "wait": False})
        time.sleep(DEFAULT_PILLAR_WAIT_S)
        try:
            bot.call("place", {"item": block_item, "pos": [bx, by + step, bz], "stop_on": []})
        except ApiError:
            pillar_ok = False
        bot.call("control", {"wait": False})
        time.sleep(0.35)
    st_top = bot.call("status", method="GET")
    equip_best_melee(bot)
    t0 = time.monotonic()
    swings = 0
    hits_connected = 0
    killed_all = False
    while time.monotonic() - t0 < 20.0:
        ent = nearest_hostile_entity(bot, radius=8)
        if not ent:
            killed_all = True
            break
        if float(ent["dist"]) <= 4.3:
            st = bot.call("status", method="GET")
            yaw, pitch = aim_angles(st["pos"], ent["pos"], height_offset=1.55)
            hres = bot.call("control", {"hit": 1, "yaw": yaw, "pitch": pitch, "ms": 70, "stop_on": []})
            swings += 1
            if (hres.get("hits") or [{}])[0].get("entity"):
                hits_connected += 1
            time.sleep(GAP_STAND_WAIT_S)
        else:
            time.sleep(0.2)
    safe_stop(bot)
    return {"height": height, "pillar_ok": pillar_ok, "top_pos": st_top.get("pos"), "killed_all": killed_all, "swings": swings, "hits_connected": hits_connected}


def action_gap_slash(bot: SimpleBotClient, *, mode: str = "foot", max_time_s: float = 15.0) -> dict[str, Any]:
    """1 格缝隙隔墙修脚(foot: 开潜行降眼高 1.27) / 修头(head: 站姿眼高 1.62)。"""
    equip_best_melee(bot)
    if mode == "foot":
        bot.call("posture", {"sneak": True})
        aim_h, eye_h = 0.35, 1.27
    else:
        bot.call("posture", {})
        aim_h, eye_h = 1.45, 1.62
    t0 = time.monotonic()
    swings = 0
    hits_connected = 0
    killed_all = False
    try:
        while time.monotonic() - t0 < max_time_s:
            ent = nearest_hostile_entity(bot, radius=8)
            if not ent:
                killed_all = True
                break
            if float(ent["dist"]) <= 3.5:
                st = bot.call("status", method="GET")
                yaw, pitch = aim_angles(st["pos"], ent["pos"], height_offset=aim_h, eye_height=eye_h)
                hres = bot.call("control", {"hit": 1, "yaw": yaw, "pitch": pitch, "ms": 80, "stop_on": []})
                swings += 1
                if (hres.get("hits") or [{}])[0].get("entity"):
                    hits_connected += 1
                time.sleep(GAP_STAND_WAIT_S)
            else:
                time.sleep(0.2)
    finally:
        safe_stop(bot)
    return {"mode": mode, "killed_all": killed_all, "swings": swings, "hits_connected": hits_connected}


# ==============================================================================
# 3. 高空隔离靶场 RCON 自动化实验（仅当填写了 RCON_PASS 时可跑）
# ==============================================================================

def _read_target_health(rcon: SimpleRconClient, tag: str = "probe_target") -> float | None:
    rep = rcon.one(f"data get entity @e[tag={tag},limit=1,sort=nearest] Health")
    m = re.search(r"entity data:\s*([0-9.]+)f?", rep)
    return float(m.group(1)) if m else None


def _read_target_pos(rcon: SimpleRconClient, tag: str = "probe_target") -> list[float] | None:
    rep = rcon.one(f"data get entity @e[tag={tag},limit=1,sort=nearest] Pos")
    m = re.search(r"\[\s*(-?[0-9.]+)d?\s*,\s*(-?[0-9.]+)d?\s*,\s*(-?[0-9.]+)d?\s*\]", rep)
    return [float(m.group(1)), float(m.group(2)), float(m.group(3))] if m else None


def _latest_seq(bot: SimpleBotClient) -> int:
    res = bot.call("events", {"since": 0, "limit": 500})
    cur = int(res.get("last_seq") or 0)
    while res.get("has_more"):
        res = bot.call("events", {"since": cur, "limit": 500})
        cur = int(res.get("last_seq") or cur)
    return cur


def _count_hurt_events(bot: SimpleBotClient, since_seq: int) -> tuple[int, int, float]:
    hurts = 0
    dmg = 0.0
    cur = since_seq
    while True:
        res = bot.call("events", {"since": cur, "limit": 200})
        cur = int(res.get("last_seq") or cur)
        for ev in res.get("events") or []:
            if ev.get("type") == "hurt":
                hurts += 1
                dmg += float(ev.get("damage") or 0.0)
            elif ev.get("type") == "death":
                raise RuntimeError("Unexpected death during probe!")
        if not res.get("has_more"):
            break
    return cur, hurts, round(dmg, 2)


def _reset_vital(rcon: SimpleRconClient, bot_user: str, x: float = CENTER_X, y: float = CENTER_Y, z: float = CENTER_Z) -> None:
    rcon.batch([
        f"kill @e[{ARENA_SEL},type=!player]",
        f"effect clear {bot_user}",
        f"effect give {bot_user} minecraft:instant_health 1 10 true",
        f"effect give {bot_user} minecraft:saturation 1 10 true",
        f"tp {bot_user} {x} {y} {z} 0 0",
    ])


def _wait_settled(bot: SimpleBotClient, tx: float, ty: float, tz: float, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            st = bot.call("status", method="GET")
            p = st.get("pos") or [0, 0, 0]
            if st.get("ready") and st.get("on_ground") and abs(p[0] - tx) < 2.5 and abs(p[1] - ty) < 1.5 and abs(p[2] - tz) < 2.5:
                return st
        except ApiError as exc:
            if exc.code not in ("not_ready", "not_connected"):
                raise
        time.sleep(0.25)
    raise RuntimeError(f"Bot did not settle near ({tx}, {ty}, {tz})")


def _setup_arena(rcon: SimpleRconClient) -> None:
    rcon.batch([
        f"forceload add {ARENA_MIN_X} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Z}",
        f"fill {ARENA_MIN_X} {ARENA_MIN_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_FLOOR_Y - 1} {ARENA_MAX_Z} polished_andesite",
        f"fill {ARENA_MIN_X} {ARENA_FLOOR_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Y} {ARENA_MAX_Z} polished_andesite hollow",
        f"fill {ARENA_MIN_X + 4} {ARENA_MAX_Y} {ARENA_MIN_Z + 4} {ARENA_MAX_X - 4} {ARENA_MAX_Y} {ARENA_MAX_Z - 4} sea_lantern",
        f"kill @e[{ARENA_SEL},type=!player]",
    ])


def _teardown_arena(rcon: SimpleRconClient, bot_user: str, orig_pos: list[float], orig_items: list[dict[str, Any]]) -> None:
    cmds = [
        f"kill @e[{ARENA_SEL},type=!player]",
        f"clear {bot_user}",
        f"effect clear {bot_user}",
        f"effect give {bot_user} minecraft:instant_health 1 10 true",
        f"effect give {bot_user} minecraft:saturation 1 10 true",
    ]
    agg: dict[str, int] = {}
    for row in orig_items:
        nm = row.get("name")
        cnt = int(row.get("count") or 0)
        if nm and cnt > 0:
            agg[nm] = agg.get(nm, 0) + cnt
    for nm, cnt in sorted(agg.items()):
        rem = cnt
        while rem > 0:
            n = min(rem, 6400)
            cmds.append(f"give {bot_user} minecraft:{nm} {n}")
            rem -= n
    cmds.append(f"tp {bot_user} {orig_pos[0]} {orig_pos[1]} {orig_pos[2]} 0 0")
    cmds.append(f"fill {ARENA_MIN_X} {ARENA_MIN_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Y} {ARENA_MAX_Z} air")
    cmds.append(f"forceload remove {ARENA_MIN_X} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_MAX_Z}")
    for i in range(0, len(cmds), 12):
        rcon.batch(cmds[i : i + 12], timeout=35.0)


def exp1_realtime_baseline(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    rtts = []
    st = {}
    for _ in range(10):
        t0 = time.perf_counter()
        st = bot.call("status", method="GET")
        rtts.append((time.perf_counter() - t0) * 1000.0)
    rcon.batch([f"clear {user}", f"give {user} minecraft:shield 1"])
    time.sleep(0.3)
    t_sh0 = time.perf_counter()
    p_on = bot.call("posture", {"shield": True})
    shield_on_ms = (time.perf_counter() - t_sh0) * 1000.0
    p_off = bot.call("posture", {})

    _reset_vital(rcon, user)
    s0 = _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
    t_c0 = time.perf_counter()
    c1 = bot.call("control", {"forward": True, "sprint": True, "yaw": 0.0, "pitch": 0.0, "ms": 900, "wait": False})
    ctrl_rtt = (time.perf_counter() - t_c0) * 1000.0
    time.sleep(0.30)
    s_mid = bot.call("status", method="GET")
    moved = math.hypot(s_mid["pos"][0] - s0["pos"][0], s_mid["pos"][2] - s0["pos"][2])
    bot.call("control", {"forward": True, "left": True, "ms": 800, "wait": False})
    time.sleep(0.18)
    s_mid2 = bot.call("status", method="GET")
    c3 = bot.call("control", {"wait": False})
    time.sleep(0.15)
    s_after = bot.call("status", method="GET")
    return {
        "status_rtt_ms": {"min": round(min(rtts), 2), "median": round(sorted(rtts)[len(rtts) // 2], 2), "max": round(max(rtts), 2)},
        "posture_shield_on_ms": round(shield_on_ms, 2),
        "blocking": p_on.get("blocking"),
        "blocking_after_reset": p_off.get("blocking"),
        "control_wait_false_rtt_ms": round(ctrl_rtt, 2),
        "moved_in_300ms": round(moved, 2),
        "speed_during_sprint": s_mid.get("speed"),
        "held_after_start": c1.get("held"),
        "held_after_takeover": s_mid2.get("held_controls"),
        "released_keys": c3.get("released"),
        "held_after_release": s_after.get("held_controls"),
        "has_held_controls_field": "held_controls" in st,
    }


def _crit_probe_once(
    bot: SimpleBotClient,
    rcon: SimpleRconClient,
    user: str,
    weapon: str,
    delay_s: float | None,
    *,
    single_call: bool = False,
    sprint: bool = False,
    moving_dist: float | None = None,
) -> dict[str, Any]:
    _reset_vital(rcon, user)
    rcon.batch([f"clear {user}", f"give {user} minecraft:{weapon} 1"])
    time.sleep(0.25)
    bot.call("equip", {"item": weapon, "slot": "hand"})
    if moving_dist is not None:
        rcon.one(f"tp {user} {CENTER_X} {CENTER_Y} {CENTER_Z + moving_dist} 0 0")
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z + moving_dist)
        tz = CENTER_Z
    else:
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        tz = CENTER_Z - 2.3
    rcon.one(f'summon minecraft:iron_golem {CENTER_X} {CENTER_Y} {tz} {{NoAI:1b,PersistenceRequired:1b,Tags:["probe_target"]}}')
    time.sleep(0.4)
    ents = bot.call("get_entities", {"radius": 8, "types": ["iron_golem"]}).get("entities") or []
    gid = ents[0]["id"]
    hp0 = _read_target_health(rcon)
    time.sleep(1.3)
    if delay_s is None:
        bot.call("look", {"entity_id": gid})
        bot.call("control", {"hit": 1, "ms": 80, "stop_on": []})
    elif single_call:
        bot.call("look", {"entity_id": gid})
        bot.call("control", {"jump": True, "hit": 1, "ms": 500, "stop_on": []})
    elif sprint:
        bot.call("look", {"entity_id": gid})
        bot.call("control", {"jump": True, "sprint": True, "forward": True, "ms": 550, "wait": False})
        time.sleep(delay_s)
        bot.call("control", {"hit": 1, "ms": 60, "wait": False, "stop_on": []})
    else:
        action_jump_crit(bot, entity_id=gid, delay_s=delay_s, moving=(moving_dist is not None))
    time.sleep(0.35)
    hp1 = _read_target_health(rcon)
    safe_stop(bot)
    return {
        "weapon": weapon,
        "delay_s": delay_s,
        "single_call": single_call,
        "sprint": sprint,
        "moving_dist": moving_dist,
        "hp_before": hp0,
        "hp_after": hp1,
        "damage": round((hp0 or 0.0) - (hp1 or 0.0), 2),
    }


def exp2_jump_crit(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    base_sword = _crit_probe_once(bot, rcon, user, "iron_sword", None)
    base_axe = _crit_probe_once(bot, rcon, user, "stone_axe", None)
    naive = _crit_probe_once(bot, rcon, user, "iron_sword", 0.0, single_call=True)
    sweep = [_crit_probe_once(bot, rcon, user, "iron_sword", d) for d in (0.28, 0.34, 0.38, 0.42, 0.46)]
    best = next((r["delay_s"] for r in sweep if r["damage"] >= 8.5), DEFAULT_CRIT_DELAY_S)
    axe_crit = _crit_probe_once(bot, rcon, user, "stone_axe", best)
    sprint_ctrl = _crit_probe_once(bot, rcon, user, "iron_sword", best, sprint=True)
    moving = [_crit_probe_once(bot, rcon, user, "iron_sword", best, moving_dist=2.9) for _ in range(5)]
    return {
        "baseline_sword": base_sword,
        "baseline_axe": base_axe,
        "naive_single_call": naive,
        "delay_sweep": sweep,
        "chosen_delay_s": best,
        "axe_crit": axe_crit,
        "sprint_cancels_crit": sprint_ctrl,
        "moving_crit_rate": f"{sum(1 for t in moving if t['damage'] >= 8.5)}/5",
        "moving_trials": moving,
    }


def exp3_knockback_and_creeper(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    kb = {}
    for mode in ("normal", "sprint"):
        _reset_vital(rcon, user)
        rcon.batch([f"clear {user}", f"give {user} minecraft:iron_sword 1"])
        time.sleep(0.25)
        bot.call("equip", {"item": "iron_sword", "slot": "hand"})
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        rcon.one(
            f'summon minecraft:zombie {CENTER_X} {CENTER_Y} {CENTER_Z - 2.3} '
            '{Silent:1b,PersistenceRequired:1b,Tags:["probe_target"],'
            'Attributes:[{Name:"minecraft:movement_speed",Base:0.0d},{Name:"minecraft:follow_range",Base:0.0d}]}'
        )
        time.sleep(0.4)
        ents = bot.call("get_entities", {"radius": 8, "types": ["zombie"]}).get("entities") or []
        p0 = _read_target_pos(rcon)
        time.sleep(0.8)
        if mode == "sprint":
            action_wtap_knockback(bot, entity_id=ents[0]["id"])
        else:
            bot.call("look", {"entity_id": ents[0]["id"]})
            bot.call("control", {"hit": 1, "ms": 80, "stop_on": []})
        time.sleep(0.6)
        p1 = _read_target_pos(rcon)
        kb[mode] = round(math.hypot(p1[0] - p0[0], p1[2] - p0[2]), 2) if (p0 and p1) else None

    _reset_vital(rcon, user)
    rcon.batch([f"clear {user}", f"give {user} minecraft:iron_sword 1", f"give {user} minecraft:shield 1"])
    time.sleep(0.25)
    _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
    rcon.one(f'summon minecraft:creeper {CENTER_X} {CENTER_Y} {CENTER_Z - 9.0} {{PersistenceRequired:1b,Tags:["probe_target"]}}')
    time.sleep(0.4)
    seq = _latest_seq(bot)
    creeper_res = action_kite_creeper(bot)
    _, hurts, dmg = _count_hurt_events(bot, seq)
    crater_rep = rcon.one(f"fill {ARENA_MIN_X} {ARENA_FLOOR_Y} {ARENA_MIN_Z} {ARENA_MAX_X} {ARENA_FLOOR_Y} {ARENA_MAX_Z} polished_andesite replace air")
    m_fill = re.search(r"Successfully filled (\d+) block", crater_rep)
    exploded = bool(m_fill and int(m_fill.group(1)) > 0)
    creeper_res.update({"exploded": exploded, "hurt_events": hurts, "damage_taken": dmg})
    return {"knockback_blocks": kb, "creeper_kite": creeper_res}


def exp4_skeleton_strategies(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    out = {}
    for strategy in ("naive", "shield", "zigzag", "pillar", "bow"):
        _reset_vital(rcon, user, CENTER_X, CENTER_Y, CENTER_Z + 6.0)
        items = [f"clear {user}", f"give {user} minecraft:iron_sword 1"]
        if strategy == "shield":
            items.append(f"give {user} minecraft:shield 1")
        if strategy == "bow":
            items += [f"give {user} minecraft:bow 1", f"give {user} minecraft:arrow 24"]
        if strategy == "pillar":
            items += [
                f"setblock {int(CENTER_X)} {int(CENTER_Y)} {int(CENTER_Z + 4)} cobblestone",
                f"setblock {int(CENTER_X)} {int(CENTER_Y + 1)} {int(CENTER_Z + 4)} cobblestone",
            ]
        rcon.batch(items)
        time.sleep(0.3)
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z + 6.0)
        rcon.one(f'summon minecraft:skeleton {CENTER_X} {CENTER_Y} {CENTER_Z - 6.0} {{PersistenceRequired:1b,Tags:["probe_target"]}}')
        time.sleep(0.4)
        seq = _latest_seq(bot)
        try:
            res = action_skeleton_duel(bot, strategy=strategy)
        except ApiError as exc:
            res = {"strategy": strategy, "error": exc.code}
        if strategy == "pillar":
            rcon.batch([
                f"setblock {int(CENTER_X)} {int(CENTER_Y)} {int(CENTER_Z + 4)} air",
                f"setblock {int(CENTER_X)} {int(CENTER_Y + 1)} {int(CENTER_Z + 4)} air",
            ])
        _, hurts, dmg = _count_hurt_events(bot, seq)
        res.update({"hurt_events": hurts, "damage_taken": dmg})
        out[strategy] = res
    return out


def exp5_tunnel_gap_slash(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    bx = int(CENTER_X)
    walls = [
        f"fill {bx - 1} 120 804 {bx - 1} 122 816 cobblestone",
        f"fill {bx + 1} 120 804 {bx + 1} 122 816 cobblestone",
        f"fill {bx} 122 804 {bx} 122 816 cobblestone",
        f"fill {bx - 1} 120 816 {bx + 1} 122 816 cobblestone",
        f"fill {bx - 1} 120 804 {bx + 1} 122 804 cobblestone",
    ]
    out = {}
    for mode in ("foot", "head"):
        _reset_vital(rcon, user, bx + 0.5, 120.0, 813.0)
        cmds = list(walls) + [f"clear {user}", f"give {user} minecraft:iron_sword 1"]
        if mode == "foot":
            cmds += [f"setblock {bx} 120 811 air", f"setblock {bx} 121 811 cobblestone"]
            stand_z = 812.6
        else:
            cmds += [f"setblock {bx} 120 811 cobblestone", f"setblock {bx} 121 811 air"]
            stand_z = 812.2
        rcon.batch(cmds)
        time.sleep(0.3)
        _wait_settled(bot, bx + 0.5, 120.0, 813.0)
        rcon.batch([
            f'summon minecraft:zombie {bx + 0.5} 120 808.0 {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:skeleton {bx + 0.5} 120 806.5 {{PersistenceRequired:1b,Tags:["probe_target"]}}',
        ])
        time.sleep(0.4)
        seq = _latest_seq(bot)
        try:
            bot.call("move_to", {"pos": [bx + 0.5, 120.0, stand_z], "range": 0.4, "timeout": 4000, "stop_on": []})
        except ApiError:
            pass
        res = action_gap_slash(bot, mode=mode)
        _, hurts, dmg = _count_hurt_events(bot, seq)
        res.update({"hurt_events": hurts, "damage_taken": dmg})
        out[mode] = res
    rcon.batch([f"fill {bx - 1} 120 804 {bx + 1} 122 816 air"])
    return out


def exp6_pillar_cheese(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    out = {}
    bx, bz = int(CENTER_X), int(CENTER_Z)
    for height in (2, 3):
        _reset_vital(rcon, user)
        rcon.batch([f"clear {user}", f"give {user} minecraft:iron_sword 1", f"give {user} minecraft:cobblestone 16"])
        time.sleep(0.3)
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)
        seq = _latest_seq(bot)
        rcon.batch([
            f'summon minecraft:zombie {CENTER_X + 4.5} {CENTER_Y} {CENTER_Z} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:zombie {CENTER_X - 4.5} {CENTER_Y} {CENTER_Z} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
            f'summon minecraft:zombie {CENTER_X} {CENTER_Y} {CENTER_Z - 4.5} {{PersistenceRequired:1b,Tags:["probe_target"]}}',
        ])
        res = action_pillar_up_and_clear(bot, height=height)
        _, hurts, dmg = _count_hurt_events(bot, seq)
        rcon.batch([f"fill {bx} {int(CENTER_Y)} {bz} {bx} {int(CENTER_Y) + 4} {bz} air"])
        res.update({"hurt_events": hurts, "damage_taken": dmg})
        out[f"pillar_{height}"] = res
    return out


def exp7_water_and_stop(bot: SimpleBotClient, rcon: SimpleRconClient, user: str) -> dict[str, Any]:
    bx, bz = int(CENTER_X) + 8, int(CENTER_Z) + 8
    _reset_vital(rcon, user)
    rcon.batch([
        f"fill {bx - 2} 120 {bz - 2} {bx + 2} 122 {bz + 2} polished_andesite hollow",
        f"fill {bx - 1} 120 {bz - 1} {bx + 1} 121 {bz + 1} water",
        f"fill {bx - 1} 122 {bz - 1} {bx + 1} 122 {bz + 1} air",
        f"tp {user} {bx + 0.5} 120.0 {bz + 0.5} 0 0",
    ])
    time.sleep(0.8)
    s_sink = bot.call("status", method="GET")
    t0 = time.perf_counter()
    p_res = bot.call("posture", {"jump": True})
    p_ms = (time.perf_counter() - t0) * 1000.0
    time.sleep(1.2)
    s_float = bot.call("status", method="GET")
    bot.call("posture", {})
    rcon.batch([f"fill {bx - 2} 120 {bz - 2} {bx + 2} 122 {bz + 2} air"])
    _reset_vital(rcon, user)
    _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z)

    move_err: dict[str, Any] = {}

    def bg_move() -> None:
        try:
            bot.call("move_to", {"pos": [CENTER_X - 15.0, CENTER_Y, CENTER_Z - 15.0], "range": 0.5})
        except ApiError as exc:
            move_err["code"] = exc.code

    th = threading.Thread(target=bg_move, daemon=True)
    th.start()
    time.sleep(0.35)
    stop_res = bot.call("stop", {})
    ctrl_ok = bot.call("control", {"hit": 1, "ms": 60, "wait": False})
    th.join(timeout=5.0)
    safe_stop(bot)
    return {
        "water_oxygen_before": s_sink.get("oxygen"),
        "water_submerged_before": s_sink.get("submerged"),
        "posture_jump_ms": round(p_ms, 2),
        "posture_jumping": p_res.get("jumping"),
        "water_submerged_after": s_float.get("submerged"),
        "water_float_y": s_float["pos"][1],
        "stop_result": stop_res,
        "immediate_control_after_stop_ok": ("held" in ctrl_ok),
    }


EXPERIMENTS = (
    ("exp1_realtime_baseline", "v1.3.14 实时操控 RTT / posture 55ms / wait=false 急停", exp1_realtime_baseline),
    ("exp2_jump_crit", "双阶段 wait=false 跳劈暴击 1.5x 与延时扫描 / 疾跑互斥对照", exp2_jump_crit),
    ("exp3_knockback_and_creeper", "W-Tap 疾跑击退格数对比 + 活体苦力怕无伤风筝", exp3_knockback_and_creeper),
    ("exp4_skeleton_strategies", "小白 5 种策略对比 (naive/shield/zigzag/pillar/bow)", exp4_skeleton_strategies),
    ("exp5_tunnel_gap_slash", "1x2 矿道半身封口修脚(sneak) vs 修头对比", exp5_tunnel_gap_slash),
    ("exp6_pillar_cheese", "原地起跳垫高 2 格 vs 3 格清群怪", exp6_pillar_cheese),
    ("exp7_water_and_stop", "1.21 oxygen 阈值实测 + posture(jump)浮面 + /stop 锁释放", exp7_water_and_stop),
)


# ==============================================================================
# 4. 联机实时交互 REPL（--interactive：自己开客户端进服边看边玩）
# ==============================================================================

INTERACTIVE_HELP = """
可用交互指令（自己开 MC 客户端进测试服站在 Bot 旁即可实时测，无需 RCON）：
  status                         查看坐标/血量/速度/姿态/held_controls
  entities [radius]              列出周围生物/掉落物及距离 (默认 radius=16)
  inv                            查看当前背包/手持/副手
  crit [delay_ms]                双阶段跳劈暴击最近怪 (默认 380ms，例: crit 340)
  mcrit [delay_ms]               移动跃进跳劈最近怪 (默认 380ms)
  wtap                           W-Tap 疾跑强击退斩最近怪
  creeper                        对最近苦力怕执行一刀击退风筝循环
  skel <naive|shield|zigzag|pillar|bow>   对最近骷髅执行指定战术
  pillar [2|3]                   原地跳垫高 2 或 3 格并低头挥砍脚下怪 (默认 3)
  gap <foot|head>                隔 1 格半身缝隙砍最近怪 (foot=潜行修脚 / head=修头)
  equip <item> [hand|off-hand]   装备物品 (例: equip iron_sword hand / equip shield off-hand)
  posture <sneak|shield|jump|off>         切换持续姿态
  stop                           紧急刹车并复位姿态
  raw <path> [json_body]         直接调任意接口 (例: raw control {"jump":true,"ms":400,"wait":false})
  help / quit
""".strip()


def run_interactive(bot: SimpleBotClient) -> None:
    print(f"\n[{bot.bot_user}] 已就绪！进入微操联机调试模式。")
    print(INTERACTIVE_HELP)
    while True:
        try:
            line = input(f"\n{bot.bot_user}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出并急停复位...")
            safe_stop(bot)
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("quit", "exit", "q"):
                safe_stop(bot)
                break
            elif cmd == "help":
                print(INTERACTIVE_HELP)
            elif cmd == "status":
                print(json.dumps(bot.call("status", method="GET"), ensure_ascii=False, indent=2))
            elif cmd == "inv":
                print(json.dumps(bot.call("inventory", method="GET"), ensure_ascii=False, indent=2))
            elif cmd == "entities":
                rad = int(parts[1]) if len(parts) > 1 else 16
                print(json.dumps(bot.call("get_entities", {"radius": rad}), ensure_ascii=False, indent=2))
            elif cmd == "crit":
                d_s = (int(parts[1]) / 1000.0) if len(parts) > 1 else DEFAULT_CRIT_DELAY_S
                print(json.dumps(action_jump_crit(bot, delay_s=d_s, moving=False), ensure_ascii=False, indent=2))
            elif cmd == "mcrit":
                d_s = (int(parts[1]) / 1000.0) if len(parts) > 1 else DEFAULT_CRIT_DELAY_S
                print(json.dumps(action_jump_crit(bot, delay_s=d_s, moving=True), ensure_ascii=False, indent=2))
            elif cmd == "wtap":
                print(json.dumps(action_wtap_knockback(bot), ensure_ascii=False, indent=2))
            elif cmd == "creeper":
                print(json.dumps(action_kite_creeper(bot), ensure_ascii=False, indent=2))
            elif cmd == "skel":
                strat = parts[1] if len(parts) > 1 else "shield"
                print(json.dumps(action_skeleton_duel(bot, strategy=strat), ensure_ascii=False, indent=2))
            elif cmd == "pillar":
                h = int(parts[1]) if len(parts) > 1 else 3
                print(json.dumps(action_pillar_up_and_clear(bot, height=h), ensure_ascii=False, indent=2))
            elif cmd == "gap":
                mode = parts[1] if len(parts) > 1 else "foot"
                print(json.dumps(action_gap_slash(bot, mode=mode), ensure_ascii=False, indent=2))
            elif cmd == "equip":
                if len(parts) < 2:
                    print("用法: equip <item> [hand|off-hand]")
                else:
                    slot = parts[2] if len(parts) > 2 else "hand"
                    print(json.dumps(bot.call("equip", {"item": parts[1], "slot": slot}), ensure_ascii=False, indent=2))
            elif cmd == "posture":
                mode = parts[1] if len(parts) > 1 else "off"
                payload = {} if mode == "off" else {mode: True}
                print(json.dumps(bot.call("posture", payload), ensure_ascii=False, indent=2))
            elif cmd == "stop":
                safe_stop(bot)
                print("已停止并复位姿态。")
            elif cmd == "raw":
                if len(parts) < 2:
                    print("用法: raw <path> [json_body]")
                else:
                    path = parts[1]
                    body_raw = line.split(None, 2)[2] if len(parts) > 2 else ""
                    body = json.loads(body_raw) if body_raw else None
                    m = "GET" if (body is None and path in ("status", "inventory", "events")) else "POST"
                    print(json.dumps(bot.call(path, body, method=m), ensure_ascii=False, indent=2))
            else:
                print(f"未知指令: {cmd} (输入 help 查看全部指令)")
        except Exception as exc:
            print(f"[ERROR] {exc}")


# ==============================================================================
# 5. CLI 入口
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone zero-dependency combat & micro-action test kit for mc-agent-api v1.3.14.")
    parser.add_argument("cases", nargs="*", help="RCON automated case name(s) to run (e.g., exp2_jump_crit)")
    parser.add_argument("--env", default=str(HERE / "testkit.env"), help="Path to testkit.env config file")
    parser.add_argument("--interactive", "-i", action="store_true", help="Run live interactive REPL (no RCON needed)")
    parser.add_argument("--all", action="store_true", help="Run all automated RCON experiments sequentially")
    parser.add_argument("--list", action="store_true", help="List available experiments and commands then exit")
    args = parser.parse_args()

    by_name = {name: fn for name, _, fn in EXPERIMENTS}
    if args.list or (not args.interactive and not args.all and not args.cases):
        print("=== 1) 无需 RCON 的联机交互实操模式 ===")
        print("  python3 run_combat_tests.py --interactive\n")
        print(INTERACTIVE_HELP)
        print("\n=== 2) 需配置 RCON_PASS 的全自动高空靶场测伤用例 ===")
        for name, desc, _ in EXPERIMENTS:
            print(f"  - {name}: {desc}")
        print("\n示例: python3 run_combat_tests.py exp2_jump_crit")
        return

    cfg = load_env_file(pathlib.Path(args.env))
    api_url = cfg.get("MC_API_URL", "http://127.0.0.1:18900/api/v1")
    bot_user = cfg.get("MC_BOT_USER", "FightBot_01")
    bot_pass = cfg.get("MC_BOT_PASS", "")
    if bot_user.lower() == "beastbot_01":
        raise SystemExit("安全拦截：请勿在队友工具包里使用 BeastBot_01，以免把直播/控制台 Bot 踢下线！请改用 FightBot_01 等独立名字。")

    bot = SimpleBotClient(api_url, bot_user, bot_pass)
    st0 = bot.ensure_ready()

    if args.interactive:
        run_interactive(bot)
        return

    rcon_pass = cfg.get("RCON_PASS", "")
    if not rcon_pass:
        raise SystemExit("自动化靶场测伤 (exp1~exp7) 需要配置 RCON_PASS；如果你只是进服联机实测，请加 --interactive 参数！")
    rcon = SimpleRconClient(cfg.get("RCON_HOST", "127.0.0.1"), int(cfg.get("RCON_PORT", "25575")), rcon_pass)

    selected = [(n, fn) for n, _, fn in EXPERIMENTS] if args.all else []
    if not args.all:
        unknown = [c for c in args.cases if c not in by_name]
        if unknown:
            raise SystemExit(f"未知用例 {unknown}，请先运行 --list 查看可用名称。")
        selected = [(c, by_name[c]) for c in args.cases]

    orig_pos = list(st0["pos"])
    inv0 = bot.call("inventory", method="GET")
    orig_items = list(inv0.get("items") or [])
    if isinstance(inv0.get("offhand"), dict) and inv0["offhand"].get("name"):
        orig_items.append({"name": inv0["offhand"]["name"], "count": int(inv0["offhand"].get("count") or 1)})
    for aitem in (inv0.get("armor") or {}).values():
        if isinstance(aitem, dict) and aitem.get("name"):
            orig_items.append({"name": aitem["name"], "count": 1})

    report: dict[str, Any] = {"bot_user": bot_user, "difficulty": rcon.one("difficulty"), "orig_pos": orig_pos, "experiments": {}}
    try:
        _setup_arena(rcon)
        _reset_vital(rcon, bot_user)
        _wait_settled(bot, CENTER_X, CENTER_Y, CENTER_Z, timeout_s=20.0)
        for name, fn in selected:
            print(f"[probe] running {name}...", flush=True)
            report["experiments"][name] = fn(bot, rcon, bot_user)
            print(f"[probe] done {name}", flush=True)
    finally:
        safe_stop(bot)
        _teardown_arena(rcon, bot_user, orig_pos, orig_items)

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(REPORT_PATH), "experiments": list(report["experiments"].keys())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
