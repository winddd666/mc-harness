"""Local Codex-style Web Console (`mc-console`) for steering `mc-harness` interactively.

Binds strictly to `127.0.0.1` by default so LAN spectators watching read-only `mc-live`
cannot send commands or mutate the running bot session, while every run automatically
writes standard `runs/<stem>.events.jsonl` and `.json` files that `mc-live` tails live.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import mimetypes
import re
import sys
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from aiohttp import WSMsgType, web

from ..bot import BotManager, BotSession
from ..config import Settings, should_trust_env
from ..errors import McAgentError
from ..harness.agent import run_mc_agent
from ..harness.context import DEFAULT_TOOL_OUTPUT_CHARS
from ..harness.model import QwenModel, QwenSpec
from ..harness.runtime_input import InputInbox

STATIC_DIR = Path(__file__).resolve().parent / "static"
_CTRL_KEY = web.AppKey("console_controller", object)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_VALID_PROVIDERS = ("nju", "deepseek", "dashscope")
_MAX_STORED_TURNS = 40
_MAX_HISTORY_SUMMARIES = 6
_MAX_TOOL_RESULT_CHARS = 16000
_NOW_PREFIX_RE = re.compile(r"^/now(?:\s+|$)", re.IGNORECASE)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pretty_json(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return "{}"
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except ValueError:
            return raw
    try:
        return json.dumps(raw, ensure_ascii=False, indent=2)
    except TypeError:
        return str(raw)


def _is_tool_failure(result: str) -> bool:
    if result.startswith("错误："):
        return True
    if '"ok": false' in result or '"ok":false' in result:
        try:
            parsed = json.loads(result)
            return isinstance(parsed, dict) and parsed.get("ok") is False
        except ValueError:
            return False
    return False


def _truncate_text(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... (已截断，完整结果共 {len(text)} 字符，详见 runs/*.events.jsonl)"


def _normalize_command_text(raw_text: str, *, immediate: bool = False) -> tuple[str, bool]:
    text = (raw_text or "").strip()
    is_urgent = bool(immediate)
    if _NOW_PREFIX_RE.match(text):
        text = _NOW_PREFIX_RE.sub("", text, count=1).strip()
        is_urgent = True
    return text, is_urgent


def _check_origin(request: web.Request, *, allow_lan: bool) -> bool:
    """Block cross-origin CSRF / CSWSH requests against localhost console."""
    if allow_lan:
        return True
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    origin_host = (parsed.hostname or "").strip().lower()
    req_host = (request.host.split(":")[0] if request.host else "").strip("[]").lower()
    return origin_host in _LOOPBACK_HOSTS or (bool(req_host) and origin_host == req_host)


def _summarize_inventory(inv: dict[str, Any] | None) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(inv, dict):
        return 0, [], None
    items_raw = inv.get("items") or inv.get("slots") or []
    counts: dict[str, int] = {}
    if isinstance(items_raw, list):
        for entry in items_raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("item")
            cnt = entry.get("count", 0)
            if isinstance(name, str) and name and isinstance(cnt, int) and cnt > 0:
                counts[name] = counts.get(name, 0) + cnt
    elif isinstance(inv.get("counts"), dict):
        for name, cnt in inv["counts"].items():
            if isinstance(name, str) and name and isinstance(cnt, int) and cnt > 0:
                counts[name] = cnt
    sorted_items = [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    ]
    held = inv.get("held")
    if not isinstance(held, dict):
        held = None
    return sum(counts.values()), sorted_items, held


def _build_telemetry_payload(
    status: dict[str, Any] | None,
    inventory: dict[str, Any] | None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    st = status if isinstance(status, dict) else {}
    pos_raw = st.get("pos")
    pos: dict[str, float] | None = None
    if isinstance(pos_raw, (list, tuple)) and len(pos_raw) >= 3:
        try:
            pos = {
                "x": round(float(pos_raw[0]), 1),
                "y": round(float(pos_raw[1]), 1),
                "z": round(float(pos_raw[2]), 1),
            }
        except (TypeError, ValueError):
            pos = None
    elif isinstance(pos_raw, dict):
        try:
            pos = {
                "x": round(float(pos_raw["x"]), 1),
                "y": round(float(pos_raw["y"]), 1),
                "z": round(float(pos_raw["z"]), 1),
            }
        except (KeyError, TypeError, ValueError):
            pos = None
    total_items, top_items, held = _summarize_inventory(inventory)
    if held is None and isinstance(st.get("held"), dict):
        held = st.get("held")
    connected = bool(st.get("connected", st.get("ready", False)))
    ready = bool(st.get("ready", False))
    return {
        "connected": connected,
        "ready": ready,
        "username": st.get("username"),
        "pos": pos,
        "dimension": st.get("dimension"),
        "biome": st.get("biome"),
        "is_day": st.get("is_day"),
        "time_of_day": st.get("time_of_day"),
        "health": st.get("health"),
        "food": st.get("food"),
        "armor": st.get("armor", st.get("armor_points")),
        "held": held,
        "inventory_total": total_items,
        "top_items": top_items,
        "updated_at": _utc_iso(),
        "error": error,
    }


@dataclasses.dataclass(slots=True)
class ConsoleConfig:
    env_file: str | None = None
    bot_username: str | None = None
    provider: str = "nju"
    base_url_override: str | None = None
    model_override: str | None = None
    rpm_budget_override: int | None = None
    max_calls: int = 24
    run_timeout_s: float = 600.0
    enable_thinking: bool = False
    max_tokens: int = 1024
    tool_output_chars: int = DEFAULT_TOOL_OUTPUT_CHARS
    host: str = "127.0.0.1"
    port: int = 8770
    live_url: str = "http://127.0.0.1:8765"
    runs_dir: Path = dataclasses.field(default_factory=lambda: Path.cwd() / "runs")
    allow_lan: bool = False
    telemetry_interval_s: float = 2.0
    connect_on_startup: bool = True


class _QuietJsonlSink:
    """Flush each harness event to disk immediately without noisy terminal printing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file = None
        self.failed = False

    def __enter__(self) -> _QuietJsonlSink:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x", encoding="utf-8")
        return self

    def write(self, event: dict[str, Any]) -> None:
        if self.failed or self._file is None:
            return
        try:
            self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._file.flush()
        except OSError:
            self.failed = True
            raise RuntimeError("事件日志写入失败，停止后续执行。") from None

    def __exit__(self, *_: object) -> None:
        if self._file is not None:
            self._file.close()


class ConsoleController:
    """Thread-safe orchestrator bridging the aiohttp UI and synchronous `run_mc_agent` worker."""

    def __init__(
        self,
        config: ConsoleConfig,
        *,
        settings: Settings | None = None,
        session_factory: Callable[[str | None], BotSession] | None = None,
        chat_factory: Callable[[str, bool, int], tuple[str, Callable]] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or Settings.load(dotenv_path=config.env_file)
        self.bot_username = config.bot_username or self.settings.default_bot_username or "Bot"
        self.runs_dir = Path(config.runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        self._session_factory = session_factory
        self._chat_factory = chat_factory
        self._lock = threading.RLock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._telemetry_task: asyncio.Task[None] | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._async_client_base: str | None = None

        self._session: BotSession | None = None
        self._connect_lock = threading.Lock()
        self._connect_error: str | None = None

        self._turns: list[dict[str, Any]] = []
        self._history_summaries: list[tuple[str, str, str]] = []
        self._active_turn_id: str | None = None
        self._active_inbox: InputInbox | None = None
        self._stop_event: threading.Event | None = None
        self._worker_thread: threading.Thread | None = None
        self._seq_counter = 0

        self._default_provider = config.provider if config.provider in _VALID_PROVIDERS else "nju"
        self._default_max_calls = max(1, int(config.max_calls))
        self._default_thinking = bool(config.enable_thinking)
        self._telemetry: dict[str, Any] = _build_telemetry_payload(None, None)

        self._model_lock = threading.Lock()
        self._cached_models: dict[tuple[str, bool, int], QwenModel] = {}

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.config.connect_on_startup:
            self._loop.create_task(self._startup_connect())
        if self.config.telemetry_interval_s > 0:
            self._telemetry_task = self._loop.create_task(self._telemetry_loop())

    async def _startup_connect(self) -> None:
        try:
            await asyncio.to_thread(self._ensure_session)
            await self.refresh_telemetry()
        except Exception as exc:
            with self._lock:
                self._connect_error = str(exc)
                self._telemetry["error"] = str(exc)
                self._telemetry["updated_at"] = _utc_iso()
                payload = deepcopy(self._telemetry)
            await self._broadcast({"type": "telemetry", "telemetry": payload})

    async def stop(self) -> None:
        with self._lock:
            stop_ev = self._stop_event
            worker = self._worker_thread
        if stop_ev is not None:
            stop_ev.set()
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._telemetry_task
            self._telemetry_task = None
        for ws in list(self._ws_clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._ws_clients.clear()
        if worker is not None and worker.is_alive():
            await asyncio.to_thread(worker.join, 5.0)
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                self._session.close()
            self._session = None
        with self._model_lock:
            for model in self._cached_models.values():
                with contextlib.suppress(Exception):
                    model.close()
            self._cached_models.clear()

    def _next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq_counter += 1
            return f"{prefix}_{self._seq_counter}"

    def _emit_ws(self, msg: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(self._broadcast(msg))
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._schedule_broadcast, msg)

    def _schedule_broadcast(self, msg: dict[str, Any]) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.create_task(self._broadcast(msg))

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        payload = json.dumps(msg, ensure_ascii=False)
        stale: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._ws_clients.discard(ws)

    def _ensure_session(self) -> BotSession:
        with self._connect_lock:
            if self._session is not None:
                return self._session
            if self._session_factory is not None:
                session = self._session_factory(self.config.bot_username)
            else:
                manager = BotManager(self.settings)
                provisioned = manager.open_bot(self.config.bot_username, wait_ready=True)
                session = provisioned.session
                self.bot_username = provisioned.bot_username
            if getattr(session, "bot_username", None):
                self.bot_username = str(session.bot_username)
            self._session = session
            self._connect_error = None
            return session

    def _get_async_client(self, base_url: str) -> httpx.AsyncClient:
        clean_base = base_url.rstrip("/")
        if self._async_client is not None and self._async_client_base == clean_base:
            return self._async_client
        if self._async_client is not None:
            old = self._async_client
            if self._loop is not None and not self._loop.is_closed():
                self._loop.create_task(old.aclose())
        self._async_client = httpx.AsyncClient(
            base_url=clean_base,
            timeout=httpx.Timeout(6.0, connect=3.0),
            trust_env=should_trust_env(clean_base),
        )
        self._async_client_base = clean_base
        return self._async_client

    async def refresh_telemetry(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            return self.get_telemetry_snapshot()
        token = getattr(session, "token", None)
        base_url = getattr(session, "base_url", None)
        # Allow pure mock sessions in tests without real HTTP server
        if hasattr(session, "poll_async_telemetry"):
            try:
                st, inv = await session.poll_async_telemetry()
                payload = _build_telemetry_payload(st, inv)
            except Exception as exc:
                payload = _build_telemetry_payload(None, None, error=str(exc))
        elif isinstance(token, str) and token and isinstance(base_url, str) and base_url.startswith(("http://", "https://")):
            client = self._get_async_client(base_url)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                st_resp, inv_resp = await asyncio.gather(
                    client.get("/status", headers=headers),
                    client.get("/inventory", headers=headers),
                )
                st_json = st_resp.json() if st_resp.status_code == 200 else {}
                inv_json = inv_resp.json() if inv_resp.status_code == 200 else {}
                st_data = st_json.get("result") if isinstance(st_json.get("result"), dict) else st_json
                inv_data = inv_json.get("result") if isinstance(inv_json.get("result"), dict) else inv_json
                err = None
                if st_resp.status_code != 200:
                    err = f"MC /status HTTP {st_resp.status_code}"
                payload = _build_telemetry_payload(st_data, inv_data, error=err)
            except Exception as exc:
                payload = _build_telemetry_payload(None, None, error=str(exc))
        else:
            return self.get_telemetry_snapshot()

        if isinstance(payload.get("username"), str) and payload["username"]:
            self.bot_username = payload["username"]
        with self._lock:
            self._telemetry = payload
        await self._broadcast({"type": "telemetry", "telemetry": deepcopy(payload)})
        return payload

    async def _telemetry_loop(self) -> None:
        while True:
            await self.refresh_telemetry()
            await asyncio.sleep(max(0.5, float(self.config.telemetry_interval_s)))

    def _get_chat_runner(self, provider: str, enable_thinking: bool, max_tokens: int) -> tuple[str, Callable]:
        if self._chat_factory is not None:
            return self._chat_factory(provider, enable_thinking, max_tokens)
        key = (provider, bool(enable_thinking), int(max_tokens))
        with self._model_lock:
            model = self._cached_models.get(key)
            if model is None:
                spec = QwenSpec(
                    dotenv_path=self.config.env_file,
                    fallback_dotenv_path=".env",
                    provider=provider,
                    base_url_override=self.config.base_url_override,
                    model_override=self.config.model_override,
                    rpm_budget_override=self.config.rpm_budget_override,
                    enable_thinking=enable_thinking,
                    max_tokens=max_tokens,
                )
                model = QwenModel(spec)
                self._cached_models[key] = model
            return model.model, model.chat

    def _compose_task_prompt(self, raw_prompt: str) -> str:
        with self._lock:
            history = list(self._history_summaries[-_MAX_HISTORY_SUMMARIES:])
        if not history:
            return raw_prompt
        lines = [
            "[本会话前序指令记录（仅供参考，当前坐标/背包/周边环境请务必以开局最新状态快照和工具实测为准）]"
        ]
        for idx, (prev_q, prev_status, prev_a) in enumerate(history, start=1):
            q_short = prev_q.replace("\n", " ")[:120]
            a_short = (prev_a or prev_status).replace("\n", " ")[:240]
            lines.append(f"{idx}. 指令：{q_short} -> 结果({prev_status})：{a_short}")
        lines.append("")
        lines.append(f"[当前新指令]\n{raw_prompt}")
        return "\n".join(lines)

    def get_telemetry_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._telemetry)

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bot_username": self.bot_username,
                "mc_api_base_url": self.settings.mc_api_base_url,
                "live_url": self.config.live_url,
                "config": {
                    "provider": self._default_provider,
                    "providers": list(_VALID_PROVIDERS),
                    "max_calls": self._default_max_calls,
                    "enable_thinking": self._default_thinking,
                    "run_timeout_s": self.config.run_timeout_s,
                    "host": self.config.host,
                    "port": self.config.port,
                },
                "running": self._active_turn_id is not None,
                "active_turn_id": self._active_turn_id,
                "connect_error": self._connect_error,
                "telemetry": deepcopy(self._telemetry),
                "turns": deepcopy(self._turns),
                "history_count": len(self._history_summaries),
            }

    async def submit_command(
        self,
        raw_text: str,
        *,
        immediate: bool = False,
        provider: str | None = None,
        max_calls: int | None = None,
        enable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        text, is_urgent = _normalize_command_text(raw_text, immediate=immediate)
        if not text:
            return {"ok": False, "error": "empty_command", "message": "指令不能为空"}

        chosen_provider = (provider or self._default_provider).strip().lower()
        if chosen_provider not in _VALID_PROVIDERS:
            chosen_provider = self._default_provider
        chosen_max_calls = max(1, min(100, int(max_calls or self._default_max_calls)))
        chosen_thinking = self._default_thinking if enable_thinking is None else bool(enable_thinking)

        with self._lock:
            self._default_provider = chosen_provider
            self._default_max_calls = chosen_max_calls
            self._default_thinking = chosen_thinking

            # Case 1: Active run -> inject into InputInbox!
            if self._active_turn_id is not None and self._active_inbox is not None:
                active_turn = self._find_turn_locked(self._active_turn_id)
                self._active_inbox.submit(text, immediate=is_urgent)
                steer_item = {
                    "id": self._next_id("steer"),
                    "kind": "user_steer",
                    "input_id": None,
                    "text": text,
                    "priority": "urgent" if is_urgent else "normal",
                    "status": "queued",
                    "boundary": None,
                    "created_at": _utc_iso(),
                }
                if active_turn is not None:
                    active_turn["items"].append(steer_item)
                turn_id = self._active_turn_id
                steered_payload = deepcopy(steer_item)
                self._emit_ws({"type": "turn_item_upsert", "turn_id": turn_id, "item": steered_payload})
                return {
                    "ok": True,
                    "mode": "steered",
                    "immediate": is_urgent,
                    "turn_id": turn_id,
                    "item": steered_payload,
                }

            # Case 2: Idle -> start a fresh worker run on persistent BotSession
            stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            # Avoid stem collision inside sub-microsecond tests
            if any(t["turn_id"] == stem for t in self._turns):
                stem = f"{stem}_{self._seq_counter + 1}"

            turn = {
                "turn_id": stem,
                "prompt": text,
                "created_at": _utc_iso(),
                "status": "running",
                "phase": "preparing",
                "phase_detail": "正在连接会话与加载最新状态快照...",
                "actor_requests": 0,
                "max_calls": chosen_max_calls,
                "provider": chosen_provider,
                "model": chosen_provider,
                "thinking": chosen_thinking,
                "items": [],
                "answer": None,
                "error": None,
                "run_file": f"{stem}.events.jsonl",
            }
            self._turns.append(turn)
            if len(self._turns) > _MAX_STORED_TURNS:
                self._turns = self._turns[-_MAX_STORED_TURNS:]

            inbox = InputInbox()
            stop_event = threading.Event()
            self._active_turn_id = stem
            self._active_inbox = inbox
            self._stop_event = stop_event

            turn_snapshot = deepcopy(turn)

        self._emit_ws({"type": "turn_started", "turn": turn_snapshot})
        worker = threading.Thread(
            target=self._run_worker,
            args=(stem, text, chosen_provider, chosen_max_calls, chosen_thinking, inbox, stop_event),
            name=f"mc-console-{stem}",
            daemon=True,
        )
        with self._lock:
            self._worker_thread = worker
        worker.start()
        return {"ok": True, "mode": "started", "turn_id": stem, "turn": turn_snapshot}

    async def stop_active_run(self) -> dict[str, Any]:
        with self._lock:
            turn_id = self._active_turn_id
            stop_ev = self._stop_event
            if stop_ev is None or turn_id is None:
                return {"ok": True, "running": False, "stopping": False}
            stop_ev.set()
            turn = self._find_turn_locked(turn_id)
            if turn is not None:
                turn["phase_detail"] = "正在发送停止信号..."
                notice = {
                    "id": self._next_id("notice"),
                    "kind": "notice",
                    "level": "warn",
                    "text": "已触发停止信号，正在中止当前大模型生成与 Mineflayer 动作...",
                }
                turn["items"].append(notice)
                self._emit_ws({"type": "turn_item_upsert", "turn_id": turn_id, "item": deepcopy(notice)})
                self._emit_ws(
                    {
                        "type": "turn_meta",
                        "turn_id": turn_id,
                        "phase": turn["phase"],
                        "phase_detail": turn["phase_detail"],
                        "actor_requests": turn["actor_requests"],
                    }
                )

        # Concurrently tell MC Agent API to halt active movement / mining
        session = self._session
        if session is not None:
            if hasattr(session, "stop_async"):
                with contextlib.suppress(Exception):
                    await session.stop_async()
            else:
                token = getattr(session, "token", None)
                base_url = getattr(session, "base_url", None)
                if isinstance(token, str) and token and isinstance(base_url, str) and base_url.startswith(("http://", "https://")):
                    client = self._get_async_client(base_url)
                    with contextlib.suppress(Exception):
                        await client.post("/stop", headers={"Authorization": f"Bearer {token}"}, json={})
                elif hasattr(session, "stop"):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(session.stop)

        return {"ok": True, "running": True, "stopping": True, "turn_id": turn_id}

    async def clear_session(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._active_turn_id is not None:
                return 409, {"ok": False, "error": "run_active", "message": "当前任务正在执行，请先停止后再新建会话"}
            self._turns.clear()
            self._history_summaries.clear()
        await self._broadcast({"type": "session_cleared"})
        return 200, {"ok": True}

    async def reconnect_bot(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._active_turn_id is not None:
                return 409, {"ok": False, "error": "run_active", "message": "任务执行中暂不支持手动重连"}
        try:
            session = await asyncio.to_thread(self._ensure_session)
            await asyncio.to_thread(session.wait_until_ready, timeout_s=30.0, poll_interval_s=1.0)
            with self._lock:
                self._connect_error = None
            telemetry = await self.refresh_telemetry()
            return 200, {"ok": True, "telemetry": telemetry}
        except Exception as exc:
            with self._lock:
                self._connect_error = str(exc)
            return 502, {"ok": False, "error": "reconnect_failed", "message": str(exc)}

    def _find_turn_locked(self, turn_id: str) -> dict[str, Any] | None:
        for t in self._turns:
            if t["turn_id"] == turn_id:
                return t
        return None

    def _run_worker(
        self,
        stem: str,
        raw_prompt: str,
        provider: str,
        max_calls: int,
        enable_thinking: bool,
        inbox: InputInbox,
        stop_event: threading.Event,
    ) -> None:
        event_path = self.runs_dir / f"{stem}.events.jsonl"
        report_path = self.runs_dir / f"{stem}.json"

        state: dict[str, Any] = {
            "active_text_by_req": {},
            "skipped_calls": set(),
            "tool_items": {},
        }

        try:
            session = self._ensure_session()
            # Quick readiness guard before kicking off model requests
            try:
                st = session.status()
                if not st.get("ready"):
                    session.wait_until_ready(timeout_s=30.0, poll_interval_s=1.0, initial_status=st)
            except Exception as exc:
                # Let runtime handle or surface cleanly if connect fails completely
                raise RuntimeError(f"Bot 连接未就绪：{exc}") from exc

            model_name, chat_fn = self._get_chat_runner(provider, enable_thinking, self.config.max_tokens)
            with self._lock:
                t = self._find_turn_locked(stem)
                if t is not None:
                    t["model"] = model_name
            self._emit_ws({"type": "turn_meta", "turn_id": stem, "model": model_name})

            composite_task = self._compose_task_prompt(raw_prompt)
            report: dict[str, Any] | None = None

            with _QuietJsonlSink(event_path) as sink:
                def on_event(event: dict[str, Any]) -> None:
                    sink.write(event)
                    self._handle_harness_event(stem, state, event)

                report = run_mc_agent(
                    composite_task,
                    session,
                    chat_fn=chat_fn,
                    model_name=model_name,
                    max_calls=max_calls,
                    run_timeout_s=self.config.run_timeout_s,
                    tool_output_chars=self.config.tool_output_chars,
                    stream=True,
                    on_event=on_event,
                    inbox=inbox,
                    include_initial_snapshot=True,
                    stop_check=stop_event.is_set,
                )

            if report is not None:
                report["prompt_display"] = raw_prompt
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                self._finalize_turn(
                    stem,
                    raw_prompt=raw_prompt,
                    status=str(report.get("status") or "finished"),
                    answer=report.get("answer"),
                    error=report.get("error"),
                    actor_requests=int(report.get("actor_requests") or 0),
                )
        except Exception as exc:
            self._finalize_turn(
                stem,
                raw_prompt=raw_prompt,
                status="runtime_error",
                answer=None,
                error=str(exc),
                actor_requests=None,
            )
        finally:
            with self._lock:
                if self._active_turn_id == stem:
                    self._active_turn_id = None
                    self._active_inbox = None
                    self._stop_event = None
                    self._worker_thread = None

    def _finalize_turn(
        self,
        stem: str,
        *,
        raw_prompt: str,
        status: str,
        answer: str | None,
        error: str | None,
        actor_requests: int | None,
    ) -> None:
        with self._lock:
            turn = self._find_turn_locked(stem)
            if turn is not None:
                turn["status"] = status
                turn["phase"] = "finished"
                turn["phase_detail"] = ""
                if actor_requests is not None:
                    turn["actor_requests"] = actor_requests
                if answer is not None:
                    turn["answer"] = answer
                if error is not None:
                    turn["error"] = error
                for item in turn["items"]:
                    if item.get("streaming"):
                        item["streaming"] = False
                summary_snippet = (answer or error or status)[:240]
                self._history_summaries.append((raw_prompt, status, summary_snippet))
                if len(self._history_summaries) > _MAX_HISTORY_SUMMARIES:
                    self._history_summaries = self._history_summaries[-_MAX_HISTORY_SUMMARIES:]
                snapshot = deepcopy(turn)
            else:
                snapshot = None
        if snapshot is not None:
            self._emit_ws({"type": "turn_finished", "turn": snapshot})

    def _handle_harness_event(self, stem: str, state: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("type")
        with self._lock:
            turn = self._find_turn_locked(stem)
            if turn is None:
                return

            if kind == "run.phase":
                turn["phase"] = str(event.get("phase") or "")
                phase_labels = {
                    "preparing": "准备上下文...",
                    "generating": "思考与生成回复中...",
                    "executing_tools": "执行 MC 工具中...",
                    "deciding": "评估新输入...",
                    "finished": "已结束",
                }
                turn["phase_detail"] = phase_labels.get(turn["phase"], turn["phase"])
                self._emit_ws(
                    {
                        "type": "turn_meta",
                        "turn_id": stem,
                        "phase": turn["phase"],
                        "phase_detail": turn["phase_detail"],
                        "actor_requests": turn["actor_requests"],
                    }
                )
                return

            if kind == "model.started":
                turn["actor_requests"] = int(event.get("request") or (turn["actor_requests"] + 1))
                turn["phase"] = "generating"
                turn["phase_detail"] = f"模型请求第 {turn['actor_requests']}/{turn['max_calls']} 轮..."
                self._emit_ws(
                    {
                        "type": "turn_meta",
                        "turn_id": stem,
                        "phase": turn["phase"],
                        "phase_detail": turn["phase_detail"],
                        "actor_requests": turn["actor_requests"],
                    }
                )
                return

            if kind in ("model.text.delta", "model.text"):
                req = int(event.get("request") or turn["actor_requests"] or 0)
                chunk = str(event.get("text") or "")
                if not chunk:
                    return
                item = state["active_text_by_req"].get(req)
                if item is None:
                    item = {
                        "id": self._next_id("msg"),
                        "kind": "assistant_text",
                        "request": req,
                        "text": chunk,
                        "reasoning": None,
                        "streaming": kind == "model.text.delta",
                    }
                    state["active_text_by_req"][req] = item
                    turn["items"].append(item)
                    self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(item)})
                else:
                    item["text"] += chunk
                    self._emit_ws(
                        {
                            "type": "turn_item_delta",
                            "turn_id": stem,
                            "item_id": item["id"],
                            "delta": chunk,
                        }
                    )
                return

            if kind == "model.tool.delta":
                name = event.get("name")
                if isinstance(name, str) and name:
                    turn["phase_detail"] = f"组织调用 {name}..."
                    self._emit_ws(
                        {
                            "type": "turn_meta",
                            "turn_id": stem,
                            "phase": turn["phase"],
                            "phase_detail": turn["phase_detail"],
                            "actor_requests": turn["actor_requests"],
                        }
                    )
                return

            if kind == "model.completed":
                req = int(event.get("request") or turn["actor_requests"] or 0)
                msg = event.get("message") or {}
                item = state["active_text_by_req"].get(req)
                reasoning = msg.get("reasoning_content")
                content = msg.get("content")
                if item is None and (content or reasoning):
                    item = {
                        "id": self._next_id("msg"),
                        "kind": "assistant_text",
                        "request": req,
                        "text": str(content or ""),
                        "reasoning": str(reasoning) if reasoning else None,
                        "streaming": False,
                    }
                    state["active_text_by_req"][req] = item
                    turn["items"].append(item)
                elif item is not None:
                    item["streaming"] = False
                    if reasoning:
                        item["reasoning"] = str(reasoning)
                    if content and not item["text"]:
                        item["text"] = str(content)
                if item is not None:
                    self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(item)})
                return

            if kind == "model.failed":
                req = int(event.get("request") or turn["actor_requests"] or 0)
                item = state["active_text_by_req"].get(req)
                if item is not None:
                    item["streaming"] = False
                    self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(item)})
                if not event.get("cancelled") and not self._stop_event_is_set():
                    notice = {
                        "id": self._next_id("notice"),
                        "kind": "notice",
                        "level": "error",
                        "text": str(event.get("error") or "模型请求失败"),
                    }
                    turn["items"].append(notice)
                    self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(notice)})
                return

            if kind == "model.rate_wait":
                wait_s = float(event.get("wait_s") or 0.0)
                if wait_s >= 0.4:
                    turn["phase_detail"] = f"RPM 限速冷却等待 {wait_s:.1f}s..."
                    self._emit_ws(
                        {
                            "type": "turn_meta",
                            "turn_id": stem,
                            "phase": turn["phase"],
                            "phase_detail": turn["phase_detail"],
                            "actor_requests": turn["actor_requests"],
                        }
                    )
                return

            if kind == "tool.started":
                call_id = str(event.get("call_id") or self._next_id("call"))
                fn = event.get("function") or {}
                name = str(fn.get("name") or "unknown_tool")
                raw_args = fn.get("arguments") or ""
                item = {
                    "id": call_id,
                    "kind": "tool_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": raw_args,
                    "arguments_pretty": _pretty_json(raw_args),
                    "status": "running",
                    "started_mono": time.monotonic(),
                    "duration_ms": None,
                    "result": None,
                }
                state["tool_items"][call_id] = item
                turn["items"].append(item)
                turn["phase_detail"] = f"正在执行 {name}..."
                self._emit_ws(
                    {
                        "type": "turn_item_upsert",
                        "turn_id": stem,
                        "item": self._public_tool_item(item),
                    }
                )
                self._emit_ws(
                    {
                        "type": "turn_meta",
                        "turn_id": stem,
                        "phase": turn["phase"],
                        "phase_detail": turn["phase_detail"],
                        "actor_requests": turn["actor_requests"],
                    }
                )
                return

            if kind == "tool.skipped":
                call_id = str(event.get("call_id") or "")
                state["skipped_calls"].add(call_id)
                item = state["tool_items"].get(call_id)
                if item is not None:
                    item["status"] = "skipped"
                    self._emit_ws(
                        {
                            "type": "turn_item_upsert",
                            "turn_id": stem,
                            "item": self._public_tool_item(item),
                        }
                    )
                return

            if kind == "mc.notable_events":
                item = state["tool_items"].get(str(event.get("call_id") or ""))
                if item is not None:
                    item["result"] = _truncate_text(
                        f"{item.get('result') or ''}\n[期间新事件] {event.get('text') or ''}"
                    )
                    self._emit_ws({
                        "type": "turn_item_upsert", "turn_id": stem,
                        "item": self._public_tool_item(item),
                    })
                return

            if kind == "tool.completed":
                call_id = str(event.get("call_id") or "")
                result = str(event.get("result") or "")
                item = state["tool_items"].get(call_id)
                if item is None:
                    item = {
                        "id": call_id or self._next_id("call"),
                        "kind": "tool_call",
                        "call_id": call_id,
                        "name": "tool",
                        "arguments": "",
                        "arguments_pretty": "",
                        "status": "skipped",
                        "started_mono": time.monotonic(),
                        "duration_ms": None,
                        "result": None,
                    }
                    state["tool_items"][call_id] = item
                    turn["items"].append(item)
                item["duration_ms"] = max(0, int((time.monotonic() - float(item.get("started_mono") or time.monotonic())) * 1000))
                item["result"] = _truncate_text(result)
                if call_id in state["skipped_calls"]:
                    item["status"] = "skipped"
                elif _is_tool_failure(result):
                    item["status"] = "failed"
                else:
                    item["status"] = "ok"
                self._emit_ws(
                    {
                        "type": "turn_item_upsert",
                        "turn_id": stem,
                        "item": self._public_tool_item(item),
                    }
                )
                return

            if kind in ("input.received", "input.applied", "input.pending"):
                input_id = event.get("input_id") if kind != "input.received" else event.get("id")
                target = None
                for item in turn["items"]:
                    if item.get("kind") != "user_steer":
                        continue
                    if input_id is not None and item.get("input_id") == input_id:
                        target = item
                        break
                    if kind == "input.received" and item.get("input_id") is None and item.get("text") == event.get("text"):
                        target = item
                        break
                if target is None and kind == "input.received":
                    target = {
                        "id": self._next_id("steer"),
                        "kind": "user_steer",
                        "input_id": input_id,
                        "text": str(event.get("text") or ""),
                        "priority": str(event.get("priority") or "normal"),
                        "status": "received",
                        "boundary": None,
                        "created_at": _utc_iso(),
                    }
                    turn["items"].append(target)
                if target is not None:
                    if input_id is not None:
                        target["input_id"] = input_id
                    target["status"] = kind.split(".", 1)[1]
                    if event.get("boundary"):
                        target["boundary"] = str(event["boundary"])
                    self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(target)})
                return

            if kind == "output.superseded":
                notice = {
                    "id": self._next_id("notice"),
                    "kind": "notice",
                    "level": "info",
                    "text": "检测到新插队指令，已取消上一版待交付回答并基于新指令重新决策。",
                }
                turn["items"].append(notice)
                self._emit_ws({"type": "turn_item_upsert", "turn_id": stem, "item": deepcopy(notice)})
                return

            if kind == "answer":
                turn["answer"] = str(event.get("content") or "")
                self._emit_ws({"type": "turn_meta", "turn_id": stem, "answer": turn["answer"]})

    def _stop_event_is_set(self) -> bool:
        ev = self._stop_event
        return bool(ev is not None and ev.is_set())

    @staticmethod
    def _public_tool_item(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != "started_mono"}


def create_console_app(
    config: ConsoleConfig | None = None,
    *,
    settings: Settings | None = None,
    session_factory: Callable[[str | None], BotSession] | None = None,
    chat_factory: Callable[[str, bool, int], tuple[str, Callable]] | None = None,
) -> web.Application:
    cfg = config or ConsoleConfig()
    controller = ConsoleController(
        cfg,
        settings=settings,
        session_factory=session_factory,
        chat_factory=chat_factory,
    )
    app = web.Application()
    app[_CTRL_KEY] = controller

    async def _on_startup(app_inst: web.Application) -> None:
        await app_inst[_CTRL_KEY].start()

    async def _on_cleanup(app_inst: web.Application) -> None:
        await app_inst[_CTRL_KEY].stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    async def handle_index(request: web.Request) -> web.StreamResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return web.FileResponse(index_path, headers={"Cache-Control": "no-store"})
        return web.Response(text="mc-console static UI missing", status=500)

    async def handle_static(request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name", "")
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise web.HTTPBadRequest(text="Invalid asset path")
        base = STATIC_DIR.resolve()
        candidate = (base / name).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise web.HTTPNotFound(text="Static asset not found")
        ctype, _ = mimetypes.guess_type(str(candidate))
        headers = {"Cache-Control": "no-store"}
        if ctype:
            headers["Content-Type"] = ctype
        return web.FileResponse(candidate, headers=headers)

    async def handle_state(request: web.Request) -> web.Response:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        return web.json_response(ctrl.get_snapshot())

    async def handle_command(request: web.Request) -> web.Response:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        if not _check_origin(request, allow_lan=ctrl.config.allow_lan):
            return web.json_response({"ok": False, "error": "cross_origin_forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

        raw_thinking = body.get("enable_thinking")
        if raw_thinking is None:
            raw_thinking = body.get("thinking")
        res = await ctrl.submit_command(
            str(body.get("text") or ""),
            immediate=bool(body.get("immediate", False)),
            provider=body.get("provider"),
            max_calls=body.get("max_calls"),
            enable_thinking=raw_thinking,
        )
        status = 200 if res.get("ok") else 400
        return web.json_response(res, status=status)

    async def handle_stop(request: web.Request) -> web.Response:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        if not _check_origin(request, allow_lan=ctrl.config.allow_lan):
            return web.json_response({"ok": False, "error": "cross_origin_forbidden"}, status=403)
        res = await ctrl.stop_active_run()
        return web.json_response(res)

    async def handle_clear(request: web.Request) -> web.Response:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        if not _check_origin(request, allow_lan=ctrl.config.allow_lan):
            return web.json_response({"ok": False, "error": "cross_origin_forbidden"}, status=403)
        status, payload = await ctrl.clear_session()
        return web.json_response(payload, status=status)

    async def handle_reconnect(request: web.Request) -> web.Response:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        if not _check_origin(request, allow_lan=ctrl.config.allow_lan):
            return web.json_response({"ok": False, "error": "cross_origin_forbidden"}, status=403)
        status, payload = await ctrl.reconnect_bot()
        return web.json_response(payload, status=status)

    async def handle_ws(request: web.Request) -> web.StreamResponse:
        ctrl: ConsoleController = request.app[_CTRL_KEY]
        if not _check_origin(request, allow_lan=ctrl.config.allow_lan):
            return web.Response(status=403, text="Forbidden cross-origin WebSocket")
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)
        snap = ctrl.get_snapshot()
        await ws.send_str(json.dumps({"type": "snapshot", **snap}, ensure_ascii=False))
        ctrl._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data.strip() == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            ctrl._ws_clients.discard(ws)
        return ws

    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/static/{name:.+}", handle_static),
            web.get("/api/state", handle_state),
            web.post("/api/command", handle_command),
            web.post("/api/stop", handle_stop),
            web.post("/api/clear", handle_clear),
            web.post("/api/reconnect", handle_reconnect),
            web.get("/ws", handle_ws),
        ]
    )
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-console",
        description="本地 Codex 风格 Minecraft Agent 交互控制台（仅默认监听 127.0.0.1，并与 mc-live 联动直播）",
    )
    parser.add_argument("--env-file", default=None, help="指定配置文件（如 .env.beast，默认 .env）")
    parser.add_argument("--bot", default=None, help="Bot 角色名（默认取配置文件中的 MC_BOT_USERNAME）")
    parser.add_argument(
        "--provider",
        choices=list(_VALID_PROVIDERS),
        default="nju",
        help="默认 LLM 提供方（nju / deepseek / dashscope，默认 nju）",
    )
    parser.add_argument("--base-url", default=None, help="覆盖 LLM Base URL")
    parser.add_argument("--model", default=None, help="覆盖模型名")
    parser.add_argument("--rpm-budget", type=int, default=None, help="覆盖每分钟请求上限")
    parser.add_argument("--max-calls", type=int, default=24, help="单条指令默认最大模型轮数（默认 24）")
    parser.add_argument("--run-timeout-s", type=float, default=600.0, help="单条指令默认超时秒数（默认 600）")
    parser.add_argument("--max-tokens", type=int, default=1024, help="单轮最大输出 token 数（默认 1024）")
    parser.add_argument("--thinking", action="store_true", help="默认开启模型思考模式")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1 仅本机）")
    parser.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    parser.add_argument("--live-url", default="http://127.0.0.1:8765", help="只读旁观页 mc-live 跳转地址")
    parser.add_argument("--runs-dir", default="runs", help="日志与录像目录（需与 mc-live 保持一致以联动直播）")
    parser.add_argument("--allow-lan", action="store_true", help="允许非本机 Origin 访问控制接口（仅限明确需要局域网控制时使用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于 0")
    if args.port < 1 or args.port > 65535:
        parser.error("--port 必须在 1..65535 之间")

    try:
        settings = Settings.load(dotenv_path=args.env_file)
    except McAgentError as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 1

    cfg = ConsoleConfig(
        env_file=args.env_file,
        bot_username=args.bot,
        provider=args.provider,
        base_url_override=args.base_url,
        model_override=args.model,
        rpm_budget_override=args.rpm_budget,
        max_calls=args.max_calls,
        run_timeout_s=args.run_timeout_s,
        enable_thinking=bool(args.thinking),
        max_tokens=args.max_tokens,
        host=args.host,
        port=args.port,
        live_url=args.live_url,
        runs_dir=Path(args.runs_dir),
        allow_lan=bool(args.allow_lan),
    )

    print(
        f"[mc-console 启动] Console=http://{cfg.host}:{cfg.port} | "
        f"Bot={cfg.bot_username or settings.default_bot_username or 'default'} | "
        f"MC={settings.mc_api_base_url} | Live={cfg.live_url} | Runs={cfg.runs_dir}",
        flush=True,
    )
    app = create_console_app(cfg, settings=settings)
    web.run_app(app, host=cfg.host, port=cfg.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
