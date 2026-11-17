#!/usr/bin/env python3
"""Safely upgrade nju-beast mc-agent-api to upstream v1.3.14 and synchronize BeastBot_01 tokens."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import json
import pathlib
import shutil
import subprocess
import tarfile
import time
import urllib.request

from mc_agent.auth import BotTokenRecord, TokenStore
from mc_agent.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
UPSTREAM_SRC = pathlib.Path("/tmp/mc-agent-api-inspect-20261116")
BASE = "/home/xia/mc-beast"
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SYNC_TARGETS = (
    "package.json",
    "README.md",
    "LICENSE",
    "config.example.json",
    "docs",
    "examples",
    "src",
    "test",
)


def remote_py(code: str, *, timeout: int = 90) -> dict:
    proc = subprocess.run(
        [
            "ssh",
            "-T",
            "-F",
            "/home/xzq/.ssh/config",
            "nju-beast",
            "wsl",
            "-d",
            "Ubuntu-22.04",
            "-u",
            "xia",
            "--",
            "python3",
            "-",
        ],
        input=code,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Remote command failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")
    raw = proc.stdout.strip()
    if not raw:
        raise SystemExit("Remote command produced empty stdout")
    return json.loads(raw.splitlines()[-1])


def build_tar_b64() -> str:
    pkg_path = UPSTREAM_SRC / "package.json"
    if not pkg_path.is_file():
        raise SystemExit(f"Upstream checkout missing: {UPSTREAM_SRC}")
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    if pkg.get("version") != "1.3.14":
        raise SystemExit(f"Expected upstream version 1.3.14, got {pkg.get('version')!r}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in SYNC_TARGETS:
            p = UPSTREAM_SRC / rel
            if not p.exists():
                raise SystemExit(f"Missing target in upstream checkout: {p}")
            tf.add(p, arcname=rel)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def preflight() -> dict:
    with NO_PROXY.open("http://127.0.0.1:8770/api/state", timeout=8) as resp:
        state = json.load(resp)
    if state.get("running"):
        raise SystemExit("Local mc-console agent is active; refusing API upgrade.")
    return remote_py(
        f'''
import json, os, pathlib, time, urllib.request

base = pathlib.Path({BASE!r})
for rel in [".env.beast", ".state-beast/tokens.json", "api/config.json", "leaves/server.properties"]:
    p = base / rel
    if not p.exists():
        raise SystemExit("Missing required file: " + rel)
    if (p.stat().st_mode & 0o777) != 0o600:
        raise SystemExit("Secret file mode is not 0600: " + rel)

cfg = json.loads((base / "api/config.json").read_text())
if cfg.get("auth", {{}}).get("type") != "offline":
    raise SystemExit("Unexpected auth provider")

cache = json.loads((base / ".state-beast/tokens.json").read_text())
rec = cache.get("tokens", {{}}).get("beastbot_01")
cooldown_until = 0
if rec and rec.get("token"):
    req = urllib.request.Request(
        "http://127.0.0.1:18900/api/v1/events?since=0&limit=500",
        headers={{"Authorization": "Bearer " + rec["token"]}},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
    try:
        with opener.open(req, timeout=8) as r:
            evs = json.load(r).get("result", {{}}).get("events", [])
        cooldown_until = max([e.get("cooldown_until", 0) for e in evs if e.get("type") == "death_loop"] + [0])
    except urllib.error.HTTPError:
        cooldown_until = 0
if time.time() * 1000 < cooldown_until:
    raise SystemExit("Death-loop cooldown still active; refusing upgrade.")

api_procs = []
for p in pathlib.Path("/proc").iterdir():
    if not p.name.isdigit():
        continue
    try:
        cwd = os.readlink(p / "cwd")
        argv = [x.decode("utf-8", "replace") for x in (p / "cmdline").read_bytes().split(b"\\x00") if x]
    except OSError:
        continue
    if cwd == str(base / "api") and argv and pathlib.Path(argv[0]).name == "node" and str(base / "api/src/index.js") in argv:
        api_procs.append({{"pid": int(p.name), "cwd": cwd, "argv": argv}})
if len(api_procs) != 1:
    raise SystemExit(f"Expected 1 managed API process, found {{len(api_procs)}}")

pkg = json.loads((base / "api/package.json").read_text())
print(json.dumps({{"old_version": pkg.get("version"), "api": api_procs[0], "cooldown_until": cooldown_until}}))
'''
    )


def main() -> None:
    checked = preflight()
    print(json.dumps({"phase": "preflight", "old_version": checked["old_version"], "api_pid": checked["api"]["pid"]}, ensure_ascii=False), flush=True)

    tar_b64 = build_tar_b64()
    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    local_cache = settings.state_dir / "tokens.json"
    if local_cache.is_file():
        backup_tok = local_cache.with_name("tokens.before-v1.3.14.json")
        if not backup_tok.exists():
            shutil.copy2(local_cache, backup_tok)
            backup_tok.chmod(0o600)

    print(json.dumps({"phase": "stopping_local_services"}), flush=True)
    subprocess.run(["bash", str(ROOT / "scripts/mc_services.sh"), "stop"], check=True)
    statuses = subprocess.run(
        ["systemctl", "--user", "show", "mc-live-proxy.service", "mc-console.service", "-p", "ActiveState", "--value"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if any(s != "inactive" for s in statuses):
        raise SystemExit(f"Local services did not stop cleanly: {statuses}")

    # Stop remote mc-live, deploy v1.3.14, restart API, login once, wait ready, sync token, restore mc-live.
    # Captured response contains the new Bearer token in memory only; pop before printing.
    result = remote_py(
        f'''
import base64, datetime, io, json, os, pathlib, shutil, signal, socket, subprocess, sys, tarfile, time, urllib.request

base = pathlib.Path({BASE!r})
api_dir = base / "api"
api_info = {checked["api"]!r}
tar_bytes = base64.b64decode({tar_b64!r})

sys.path.insert(0, str(base / "mc/live_viewer"))
from beast_ctl import _list_managed_pids, _spawn_detached

old_live = _list_managed_pids(18765)["mc_live"]
for pid in old_live:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
time.sleep(1.0)
for pid in old_live:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
if _list_managed_pids(18765)["mc_live"]:
    raise SystemExit("Remote mc-live did not stop; login refused.")

backup_dir = api_dir / "backups/20261116-before-1.3.14"
backup_dir.mkdir(parents=True, exist_ok=True)
for rel in ["package.json", "README.md", "LICENSE", "config.example.json", "src", "docs", "examples", "test"]:
    src_p = api_dir / rel
    dst_p = backup_dir / rel
    if src_p.exists() and not dst_p.exists():
        if src_p.is_dir():
            shutil.copytree(src_p, dst_p)
        else:
            shutil.copy2(src_p, dst_p)

stage_dir = api_dir / ".stage_v1314"
if stage_dir.exists():
    shutil.rmtree(stage_dir)
stage_dir.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
    for member in tf.getmembers():
        dest = (stage_dir / member.name).resolve()
        if not dest.is_relative_to(stage_dir.resolve()):
            raise SystemExit(f"Unsafe tar path: {{member.name}}")
    if hasattr(tarfile, "data_filter"):
        tf.extractall(stage_dir, filter="data")
    else:
        tf.extractall(stage_dir)

node_bin = str(base / "runtime/node-v22.14.0-linux-x64/bin/node")
for js in sorted((stage_dir / "src").rglob("*.js")):
    chk = subprocess.run([node_bin, "--check", str(js)], capture_output=True, text=True)
    if chk.returncode != 0:
        raise SystemExit(f"Syntax check failed on {{js}}: {{chk.stderr}}")

for rel in ["docs", "examples", "src", "test"]:
    dst_d = api_dir / rel
    if dst_d.exists():
        shutil.rmtree(dst_d)
    shutil.move(str(stage_dir / rel), str(dst_d))
for rel in ["package.json", "README.md", "LICENSE", "config.example.json"]:
    shutil.move(str(stage_dir / rel), str(api_dir / rel))
shutil.rmtree(stage_dir, ignore_errors=True)

pkg = json.loads((api_dir / "package.json").read_text())
if pkg.get("version") != "1.3.14":
    raise SystemExit("Deployed package.json version mismatch")

# Stop old API process (and any workers)
old_pid = api_info["pid"]
try:
    os.kill(old_pid, signal.SIGTERM)
except ProcessLookupError:
    pass
for _ in range(40):
    if not pathlib.Path(f"/proc/{{old_pid}}").exists():
        break
    time.sleep(0.1)
else:
    try:
        os.kill(old_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

log_fh = (base / "logs/api.log").open("ab", buffering=0)
proc = subprocess.Popen(
    [node_bin, str(api_dir / "src/index.js"), "config.json"],
    cwd=str(api_dir),
    stdin=subprocess.DEVNULL,
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)

for _ in range(60):
    try:
        with socket.create_connection(("127.0.0.1", 18900), timeout=0.25):
            break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("Upgraded v1.3.14 API did not open port 18900")

cfg = json.loads((api_dir / "config.json").read_text())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
login_req = urllib.request.Request(
    "http://127.0.0.1:18900/api/v1/auth/login",
    data=json.dumps({{"username": "BeastBot_01", "password": cfg["auth"].get("password", "")}}).encode(),
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
with opener.open(login_req, timeout=15) as r:
    auth = json.load(r)
if not auth.get("ok") or not auth.get("token"):
    raise SystemExit("Controlled login failed after API upgrade")

token = auth["token"]
ready_ok = False
last_status = None
for _ in range(45):
    st_req = urllib.request.Request(
        "http://127.0.0.1:18900/api/v1/status",
        headers={{"Authorization": "Bearer " + token}},
        method="GET",
    )
    with opener.open(st_req, timeout=10) as r:
        st_payload = json.load(r)
    if st_payload.get("ok") and st_payload.get("result", {{}}).get("ready"):
        ready_ok = True
        last_status = st_payload["result"]
        break
    time.sleep(1.0)
if not ready_ok:
    raise SystemExit("Bot did not reach ready=true within 45s after v1.3.14 upgrade")

record = {{
    "bot_username": "BeastBot_01",
    "token": token,
    "expires_at": auth.get("expires_at"),
    "login_username": "BeastBot_01",
    "obtained_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}}
cache_path = base / ".state-beast/tokens.json"
backup_tok = cache_path.with_name("tokens.before-v1.3.14.json")
if not backup_tok.exists():
    shutil.copy2(cache_path, backup_tok)
    backup_tok.chmod(0o600)
cache = json.loads(cache_path.read_text())
cache["tokens"]["beastbot_01"] = record
pending = cache_path.with_name("tokens.maintenance.json")
fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cache, f, indent=2)
pending.chmod(0o600)
os.replace(pending, cache_path)

mc_live_pid = _spawn_detached(
    [
        str(base / "mc/.venv/bin/mc-live"),
        "--host", "0.0.0.0",
        "--port", "18765",
        "--env-file", str(base / ".env.beast"),
        "--bot", "BeastBot_01",
        "--viewer-url", "http://127.0.0.1:8766",
        "--director-url", "http://127.0.0.1:8767",
    ],
    cwd=base / "mc",
    log_path=base / "logs/mc_live.log",
)

remote_live_ok = False
for _ in range(25):
    try:
        with opener.open("http://127.0.0.1:18765/api/info", timeout=3) as lr:
            linfo = json.load(lr)
        if linfo.get("online") and linfo.get("ready"):
            remote_live_ok = True
            break
    except Exception:
        pass
    time.sleep(0.4)

print(json.dumps({{
    "new_version": pkg.get("version"),
    "api_pid": proc.pid,
    "mc_live_pid": mc_live_pid,
    "remote_live_ok": remote_live_ok,
    "bot_pos": last_status.get("pos"),
    "bot_health": last_status.get("health"),
    "has_held_controls_field": ("held_controls" in last_status),
    "record": record,
}}))
''',
        timeout=150,
    )

    record_data = result.pop("record")
    TokenStore(local_cache).put(BotTokenRecord.from_dict(record_data))
    local_cache.chmod(0o600)

    print(json.dumps({"phase": "upgraded_and_synced", **result}, ensure_ascii=False), flush=True)

    subprocess.run(["bash", str(ROOT / "scripts/mc_services.sh"), "start"], check=True)
    for _ in range(30):
        try:
            with NO_PROXY.open("http://127.0.0.1:8770/api/state", timeout=3) as resp:
                state = json.load(resp)
            tel = state.get("telemetry") or {}
            if state.get("runtime") and tel.get("ready"):
                print(
                    json.dumps(
                        {
                            "phase": "complete",
                            "version": result["new_version"],
                            "telemetry": {
                                "connected": tel.get("connected"),
                                "ready": tel.get("ready"),
                                "pos": tel.get("pos"),
                                "health": tel.get("health"),
                            },
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("Local mc-console did not report ready=true within 15s")


if __name__ == "__main__":
    main()
