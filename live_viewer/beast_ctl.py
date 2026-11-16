#!/usr/bin/env python3
"""Lifecycle & tunnel sync helper for `nju-beast` headless capture stack.

Subcommands:
- `sync-env-to-tunnel`: POST `/home/xia/mc-beast/.env.beast` bytes to `127.0.0.1:18799/upload-env`
  without printing any secret values.
- `sync-tokens-to-tunnel`: POST `/home/xia/mc-beast/.state-beast/tokens.json` bytes to
  `127.0.0.1:18799/upload-tokens` so local/remote `mc-live` share the same valid Bot token.
- `start`: normalize `.env.beast` (`0600`), install Python/Node deps, ensure cached Bot token,
  apply Leaves 1.21.11 RCON spectator/feedback setup, restart only `cam_server.js` (:8766),
  `mc-live` (:18765), and `beast_director.py` (:8767), then poll health until ready.
- `stop`: gracefully stop only `cam_server.js`, `mc-live` (:18765), and `beast_director.py`.
- `status`: inspect managed processes and `/api/info` + `/health` endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent

DEFAULT_BASE_DIR = pathlib.Path("/home/xia/mc-beast")
DEFAULT_MC_DIR = DEFAULT_BASE_DIR / "mc"
DEFAULT_ENV_FILE = DEFAULT_BASE_DIR / ".env.beast"
DEFAULT_STATE_DIR = DEFAULT_BASE_DIR / ".state-beast"
DEFAULT_LOGS_DIR = DEFAULT_BASE_DIR / "logs"
DEFAULT_NODE_BIN = DEFAULT_BASE_DIR / "runtime/node-v22.14.0-linux-x64/bin/node"
DEFAULT_NPM_BIN = DEFAULT_BASE_DIR / "runtime/node-v22.14.0-linux-x64/bin/npm"
DEFAULT_UV_BIN = pathlib.Path("/home/xia/.local/bin/uv")

MC_LIVE_PORT = 18765
CAM_SERVER_PORT = 8766
DIRECTOR_PORT = 8767
LEAVES_PORT = 25565
RCON_PORT = 25575
DEFAULT_BOT = "BeastBot_01"
DEFAULT_CAM_USER = "BeastCam"
DEFAULT_VIEWER_BOT = "LiveCam_01"

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(
    url: str,
    *,
    method: str = "GET",
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 4.0,
) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=raw_body, method=method, headers=headers or {})
    with _NO_PROXY_OPENER.open(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(data)
        except json.JSONDecodeError:
            return resp.status, data


def _post_file_to_tunnel(file_path: pathlib.Path, tunnel_url: str, *, label: str) -> dict[str, Any]:
    if not file_path.is_file():
        raise FileNotFoundError(f"{label} file not found: {file_path}")
    payload = file_path.read_bytes()
    status, resp = _http_json(
        tunnel_url,
        method="POST",
        raw_body=payload,
        headers={"Content-Type": "application/octet-stream"},
        timeout_s=10.0,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Tunnel upload failed ({status}) for {label}")
    return {"ok": True, "bytes": len(payload), "tunnel_resp": resp}


def _write_env_0600(env_path: pathlib.Path, updates: dict[str, str]) -> None:
    """Update specific keys in an env file while preserving secrets and 0600 permissions."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(raw)
    for key, val in remaining.items():
        out.append(f"{key}={val}")

    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out).rstrip("\n") + "\n")
    except Exception:
        raise


def _list_managed_pids(mc_live_port: int = MC_LIVE_PORT) -> dict[str, list[int]]:
    """Find PIDs of only `cam_server.js`, `mc-live` on `mc_live_port`, and `beast_director.py`.

    Never matches Docker `local-shell-mcp` (:8765), Leaves (:25565), `mc-agent-api` (:18900),
    `Xvfb :99`, or `BeastCam`.
    """
    found: dict[str, list[int]] = {
        "cam_server": [],
        "mc_live": [],
        "beast_director": [],
    }
    self_pid = os.getpid()
    proc_root = pathlib.Path("/proc")
    if not proc_root.is_dir():
        return found
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            raw_cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw_cmd:
            continue
        args = [part.decode("utf-8", errors="replace") for part in raw_cmd.split(b"\x00") if part]
        joined = " ".join(args)
        if "beast_ctl.py" in joined:
            continue
        if "cam_server.js" in joined:
            found["cam_server"].append(pid)
        elif "beast_director.py" in joined:
            found["beast_director"].append(pid)
        elif "mc-live" in joined and (
            f"--port {mc_live_port}" in joined or f"--port={mc_live_port}" in joined
        ):
            found["mc_live"].append(pid)
    return found


def _stop_managed_processes(mc_live_port: int = MC_LIVE_PORT) -> dict[str, list[int]]:
    pids_by_role = _list_managed_pids(mc_live_port)
    all_pids = [pid for pids in pids_by_role.values() for pid in pids]
    for pid in all_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3.0
    alive = set(all_pids)
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive.discard(pid)
        if alive:
            time.sleep(0.15)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids_by_role


def _install_deps(mc_dir: pathlib.Path, uv_bin: pathlib.Path, node_bin: pathlib.Path, npm_bin: pathlib.Path) -> None:
    subprocess.run([str(uv_bin), "sync", "--frozen"], cwd=mc_dir, check=True)
    node_env = dict(os.environ)
    node_env["PATH"] = f"{node_bin.parent}:{node_env.get('PATH', '')}"
    subprocess.run(
        [
            str(npm_bin),
            "--prefix",
            str(mc_dir / "live_viewer"),
            "install",
            "--omit=optional",
            "--no-audit",
            "--no-fund",
        ],
        cwd=mc_dir,
        env=node_env,
        check=True,
    )


def _ensure_bot_token(
    mc_dir: pathlib.Path,
    uv_bin: pathlib.Path,
    env_file: pathlib.Path,
    state_dir: pathlib.Path,
    bot_name: str,
) -> None:
    """Populate `.state-beast/tokens.json` via `mc-bot` CLI if missing or rejected."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tokens_file = state_dir / "tokens.json"
    if tokens_file.is_file():
        probe = subprocess.run(
            [str(uv_bin), "run", "mc-bot", "--env-file", str(env_file), "status", bot_name],
            cwd=mc_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
        )
        if probe.returncode == 0:
            os.chmod(tokens_file, 0o600)
            return

    subprocess.run(
        [str(uv_bin), "run", "mc-bot", "--env-file", str(env_file), "login", bot_name],
        cwd=mc_dir,
        stdout=subprocess.DEVNULL,
        check=True,
        timeout=60,
    )
    if tokens_file.is_file():
        os.chmod(tokens_file, 0o600)


def _run_rcon_commands(
    mc_dir: pathlib.Path,
    env_file: pathlib.Path,
    host: str,
    port: int,
    commands: list[str],
) -> list[dict[str, str]]:
    """Invoke `AsyncRconClient` inside the synced `.venv` so `beast_ctl.py` stays stdlib-only."""
    venv_py = mc_dir / ".venv/bin/python"
    script = (
        "import asyncio, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from beast_director import AsyncRconClient, load_env_file_vars\n"
        "pw = load_env_file_vars(sys.argv[2]).get('MC_LOCAL_RCON_PASSWORD', '')\n"
        "if not pw:\n"
        "  raise SystemExit('MC_LOCAL_RCON_PASSWORD missing in env file')\n"
        "cmds = json.loads(sys.argv[5])\n"
        "async def run():\n"
        "  c = AsyncRconClient(sys.argv[3], int(sys.argv[4]), pw, timeout_s=5.0)\n"
        "  out = []\n"
        "  try:\n"
        "    for cmd in cmds:\n"
        "      try:\n"
        "        rep = await c.command(cmd)\n"
        "      except Exception as exc:\n"
        "        rep = f'ERROR: {exc}'\n"
        "      out.append({'cmd': cmd, 'reply': rep.strip()})\n"
        "  finally:\n"
        "    await c.close()\n"
        "  print(json.dumps(out, ensure_ascii=False))\n"
        "asyncio.run(run())\n"
    )
    proc = subprocess.run(
        [str(venv_py), "-c", script, str(mc_dir / "live_viewer"), str(env_file), host, str(port), json.dumps(commands)],
        cwd=mc_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"RCON helper failed ({proc.returncode}): {err}")
    return json.loads(proc.stdout.strip())


def _spawn_detached(cmd: list[str], *, cwd: pathlib.Path, log_path: pathlib.Path, extra_env: dict[str, str] | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    with open(log_path, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def _collect_status(mc_live_port: int, viewer_port: int, director_port: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pids": _list_managed_pids(mc_live_port),
        "mc_live": None,
        "cam_server": None,
        "beast_director": None,
    }
    for key, url in (
        ("mc_live", f"http://127.0.0.1:{mc_live_port}/api/info"),
        ("cam_server", f"http://127.0.0.1:{viewer_port}/health"),
        ("beast_director", f"http://127.0.0.1:{director_port}/health"),
    ):
        try:
            _, data = _http_json(url, timeout_s=3.0)
            out[key] = data
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            out[key] = {"error": str(exc)}
    return out


def _is_stack_ready(snap: dict[str, Any]) -> bool:
    info = snap.get("mc_live")
    cam = snap.get("cam_server")
    director = snap.get("beast_director")
    if not isinstance(info, dict) or not isinstance(cam, dict) or not isinstance(director, dict):
        return False
    cap = info.get("capture_stream") or {}
    pv = info.get("prismarine_viewer") or {}
    return bool(
        info.get("online")
        and info.get("ready")
        and cap.get("active")
        and cap.get("director_available")
        and pv.get("available")
        and director.get("ok")
        and cam.get("ok")
    )


def _sanitized_snapshot(snap: dict[str, Any], spawned_pids: dict[str, int] | None = None) -> dict[str, Any]:
    info = snap.get("mc_live") if isinstance(snap.get("mc_live"), dict) else {}
    return {
        "ok": _is_stack_ready(snap),
        "spawned_pids": spawned_pids or snap.get("pids"),
        "running_pids": snap.get("pids"),
        "bot_username": info.get("bot_username"),
        "mc_api_base_url": info.get("mc_api_base_url"),
        "has_cached_token": info.get("has_cached_token"),
        "online": info.get("online"),
        "ready": info.get("ready"),
        "capture_stream": info.get("capture_stream"),
        "prismarine_viewer": info.get("prismarine_viewer"),
        "cam_server": snap.get("cam_server"),
        "beast_director": snap.get("beast_director"),
    }


def cmd_start(args: argparse.Namespace) -> int:
    env_file = pathlib.Path(args.env_file)
    mc_dir = pathlib.Path(args.mc_dir)
    state_dir = pathlib.Path(args.state_dir)
    logs_dir = pathlib.Path(args.logs_dir)
    uv_bin = pathlib.Path(args.uv_bin)
    node_bin = pathlib.Path(args.node_bin)
    npm_bin = pathlib.Path(args.npm_bin)

    _write_env_0600(
        env_file,
        {
            "MC_API_BASE_URL": "http://127.0.0.1:18900/api/v1",
            "MC_STATE_DIR": str(state_dir),
            "MC_BOT_USERNAME": args.bot,
        },
    )
    if not args.skip_install:
        _install_deps(mc_dir, uv_bin, node_bin, npm_bin)

    _ensure_bot_token(mc_dir, uv_bin, env_file, state_dir, args.bot)

    rcon_init = _run_rcon_commands(
        mc_dir,
        env_file,
        "127.0.0.1",
        RCON_PORT,
        [
            "gamerule sendCommandFeedback false",
            "gamerule send_command_feedback false",
            f"op {args.cam_user}",
            f"op {args.viewer_bot}",
            f"gamemode spectator {args.cam_user}",
        ],
    )

    stopped = _stop_managed_processes(args.mc_live_port)

    node_extra_env = {"PATH": f"{node_bin.parent}:{os.environ.get('PATH', '')}"}
    pid_cam = _spawn_detached(
        [
            str(node_bin),
            str(mc_dir / "live_viewer/cam_server.js"),
            "--mc-host",
            "127.0.0.1",
            "--mc-port",
            str(LEAVES_PORT),
            "--cam-user",
            args.viewer_bot,
            "--viewer-host",
            "0.0.0.0",
            "--viewer-port",
            str(args.viewer_port),
            "--follow-api",
            f"http://127.0.0.1:{args.mc_live_port}/api/telemetry-pos",
        ],
        cwd=mc_dir / "live_viewer",
        log_path=logs_dir / "cam_server.log",
        extra_env=node_extra_env,
    )
    pid_live = _spawn_detached(
        [
            str(uv_bin),
            "run",
            "mc-live",
            "--host",
            "0.0.0.0",
            "--port",
            str(args.mc_live_port),
            "--env-file",
            str(env_file),
            "--bot",
            args.bot,
            "--viewer-url",
            f"http://127.0.0.1:{args.viewer_port}",
            "--director-url",
            f"http://127.0.0.1:{args.director_port}",
        ],
        cwd=mc_dir,
        log_path=logs_dir / "mc_live.log",
    )
    pid_dir = _spawn_detached(
        [
            str(uv_bin),
            "run",
            "python",
            str(mc_dir / "live_viewer/beast_director.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.director_port),
            "--ws-url",
            f"ws://127.0.0.1:{args.mc_live_port}/ws-capture",
            "--env-file",
            str(env_file),
            "--rcon-host",
            "127.0.0.1",
            "--rcon-port",
            str(RCON_PORT),
            "--bot",
            args.bot,
            "--cam-user",
            args.cam_user,
            "--capture-mode",
            "x11grab",
            "--display",
            ":99",
            "--video-size",
            "1280x720",
            "--fps",
            "15",
            "--mode",
            "third_person",
        ],
        cwd=mc_dir,
        log_path=logs_dir / "beast_director.log",
    )

    time.sleep(3.5)
    rcon_viewer = _run_rcon_commands(
        mc_dir,
        env_file,
        "127.0.0.1",
        RCON_PORT,
        [f"gamemode spectator {args.viewer_bot}"],
    )

    deadline = time.monotonic() + args.wait_timeout
    snap = _collect_status(args.mc_live_port, args.viewer_port, args.director_port)
    while not _is_stack_ready(snap) and time.monotonic() < deadline:
        time.sleep(1.0)
        snap = _collect_status(args.mc_live_port, args.viewer_port, args.director_port)
        if any("No player was found" in item.get("reply", "") for item in rcon_viewer):
            rcon_viewer = _run_rcon_commands(
                mc_dir,
                env_file,
                "127.0.0.1",
                RCON_PORT,
                [f"gamemode spectator {args.viewer_bot}"],
            )

    out = _sanitized_snapshot(snap, {"cam_server": pid_cam, "mc_live": pid_live, "beast_director": pid_dir})
    out["stopped_previous"] = stopped
    out["rcon_init"] = rcon_init
    out["rcon_viewer"] = rcon_viewer
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["ok"] else 1


def cmd_stop(args: argparse.Namespace) -> int:
    stopped = _stop_managed_processes(args.mc_live_port)
    print(json.dumps({"ok": True, "stopped": stopped}, indent=2, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    snap = _collect_status(args.mc_live_port, args.viewer_port, args.director_port)
    out = _sanitized_snapshot(snap)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beast_ctl", description="Manage nju-beast live capture processes")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_sync_env = sub.add_parser("sync-env-to-tunnel", help="POST remote .env.beast to loopback tunnel")
    p_sync_env.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    p_sync_env.add_argument("--tunnel-url", default="http://127.0.0.1:18799/upload-env")

    p_sync_tok = sub.add_parser("sync-tokens-to-tunnel", help="POST remote tokens.json to loopback tunnel")
    p_sync_tok.add_argument("--tokens-file", default=str(DEFAULT_STATE_DIR / "tokens.json"))
    p_sync_tok.add_argument("--tunnel-url", default="http://127.0.0.1:18799/upload-tokens")

    for name in ("start", "stop", "status"):
        p_cmd = sub.add_parser(name)
        p_cmd.add_argument("--mc-live-port", type=int, default=MC_LIVE_PORT)
        p_cmd.add_argument("--viewer-port", type=int, default=CAM_SERVER_PORT)
        p_cmd.add_argument("--director-port", type=int, default=DIRECTOR_PORT)
        if name == "start":
            p_cmd.add_argument("--mc-dir", default=str(DEFAULT_MC_DIR))
            p_cmd.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
            p_cmd.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
            p_cmd.add_argument("--logs-dir", default=str(DEFAULT_LOGS_DIR))
            p_cmd.add_argument("--uv-bin", default=str(DEFAULT_UV_BIN))
            p_cmd.add_argument("--node-bin", default=str(DEFAULT_NODE_BIN))
            p_cmd.add_argument("--npm-bin", default=str(DEFAULT_NPM_BIN))
            p_cmd.add_argument("--bot", default=DEFAULT_BOT)
            p_cmd.add_argument("--cam-user", default=DEFAULT_CAM_USER)
            p_cmd.add_argument("--viewer-bot", default=DEFAULT_VIEWER_BOT)
            p_cmd.add_argument("--wait-timeout", type=float, default=35.0)
            p_cmd.add_argument("--skip-install", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "sync-env-to-tunnel":
        res = _post_file_to_tunnel(pathlib.Path(args.env_file), args.tunnel_url, label=".env.beast")
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.subcommand == "sync-tokens-to-tunnel":
        res = _post_file_to_tunnel(pathlib.Path(args.tokens_file), args.tunnel_url, label="tokens.json")
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.subcommand == "start":
        return cmd_start(args)
    if args.subcommand == "stop":
        return cmd_stop(args)
    if args.subcommand == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
