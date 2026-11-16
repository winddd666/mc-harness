"""Read-only `mc-live` aiohttp web server, `.events.jsonl` live tailer, and MC telemetry poller."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from aiohttp import WSMsgType, web

from ..auth import BotTokenRecord, TokenStore
from ..config import Settings, parse_dotenv, should_trust_env
from ..runtime import runtime_info
from ..errors import ApiError, AuthError
from .state import (
    VOXEL_QUERY_FEATURES,
    VOXEL_QUERY_TERRAIN,
    LiveStateStore,
    ReplayIncrementer,
    _dist3,
    _extract_blocks_from_find_result,
    _is_station_block,
    _load_companion_files,
    _norm_pos_float,
    _station_label,
    list_runs,
    merge_advancements_with_catalog,
    parse_replay_bundle,
    resolve_safe_run_path,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_VIEWER_PORT = 8766
VALID_CAMERA_MODES = frozenset({"first_person", "third_person"})
OFFLINE_VIEWER_NOTE = (
    "可选本地测试服旁观进程未启动（仅推荐在 5ease 本地服运行 node live_viewer/cam_server.js；"
    "线上比赛服请使用内置零侵入 3D 体素视界）"
)


def _resolve_optional_str(
    explicit: str | None,
    env_key: str,
    dotenv_vars: Mapping[str, str] | None = None,
    *,
    strip_trailing_slash: bool = False,
) -> str | None:
    """Normalize an optional string from explicit arg, environment, or .env mapping."""
    if explicit is not None:
        cleaned = explicit.strip()
        if strip_trailing_slash:
            cleaned = cleaned.rstrip("/")
        return cleaned or None
    raw = os.environ.get(env_key)
    if (raw is None or not raw.strip()) and dotenv_vars is not None:
        raw = dotenv_vars.get(env_key)
    if raw is None:
        return None
    cleaned = raw.strip()
    if strip_trailing_slash:
        cleaned = cleaned.rstrip("/")
    return cleaned or None


def _resolve_optional_url(
    explicit: str | None,
    env_key: str,
    dotenv_vars: Mapping[str, str] | None = None,
) -> str | None:
    """Normalize an optional base URL from explicit arg, environment, or .env mapping."""
    return _resolve_optional_str(explicit, env_key, dotenv_vars, strip_trailing_slash=True)


def _resolve_optional_int(
    explicit: int | None,
    env_key: str,
    dotenv_vars: Mapping[str, str] | None = None,
) -> int | None:
    """Resolve an optional integer from explicit arg, environment, or .env mapping."""
    if explicit is not None:
        return int(explicit)
    raw = _resolve_optional_str(None, env_key, dotenv_vars)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {env_key}: {raw!r}") from exc


def _parse_host_port(raw: str) -> tuple[str, int]:
    """Parse `HOST:PORT` (including IPv6 `[::1]:8766` or `http://host:port`) into `(host, port)`."""
    text = raw.strip()
    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname or parsed.port is None:
            raise ValueError(f"Invalid viewer proxy target: {raw!r}")
        return parsed.hostname, parsed.port
    if text.startswith("["):
        host_part, sep, port_str = text[1:].partition("]:")
        if sep != ":":
            raise ValueError(f"Invalid IPv6 host:port target: {raw!r}")
    else:
        host_part, sep, port_str = text.rpartition(":")
        if sep != ":":
            raise ValueError(f"Expected HOST:PORT for viewer proxy target, got: {raw!r}")
    host_clean = host_part.strip()
    try:
        port_num = int(port_str.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid port in viewer proxy target: {raw!r}") from exc
    if not host_clean or not (1 <= port_num <= 65535):
        raise ValueError(f"Invalid host/port in viewer proxy target: {raw!r}")
    return host_clean, port_num

# Hard safety allowlist: ONLY non-locking observation endpoints are permitted.
_ALLOWED_OBSERVATION_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/status"),
        ("GET", "/events"),
        ("GET", "/inventory"),
        ("POST", "/get_entities"),
        ("POST", "/advancements"),
        ("POST", "/find_blocks"),
    }
)


class ReadOnlyTokenProvider:
    """Strictly read-only reader over `TokenStore` — NEVER writes, removes, or logs in."""

    def __init__(self, settings: Settings, *, pinned_bot: str | None = None) -> None:
        self.settings = settings
        self.pinned_bot = pinned_bot.strip() if pinned_bot and pinned_bot.strip() else None
        self.token_store = TokenStore(settings.state_dir / "tokens.json")

    def resolve(self) -> tuple[str | None, BotTokenRecord | None]:
        """Return `(bot_username_display, cached_record_or_none)` without mutating disk."""
        if self.pinned_bot:
            rec = self.token_store.get(self.pinned_bot)
            return (rec.bot_username if rec else self.pinned_bot), rec

        for candidate in (self.settings.default_bot_username, self.settings.skin_email):
            if candidate:
                rec = self.token_store.get(candidate)
                if rec is not None:
                    return rec.bot_username, rec

        all_records = self.token_store.list_all()
        if all_records:
            newest = max(all_records, key=lambda r: r.obtained_at or "")
            return newest.bot_username, newest

        return self.settings.default_bot_username, None


class ReadOnlyMcClient:
    """Async read-only client for MC Agent API observation endpoints (no reauth, no actions)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(4.0, timeout_s)),
            transport=transport,
            trust_env=should_trust_env(self.base_url),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def observe(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        upper = method.upper()
        rel_path = "/" + path.lstrip("/")
        if (upper, rel_path) not in _ALLOWED_OBSERVATION_ENDPOINTS:
            raise PermissionError(
                f"Blocked non-observation MC endpoint in mc-live read-only mode: {upper} {rel_path}"
            )

        headers = {"Authorization": f"Bearer {token}"}
        if upper == "GET":
            resp = await self._client.get(rel_path, headers=headers, params=params)
        else:
            resp = await self._client.post(
                rel_path,
                headers=headers,
                params=params,
                json={} if json_body is None else json_body,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(
                code="bad_json",
                message=f"Invalid JSON from {rel_path} (HTTP {resp.status_code})",
                http_status=resp.status_code,
            ) from exc

        if not isinstance(payload, dict):
            raise ApiError(
                code="bad_json",
                message=f"Unexpected payload type from {rel_path}",
                http_status=resp.status_code,
            )

        if resp.status_code == 200 and payload.get("ok") is True:
            result = payload.get("result")
            return result if isinstance(result, dict) else {k: v for k, v in payload.items() if k not in ("ok", "ms")}

        raise ApiError.from_payload(payload, http_status=resp.status_code, headers=dict(resp.headers))


class ViewerDetector:
    """Lightweight cached auto-detector for optional local `prismarine-viewer` cam server."""

    def __init__(
        self,
        viewer_url: str | None = None,
        *,
        cache_ttl_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.explicit_viewer_url = viewer_url.strip().rstrip("/") if viewer_url and viewer_url.strip() else None
        self.default_probe_host: str = "127.0.0.1"
        self.default_port: int = DEFAULT_VIEWER_PORT
        self.cache_ttl_s = cache_ttl_s
        self._transport = transport
        self._last_probe_mono = -999.0
        self._cached_available = False
        self._cached_note = OFFLINE_VIEWER_NOTE

    async def probe(self, request_host: str | None = None, request_scheme: str = "http") -> dict[str, Any]:
        now = time.monotonic()
        if (now - self._last_probe_mono) >= self.cache_ttl_s:
            self._last_probe_mono = now
            probe_base = self.explicit_viewer_url or f"http://{self.default_probe_host}:{self.default_port}"
            ok, note = await self._check_http(probe_base)
            self._cached_available = ok
            self._cached_note = note

        if not self._cached_available:
            return {
                "available": False,
                "embed_url": None,
                "note": self._cached_note,
            }

        if self.explicit_viewer_url:
            embed_url = self.explicit_viewer_url
        else:
            host_only = "127.0.0.1"
            if request_host:
                parsed_h = urlparse(f"{request_scheme}://{request_host}")
                if parsed_h.hostname:
                    host_only = parsed_h.hostname
            embed_url = f"{request_scheme}://{host_only}:{self.default_port}"

        return {
            "available": True,
            "embed_url": embed_url,
            "note": self._cached_note,
        }

    async def _check_http(self, base_url: str) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(0.45, connect=0.35),
                transport=self._transport,
                trust_env=False,
            ) as client:
                for sub in ("/health", "/"):
                    try:
                        resp = await client.get(f"{base_url}{sub}")
                        if resp.status_code < 500:
                            if sub == "/health":
                                try:
                                    data = resp.json()
                                    if isinstance(data, dict) and data.get("note"):
                                        return True, str(data["note"])
                                except ValueError:
                                    pass
                            return True, f"Prismarine 1.21.11 实景旁观服务在线 (:{self.default_port})"
                    except httpx.HTTPError:
                        continue
        except Exception:
            pass
        return False, OFFLINE_VIEWER_NOTE


class LiveCoordinator:
    """Coordinates live `.events.jsonl` tailing, non-locking MC API polling, and WS broadcasting."""

    def __init__(
        self,
        *,
        settings: Settings,
        runs_dir: Path,
        pinned_bot: str | None = None,
        viewer_url: str | None = None,
        director_url: str | None = None,
        upstream_live_url: str | None = None,
        viewer_proxy_target: str | None = None,
        viewer_proxy_port: int | None = None,
        viewer_proxy_host: str = "127.0.0.1",
        static_dir: Path = STATIC_DIR,
        mc_transport: httpx.AsyncBaseTransport | None = None,
        viewer_transport: httpx.AsyncBaseTransport | None = None,
        director_transport: httpx.AsyncBaseTransport | None = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
        enable_mc_polling: bool = True,
        tail_interval_s: float = 0.2,
        status_interval_s: float = 1.0,
        entities_interval_s: float = 1.5,
        inv_adv_interval_s: float = 5.0,
        voxel_min_interval_s: float = 3.0,
        voxel_min_move_blocks: float = 4.0,
        upstream_poll_interval_s: float = 1.0,
    ) -> None:
        self.settings = settings
        self.runs_dir = runs_dir
        self.static_dir = static_dir
        self.enable_mc_polling = enable_mc_polling
        self.tail_interval_s = tail_interval_s
        self.status_interval_s = status_interval_s
        self.entities_interval_s = entities_interval_s
        self.inv_adv_interval_s = inv_adv_interval_s
        self.voxel_min_interval_s = voxel_min_interval_s
        self.voxel_min_move_blocks = voxel_min_move_blocks
        self.upstream_poll_interval_s = upstream_poll_interval_s

        self.director_url = _resolve_optional_url(director_url, "MC_CAPTURE_DIRECTOR_URL")
        self.upstream_live_url = _resolve_optional_url(upstream_live_url, "MC_UPSTREAM_LIVE_URL")
        self.viewer_proxy_target = _resolve_optional_str(viewer_proxy_target, "MC_VIEWER_PROXY_TARGET")
        self.viewer_proxy_port = _resolve_optional_int(viewer_proxy_port, "MC_VIEWER_PROXY_PORT")
        self.viewer_proxy_host = (viewer_proxy_host or "127.0.0.1").strip() or "127.0.0.1"
        self._viewer_proxy_target_addr: tuple[str, int] | None = None
        if self.viewer_proxy_target and self.viewer_proxy_port is not None:
            if not (0 <= self.viewer_proxy_port <= 65535):
                raise ValueError(f"Invalid viewer_proxy_port: {self.viewer_proxy_port}")
            self._viewer_proxy_target_addr = _parse_host_port(self.viewer_proxy_target)
        elif self.viewer_proxy_target:
            _parse_host_port(self.viewer_proxy_target)

        self.token_provider = ReadOnlyTokenProvider(settings, pinned_bot=pinned_bot)
        self.mc_client = ReadOnlyMcClient(settings.mc_api_base_url, transport=mc_transport)
        self.viewer_detector = ViewerDetector(viewer_url, transport=viewer_transport)
        self._director_client: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(4.0, connect=2.0),
                transport=director_transport,
                trust_env=should_trust_env(self.director_url),
            )
            if self.director_url
            else None
        )
        self._upstream_client: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(4.0, connect=2.0),
                transport=upstream_transport,
                trust_env=should_trust_env(self.upstream_live_url),
            )
            if self.upstream_live_url
            else None
        )
        self.live_state = LiveStateStore()
        self.ws_clients: set[web.WebSocketResponse] = set()

        # Active `.events.jsonl` tailer state
        self.active_run_id: str | None = None
        self.active_run_path: Path | None = None
        self.active_byte_offset: int = 0
        self.active_incrementer: ReplayIncrementer | None = None
        self._last_scan_mono: float = -999.0

        # Non-locking MC API polling state
        self._active_token: str | None = None
        self._token_rejected: bool = False
        self._last_401_mono: float = -999.0
        self._events_primed: bool = False
        self._own_event_cursor: int = 0
        self._last_entities_mono: float = -999.0
        self._last_inv_adv_mono: float = -999.0
        self._last_voxel_mono: float = -999.0
        self._last_voxel_center: list[float] | None = None

        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self.shutting_down = False
        self._shutdown_event = asyncio.Event()

        # In-memory window capture relay (zero-disk LAN broadcast)
        self.capture_latest_jpeg: bytes | None = None
        self.capture_seq: int = 0
        self.capture_updated_mono: float = -999.0
        self.capture_meta: dict[str, Any] = {"width": 0, "height": 0, "fps": 0, "source": None}
        self.capture_camera_mode: str = "third_person"
        self._ws_director_available: bool = False
        self.capture_cond: asyncio.Condition = asyncio.Condition()
        self._capture_publisher_ws: web.WebSocketResponse | None = None
        self._upstream_capture_info: dict[str, Any] | None = None
        self._last_upstream_sig: tuple[Any, ...] | None = None

        # Built-in async TCP relay for Prismarine viewer (e.g. Tailscale/LAN forwarding)
        self._viewer_proxy_server: asyncio.AbstractServer | None = None
        self.viewer_proxy_bound_port: int | None = None
        self._viewer_proxy_tasks: set[asyncio.Task[None]] = set()
        self._viewer_proxy_writers: set[asyncio.StreamWriter] = set()

    async def _relay_tcp_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not self.shutting_down:
                data = await reader.read(65536)
                if not data:
                    with contextlib.suppress(Exception):
                        if writer.can_write_eof() and not writer.is_closing():
                            writer.write_eof()
                    break
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.CancelledError, RuntimeError):
            pass

    async def _handle_viewer_proxy_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._viewer_proxy_tasks.add(task)
        if self.shutting_down or self._viewer_proxy_target_addr is None:
            with contextlib.suppress(Exception):
                client_writer.close()
            if task is not None:
                self._viewer_proxy_tasks.discard(task)
            return

        remote_writer: asyncio.StreamWriter | None = None
        self._viewer_proxy_writers.add(client_writer)
        try:
            target_host, target_port = self._viewer_proxy_target_addr
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=5.0,
            )
            self._viewer_proxy_writers.add(remote_writer)
            if self.shutting_down:
                return
            await asyncio.gather(
                self._relay_tcp_stream(client_reader, remote_writer),
                self._relay_tcp_stream(remote_reader, client_writer),
                return_exceptions=True,
            )
        except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            for writer in (client_writer, remote_writer):
                if writer is not None:
                    self._viewer_proxy_writers.discard(writer)
                    with contextlib.suppress(Exception):
                        if not writer.is_closing():
                            writer.close()
            if task is not None:
                self._viewer_proxy_tasks.discard(task)

    async def start(self) -> None:
        # Refresh initial token metadata (without network blocking startup)
        bot_name, rec = self.token_provider.resolve()
        self.live_state.bot_username = bot_name
        self.live_state.has_cached_token = rec is not None

        # Prime active run from newest existing `.events.jsonl`
        await self._discover_or_tail(initial=True)

        if self._viewer_proxy_target_addr is not None:
            target_host, target_port = self._viewer_proxy_target_addr
            self._viewer_proxy_server = await asyncio.start_server(
                self._handle_viewer_proxy_client,
                host=self.viewer_proxy_host,
                port=self.viewer_proxy_port,
            )
            if self._viewer_proxy_server.sockets:
                self.viewer_proxy_bound_port = int(self._viewer_proxy_server.sockets[0].getsockname()[1])
            else:
                self.viewer_proxy_bound_port = self.viewer_proxy_port
            if self.viewer_detector.explicit_viewer_url is None and self.viewer_proxy_bound_port:
                self.viewer_detector.default_port = self.viewer_proxy_bound_port
                if self.viewer_proxy_host not in ("0.0.0.0", "::", ""):
                    self.viewer_detector.default_probe_host = self.viewer_proxy_host
                elif self.viewer_proxy_host == "::":
                    self.viewer_detector.default_probe_host = "::1"
                else:
                    self.viewer_detector.default_probe_host = "127.0.0.1"
            print(
                f"[mc-live] Prismarine TCP 转发已启动: "
                f"{self.viewer_proxy_host}:{self.viewer_proxy_bound_port} -> {target_host}:{target_port}",
                flush=True,
            )

        self._tasks.append(asyncio.create_task(self._tail_loop(), name="mc-live-tailer"))
        if self.enable_mc_polling:
            self._tasks.append(asyncio.create_task(self._mc_poll_loop(), name="mc-live-poller"))
        if self.upstream_live_url:
            self._last_upstream_sig = self._capture_status_sig(self.get_capture_info())
            self._tasks.append(asyncio.create_task(self._upstream_watch_loop(), name="mc-live-upstream-watch"))

    async def prepare_shutdown(self) -> None:
        self._closed = True
        self.shutting_down = True
        self._shutdown_event.set()
        self.capture_latest_jpeg = None
        self.capture_updated_mono = -999.0
        async with self.capture_cond:
            self.capture_cond.notify_all()
        if self._viewer_proxy_server is not None:
            with contextlib.suppress(Exception):
                self._viewer_proxy_server.close()
        for writer in list(self._viewer_proxy_writers):
            with contextlib.suppress(Exception):
                writer.close()
            transport = getattr(writer, "transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.abort()
        for proxy_task in list(self._viewer_proxy_tasks):
            proxy_task.cancel()
        if self._capture_publisher_ws is not None and not self._capture_publisher_ws.closed:
            with contextlib.suppress(Exception):
                await self._capture_publisher_ws.close()
        for ws in list(self.ws_clients):
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()

    async def stop(self) -> None:
        await self.prepare_shutdown()
        self._capture_publisher_ws = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._viewer_proxy_tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*list(self._viewer_proxy_tasks), return_exceptions=True),
                    timeout=2.0,
                )
        self._viewer_proxy_tasks.clear()
        self._viewer_proxy_writers.clear()
        if self._viewer_proxy_server is not None:
            with contextlib.suppress(Exception):
                self._viewer_proxy_server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._viewer_proxy_server.wait_closed(), timeout=2.0)
            self._viewer_proxy_server = None
        self.viewer_proxy_bound_port = None
        self.ws_clients.clear()
        await self.mc_client.close()
        if self._director_client is not None:
            await self._director_client.aclose()
        if self._upstream_client is not None:
            await self._upstream_client.aclose()

    @property
    def has_local_capture(self) -> bool:
        return self._capture_publisher_ws is not None or (
            self.capture_latest_jpeg is not None
            and (time.monotonic() - self.capture_updated_mono) <= 6.0
        )

    @property
    def is_proxying_capture(self) -> bool:
        return bool(self.upstream_live_url) and not self.has_local_capture

    @property
    def is_proxying_director(self) -> bool:
        return bool(self.upstream_live_url) and not self.director_url and not self.has_local_capture

    @staticmethod
    def _capture_status_sig(info: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(info.get("active")),
            info.get("camera_mode"),
            bool(info.get("director_available")),
            int(info.get("width") or 0),
            int(info.get("height") or 0),
            int(info.get("fps") or 0),
            info.get("source"),
        )

    def get_capture_info(self) -> dict[str, Any]:
        active = (
            self.capture_latest_jpeg is not None
            and (time.monotonic() - self.capture_updated_mono) <= 6.0
        )
        director_avail = bool(self.director_url) or bool(self._ws_director_available)
        return {
            "active": active,
            "stream_url": "/api/capture/stream.mjpg" if active else None,
            "snapshot_url": "/api/capture/frame.jpg" if active else None,
            "width": int(self.capture_meta.get("width") or 0) if active else 0,
            "height": int(self.capture_meta.get("height") or 0) if active else 0,
            "fps": int(self.capture_meta.get("fps") or 0) if active else 0,
            "source": self.capture_meta.get("source") if active else None,
            "seq": self.capture_seq,
            "camera_mode": self.capture_camera_mode,
            "director_available": director_avail,
        }

    def apply_capture_meta(self, payload: dict[str, Any]) -> None:
        """Apply metadata fields from a `/ws-capture` meta message without overwriting unset source."""
        for key in ("width", "height", "fps"):
            if key in payload and payload[key] is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self.capture_meta[key] = int(payload[key])
        if "source" in payload and payload["source"]:
            self.capture_meta["source"] = str(payload["source"])
        elif not self.capture_meta.get("source"):
            self.capture_meta["source"] = "browser"
        cam_mode = payload.get("camera_mode")
        if cam_mode in VALID_CAMERA_MODES:
            self.capture_camera_mode = cam_mode
        if "director_available" in payload and isinstance(payload["director_available"], bool):
            self._ws_director_available = payload["director_available"]

    def _normalize_upstream_capture(self, up_cap: dict[str, Any]) -> dict[str, Any]:
        active = bool(up_cap.get("active"))
        raw_mode = up_cap.get("camera_mode")
        mode = raw_mode if raw_mode in VALID_CAMERA_MODES else self.capture_camera_mode
        self.capture_camera_mode = mode
        merged = {
            "active": active,
            "stream_url": "/api/capture/stream.mjpg" if active else None,
            "snapshot_url": "/api/capture/frame.jpg" if active else None,
            "width": int(up_cap.get("width") or 0) if active else 0,
            "height": int(up_cap.get("height") or 0) if active else 0,
            "fps": int(up_cap.get("fps") or 0) if active else 0,
            "source": up_cap.get("source") if active else None,
            "seq": int(up_cap.get("seq") or 0),
            "camera_mode": mode,
            "director_available": bool(up_cap.get("director_available", False)),
        }
        self._upstream_capture_info = merged
        return merged

    async def fetch_upstream_capture_info(self) -> dict[str, Any] | None:
        if not self.upstream_live_url or self._upstream_client is None:
            return None
        try:
            resp = await self._upstream_client.get(
                f"{self.upstream_live_url}/api/info",
                timeout=httpx.Timeout(1.5, connect=0.8),
            )
            if resp.status_code != 200:
                self._upstream_capture_info = None
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            self._upstream_capture_info = None
            return None
        if not isinstance(data, dict) or not isinstance(data.get("capture_stream"), dict):
            self._upstream_capture_info = None
            return None
        return self._normalize_upstream_capture(data["capture_stream"])

    async def _upstream_watch_loop(self) -> None:
        while not self._closed:
            try:
                if self.is_proxying_capture:
                    info_now = await self.fetch_upstream_capture_info()
                    effective = info_now if info_now is not None else self.get_capture_info()
                    sig = self._capture_status_sig(effective)
                    if sig != self._last_upstream_sig:
                        self._last_upstream_sig = sig
                        await self.broadcast({"type": "capture_status", "capture_stream": effective})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.upstream_poll_interval_s)

    async def update_capture_frame(self, data: bytes, meta: dict[str, Any] | None = None) -> bool:
        if not isinstance(data, (bytes, bytearray)) or not (4 <= len(data) <= 3 * 1024 * 1024):
            return False
        is_jpeg = data[:2] == b"\xff\xd8"
        is_webp = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        if not (is_jpeg or is_webp):
            return False

        was_active = self.get_capture_info()["active"]
        self.capture_latest_jpeg = bytes(data)
        self.capture_seq += 1
        self.capture_updated_mono = time.monotonic()
        if meta:
            for key in ("width", "height", "fps", "source"):
                val = meta.get(key)
                if val is not None:
                    self.capture_meta[key] = int(val) if key in ("width", "height", "fps") else val

        async with self.capture_cond:
            self.capture_cond.notify_all()

        if not was_active:
            await self.broadcast({"type": "capture_status", "capture_stream": self.get_capture_info()})
        return True

    async def clear_capture_stream(self) -> None:
        had_frame = self.capture_latest_jpeg is not None
        had_ws_dir = self._ws_director_available
        self.capture_latest_jpeg = None
        self.capture_updated_mono = -999.0
        self._ws_director_available = False
        async with self.capture_cond:
            self.capture_cond.notify_all()
        if had_frame or had_ws_dir:
            await self.broadcast({"type": "capture_status", "capture_stream": self.get_capture_info()})

    async def build_info(self, request: web.Request | None = None) -> dict[str, Any]:
        bot_name, rec = self.token_provider.resolve()
        if self.live_state.bot_username and self.live_state.online:
            display_bot = self.live_state.bot_username
        else:
            display_bot = (
                bot_name
                or self.live_state.bot_username
                or ((self.active_incrementer.meta.get("bot")) if self.active_incrementer else None)
            )
        self.live_state.has_cached_token = rec is not None

        req_host = request.host if request is not None else None
        req_scheme = request.scheme if request is not None else "http"
        viewer_info = await self.viewer_detector.probe(request_host=req_host, request_scheme=req_scheme)

        if self.is_proxying_capture:
            up_cap = await self.fetch_upstream_capture_info()
            capture_info = up_cap if up_cap is not None else self.get_capture_info()
        else:
            capture_info = self.get_capture_info()

        return {
            "runtime": runtime_info(),
            "bot_username": display_bot,
            "mc_api_base_url": self.settings.mc_api_base_url,
            "has_cached_token": bool(rec is not None),
            "online": bool(self.live_state.online),
            "ready": bool(self.live_state.ready),
            "active_run_id": self.active_run_id,
            "prismarine_viewer": viewer_info,
            "capture_stream": capture_info,
        }

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.ws_clients:
            return
        raw = json.dumps(payload, ensure_ascii=False)
        stale: list[web.WebSocketResponse] = []
        for ws in list(self.ws_clients):
            if ws.closed:
                stale.append(ws)
                continue
            try:
                await ws.send_str(raw)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.ws_clients.discard(ws)

    # ---- Live `.events.jsonl` Tailer ----

    async def _tail_loop(self) -> None:
        while not self._closed:
            try:
                if (
                    self._capture_publisher_ws is None
                    and self.capture_latest_jpeg is not None
                    and (time.monotonic() - self.capture_updated_mono) > 6.0
                ):
                    await self.clear_capture_stream()
                await self._discover_or_tail(initial=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.tail_interval_s)

    def _find_newest_events_file(self) -> tuple[str, Path] | None:
        if not self.runs_dir.is_dir():
            return None
        base = self.runs_dir.resolve()
        best_key: tuple[int, str] | None = None
        best_pair: tuple[str, Path] | None = None
        for p in base.rglob("*.events.jsonl"):
            if p.is_symlink():
                continue
            try:
                resolved = p.resolve()
                if not resolved.is_relative_to(base) or not resolved.is_file():
                    continue
                mtime_ns = resolved.stat().st_mtime_ns
            except OSError:
                continue
            rel_id = resolved.relative_to(base).as_posix()
            key = (mtime_ns, rel_id)
            if best_key is None or key > best_key:
                best_key = key
                best_pair = (rel_id, resolved)
        return best_pair

    @staticmethod
    def _read_complete_lines_from(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
        try:
            size = path.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                return [], offset
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
        except OSError:
            return [], offset

        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return [], offset

        complete = chunk[: last_nl + 1]
        new_offset = offset + len(complete)
        events: list[dict[str, Any]] = []
        for raw_line in complete.decode("utf-8", errors="replace").splitlines():
            s = raw_line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
        return events, new_offset

    async def _discover_or_tail(self, *, initial: bool) -> None:
        now = time.monotonic()
        if initial or (now - self._last_scan_mono) >= max(0.6, self.tail_interval_s * 3):
            self._last_scan_mono = now
            newest = self._find_newest_events_file()
            if newest is not None and newest[0] != self.active_run_id:
                rel_id, path = newest
                report_obj, summary_obj = _load_companion_files(path)
                inc = ReplayIncrementer(
                    rel_id,
                    coalesce_thought_window_s=0.6 if initial else 0.2,
                    companion_report=report_obj,
                    companion_summary=summary_obj,
                )
                evs, new_off = self._read_complete_lines_from(path, 0)
                inc.feed_events(evs, coalesce_window_s=0.6 if initial else 0.2)

                self.active_run_id = rel_id
                self.active_run_path = path
                self.active_byte_offset = new_off
                self.active_incrementer = inc

                bundle = inc.to_bundle()
                self.live_state.seed_from_bundle_if_empty(bundle)
                if not initial:
                    # Also harvest blocks/markers from the newly switched run
                    added_b, rem_b = self.live_state.voxel_cache.put_blocks(
                        bundle.get("blocks") or [],
                        anchor_pos=_norm_pos_float(self.live_state.status.get("pos")),
                    )
                    await self.broadcast({"type": "run_switched", "run": bundle})
                    if added_b or rem_b:
                        await self.broadcast({"type": "blocks_patch", "blocks": added_b, "removed": rem_b})
                return

        if self.active_run_path is None or self.active_incrementer is None or self.active_run_id is None:
            return

        new_evs, new_off = self._read_complete_lines_from(self.active_run_path, self.active_byte_offset)
        if not new_evs:
            return
        self.active_byte_offset = new_off
        new_frames = self.active_incrementer.feed_events(new_evs, coalesce_window_s=0.2)
        for frame in new_frames:
            await self.broadcast(
                {
                    "type": "harness_frame",
                    "run_id": self.active_run_id,
                    "frame": frame,
                }
            )
            added_b, rem_b = self.live_state.absorb_harness_patch(frame.get("state_patch") or {})
            if added_b or rem_b:
                await self.broadcast({"type": "blocks_patch", "blocks": added_b, "removed": rem_b})

    # ---- Non-Locking MC API Poller ----

    async def _mc_poll_loop(self) -> None:
        while not self._closed:
            try:
                await self._poll_mc_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.live_state.online = False
                self.live_state.ready = False
            await asyncio.sleep(self.status_interval_s)

    async def _poll_mc_events(self, token: str) -> None:
        # Bound each poll so a large backlog cannot starve telemetry. If priming
        # spans multiple polls, keep discarding history until the final page.
        limit = 100
        for _ in range(10):
            since = self._own_event_cursor
            result = await self.mc_client.observe(
                "GET", "/events", token=token, params={"since": since, "limit": limit},
            )
            page = result.get("events") or []
            fresh = [e for e in page if isinstance(e, dict) and int(e.get("seq") or 0) > since]
            has_more = result.get("has_more")
            if not isinstance(has_more, bool):
                has_more = len(page) >= limit
            page_max = max((int(e["seq"]) for e in fresh), default=since)
            if not has_more:
                page_max = max(page_max, int(result.get("last_seq") or 0))
            self._own_event_cursor = max(since, page_max)
            if self._events_primed and fresh:
                markers = self.live_state.apply_mc_events(fresh)
                await self.broadcast({"type": "mc_events", "events": fresh, "markers": markers})
            if not has_more:
                self._events_primed = True
                return
            if self._own_event_cursor <= since:
                return

    async def _poll_mc_once(self) -> None:
        bot_name, rec = self.token_provider.resolve()
        if bot_name and not self.live_state.online:
            self.live_state.bot_username = bot_name
        if rec is None or not rec.token:
            self.live_state.has_cached_token = False
            self.live_state.online = False
            self.live_state.ready = False
            self._active_token = None
            return

        self.live_state.has_cached_token = True
        if rec.token != self._active_token:
            self._active_token = rec.token
            self._token_rejected = False
            self._events_primed = False
            self._own_event_cursor = 0
            self._last_voxel_center = None

        now = time.monotonic()
        if self._token_rejected and (now - self._last_401_mono) < max(5.0, self.status_interval_s * 5):
            return

        token = rec.token
        try:
            st = await self.mc_client.observe("GET", "/status", token=token)
        except AuthError:
            # CRITICAL REDLINE: NEVER re-login on 401!
            self._token_rejected = True
            self._last_401_mono = now
            self.live_state.online = False
            self.live_state.ready = False
            self.live_state.auth_note = "token_unauthorized_readonly"
            return
        except (ApiError, httpx.HTTPError):
            self.live_state.online = False
            self.live_state.ready = False
            return

        self._token_rejected = False
        self.live_state.auth_note = None
        pos_point = self.live_state.apply_polled_status(st)

        # Drain historical pages before broadcasting live events on first attach.
        try:
            await self._poll_mc_events(token)
        except (ApiError, httpx.HTTPError):
            pass

        # ~1.5s entities scan when ready
        if self.live_state.ready and (now - self._last_entities_mono) >= self.entities_interval_s:
            self._last_entities_mono = now
            try:
                ent_res = await self.mc_client.observe(
                    "POST",
                    "/get_entities",
                    token=token,
                    json_body={"radius": 32},
                )
                if isinstance(ent_res.get("entities"), list):
                    self.live_state.entities = ent_res["entities"]
            except (ApiError, httpx.HTTPError):
                pass

        await self.broadcast(
            {
                "type": "telemetry",
                "ts": round(time.time(), 3),
                "status": dict(self.live_state.status),
                "entities": list(self.live_state.entities),
                "pos_point": pos_point,
            }
        )

        if not self.live_state.ready:
            return

        # ~5.0s inventory + advancements merge
        now = time.monotonic()
        if (now - self._last_inv_adv_mono) >= self.inv_adv_interval_s:
            self._last_inv_adv_mono = now
            inv_updated = False
            try:
                inv_res = await self.mc_client.observe("GET", "/inventory", token=token)
                self.live_state.inventory = inv_res
                inv_updated = True
            except (ApiError, httpx.HTTPError):
                pass

            try:
                adv_res = await self.mc_client.observe(
                    "POST",
                    "/advancements",
                    token=token,
                    json_body={"include_recipes": False},
                )
                self.live_state.advancements = merge_advancements_with_catalog(adv_res)
                inv_updated = True
            except (ApiError, httpx.HTTPError):
                pass

            if inv_updated:
                await self.broadcast(
                    {
                        "type": "inventory_advancements",
                        "inventory": dict(self.live_state.inventory),
                        "advancements": dict(self.live_state.advancements),
                    }
                )

        # Low-frequency semantic voxel sampling: online + ready + moved >= 4 blocks + >= 3s
        cur_pos = _norm_pos_float(self.live_state.status.get("pos"))
        if cur_pos is not None and (now - self._last_voxel_mono) >= self.voxel_min_interval_s:
            moved_far = (
                self._last_voxel_center is None
                or _dist3(cur_pos, self._last_voxel_center) >= self.voxel_min_move_blocks
            )
            if moved_far:
                self._last_voxel_mono = now
                self._last_voxel_center = list(cur_pos)
                sampled_blocks: list[dict[str, Any]] = []
                for types_batch in (VOXEL_QUERY_TERRAIN, VOXEL_QUERY_FEATURES):
                    try:
                        fb_res = await self.mc_client.observe(
                            "POST",
                            "/find_blocks",
                            token=token,
                            json_body={"types": types_batch, "radius": 14, "limit": 60},
                        )
                        sampled_blocks.extend(_extract_blocks_from_find_result(fb_res))
                    except (ApiError, httpx.HTTPError):
                        continue

                if sampled_blocks:
                    added_b, rem_b = self.live_state.voxel_cache.put_blocks(
                        sampled_blocks,
                        anchor_pos=cur_pos,
                    )
                    cur_dim = str(self.live_state.status.get("dimension") or "overworld")
                    for b in added_b:
                        if _is_station_block(b["name"]):
                            self.live_state._add_marker(
                                elapsed=self.live_state.elapsed(),
                                kind="station",
                                label=_station_label(b["name"]),
                                pos=_norm_pos_float(b["pos"]),
                                dimension=cur_dim,
                            )
                    if added_b or rem_b:
                        await self.broadcast(
                            {
                                "type": "blocks_patch",
                                "blocks": added_b,
                                "removed": rem_b,
                            }
                        )


_COORD_KEY = web.AppKey("coordinator", LiveCoordinator)


def create_app(
    *,
    settings: Settings | None = None,
    dotenv_path: str | Path | None = None,
    bot_username: str | None = None,
    runs_dir: str | Path = "runs",
    viewer_url: str | None = None,
    director_url: str | None = None,
    upstream_live_url: str | None = None,
    viewer_proxy_target: str | None = None,
    viewer_proxy_port: int | None = None,
    viewer_proxy_host: str | None = None,
    static_dir: Path = STATIC_DIR,
    mc_transport: httpx.AsyncBaseTransport | None = None,
    viewer_transport: httpx.AsyncBaseTransport | None = None,
    director_transport: httpx.AsyncBaseTransport | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    enable_mc_polling: bool = True,
    tail_interval_s: float = 0.2,
    status_interval_s: float = 1.0,
    entities_interval_s: float = 1.5,
    inv_adv_interval_s: float = 5.0,
    voxel_min_interval_s: float = 3.0,
    voxel_min_move_blocks: float = 4.0,
    upstream_poll_interval_s: float = 1.0,
) -> web.Application:
    """Create the `mc-live` aiohttp web application."""
    resolved_settings = settings or Settings.load(dotenv_path=dotenv_path)
    runs_path = Path(runs_dir)

    dotenv_vars: dict[str, str] | None = None
    if dotenv_path is not None:
        dotenv_vars = parse_dotenv(Path(dotenv_path))
    elif settings is None:
        dotenv_vars = parse_dotenv(Path.cwd() / ".env")

    resolved_director = _resolve_optional_url(director_url, "MC_CAPTURE_DIRECTOR_URL", dotenv_vars)
    resolved_upstream = _resolve_optional_url(upstream_live_url, "MC_UPSTREAM_LIVE_URL", dotenv_vars)
    resolved_proxy_target = _resolve_optional_str(viewer_proxy_target, "MC_VIEWER_PROXY_TARGET", dotenv_vars)
    resolved_proxy_port = _resolve_optional_int(viewer_proxy_port, "MC_VIEWER_PROXY_PORT", dotenv_vars)
    resolved_proxy_host = _resolve_optional_str(viewer_proxy_host, "MC_VIEWER_PROXY_HOST", dotenv_vars) or "127.0.0.1"

    coordinator = LiveCoordinator(
        settings=resolved_settings,
        runs_dir=runs_path,
        pinned_bot=bot_username,
        viewer_url=viewer_url,
        director_url="" if director_url == "" else resolved_director,
        upstream_live_url="" if upstream_live_url == "" else resolved_upstream,
        viewer_proxy_target="" if viewer_proxy_target == "" else resolved_proxy_target,
        viewer_proxy_port=resolved_proxy_port,
        viewer_proxy_host=resolved_proxy_host,
        static_dir=static_dir,
        mc_transport=mc_transport,
        viewer_transport=viewer_transport,
        director_transport=director_transport,
        upstream_transport=upstream_transport,
        enable_mc_polling=enable_mc_polling,
        tail_interval_s=tail_interval_s,
        status_interval_s=status_interval_s,
        entities_interval_s=entities_interval_s,
        inv_adv_interval_s=inv_adv_interval_s,
        voxel_min_interval_s=voxel_min_interval_s,
        voxel_min_move_blocks=voxel_min_move_blocks,
        upstream_poll_interval_s=upstream_poll_interval_s,
    )

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app[_COORD_KEY] = coordinator

    async def _on_startup(app_inst: web.Application) -> None:
        await app_inst[_COORD_KEY].start()

    async def _on_shutdown(app_inst: web.Application) -> None:
        await app_inst[_COORD_KEY].prepare_shutdown()

    async def _on_cleanup(app_inst: web.Application) -> None:
        await app_inst[_COORD_KEY].stop()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    app.on_cleanup.append(_on_cleanup)

    async def handle_index(request: web.Request) -> web.StreamResponse:
        coord = request.app[_COORD_KEY]
        index_path = coord.static_dir / "index.html"
        if index_path.is_file():
            return web.FileResponse(index_path, headers={"Cache-Control": "no-cache"})
        return web.Response(
            text="<!doctype html><meta charset='utf-8'><title>mc-live</title><p>Static UI initializing...</p>",
            content_type="text/html",
        )

    async def handle_static(request: web.Request) -> web.StreamResponse:
        coord = request.app[_COORD_KEY]
        name = request.match_info.get("name", "")
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise web.HTTPBadRequest(text="Invalid static asset path")
        base = coord.static_dir.resolve()
        candidate = (base / name).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise web.HTTPNotFound(text="Static asset not found")
        ctype, _ = mimetypes.guess_type(str(candidate))
        headers = {"Cache-Control": "no-cache"}
        if ctype:
            headers["Content-Type"] = ctype
        return web.FileResponse(candidate, headers=headers)

    async def handle_info(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        info = await coord.build_info(request)
        return web.json_response(info)

    async def handle_telemetry_pos(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        st = coord.live_state.status
        pos = _norm_pos_float(st.get("pos"))
        yaw_raw = st.get("yaw")
        pitch_raw = st.get("pitch")
        yaw = float(yaw_raw) if isinstance(yaw_raw, (int, float)) else None
        pitch = float(pitch_raw) if isinstance(pitch_raw, (int, float)) else None
        dim = str(st.get("dimension")) if st.get("dimension") else None
        return web.json_response(
            {
                "ok": bool(pos is not None),
                "pos": pos,
                "yaw": yaw,
                "pitch": pitch,
                "dimension": dim,
                "online": bool(coord.live_state.online),
                "ready": bool(coord.live_state.ready),
            }
        )

    async def handle_runs(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        runs = await asyncio.to_thread(list_runs, coord.runs_dir)
        return web.json_response({"runs": runs})

    async def handle_replay(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        rel_id = request.query.get("id", "")
        try:
            resolve_safe_run_path(coord.runs_dir, rel_id)
        except ValueError as exc:
            return web.json_response(
                {"ok": False, "error": "invalid_run_id", "message": str(exc)},
                status=400,
            )

        try:
            bundle = await asyncio.to_thread(parse_replay_bundle, coord.runs_dir, rel_id)
        except ValueError as exc:
            return web.json_response(
                {"ok": False, "error": "invalid_run_id", "message": str(exc)},
                status=400,
            )
        except FileNotFoundError:
            return web.json_response(
                {"ok": False, "error": "not_found", "message": f"Run log not found: {rel_id}"},
                status=404,
            )
        return web.json_response(bundle)

    async def handle_ws(request: web.Request) -> web.WebSocketResponse:
        coord = request.app[_COORD_KEY]
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)

        info = await coord.build_info(request)
        active_bundle = coord.active_incrementer.to_bundle() if coord.active_incrementer is not None else None
        snapshot_msg = {
            "type": "snapshot",
            "info": info,
            "active_run": active_bundle,
            "live": coord.live_state.to_snapshot(),
        }
        await ws.send_str(json.dumps(snapshot_msg, ensure_ascii=False))
        coord.ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data.strip() == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            coord.ws_clients.discard(ws)
        return ws

    def _capture_content_type(frame: bytes) -> str:
        if len(frame) >= 12 and frame[:4] == b"RIFF" and frame[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    async def handle_ws_capture(request: web.Request) -> web.WebSocketResponse:
        coord = request.app[_COORD_KEY]
        ws = web.WebSocketResponse(heartbeat=25.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)

        prev_ws = coord._capture_publisher_ws
        coord._capture_publisher_ws = ws
        if prev_ws is not None and prev_ws is not ws and not prev_ws.closed:
            with contextlib.suppress(Exception):
                await prev_ws.close()

        try:
            async for msg in ws:
                if coord._closed:
                    break
                if msg.type == WSMsgType.TEXT:
                    txt = msg.data.strip()
                    if txt == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                        continue
                    try:
                        payload = json.loads(txt)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    mtype = payload.get("type")
                    if mtype == "stop":
                        break
                    if mtype == "meta":
                        prev_info = coord.get_capture_info()
                        coord.apply_capture_meta(payload)
                        info_now = coord.get_capture_info()
                        if info_now["active"] or info_now != prev_info:
                            await coord.broadcast({"type": "capture_status", "capture_stream": info_now})
                elif msg.type == WSMsgType.BINARY:
                    src = coord.capture_meta.get("source") or "browser"
                    await coord.update_capture_frame(msg.data, {"source": src})
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            if coord._capture_publisher_ws is ws:
                coord._capture_publisher_ws = None
                await coord.clear_capture_stream()
        return ws

    async def handle_capture_post_frame(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        max_bytes = 3 * 1024 * 1024
        if request.content_length is not None and request.content_length > max_bytes:
            return web.json_response(
                {"ok": False, "error": "payload_too_large"},
                status=413,
            )
        body = await request.read()
        if len(body) > max_bytes:
            return web.json_response(
                {"ok": False, "error": "payload_too_large"},
                status=413,
            )
        meta: dict[str, Any] = {"source": request.query.get("source") or "http_push"}
        for key in ("width", "height", "fps"):
            raw_v = request.query.get(key)
            if raw_v is not None:
                with contextlib.suppress(ValueError):
                    meta[key] = int(raw_v)
        ok = await coord.update_capture_frame(body, meta)
        if not ok:
            return web.json_response(
                {"ok": False, "error": "invalid_image"},
                status=400,
            )
        return web.json_response({"ok": True, "seq": coord.capture_seq})

    async def handle_capture_get_camera_mode(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        if coord.is_proxying_director and coord._upstream_client is not None and coord.upstream_live_url:
            try:
                resp = await coord._upstream_client.get(
                    f"{coord.upstream_live_url}/api/capture/camera-mode",
                    timeout=httpx.Timeout(2.5, connect=1.0),
                )
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"Upstream camera-mode request failed: {exc}",
                        "mode": coord.capture_camera_mode,
                        "director_available": False,
                    },
                    status=502,
                )
            if not isinstance(data, dict) or resp.status_code >= 500:
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"Upstream returned HTTP {resp.status_code}",
                        "mode": coord.capture_camera_mode,
                        "director_available": False,
                    },
                    status=502,
                )
            mode_val = data.get("mode")
            if mode_val in VALID_CAMERA_MODES:
                coord.capture_camera_mode = mode_val
            return web.json_response(data, status=resp.status_code)

        info_now = coord.get_capture_info()
        return web.json_response(
            {
                "ok": True,
                "mode": info_now["camera_mode"],
                "director_available": info_now["director_available"],
            }
        )

    async def handle_capture_post_camera_mode(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "JSON body must be an object"}, status=400)
        mode = payload.get("mode")
        if mode not in VALID_CAMERA_MODES:
            return web.json_response(
                {"ok": False, "error": "Invalid mode; expected 'first_person' or 'third_person'"},
                status=400,
            )

        if coord.is_proxying_director and coord._upstream_client is not None and coord.upstream_live_url:
            try:
                resp = await coord._upstream_client.post(
                    f"{coord.upstream_live_url}/api/capture/camera-mode",
                    json={"mode": mode},
                    timeout=httpx.Timeout(4.0, connect=1.5),
                )
                up_data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return web.json_response(
                    {"ok": False, "error": f"Upstream camera-mode request failed: {exc}"},
                    status=502,
                )
            if not isinstance(up_data, dict):
                return web.json_response(
                    {"ok": False, "error": "Invalid response payload from upstream"},
                    status=502,
                )
            if resp.status_code == 200 and up_data.get("ok") is True:
                new_mode = up_data.get("mode") if up_data.get("mode") in VALID_CAMERA_MODES else mode
                dir_avail = bool(up_data.get("director_available", True))
                coord.capture_camera_mode = new_mode
                if coord._upstream_capture_info is not None:
                    coord._upstream_capture_info["camera_mode"] = new_mode
                    coord._upstream_capture_info["director_available"] = dir_avail
                    cap_payload = dict(coord._upstream_capture_info)
                else:
                    cap_payload = coord.get_capture_info()
                    cap_payload["camera_mode"] = new_mode
                    cap_payload["director_available"] = dir_avail
                coord._last_upstream_sig = coord._capture_status_sig(cap_payload)
                await coord.broadcast({"type": "capture_status", "capture_stream": cap_payload})
                return web.json_response(up_data, status=200)
            if resp.status_code in (400, 503):
                return web.json_response(up_data, status=resp.status_code)
            return web.json_response(
                {"ok": False, "error": up_data.get("error") or f"Upstream returned HTTP {resp.status_code}"},
                status=502,
            )

        if coord.director_url and coord._director_client is not None:
            try:
                resp = await coord._director_client.post(
                    f"{coord.director_url}/mode",
                    json={"mode": mode},
                    timeout=httpx.Timeout(4.0, connect=1.5),
                )
            except httpx.HTTPError as exc:
                return web.json_response(
                    {"ok": False, "error": f"Capture director request failed: {exc}"},
                    status=502,
                )
            if resp.status_code >= 400:
                return web.json_response(
                    {"ok": False, "error": f"Capture director returned HTTP {resp.status_code}"},
                    status=502,
                )
            coord.capture_camera_mode = mode
            info_now = coord.get_capture_info()
            await coord.broadcast({"type": "capture_status", "capture_stream": info_now})
            return web.json_response({"ok": True, "mode": mode, "director_available": True})

        return web.json_response(
            {"ok": False, "error": "Capture director is not configured or unavailable"},
            status=503,
        )

    async def handle_capture_snapshot(request: web.Request) -> web.Response:
        coord = request.app[_COORD_KEY]
        if coord.is_proxying_capture and coord._upstream_client is not None and coord.upstream_live_url:
            try:
                resp = await coord._upstream_client.get(
                    f"{coord.upstream_live_url}/api/capture/frame.jpg",
                    timeout=httpx.Timeout(4.0, connect=1.5),
                )
            except httpx.HTTPError as exc:
                return web.json_response(
                    {"ok": False, "error": f"Upstream frame request failed: {exc}"},
                    status=502,
                )
            if resp.status_code == 404:
                raise web.HTTPNotFound(text="No active capture frame")
            if resp.status_code != 200:
                return web.json_response(
                    {"ok": False, "error": f"Upstream frame returned HTTP {resp.status_code}"},
                    status=502,
                )
            return web.Response(
                body=resp.content,
                content_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "no-store"},
            )

        info_now = coord.get_capture_info()
        frame = coord.capture_latest_jpeg
        if not info_now["active"] or not frame:
            raise web.HTTPNotFound(text="No active capture frame")
        return web.Response(
            body=frame,
            content_type=_capture_content_type(frame),
            headers={"Cache-Control": "no-store"},
        )

    async def _proxy_upstream_mjpeg(coord: LiveCoordinator, request: web.Request) -> web.StreamResponse:
        if coord.shutting_down:
            raise web.HTTPServiceUnavailable(text="Server shutting down")
        client = coord._upstream_client
        upstream = coord.upstream_live_url
        assert client is not None and upstream is not None
        try:
            async with client.stream(
                "GET",
                f"{upstream}/api/capture/stream.mjpg",
                timeout=httpx.Timeout(None, connect=3.0),
            ) as up_resp:
                if up_resp.status_code == 404:
                    raise web.HTTPNotFound(text="No active capture stream")
                if up_resp.status_code != 200:
                    return web.json_response(
                        {"ok": False, "error": f"Upstream stream returned HTTP {up_resp.status_code}"},
                        status=502,
                    )

                ctype = up_resp.headers.get(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=mcliveframe",
                )
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": ctype,
                        "Cache-Control": "no-store, no-cache, must-revalidate",
                        "Pragma": "no-cache",
                    },
                )
                await resp.prepare(request)

                chunk_iter = up_resp.aiter_raw().__aiter__()
                shutdown_task = asyncio.create_task(coord._shutdown_event.wait())
                try:
                    while not coord.shutting_down:
                        next_task = asyncio.create_task(chunk_iter.__anext__())
                        done, _ = await asyncio.wait(
                            {next_task, shutdown_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if shutdown_task in done or coord.shutting_down:
                            if not next_task.done():
                                next_task.cancel()
                                with contextlib.suppress(BaseException):
                                    await next_task
                            else:
                                with contextlib.suppress(BaseException):
                                    next_task.exception()
                            break
                        try:
                            chunk = next_task.result()
                        except (StopAsyncIteration, httpx.HTTPError):
                            break
                        if chunk:
                            await resp.write(chunk)
                except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                    pass
                finally:
                    if not shutdown_task.done():
                        shutdown_task.cancel()
                        with contextlib.suppress(BaseException):
                            await shutdown_task
                return resp
        except web.HTTPException:
            raise
        except httpx.HTTPError as exc:
            return web.json_response(
                {"ok": False, "error": f"Upstream stream request failed: {exc}"},
                status=502,
            )

    async def handle_capture_mjpeg(request: web.Request) -> web.StreamResponse:
        coord = request.app[_COORD_KEY]
        if coord.is_proxying_capture and coord._upstream_client is not None and coord.upstream_live_url:
            return await _proxy_upstream_mjpeg(coord, request)

        if not coord.get_capture_info()["active"] or not coord.capture_latest_jpeg:
            raise web.HTTPNotFound(text="No active capture stream")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=mcliveframe",
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        await resp.prepare(request)

        last_sent_seq = 0
        try:
            while not coord._closed and not coord.shutting_down:
                info_now = coord.get_capture_info()
                frame = coord.capture_latest_jpeg
                seq_now = coord.capture_seq
                if not info_now["active"] or not frame:
                    break

                if seq_now != last_sent_seq:
                    last_sent_seq = seq_now
                    ctype = _capture_content_type(frame)
                    header = (
                        f"--mcliveframe\r\nContent-Type: {ctype}\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii")
                    await resp.write(header + frame + b"\r\n")

                async with coord.capture_cond:
                    if coord._closed or coord.shutting_down or not coord.get_capture_info()["active"]:
                        break
                    if coord.capture_seq == last_sent_seq:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(coord.capture_cond.wait(), timeout=2.0)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        return resp

    app.router.add_get("/", handle_index)
    app.router.add_get("/static/{name:.+}", handle_static)
    app.router.add_get("/api/info", handle_info)
    app.router.add_get("/api/telemetry-pos", handle_telemetry_pos)
    app.router.add_get("/api/runs", handle_runs)
    app.router.add_get("/api/replay", handle_replay)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/ws-capture", handle_ws_capture)
    app.router.add_post("/api/capture/frame", handle_capture_post_frame)
    app.router.add_get("/api/capture/camera-mode", handle_capture_get_camera_mode)
    app.router.add_post("/api/capture/camera-mode", handle_capture_post_camera_mode)
    app.router.add_get("/api/capture/frame.jpg", handle_capture_snapshot)
    app.router.add_get("/api/capture/stream.mjpg", handle_capture_mjpeg)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-live",
        description="Minecraft Bot 零侵入只读实时监看与远征历史回放服务",
    )
    parser.add_argument("--env-file", default=None, help="指定配置文件（如 .env.local 连本地服，默认 .env）")
    parser.add_argument("--bot", default=None, help="指定监看的 Bot 角色名（默认取配置或最新缓存 Token）")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP/WS 监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="HTTP/WS 监听端口（默认 8765）")
    parser.add_argument("--runs-dir", default="runs", help="轨迹日志目录（默认 runs）")
    parser.add_argument("--viewer-url", default=None, help="可选 Prismarine 旁观服务地址（默认自动探测 :8766）")
    parser.add_argument(
        "--director-url",
        default=os.environ.get("MC_CAPTURE_DIRECTOR_URL") or None,
        help="可选实景导播控制地址（默认读取 MC_CAPTURE_DIRECTOR_URL，例如 http://127.0.0.1:8767）",
    )
    parser.add_argument(
        "--upstream-live-url",
        default=os.environ.get("MC_UPSTREAM_LIVE_URL") or None,
        help="可选上游 mc-live 代理地址（默认读取 MC_UPSTREAM_LIVE_URL，转发实景流与视角切换）",
    )
    parser.add_argument(
        "--viewer-proxy-target",
        default=os.environ.get("MC_VIEWER_PROXY_TARGET") or None,
        help="可选 Prismarine TCP 转发目标 HOST:PORT（默认读取 MC_VIEWER_PROXY_TARGET，例如 100.93.211.71:8766）",
    )
    parser.add_argument(
        "--viewer-proxy-port",
        type=int,
        default=_resolve_optional_int(None, "MC_VIEWER_PROXY_PORT"),
        help="可选 Prismarine TCP 转发本地监听端口（默认读取 MC_VIEWER_PROXY_PORT，例如 8776）",
    )
    parser.add_argument(
        "--viewer-proxy-host",
        default=os.environ.get("MC_VIEWER_PROXY_HOST") or None,
        help="可选 Prismarine TCP 转发监听地址（默认读取 MC_VIEWER_PROXY_HOST，否则跟随 --host）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.load(dotenv_path=args.env_file)
    runs_dir = Path(args.runs_dir)
    env_file = Path(args.env_file) if args.env_file else (Path.cwd() / ".env")
    dotenv_vars = parse_dotenv(env_file) if env_file.is_file() else {}
    viewer_proxy_host = args.viewer_proxy_host or dotenv_vars.get("MC_VIEWER_PROXY_HOST") or args.host
    app = create_app(
        settings=settings,
        dotenv_path=env_file if env_file.is_file() else None,
        bot_username=args.bot,
        runs_dir=runs_dir,
        viewer_url=args.viewer_url,
        director_url=args.director_url,
        upstream_live_url=args.upstream_live_url,
        viewer_proxy_target=args.viewer_proxy_target,
        viewer_proxy_port=args.viewer_proxy_port,
        viewer_proxy_host=viewer_proxy_host,
    )

    print(
        f"[mc-live] 启动只读监看与回放面板: http://{args.host}:{args.port} | "
        f"MC={settings.mc_api_base_url} | runs={runs_dir.resolve()}",
        flush=True,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
