#!/usr/bin/env python3
"""Unified 22-Bot Fleet Orchestrator & Cross-Model P0/P1 Bug Aggregator for v2-integrated.

Squads:
  - nju  (3): NJU Qwen3.6-35B competition model (soft-oversubscribed 3-way on 2-concurrency gateway, rpm=9)
  - glm  (5): OpenCode GLM-5.3 (complex mid/late-game: fortress, bastion, village, enchant, midgame)
  - ds   (4): OpenCode DeepSeek-v4.1-flash (cost-controlled cross-validation)
  - hy4 (10): OpenCode HY4-flash-free (free high-concurrency broad terrain & edge-case sweep)

Usage:
  python scripts/v2_fleet.py provision
  python scripts/v2_fleet.py launch --wave r11 --duration 1200 [--squads nju,glm,ds,hy4]
  python scripts/v2_fleet.py triage --wave r11
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import pathlib
import subprocess
import sys
import time
from collections import Counter, defaultdict

WT_ROOT = pathlib.Path("/home/xzq/projects/mc/.worktrees/v2-integrated")
MAIN_ROOT = pathlib.Path("/home/xzq/projects/mc")
VENV_PY = MAIN_ROOT / ".venv/bin/python"
RUNNER = WT_ROOT / "scripts/l3_wild_endurance_runner.py"
ENV_FILE = MAIN_ROOT / ".env.beast"
FLEET_TOKEN_FILE = MAIN_ROOT / ".state-fleet/tokens.json"


@dataclasses.dataclass(frozen=True)
class FleetSlot:
    bot: str
    squad: str
    provider: str
    model: str | None
    rpm_budget: int
    stage: str
    site_index: int
    token_file: str


FLEET: tuple[FleetSlot, ...] = (
    # --- Squad 1: NJU Competition Benchmark (3 bots, soft-oversubscribed) ---
    FleetSlot("MoveTest_01", "nju", "nju", None, 9, "bare", 0, str(MAIN_ROOT / ".state-move-test/tokens.json")),
    FleetSlot("GuiTest_01", "nju", "nju", None, 9, "nether", 1, str(MAIN_ROOT / ".state-gui-test/tokens.json")),
    FleetSlot("NJU_3", "nju", "nju", None, 9, "village", 0, str(FLEET_TOKEN_FILE)),
    # --- Squad 2: GLM-5.3 Mid/Late-Game Strike Team (5 bots) ---
    FleetSlot("GLM_1", "glm", "opencode", "glm-5.3", 0, "nether_fortress", 0, str(MAIN_ROOT / ".state-beast/tokens.json")),
    FleetSlot("GLM_2", "glm", "opencode", "glm-5.3", 0, "nether_bastion", 1, str(MAIN_ROOT / ".state-beast/tokens.json")),
    FleetSlot("GLM_3", "glm", "opencode", "glm-5.3", 0, "village", 1, str(MAIN_ROOT / ".state-beast/tokens.json")),
    FleetSlot("GLM_4", "glm", "opencode", "glm-5.3", 0, "enchant_anvil", 2, str(MAIN_ROOT / ".state-beast/tokens.json")),
    FleetSlot("GLM_5", "glm", "opencode", "glm-5.3", 0, "midgame", 3, str(MAIN_ROOT / ".state-glm5/tokens.json")),
    # --- Squad 3: DeepSeek-v4.1-flash Cross-Validation (4 bots) ---
    FleetSlot("DS_1", "ds", "opencode", "deepseek-v4.1-flash", 0, "bare", 4, str(FLEET_TOKEN_FILE)),
    FleetSlot("DS_2", "ds", "opencode", "deepseek-v4.1-flash", 0, "iron", 5, str(FLEET_TOKEN_FILE)),
    FleetSlot("DS_3", "ds", "opencode", "deepseek-v4.1-flash", 0, "village", 2, str(FLEET_TOKEN_FILE)),
    FleetSlot("DS_4", "ds", "opencode", "deepseek-v4.1-flash", 0, "nether_fortress", 2, str(FLEET_TOKEN_FILE)),
    # --- Squad 4: HY4-flash-free High-Concurrency Broad Sweeper (10 bots) ---
    FleetSlot("HY_1", "hy4", "opencode", "hy4-flash-free", 0, "bare", 6, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_2", "hy4", "opencode", "hy4-flash-free", 0, "bare", 7, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_3", "hy4", "opencode", "hy4-flash-free", 0, "iron", 8, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_4", "hy4", "opencode", "hy4-flash-free", 0, "iron", 0, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_5", "hy4", "opencode", "hy4-flash-free", 0, "midgame", 1, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_6", "hy4", "opencode", "hy4-flash-free", 0, "midgame", 2, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_7", "hy4", "opencode", "hy4-flash-free", 0, "village", 3, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_8", "hy4", "opencode", "hy4-flash-free", 0, "nether", 3, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_9", "hy4", "opencode", "hy4-flash-free", 0, "deep_water", 4, str(FLEET_TOKEN_FILE)),
    FleetSlot("HY_10", "hy4", "opencode", "hy4-flash-free", 0, "enchant_anvil", 5, str(FLEET_TOKEN_FILE)),
)


def cmd_provision() -> int:
    """Provision tokens for all new fleet bots stored in .state-fleet/tokens.json (never touches BeastBot_01)."""
    sys.path.insert(0, str(WT_ROOT / "src"))
    import httpx
    from mc_agent.auth import BotTokenRecord, TokenStore
    from mc_agent.config import Settings, parse_dotenv

    settings = Settings.load(dotenv_path=ENV_FILE)
    env_map = parse_dotenv(ENV_FILE)
    api_base = settings.mc_api_base_url.rstrip("/")
    # Offline auth password from .env.beast (never printed)
    password = env_map.get("MC_SKIN_PASSWORD") or env_map.get("MC_BOT_PASSWORD") or ""
    FLEET_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    store = TokenStore(FLEET_TOKEN_FILE)
    needed = [s.bot for s in FLEET if s.token_file == str(FLEET_TOKEN_FILE)]
    ok_cnt = 0
    for bot_name in needed:
        existing = store.get(bot_name)
        if existing is not None:
            print(f"[provision] {bot_name}: already cached")
            ok_cnt += 1
            continue
        r = httpx.post(
            f"{api_base}/auth/login",
            json={"username": bot_name, "password": password},
            timeout=15.0,
        )
        if r.status_code != 200:
            print(f"[provision] {bot_name}: FAILED HTTP {r.status_code} ({r.text[:120]})")
            continue
        payload = r.json()
        res = payload.get("result") or payload
        tok = res.get("token")
        if not tok:
            print(f"[provision] {bot_name}: FAILED no token in response")
            continue
        store.put(
            BotTokenRecord(
                bot_username=res.get("bot_username") or bot_name,
                token=tok,
                expires_at=res.get("expires_at"),
                login_username=bot_name,
            )
        )
        print(f"[provision] {bot_name}: issued & saved")
        ok_cnt += 1
    try:
        os.chmod(FLEET_TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"[provision] done: {ok_cnt}/{len(needed)} ready in {FLEET_TOKEN_FILE}")
    return 0 if ok_cnt == len(needed) else 1


def cmd_launch(wave: str, duration_s: float, max_episodes: int, squads: set[str], stagger_s: float) -> int:
    selected = [s for s in FLEET if s.squad in squads]
    if not selected:
        print("No fleet slots matched squads:", squads)
        return 1
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WT_ROOT / "src")
    procs: list[tuple[FleetSlot, subprocess.Popen, pathlib.Path]] = []
    print(f"[Fleet Launch] wave={wave} bots={len(selected)} duration_s={duration_s} max_eps={max_episodes}")
    for idx, slot in enumerate(selected):
        subdir = f"{wave}/{slot.squad}_{slot.bot.lower()}_{slot.stage}"
        log_dir = WT_ROOT / "runs" / subdir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "stdout.log"
        cmd = [
            str(VENV_PY),
            str(RUNNER),
            "--bot", slot.bot,
            "--token-file", slot.token_file,
            "--provider", slot.provider,
            "--rpm-budget", str(slot.rpm_budget),
            "--stage", slot.stage,
            "--runs-subdir", subdir,
            "--site-index", str(slot.site_index),
            "--duration-s", str(int(duration_s)),
            "--max-episodes", str(max_episodes),
            "--tag", f"{wave}_{slot.bot.lower()}_{slot.stage}",
        ]
        if slot.model:
            cmd.extend(["--model", slot.model])
        if idx > 0:
            cmd.append("--keep-time")
        lf = open(log_path, "wb")
        proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT)
        procs.append((slot, proc, log_path))
        print(f"  [{idx+1:2d}/{len(selected):2d}] started {slot.bot:12s} ({slot.squad:3s}/{slot.model or 'nju':19s}) stage={slot.stage:15s} pid={proc.pid}", flush=True)
        if idx + 1 < len(selected):
            time.sleep(stagger_s)

    # Wait for all bots to finish
    failed = 0
    for slot, proc, _ in procs:
        rc = proc.wait()
        if rc != 0:
            failed += 1
        print(f"  [DONE] {slot.bot:12s} ({slot.stage:15s}) rc={rc}", flush=True)
    print(f"[Fleet Complete] wave={wave} total={len(procs)} failed={failed}")
    return cmd_triage(wave)


def cmd_triage(wave: str) -> int:
    wave_dir = WT_ROOT / "runs" / wave
    bot_dirs = sorted(p for p in wave_dir.iterdir() if p.is_dir()) if wave_dir.is_dir() else []
    if not bot_dirs:
        print(f"No run directories found under {wave_dir}")
        return 1

    # Aggregate cross-model P0/P1 errors & squad scores
    err_counts: Counter[str] = Counter()
    err_bots: defaultdict[str, set[str]] = defaultdict(set)
    slow_tools: list[tuple[float, str, str]] = []
    rows = []

    for bd in bot_dirs:
        name = bd.name
        squad = name.split("_")[0]
        summaries = sorted(bd.glob("wild-endurance-*.json"))
        score = 0
        deaths = 0
        g_inc = 0
        status = "running"
        gained_all: list[str] = []
        if summaries:
            try:
                d = json.loads(summaries[-1].read_text())
                score = int(d.get("final_score", 0) or 0)
                status = "done"
                g_inc = int((d.get("guardian") or {}).get("incidents", 0) or 0)
                for ep in (d.get("curriculum") or {}).get("episodes") or d.get("episodes") or []:
                    gained_all.extend(ep.get("gained_advancements") or [])
            except Exception:
                pass
        c429 = 0
        t_start: dict[str, tuple[str, float]] = {}
        for evf in sorted(bd.glob("*.events.jsonl")):
            for ln in evf.read_text(errors="ignore").splitlines():
                if not ln.strip():
                    continue
                if '"429"' in ln or "rate_limit" in ln.lower():
                    c429 += 1
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                tp = ev.get("type", "")
                if tp == "guardian.incident":
                    if not summaries:
                        g_inc += 1
                    if "death_or_connection_changed" in (ev.get("reasons") or []):
                        deaths += 1
                elif tp == "tool.started":
                    t_start[str(ev.get("call_id"))] = (str(ev.get("name") or "?"), float(ev.get("elapsed") or 0))
                elif tp == "tool.finished":
                    cid = str(ev.get("call_id"))
                    nm = str(ev.get("name") or "?")
                    if cid in t_start:
                        nm, s0 = t_start.pop(cid)
                        dt = max(0.0, float(ev.get("elapsed") or 0) - s0)
                        if dt >= 60.0:
                            slow_tools.append((dt, name, nm))
                    if ev.get("ok") is False:
                        err = ev.get("error")
                        code = err.get("code") if isinstance(err, dict) else str(err or "unknown_fail")
                        key = f"{nm}:{code}"
                        err_counts[key] += 1
                        err_bots[key].add(name)
        rows.append((squad, name, status, score, deaths, g_inc, c429, sorted(set(gained_all))))

    print(f"\n================ FLEET TRIAGE: {wave} ({len(rows)} bots) ================")
    for sq, nm, st, sc, dth, gi, r429, gnd in rows:
        g_str = ",".join(x.split("/")[-1] for x in gnd[:4])
        print(f"  [{sq:3s}] {nm:30s} {st:7s} score={sc:3d} deaths={dth:2d} guard={gi:2d} 429={r429:2d} gained=[{g_str}]")

    print("\n--- CROSS-MODEL P0/P1 BUG LEADERBOARD (ranked by affected_bots x count) ---")
    ranked = sorted(err_counts.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))
    for key, cnt in ranked[:15]:
        bots = sorted(err_bots[key])
        print(f"  {len(bots):2d} bots | {cnt:3d}x | {key:42s} | sample={bots[:4]}")

    if slow_tools:
        print("\n--- SLOW TOOL CALLS (>=60s) ---")
        for dt, nm, tool in sorted(slow_tools, reverse=True)[:10]:
            print(f"  {dt:5.1f}s | {nm:28s} | {tool}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("provision")
    lp = sub.add_parser("launch")
    lp.add_argument("--wave", required=True)
    lp.add_argument("--duration", type=float, default=1200.0)
    lp.add_argument("--max-episodes", type=int, default=20)
    lp.add_argument("--squads", default="nju,glm,ds,hy4")
    lp.add_argument("--stagger", type=float, default=1.5)
    tp = sub.add_parser("triage")
    tp.add_argument("--wave", required=True)
    args = ap.parse_args()
    if args.cmd == "provision":
        return cmd_provision()
    if args.cmd == "launch":
        return cmd_launch(args.wave, args.duration, args.max_episodes, {s.strip() for s in args.squads.split(",") if s.strip()}, args.stagger)
    return cmd_triage(args.wave)


if __name__ == "__main__":
    raise SystemExit(main())
