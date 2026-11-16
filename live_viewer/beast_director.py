#!/usr/bin/env python3
"""Headless/Desktop Minecraft 1.21.11 camera director & MJPEG-to-WS publisher for `mc-live`.

Responsibilities:
1. Serves a local HTTP control API (`GET /health`, `POST /mode`, `GET /mode`) on `--host/--port`
   (default `127.0.0.1:8767`) for switching between `first_person` and `third_person`.
2. Maintains an asyncio Source RCON loop against the local Leaves 1.21.11 server (`127.0.0.1:25575`)
   to keep `BeastCam` in `spectator` mode and seamlessly switch/track `BeastBot_01`:
   - `first_person`: `/spectate <bot> <cam>`
   - `third_person`: `/execute as <cam> run spectate` + 6 Hz relative chase `/tp` behind bot
3. Spawns `ffmpeg` (`x11grab` on Linux/WSL2 or `gdigrab`/`ddagrab` on Windows), parses raw MJPEG
   JPEG frames (`0xFFD8`..`0xFFD9`) from stdout, and streams them with `meta` updates to
   `mc-live` (`ws://127.0.0.1:8765/ws-capture`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import platform
import shlex
import shutil
import struct
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web

VALID_CAMERA_MODES = ("first_person", "third_person")

_RCON_SERVERDATA_RESPONSE_VALUE = 0
_RCON_SERVERDATA_EXECCOMMAND = 2
_RCON_SERVERDATA_AUTH_RESPONSE = 2
_RCON_SERVERDATA_AUTH = 3


def load_env_file_vars(path: pathlib.Path | str | None) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from an env file without mutating os.environ."""
    if not path:
        return {}
    p = pathlib.Path(path)
    if not p.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            values[key] = val
    return values


def encode_rcon_packet(req_id: int, pkt_type: int, payload: str) -> bytes:
    """Encode a Source RCON TCP packet."""
    body = payload.encode("utf-8") + b"\x00\x00"
    header = struct.pack("<iii", 8 + len(body), req_id, pkt_type)
    return header + body


def decode_rcon_packet(raw: bytes) -> tuple[int, int, str]:
    """Decode the body (after 4-byte length prefix) of a Source RCON packet."""
    if len(raw) < 10:
        raise ValueError(f"Truncated RCON packet ({len(raw)} bytes)")
    req_id, pkt_type = struct.unpack("<ii", raw[:8])
    text_bytes = raw[8:-2] if raw.endswith(b"\x00\x00") else raw[8:].rstrip(b"\x00")
    return req_id, pkt_type, text_bytes.decode("utf-8", errors="replace")


class AsyncRconClient:
    """Minimal asyncio Source RCON client with single-flight locking & auto-reconnect."""

    def __init__(self, host: str, port: int, password: str, *, timeout_s: float = 5.0) -> None:
        self.host = host
        self.port = port
        self._password = password
        self.timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _read_packet(self) -> tuple[int, int, str]:
        assert self._reader is not None
        len_raw = await asyncio.wait_for(self._reader.readexactly(4), timeout=self.timeout_s)
        (pkt_len,) = struct.unpack("<i", len_raw)
        if pkt_len < 10 or pkt_len > 1024 * 1024:
            raise ValueError(f"Invalid RCON packet length: {pkt_len}")
        body = await asyncio.wait_for(self._reader.readexactly(pkt_len), timeout=self.timeout_s)
        return decode_rcon_packet(body)

    async def _connect_unlocked(self) -> None:
        await self.close()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout_s,
        )
        self._reader = reader
        self._writer = writer

        auth_id = self._next_id
        self._next_id += 1
        writer.write(encode_rcon_packet(auth_id, _RCON_SERVERDATA_AUTH, self._password))
        await asyncio.wait_for(writer.drain(), timeout=self.timeout_s)

        # Minecraft RCON sends an empty RESPONSE_VALUE (type 0) followed by AUTH_RESPONSE (type 2)
        for _ in range(3):
            req_id, pkt_type, _ = await self._read_packet()
            if pkt_type == _RCON_SERVERDATA_AUTH_RESPONSE:
                if req_id == -1:
                    await self.close()
                    raise PermissionError("RCON authentication rejected (invalid password)")
                if req_id == auth_id:
                    return
        await self.close()
        raise ConnectionError("RCON authentication handshake did not complete")

    async def command(self, cmd: str) -> str:
        async with self._lock:
            if not self.connected:
                await self._connect_unlocked()
            assert self._writer is not None
            req_id = self._next_id
            self._next_id += 1
            try:
                self._writer.write(encode_rcon_packet(req_id, _RCON_SERVERDATA_EXECCOMMAND, cmd))
                await asyncio.wait_for(self._writer.drain(), timeout=self.timeout_s)
                resp_id, _, text = await self._read_packet()
                if resp_id == -1:
                    raise PermissionError("RCON rejected command (not authenticated)")
                return text
            except Exception:
                await self.close()
                raise


class MjpegFrameExtractor:
    """Incremental parser extracting complete JPEG frames (`0xFFD8`..`0xFFD9`) from a byte stream."""

    def __init__(self, max_buffer_bytes: int = 4 * 1024 * 1024) -> None:
        self._buf = bytearray()
        self.max_buffer_bytes = max_buffer_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._buf.extend(chunk)
        frames: list[bytes] = []

        while True:
            soi = self._buf.find(b"\xff\xd8")
            if soi < 0:
                if len(self._buf) > 2:
                    del self._buf[:-1]
                break
            if soi > 0:
                del self._buf[:soi]

            eoi = self._buf.find(b"\xff\xd9", 2)
            if eoi < 0:
                if len(self._buf) > self.max_buffer_bytes:
                    # Drop stale incomplete frame if buffer exceeds ceiling
                    del self._buf[:]
                break

            end_idx = eoi + 2
            frame = bytes(self._buf[:end_idx])
            del self._buf[:end_idx]
            if len(frame) >= 256:
                frames.append(frame)

        return frames


def parse_video_size(video_size: str) -> tuple[int, int]:
    parts = video_size.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid --video-size {video_size!r}; expected WxH like 1280x720")
    w = int(parts[0])
    h = int(parts[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"Video dimensions must be positive: {video_size!r}")
    return w, h


def resolve_windows_title(title_hint: str | None) -> str | None:
    """Resolve a substring window title to the exact visible Win32 window title for ffmpeg gdigrab."""
    if not title_hint or platform.system() != "Windows":
        return title_hint
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        titles: list[str] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            val = buf.value.strip()
            if val:
                titles.append(val)
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
        for exact in titles:
            if exact == title_hint:
                return exact
        low_hint = title_hint.lower()
        for match in titles:
            if low_hint in match.lower():
                return match
    except Exception:
        pass
    return title_hint


def build_ffmpeg_command(
    *,
    ffmpeg_bin: str,
    capture_mode: str,
    display: str,
    window_title: str | None,
    video_size: str,
    fps: int,
    quality: int,
    offset_x: int = 0,
    offset_y: int = 0,
) -> list[str]:
    """Construct the `ffmpeg` command that emits raw concatenated JPEG frames to stdout."""
    w, h = parse_video_size(video_size)
    mode = capture_mode
    if mode == "auto":
        mode = "gdigrab" if platform.system() == "Windows" else "x11grab"

    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "warning"]
    if mode == "x11grab":
        inp = f"{display}+{offset_x},{offset_y}" if (offset_x or offset_y) else display
        cmd.extend(
            [
                "-f",
                "x11grab",
                "-framerate",
                str(fps),
                "-video_size",
                f"{w}x{h}",
                "-draw_mouse",
                "0",
                "-i",
                inp,
            ]
        )
    elif mode == "gdigrab":
        resolved_title = resolve_windows_title(window_title)
        if resolved_title:
            target = f"title={resolved_title}"
            cmd.extend(
                [
                    "-f",
                    "gdigrab",
                    "-framerate",
                    str(fps),
                    "-draw_mouse",
                    "0",
                    "-i",
                    target,
                    "-vf",
                    f"scale={w}:{h}:flags=fast_bilinear",
                ]
            )
        else:
            cmd.extend(
                [
                    "-f",
                    "gdigrab",
                    "-framerate",
                    str(fps),
                    "-offset_x",
                    str(offset_x),
                    "-offset_y",
                    str(offset_y),
                    "-video_size",
                    f"{w}x{h}",
                    "-draw_mouse",
                    "0",
                    "-i",
                    "desktop",
                ]
            )
    elif mode == "ddagrab":
        cmd.extend(
            [
                "-f",
                "ddagrab",
                "-framerate",
                str(fps),
                "-draw_mouse",
                "0",
                "-i",
                "desktop",
                "-vf",
                f"hwdownload,format=bgra,scale={w}:{h}:flags=fast_bilinear",
            ]
        )
    else:
        raise ValueError(f"Unsupported capture_mode: {mode!r}")

    cmd.extend(
        [
            "-an",
            "-c:v",
            "mjpeg",
            "-q:v",
            str(quality),
            "-pix_fmt",
            "yuvj420p",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
    )
    return cmd


def _normalize_command(cmd: str) -> str:
    return cmd.strip().lstrip("/")


class BeastDirector:
    """Coordinates RCON camera direction, ffmpeg frame capture, WS streaming, and HTTP API."""

    def __init__(
        self,
        *,
        ws_url: str = "ws://127.0.0.1:8765/ws-capture",
        rcon_host: str = "127.0.0.1",
        rcon_port: int = 25575,
        rcon_password: str = "",
        bot_username: str = "BeastBot_01",
        cam_username: str = "BeastCam",
        initial_mode: str = "third_person",
        capture_mode: str = "auto",
        display: str = ":99",
        window_title: str | None = None,
        video_size: str = "1280x720",
        fps: int = 15,
        quality: int = 5,
        tp_hz: float = 6.0,
        cam_back: float = 3.8,
        cam_up: float = 1.8,
        ffmpeg_bin: str = "ffmpeg",
        offset_x: int = 0,
        offset_y: int = 0,
        enable_rcon: bool = True,
        enable_ffmpeg: bool = True,
    ) -> None:
        if initial_mode not in VALID_CAMERA_MODES:
            raise ValueError(f"Invalid initial_mode: {initial_mode!r}")
        self.ws_url = ws_url
        self.bot_username = bot_username
        self.cam_username = cam_username
        self.camera_mode: str = initial_mode
        self.capture_mode = capture_mode
        self.display = display
        self.window_title = window_title
        self.video_size = video_size
        self.width, self.height = parse_video_size(video_size)
        self.fps = fps
        self.quality = quality
        self.tp_hz = max(1.0, min(20.0, tp_hz))
        self.cam_back = cam_back
        self.cam_up = cam_up
        self.ffmpeg_bin = ffmpeg_bin
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.enable_rcon = enable_rcon and bool(rcon_password)
        self.enable_ffmpeg = enable_ffmpeg

        self.rcon = AsyncRconClient(rcon_host, rcon_port, rcon_password) if self.enable_rcon else None

        # Runtime state
        self._closed = False
        self._tasks: list[asyncio.Task[None]] = []
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._ws_conn: aiohttp.ClientWebSocketResponse | None = None
        self._ws_send_lock = asyncio.Lock()

        self._frame_cond = asyncio.Condition()
        self._latest_frame: bytes | None = None
        self._latest_frame_seq: int = 0
        self._last_frame_mono: float = -999.0
        self.frames_captured: int = 0
        self.frames_sent: int = 0

        self._mode_event = asyncio.Event()
        self._applied_mode: str | None = None
        self._rcon_primed: bool = False
        self._first_person_attached: bool = False
        self._last_rcon_ok_mono: float = -999.0
        self._last_rcon_err: str | None = None
        self._last_ffmpeg_err: str | None = None
        self._last_ws_err: str | None = None
        self._last_err_log_mono: dict[str, float] = {}

    def _log_throttled(self, key: str, msg: str, interval_s: float = 10.0) -> None:
        now = time.monotonic()
        if (now - self._last_err_log_mono.get(key, -999.0)) >= interval_s:
            self._last_err_log_mono[key] = now
            print(msg, file=sys.stderr, flush=True)

    def build_meta_payload(self) -> dict[str, Any]:
        return {
            "type": "meta",
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "source": "beast_director",
            "camera_mode": self.camera_mode,
            "director_available": True,
        }

    def get_health(self) -> dict[str, Any]:
        now = time.monotonic()
        frame_age = round(now - self._last_frame_mono, 2) if self._last_frame_mono > 0 else None
        rcon_recent = (
            self.rcon is not None
            and self.rcon.connected
            and self._last_rcon_mono_recent(now)
        )
        return {
            "ok": True,
            "camera_mode": self.camera_mode,
            "director_available": True,
            "bot_username": self.bot_username,
            "cam_username": self.cam_username,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "ws_url": self.ws_url,
            "ws_connected": self._ws_conn is not None and not self._ws_conn.closed,
            "ffmpeg_running": self._ffmpeg_proc is not None and self._ffmpeg_proc.returncode is None,
            "rcon_enabled": self.enable_rcon,
            "rcon_connected": self.rcon.connected if self.rcon else False,
            "rcon_active": rcon_recent,
            "frames_captured": self.frames_captured,
            "frames_sent": self.frames_sent,
            "last_frame_age_s": frame_age,
            "last_rcon_error": self._last_rcon_err,
            "last_ffmpeg_error": self._last_ffmpeg_err,
            "last_ws_error": self._last_ws_err,
        }

    def _last_rcon_mono_recent(self, now: float) -> bool:
        return self._last_rcon_ok_mono > 0 and (now - self._last_rcon_ok_mono) <= 5.0

    async def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in VALID_CAMERA_MODES:
            raise ValueError(f"Invalid mode {mode!r}; expected 'first_person' or 'third_person'")
        changed = mode != self.camera_mode
        self.camera_mode = mode
        self._applied_mode = None
        if mode == "first_person":
            self._first_person_attached = False
        self._mode_event.set()
        if changed or (self._ws_conn is not None and not self._ws_conn.closed):
            await self.send_ws_meta()
        return {"ok": True, "mode": self.camera_mode, "director_available": True}

    async def send_ws_meta(self) -> None:
        ws = self._ws_conn
        if ws is None or ws.closed:
            return
        async with self._ws_send_lock:
            ws = self._ws_conn
            if ws is not None and not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.send_str(json.dumps(self.build_meta_payload(), ensure_ascii=False))

    async def push_frame(self, jpeg_bytes: bytes) -> None:
        self._latest_frame = jpeg_bytes
        self._latest_frame_seq += 1
        self._last_frame_mono = time.monotonic()
        self.frames_captured += 1
        async with self._frame_cond:
            self._frame_cond.notify_all()

    async def start(self) -> None:
        self._closed = False
        if self.enable_rcon and self.rcon is not None:
            self._tasks.append(asyncio.create_task(self._rcon_loop(), name="director-rcon"))
        if self.enable_ffmpeg:
            self._tasks.append(asyncio.create_task(self._ffmpeg_loop(), name="director-ffmpeg"))
        self._tasks.append(asyncio.create_task(self._ws_loop(), name="director-ws"))

    async def stop(self) -> None:
        self._closed = True
        self._mode_event.set()
        async with self._frame_cond:
            self._frame_cond.notify_all()

        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

        if self._ws_conn is not None and not self._ws_conn.closed:
            with contextlib.suppress(Exception):
                await self._ws_conn.send_str(json.dumps({"type": "stop"}))
                await self._ws_conn.close()
        self._ws_conn = None

        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self.rcon is not None:
            await self.rcon.close()

    @staticmethod
    def _is_missing_entity_reply(reply: str) -> bool:
        low = (reply or "").lower()
        return (
            "no entity was found" in low
            or "no player was found" in low
            or "unknown" in low
            or "not found" in low
        )

    async def _prime_rcon_world(self) -> None:
        assert self.rcon is not None
        # Disable command feedback spam (support both camelCase & snake_case gamerules across 1.21.x)
        r1 = await self.rcon.command(_normalize_command("gamerule sendCommandFeedback false"))
        if "incorrect" in r1.lower() or "unknown" in r1.lower():
            await self.rcon.command(_normalize_command("gamerule send_command_feedback false"))
        await self.rcon.command(_normalize_command(f"gamemode spectator {self.cam_username}"))
        self._rcon_primed = True

    async def _rcon_step(self) -> None:
        assert self.rcon is not None
        if not self._rcon_primed:
            await self._prime_rcon_world()

        mode = self.camera_mode
        mode_changed = self._applied_mode != mode
        if mode_changed:
            await self.rcon.command(_normalize_command(f"gamemode spectator {self.cam_username}"))
            if mode == "third_person":
                # Detach spectate before positioning BeastCam behind BeastBot_01
                await self.rcon.command(_normalize_command(f"execute as {self.cam_username} run spectate"))
                self._first_person_attached = False
            else:
                self._first_person_attached = False
            self._applied_mode = mode

        if mode == "first_person":
            if not self._first_person_attached or mode_changed:
                tp_reply = await self.rcon.command(
                    _normalize_command(f"tp {self.cam_username} {self.bot_username}")
                )
                if self._is_missing_entity_reply(tp_reply):
                    self._last_rcon_err = tp_reply.strip() or f"Waiting for {self.bot_username}/{self.cam_username}"
                    self._log_throttled("rcon_wait", f"[director:rcon] {self._last_rcon_err}", 15.0)
                    return
                spec_reply = await self.rcon.command(
                    _normalize_command(f"spectate {self.bot_username} {self.cam_username}")
                )
                if self._is_missing_entity_reply(spec_reply):
                    self._last_rcon_err = spec_reply.strip() or "spectate entity missing"
                    return
                self._first_person_attached = True
                self._last_rcon_err = None
            else:
                # Lightweight heartbeat: ensure cam remains spectating bot (even after bot respawn/reconnect)
                chk = await self.rcon.command(
                    _normalize_command(f"spectate {self.bot_username} {self.cam_username}")
                )
                if self._is_missing_entity_reply(chk):
                    self._first_person_attached = False
                    self._last_rcon_err = chk.strip()
                    return
                self._last_rcon_err = None
        else:
            # Third-person chase camera behind & slightly above bot, facing bot's eyes
            cmd = (
                f"execute as {self.cam_username} at {self.bot_username} rotated ~ 0 "
                f"run tp @s ^ ^{self.cam_up:.2f} ^-{self.cam_back:.2f} "
                f"facing entity {self.bot_username} eyes"
            )
            reply = await self.rcon.command(cmd)
            if self._is_missing_entity_reply(reply):
                self._last_rcon_err = reply.strip() or f"Waiting for {self.bot_username}/{self.cam_username}"
                self._log_throttled("rcon_wait", f"[director:rcon] {self._last_rcon_err}", 15.0)
            else:
                self._last_rcon_err = None

        self._last_rcon_ok_mono = time.monotonic()

    async def _rcon_loop(self) -> None:
        last_fp_check = 0.0
        while not self._closed:
            self._mode_event.clear()
            try:
                now = time.monotonic()
                if self.camera_mode == "third_person" or self._applied_mode != self.camera_mode:
                    await self._rcon_step()
                    last_fp_check = now
                elif not self._first_person_attached or (now - last_fp_check) >= 2.0:
                    await self._rcon_step()
                    last_fp_check = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._rcon_primed = False
                self._first_person_attached = False
                self._last_rcon_err = f"{type(exc).__name__}: {exc}"
                self._log_throttled("rcon_err", f"[director:rcon] error: {self._last_rcon_err}", 10.0)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._mode_event.wait(), timeout=1.5)
                continue

            interval = (1.0 / self.tp_hz) if self.camera_mode == "third_person" else 0.5
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._mode_event.wait(), timeout=interval)

    async def _ffmpeg_loop(self) -> None:
        while not self._closed:
            cmd = build_ffmpeg_command(
                ffmpeg_bin=self.ffmpeg_bin,
                capture_mode=self.capture_mode,
                display=self.display,
                window_title=self.window_title,
                video_size=self.video_size,
                fps=self.fps,
                quality=self.quality,
                offset_x=self.offset_x,
                offset_y=self.offset_y,
            )
            stderr_tail: list[str] = []
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._ffmpeg_proc = proc
                self._last_ffmpeg_err = None
                print(f"[director:ffmpeg] started: {shlex.join(cmd)}", flush=True)

                async def _drain_stderr() -> None:
                    assert proc.stderr is not None
                    while not self._closed:
                        line = await proc.stderr.readline()
                        if not line:
                            break
                        txt = line.decode("utf-8", errors="replace").strip()
                        if txt:
                            stderr_tail.append(txt)
                            if len(stderr_tail) > 10:
                                stderr_tail.pop(0)

                stderr_task = asyncio.create_task(_drain_stderr())
                extractor = MjpegFrameExtractor()
                assert proc.stdout is not None
                try:
                    while not self._closed:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        frames = extractor.feed(chunk)
                        if frames:
                            # Push only the newest complete frame in this read chunk to keep latency low
                            await self.push_frame(frames[-1])
                finally:
                    stderr_task.cancel()
                    with contextlib.suppress(Exception):
                        await stderr_task
                    rc = await proc.wait()
                    self._ffmpeg_proc = None
                    if not self._closed:
                        tail = " | ".join(stderr_tail[-3:])
                        self._last_ffmpeg_err = f"ffmpeg exited rc={rc}" + (f": {tail}" if tail else "")
                        self._log_throttled("ffmpeg_exit", f"[director:ffmpeg] {self._last_ffmpeg_err}", 5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ffmpeg_proc = None
                self._last_ffmpeg_err = f"{type(exc).__name__}: {exc}"
                self._log_throttled("ffmpeg_err", f"[director:ffmpeg] error: {self._last_ffmpeg_err}", 5.0)

            if not self._closed:
                await asyncio.sleep(1.5)

    async def _ws_loop(self) -> None:
        while not self._closed:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        self.ws_url,
                        heartbeat=20.0,
                        max_msg_size=4 * 1024 * 1024,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as ws:
                        self._ws_conn = ws
                        self._last_ws_err = None
                        print(f"[director:ws] connected to {self.ws_url}", flush=True)
                        await self.send_ws_meta()

                        last_sent_seq = 0
                        last_meta_mono = time.monotonic()

                        # Drain incoming pong/control messages in background while pushing frames
                        async def _drain_ws() -> None:
                            async for _ in ws:
                                pass

                        drain_task = asyncio.create_task(_drain_ws())
                        try:
                            while not self._closed and not ws.closed:
                                frame_to_send: bytes | None = None
                                async with self._frame_cond:
                                    if self._latest_frame is not None and self._latest_frame_seq != last_sent_seq:
                                        frame_to_send = self._latest_frame
                                        last_sent_seq = self._latest_frame_seq
                                    else:
                                        with contextlib.suppress(asyncio.TimeoutError):
                                            await asyncio.wait_for(self._frame_cond.wait(), timeout=1.0)
                                        if (
                                            not self._closed
                                            and self._latest_frame is not None
                                            and self._latest_frame_seq != last_sent_seq
                                        ):
                                            frame_to_send = self._latest_frame
                                            last_sent_seq = self._latest_frame_seq

                                if frame_to_send is not None:
                                    async with self._ws_send_lock:
                                        await ws.send_bytes(frame_to_send)
                                    self.frames_sent += 1

                                now = time.monotonic()
                                if (now - last_meta_mono) >= 4.0:
                                    last_meta_mono = now
                                    await self.send_ws_meta()

                                if drain_task.done():
                                    break
                        finally:
                            drain_task.cancel()
                            with contextlib.suppress(Exception):
                                await drain_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_ws_err = f"{type(exc).__name__}: {exc}"
                self._log_throttled("ws_err", f"[director:ws] error: {self._last_ws_err}", 10.0)
            finally:
                self._ws_conn = None

            if not self._closed:
                await asyncio.sleep(1.5)


def create_director_app(director: BeastDirector) -> web.Application:
    """Create the aiohttp HTTP control app (`GET /health`, `POST /mode`, `GET /mode`)."""
    app = web.Application()

    @web.middleware
    async def cors_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.method == "OPTIONS":
            resp: web.StreamResponse = web.Response(status=204)
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    app.middlewares.append(cors_middleware)

    async def _on_startup(_app: web.Application) -> None:
        await director.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await director.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response(director.get_health())

    async def handle_get_mode(request: web.Request) -> web.Response:
        req_mode = request.query.get("mode")
        if req_mode is not None:
            if req_mode not in VALID_CAMERA_MODES:
                return web.json_response(
                    {"ok": False, "error": "Invalid mode; expected 'first_person' or 'third_person'"},
                    status=400,
                )
            res = await director.set_mode(req_mode)
            return web.json_response(res)
        return web.json_response(
            {"ok": True, "mode": director.camera_mode, "director_available": True}
        )

    async def handle_post_mode(request: web.Request) -> web.Response:
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
        res = await director.set_mode(mode)
        return web.json_response(res)

    app.router.add_get("/health", handle_health)
    app.router.add_get("/mode", handle_get_mode)
    app.router.add_post("/mode", handle_post_mode)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beast_director",
        description="Minecraft 1.21.11 BeastCam RCON camera director & MJPEG->WS publisher for mc-live",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP control bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8767, help="HTTP control port (default: 8767)")
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:8765/ws-capture",
        help="Target mc-live WebSocket capture endpoint",
    )
    parser.add_argument("--env-file", default=".env.beast", help="Optional env file for MC_LOCAL_RCON_PASSWORD / MC_BOT_USERNAME")
    parser.add_argument("--rcon-host", default="127.0.0.1", help="Leaves RCON host (default: 127.0.0.1)")
    parser.add_argument("--rcon-port", type=int, default=25575, help="Leaves RCON port (default: 25575)")
    parser.add_argument("--rcon-password", default=None, help="RCON password (prefer env MC_LOCAL_RCON_PASSWORD)")
    parser.add_argument("--bot", default=None, help="Target bot username to track (default: MC_BOT_USERNAME or BeastBot_01)")
    parser.add_argument("--cam-user", default="BeastCam", help="Spectator camera client username (default: BeastCam)")
    parser.add_argument(
        "--mode",
        choices=VALID_CAMERA_MODES,
        default="third_person",
        help="Initial camera mode (default: third_person)",
    )
    parser.add_argument(
        "--capture-mode",
        choices=("auto", "x11grab", "gdigrab", "ddagrab"),
        default="auto",
        help="ffmpeg capture backend (default: auto)",
    )
    parser.add_argument("--display", default=":99", help="X11 display for x11grab (default: :99)")
    parser.add_argument("--window-title", default=None, help="Windows window title for gdigrab (omit for desktop)")
    parser.add_argument("--offset-x", type=int, default=0, help="Desktop capture X offset")
    parser.add_argument("--offset-y", type=int, default=0, help="Desktop capture Y offset")
    parser.add_argument("--video-size", default="1280x720", help="Capture resolution WxH (default: 1280x720)")
    parser.add_argument("--fps", type=int, default=15, help="Capture framerate (default: 15)")
    parser.add_argument("--quality", type=int, default=5, help="MJPEG -q:v quality 2..31 (default: 5)")
    parser.add_argument("--tp-hz", type=float, default=6.0, help="Third-person RCON tracking rate Hz (default: 6.0)")
    parser.add_argument("--cam-back", type=float, default=3.8, help="Third-person blocks behind bot (default: 3.8)")
    parser.add_argument("--cam-up", type=float, default=1.8, help="Third-person blocks above bot feet (default: 1.8)")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="Path to ffmpeg binary (default: ffmpeg)")
    parser.add_argument("--no-rcon", action="store_true", help="Disable RCON loop (capture-only test mode)")
    parser.add_argument("--no-ffmpeg", action="store_true", help="Disable ffmpeg capture (RCON/HTTP control only)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env_vars = load_env_file_vars(args.env_file)
    rcon_pwd = (
        args.rcon_password
        or os.environ.get("MC_LOCAL_RCON_PASSWORD")
        or env_vars.get("MC_LOCAL_RCON_PASSWORD")
        or ""
    )
    bot_user = (
        args.bot
        or os.environ.get("MC_BOT_USERNAME")
        or env_vars.get("MC_BOT_USERNAME")
        or "BeastBot_01"
    )

    if not args.no_rcon and not rcon_pwd:
        print(
            "[beast_director] WARNING: MC_LOCAL_RCON_PASSWORD not provided; running with RCON disabled.",
            file=sys.stderr,
            flush=True,
        )

    if not args.no_ffmpeg and shutil.which(args.ffmpeg_bin) is None:
        print(
            f"[beast_director] WARNING: ffmpeg binary {args.ffmpeg_bin!r} not found in PATH.",
            file=sys.stderr,
            flush=True,
        )

    director = BeastDirector(
        ws_url=args.ws_url,
        rcon_host=args.rcon_host,
        rcon_port=args.rcon_port,
        rcon_password=rcon_pwd,
        bot_username=bot_user,
        cam_username=args.cam_user,
        initial_mode=args.mode,
        capture_mode=args.capture_mode,
        display=args.display,
        window_title=args.window_title,
        video_size=args.video_size,
        fps=args.fps,
        quality=args.quality,
        tp_hz=args.tp_hz,
        cam_back=args.cam_back,
        cam_up=args.cam_up,
        ffmpeg_bin=args.ffmpeg_bin,
        offset_x=args.offset_x,
        offset_y=args.offset_y,
        enable_rcon=not args.no_rcon and bool(rcon_pwd),
        enable_ffmpeg=not args.no_ffmpeg,
    )
    app = create_director_app(director)
    print(
        f"[beast_director] HTTP=http://{args.host}:{args.port} -> WS={args.ws_url} | "
        f"bot={bot_user} cam={args.cam_user} mode={args.mode} size={args.video_size}@{args.fps}fps",
        flush=True,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
