#!/usr/bin/env python3
"""Fleet Orchestrator & Cross-Model P0/P1 Bug Aggregator for v2-integrated.

Squads:
  - nju  (3): NJU Qwen3.6-35B competition model (soft-oversubscribed 3-way on 2-concurrency gateway, rpm=9)
  - glm  (5): OpenCode GLM-5.3 (complex mid/late-game: fortress, bastion, village, enchant, midgame)
  - ds   (4): OpenCode DeepSeek-v4.1-flash (cost-controlled cross-validation)
  - hy4 (10): OpenCode HY4-flash-free (free high-concurrency broad terrain & edge-case sweep)
  - lc   (4): OpenCode LongCat-2.5-free (village and midgame coverage)

Usage:
  python scripts/v2_fleet.py provision
  python scripts/v2_fleet.py launch --wave r11 --duration 1200 [--squads nju,glm,ds,hy4,lc]
  python scripts/v2_fleet.py triage --wave r11 [--markdown [PATH]] [--no-markdown]

`triage` parses each bot's `wild-endurance-*.json` + `*.events.jsonl` and auto-exports
`runs/<wave>/triage-<wave>.md` with a cross-model error leaderboard and generated work orders.
Runs root can be overridden with `--runs-root` or `MC_FLEET_RUNS_ROOT`.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import glob
import json
import os
import pathlib
import subprocess
import sys
import time
from collections import Counter, defaultdict
from typing import TextIO

if __package__:
    from .fleet_lifecycle import LifecycleCounter
    from .v2_triage import is_http_429_failure
else:
    from fleet_lifecycle import LifecycleCounter
    from v2_triage import is_http_429_failure

WT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_ROOT = pathlib.Path("/home/xzq/projects/mc")
VENV_PY = MAIN_ROOT / ".venv/bin/python"
RUNNER = WT_ROOT / "scripts/l3_wild_endurance_runner.py"
ENV_FILE = MAIN_ROOT / ".env.beast"
FLEET_TOKEN_FILE = MAIN_ROOT / ".state-fleet/tokens.json"
RUNS_ROOT = pathlib.Path(os.environ.get("MC_FLEET_RUNS_ROOT") or (WT_ROOT / "runs"))
LOCK_DIR = WT_ROOT / ".state-fleet" / "locks"
SLOW_TOOL_THRESHOLD_S = 60.0
KNOWN_STAGES: tuple[str, ...] = (
    "village_to_stronghold_end",
    "day2_village_near",
    "nether_fortress",
    "brewing_alchemy",
    "nether_bastion",
    "enchant_anvil",
    "village_trade",
    "cave_combat",
    "deep_water",
    "midgame",
    "village",
    "nether",
    "bare",
    "iron",
)


def _stage_from_dirname(name: str) -> str:
    for stage in KNOWN_STAGES:  # longest first so nether_fortress wins over nether
        if name.endswith("_" + stage):
            return stage
    return name.split("_")[-1] if name.count("_") >= 2 else "?"

# Routing hints used to auto-assign exported Markdown tickets to the owning slice.
SLICE_HINTS: tuple[tuple[str, str], ...] = (
    ("mine_dfs", "Slice A (#3 Codex #1)"),
    ("tunnel", "Slice A (#3 Codex #1)"),
    ("staircase", "Slice C (#4 Codex #2)"),
    ("mc_move_to", "Slice C (#4 Codex #2)"),
    ("build_portal", "Slice C (#4 Codex #2)"),
    ("craft_", "Slice C (#4 Codex #2)"),
    ("smelt", "Slice C (#4 Codex #2)"),
    ("guardian", "Slice B (#2 子 AGY)"),
    ("combat", "Slice B (#2 子 AGY)"),
    ("safety", "Slice B (#2 子 AGY)"),
)


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
    duration_s: int = 1200


FLEET: tuple[FleetSlot, ...] = (
    # --- Squad 1: NJU Competition Benchmark (5 bots, soft-oversubscribed at rpm=6 <= 30 cap) ---
    FleetSlot("MoveTest_01", "nju", "nju", None, 6, "bare", 0, str(MAIN_ROOT / ".state-move-test/tokens.json"), duration_s=2700),
    FleetSlot("GuiTest_01", "nju", "nju", None, 6, "nether", 1, str(MAIN_ROOT / ".state-gui-test/tokens.json"), duration_s=1200),
    FleetSlot("NJU_3", "nju", "nju", None, 6, "village_trade", 2, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("DS_1", "nju", "nju", None, 6, "iron", 8, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("DS_2", "nju", "nju", None, 6, "cave_combat", 9, str(FLEET_TOKEN_FILE), duration_s=900),
    # --- Squad 2: GLM-5.3 Mid/Late-Game Strike Team (5 bots, rpm=10 anti-burst) ---
    FleetSlot("GLM_1", "glm", "opencode", "glm-5.3", 10, "nether_fortress", 3, str(MAIN_ROOT / ".state-beast/tokens.json"), duration_s=1200),
    FleetSlot("GLM_2", "glm", "opencode", "glm-5.3", 10, "nether_bastion", 4, str(MAIN_ROOT / ".state-beast/tokens.json"), duration_s=1200),
    FleetSlot("GLM_3", "glm", "opencode", "glm-5.3", 10, "village_trade", 5, str(MAIN_ROOT / ".state-beast/tokens.json"), duration_s=1200),
    FleetSlot("GLM_4", "glm", "opencode", "glm-5.3", 10, "brewing_alchemy", 6, str(MAIN_ROOT / ".state-beast/tokens.json"), duration_s=900),
    FleetSlot("GLM_5", "glm", "opencode", "glm-5.3", 10, "midgame", 7, str(MAIN_ROOT / ".state-glm5/tokens.json"), duration_s=2700),
    # --- Squad 3: HY4-flash-free Broad Sweeper (10 bots, rpm=6 anti-429, 100% free) ---
    FleetSlot("HY_1", "hy4", "opencode", "hy4-flash-free", 6, "bare", 12, str(FLEET_TOKEN_FILE), duration_s=2700),
    FleetSlot("HY_2", "hy4", "opencode", "hy4-flash-free", 6, "iron", 13, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("HY_3", "hy4", "opencode", "hy4-flash-free", 6, "midgame", 14, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("HY_4", "hy4", "opencode", "hy4-flash-free", 6, "cave_combat", 15, str(FLEET_TOKEN_FILE), duration_s=900),
    FleetSlot("HY_5", "hy4", "opencode", "hy4-flash-free", 6, "brewing_alchemy", 16, str(FLEET_TOKEN_FILE), duration_s=900),
    FleetSlot("HY_6", "hy4", "opencode", "hy4-flash-free", 6, "nether_bastion", 17, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("HY_7", "hy4", "opencode", "hy4-flash-free", 6, "village", 18, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("HY_8", "hy4", "opencode", "hy4-flash-free", 6, "nether", 19, str(FLEET_TOKEN_FILE), duration_s=1200),
    FleetSlot("HY_9", "hy4", "opencode", "hy4-flash-free", 6, "deep_water", 20, str(FLEET_TOKEN_FILE), duration_s=900),
    FleetSlot("HY_10", "hy4", "opencode", "hy4-flash-free", 6, "enchant_anvil", 21, str(FLEET_TOKEN_FILE), duration_s=900),
    # --- Squad 4: LongCat-2.5-free Auxiliary Sweeper (4 bots, rpm=6 anti-429, 100% free) ---
    FleetSlot("LC_1", "lc", "opencode", "longcat-2.5-free", 6, "day2_village_near", 10, str(FLEET_TOKEN_FILE), duration_s=1800),
    FleetSlot("LC_2", "lc", "opencode", "longcat-2.5-free", 6, "day2_village_near", 11, str(FLEET_TOKEN_FILE), duration_s=1800),
    FleetSlot("LC_3", "lc", "opencode", "longcat-2.5-free", 6, "midgame", 22, str(FLEET_TOKEN_FILE), duration_s=1800),
    FleetSlot("LC_4", "lc", "opencode", "longcat-2.5-free", 6, "midgame", 23, str(FLEET_TOKEN_FILE), duration_s=1800),
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


_RUNNER_MODULE: Any = None
_BASELINE_CACHE: dict[str, int] = {}


def _load_runner_module() -> Any:
    """Import the runner script once (module body only defines constants/functions)."""
    global _RUNNER_MODULE
    if _RUNNER_MODULE is not None:
        return _RUNNER_MODULE
    import importlib.util

    spec = importlib.util.spec_from_file_location("_l3_runner_scene", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import runner from {RUNNER}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses needs the module registered before exec
    spec.loader.exec_module(mod)
    _RUNNER_MODULE = mod
    return mod


def _load_runner_sites() -> list[tuple[int, int]]:
    try:
        mod = _load_runner_module()
    except Exception:
        return []
    return list(getattr(mod, "CANDIDATE_SITES", []))


def baseline_points_for_stage(stage: str) -> int:
    """Points the slice loadout pre-grants (P2-32); computed from the runner's loadout recipe."""
    if stage in _BASELINE_CACHE:
        return _BASELINE_CACHE[stage]
    pts = 0
    try:
        mod = _load_runner_module()
        info = mod.compute_stage_baseline(stage) or {}
        pts = int(info.get("points") or 0)
    except Exception:
        pts = 0
    _BASELINE_CACHE[stage] = pts
    return pts


def _assign_isolated_sites(selected: list[FleetSlot]) -> list[FleetSlot]:
    """Ensure every launched bot gets a unique scene anchor (avoids shared-island friendly fire)."""
    used: set[int] = set()
    out: list[FleetSlot] = []
    for slot in selected:
        idx = slot.site_index
        if idx in used:
            new_idx = idx
            while new_idx in used:
                new_idx += 1
            print(f"[Fleet Site] {slot.bot}: site {idx} already taken -> reassigned to {new_idx}", flush=True)
            idx = new_idx
        used.add(idx)
        out.append(dataclasses.replace(slot, site_index=idx) if idx != slot.site_index else slot)
    return out


def cmd_sites() -> int:
    sites = _load_runner_sites()
    if not sites:
        print(f"Could not load CANDIDATE_SITES from {RUNNER}")
        return 1
    print(f"Scene anchors ({len(sites)} available, {len(FLEET)} fleet slots):")
    for slot in FLEET:
        if slot.site_index >= len(sites):
            print(f"  {slot.bot:12s} {slot.squad:3s} {slot.stage:15s} site={slot.site_index:2d} -> OUT OF RANGE")
            continue
        ax, az = sites[slot.site_index]
        print(f"  {slot.bot:12s} {slot.squad:3s} {slot.stage:15s} site={slot.site_index:2d} -> ({ax}, {az})")
    seen: dict[int, str] = {}
    clash = 0
    for slot in FLEET:
        if slot.site_index in seen:
            clash += 1
            print(f"  [WARN] site {slot.site_index} shared by {seen[slot.site_index]} and {slot.bot}")
        else:
            seen[slot.site_index] = slot.bot
    print(f"Unique sites: {len(seen)}/{len(FLEET)} (clashes={clash})")
    return 0 if clash == 0 and max(s.site_index for s in FLEET) < len(sites) else 1


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_bot_locks(selected: list[FleetSlot], wave: str) -> dict[str, TextIO]:
    """Hold an exclusive lock for each bot's live session (BUG-036 single-token mutex)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    acquired: dict[str, TextIO] = {}
    try:
        for slot in selected:
            name = slot.bot.lower()
            if name in acquired:
                raise RuntimeError(f"Refusing to launch: {slot.bot} appears twice in wave '{wave}'.")
            lock = (LOCK_DIR / f"{name}.json").open("a+", encoding="utf-8")
            acquired[name] = lock
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"Refusing to launch: {slot.bot} already has an active fleet lock.") from None

            lock.seek(0)
            try:
                data = json.load(lock)
            except ValueError:
                data = {}
            # Earlier launchers wrote PID metadata without taking an OS lock.
            if isinstance(data, dict) and not data.get("flock"):
                legacy_pid = data.get("pid")
                if isinstance(legacy_pid, int) and _pid_alive(legacy_pid):
                    raise RuntimeError(
                        f"Refusing to launch: {slot.bot} is active in wave '{data.get('wave')}' (pid {legacy_pid})."
                    )
            lock.seek(0)
            lock.truncate()
            json.dump({"bot": slot.bot, "wave": wave, "pid": os.getpid(), "flock": True}, lock)
            lock.flush()
    except BaseException:
        _release_bot_locks(acquired)
        raise
    return acquired


def _release_bot_locks(locks: dict[str, TextIO]) -> None:
    for lock in locks.values():
        # Keep the inode in place. The child can still hold the same open file description.
        lock.close()


def cmd_launch(
    wave: str,
    duration_s: float,
    max_episodes: int,
    squads: set[str],
    stagger_s: float,
    use_slot_durations: bool = False,
    min_slot_duration: int | None = None,
    max_slot_duration: int | None = None,
    bots_filter: set[str] | None = None,
    rpm_override: int | None = None,
    stage_override: str | None = None,
) -> int:
    selected_raw = [
        dataclasses.replace(s, stage=stage_override) if stage_override else s
        for s in FLEET
        if s.squad in squads
        and (min_slot_duration is None or s.duration_s >= min_slot_duration)
        and (max_slot_duration is None or s.duration_s <= max_slot_duration)
        and (not bots_filter or s.bot.lower() in bots_filter)
    ]
    if not selected_raw:
        print("No fleet slots matched squads:", squads)
        return 1
    selected = _assign_isolated_sites(selected_raw)
    try:
        locks = _acquire_bot_locks(selected, wave)
    except (OSError, RuntimeError) as exc:
        print(str(exc))
        return 2
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(WT_ROOT / "src")
        procs: list[tuple[FleetSlot, subprocess.Popen, pathlib.Path]] = []
        dur_label = "per-slot" if use_slot_durations else str(duration_s)
        print(f"[Fleet Launch] wave={wave} bots={len(selected)} duration_s={dur_label} max_eps={max_episodes or 'deadline'}")
        for idx, slot in enumerate(selected):
            slot_dur = slot.duration_s if use_slot_durations else duration_s
            subdir = f"{wave}/{slot.squad}_{slot.bot.lower()}_{slot.stage}"
            log_dir = RUNS_ROOT / subdir
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "stdout.log"
            cmd = [
                str(VENV_PY),
                str(RUNNER),
                "--bot", slot.bot,
                "--token-file", slot.token_file,
                "--provider", slot.provider,
                "--rpm-budget", str(slot.rpm_budget if rpm_override is None else rpm_override),
                "--stage", slot.stage,
                "--runs-subdir", subdir,
                "--site-index", str(slot.site_index),
                "--duration-s", str(slot_dur),
                "--max-episodes", str(max_episodes),
                "--tag", f"{wave}_{slot.bot.lower()}_{slot.stage}",
            ]
            if slot.model:
                cmd.extend(["--model", slot.model])
            if stage_override == "bare":
                cmd.append("--day1")
            if idx > 0:
                cmd.append("--keep-time")
            with log_path.open("wb") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    pass_fds=(locks[slot.bot.lower()].fileno(),),
                )
            procs.append((slot, proc, log_path))
            print(f"  [{idx+1:2d}/{len(selected):2d}] started {slot.bot:12s} ({slot.squad:3s}/{slot.model or 'nju':19s}) stage={slot.stage:15s} dur={slot_dur:g}s pid={proc.pid}", flush=True)
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
    finally:
        _release_bot_locks(locks)
    return cmd_triage(wave)


def _decode_payload(raw: Any) -> dict[str, Any]:
    """Decode a tool result payload (JSON string / dict / None) into a dict."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {"_raw": raw[:400]}
    return obj if isinstance(obj, dict) else {"_value": obj}


def _extract_error_code(payload: dict[str, Any]) -> str:
    """Error code from a decoded tool payload (aligned with `v2_triage._extract_error_code`)."""
    if not payload:
        return "unknown_fail"
    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("reason")
        if code:
            return str(code)
    elif isinstance(err, str) and err:
        return err
    for key in ("stop_reason", "failure_reason", "reason"):
        if payload.get(key):
            return str(payload[key])
    pe = payload.get("partial_error")
    if isinstance(pe, dict) and pe.get("code"):
        return str(pe["code"])
    if isinstance(pe, str) and pe:
        return pe
    return "unknown_fail"


@dataclasses.dataclass
class BotRow:
    name: str
    squad: str
    stage: str
    status: str
    score: int | None
    episodes_done: int
    elapsed_s: float
    deaths: int
    guardian_incidents: int
    preemptions: int
    http_429: int
    rate_waits: int
    gained: list[str]
    top_tools: list[tuple[str, int, int]]
    top_errors: list[tuple[str, int]]
    slow_calls: list[tuple[float, str, str]]
    crash_tail: str = ""
    scene_stage: str = ""
    scene_ok: bool | None = None
    scene_anchor: str = ""
    scene_ticks: int | None = None
    scene_preempt_skips: int | None = None
    baseline_points: int | None = None
    earned_points: int | None = None
    error: str = ""
    death_receipts: int = 0
    respawns: int = 0
    connection_changes: int = 0
    mixed_guardian_incidents: int = 0
    http_429_retries: int = 0

    @property
    def gained_short(self) -> list[str]:
        return [x.split("/")[-1] for x in self.gained]


def _read_crash_tail(bd: pathlib.Path) -> str:
    log = bd / "stdout.log"
    if not log.is_file():
        return ""
    try:
        text = log.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if "Traceback (most recent call last)" not in text:
        return ""
    tail = text[-2000:]
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return "\n".join(lines[-6:])


def triage_bot_dir(bd: pathlib.Path) -> BotRow:
    """Parse one fleet bot directory (summary + raw events) into a normalized row."""
    name = bd.name
    squad = name.split("_")[0]
    stage = _stage_from_dirname(name)

    summaries = sorted(bd.glob("wild-endurance-*.json"))
    summary: dict[str, Any] = {}
    if summaries:
        try:
            summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    if not summary:
        cur = sorted(bd.glob("curriculum-*.json"))
        if cur:
            try:
                summary = json.loads(cur[-1].read_text(encoding="utf-8"))
            except Exception:
                summary = {}

    cs = summary.get("curriculum_summary")
    if not isinstance(cs, dict) or not cs:
        cs = summary
    episodes = cs.get("episodes") or summary.get("episodes") or []
    recorded_scores = (cs.get("final_score"), summary.get("final_score"), cs.get("current_score"))
    score_value = next((value for value in recorded_scores if value not in (None, "")), None)
    elapsed_s = float(cs.get("elapsed_s") or 0.0)
    gained: list[str] = []
    for ep in episodes:
        gained.extend(ep.get("gained_advancements") or [])

    guardian = summary.get("guardian_totals") or summary.get("guardian") or {}
    guardian_incidents = int(guardian.get("incidents", 0) or 0)
    preemptions = int(guardian.get("tool_preemptions", 0) or 0)

    tool_calls: Counter[str] = Counter()
    tool_fails: Counter[str] = Counter()
    err_codes: Counter[str] = Counter()
    slow_calls: list[tuple[float, str, str]] = []
    lifecycle = LifecycleCounter(name)
    http_429 = 0
    http_429_retries = 0
    rate_waits = 0
    event_preemptions = 0
    has_events = False

    for evf in sorted(bd.glob("*.events.jsonl")):
        call_start: dict[str, tuple[float, str]] = {}
        guardian_preemptions = 0
        preemption_receipts = 0
        try:
            lines = evf.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for ln in lines:
            if not ln.strip():
                continue
            has_events = True
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            lifecycle.observe(ev)
            tp = str(ev.get("type") or "")
            el = float(ev.get("elapsed") or 0.0)
            if tp in ("tool.started", "tool.ready"):
                cid = str(ev.get("call_id") or "")
                fn = ev.get("function") or {}
                tname = str(ev.get("tool") or ev.get("name") or (fn.get("name") if isinstance(fn, dict) else "") or "")
                if cid and tname:
                    call_start[cid] = (el, tname)
            elif tp == "tool.completed":
                cid = str(ev.get("call_id") or "")
                st_info = call_start.pop(cid, None)
                fn = ev.get("function") or {}
                tname = str(
                    ev.get("tool")
                    or ev.get("name")
                    or (fn.get("name") if isinstance(fn, dict) else "")
                    or (st_info[1] if st_info else "unknown")
                )
                dt = max(0.0, el - st_info[0]) if st_info else float(ev.get("duration") or 0.0)
                tool_calls[tname] += 1
                payload = _decode_payload(ev.get("result") if "result" in ev else ev.get("output"))
                if payload.get("preempted_by_reflex") is True:
                    preemption_receipts += 1
                elif payload.get("ok") is False:
                    code = _extract_error_code(payload)
                    tool_fails[tname] += 1
                    err_codes[f"{tname}:{code}"] += 1
                if dt >= SLOW_TOOL_THRESHOLD_S:
                    slow_calls.append((dt, name, tname))
            elif tp == "guardian.preempted_tool":
                guardian_preemptions += 1
            elif tp == "model.rate_wait":
                if float(ev.get("wait_s") or 0.0) > 0:
                    rate_waits += 1
            elif tp == "model.http_retry" and ev.get("status_code") in (429, "429"):
                http_429_retries += 1
            elif tp == "model.failed" and is_http_429_failure(ev):
                http_429 += 1
        # Guardian emits both a dedicated event and the resulting tool receipt.
        event_preemptions += max(guardian_preemptions, preemption_receipts)

    if summaries:
        status = str(cs.get("status") or "done")
    elif has_events:
        status = "incomplete"
    else:
        status = "empty"
    crash_tail = _read_crash_tail(bd)
    if crash_tail and not summaries:
        status = "crash"
    score = int(score_value) if score_value is not None and status != "not_ready" else None

    slow_calls.sort(reverse=True)
    scene_meta = summary.get("scene") or {}
    scene_anchor = ""
    if isinstance(scene_meta.get("anchor"), dict):
        a = scene_meta["anchor"]
        scene_anchor = f"({a.get('x')}, {a.get('y')}, {a.get('z')})"
    baseline_raw = scene_meta.get("baseline_points")
    if isinstance(baseline_raw, int):
        baseline_points = baseline_raw
    elif stage == "brewing_alchemy" and summaries and summary.get("spawn") is not None:
        # Finished summaries before P2-32 predate removal of the 30-point Nether travel grant.
        baseline_points = 50
    else:
        baseline_points = baseline_points_for_stage(stage)
    return BotRow(
        name=name,
        squad=squad,
        stage=stage,
        status=status,
        score=score,
        episodes_done=len(episodes),
        elapsed_s=elapsed_s,
        deaths=lifecycle.deaths,
        guardian_incidents=guardian_incidents,
        preemptions=max(preemptions, event_preemptions),
        http_429=http_429,
        http_429_retries=http_429_retries,
        rate_waits=rate_waits,
        gained=sorted(set(gained)),
        top_tools=[(k, tool_calls[k], tool_fails[k]) for k in sorted(tool_calls, key=lambda t: -tool_calls[t])[:6]],
        top_errors=err_codes.most_common(5),
        slow_calls=slow_calls[:4],
        crash_tail=crash_tail,
        scene_stage=str(scene_meta.get("stage") or ""),
        scene_ok=scene_meta.get("entry_verified") if "entry_verified" in scene_meta else None,
        scene_anchor=scene_anchor,
        scene_ticks=scene_meta.get("watchdog_ticks") if isinstance(scene_meta.get("watchdog_ticks"), int) else None,
        scene_preempt_skips=(
            scene_meta.get("watchdog_preempt_skips")
            if isinstance(scene_meta.get("watchdog_preempt_skips"), int)
            else None
        ),
        baseline_points=baseline_points,
        earned_points=score - baseline_points if score is not None else None,
        error=str(cs.get("error") or summary.get("error") or scene_meta.get("error") or ""),
        death_receipts=lifecycle.death_receipts,
        respawns=lifecycle.respawns,
        connection_changes=lifecycle.connection_changes,
        mixed_guardian_incidents=lifecycle.mixed_guardian_incidents,
    )


def _suggest_owner(key: str) -> str:
    low = key.lower()
    for needle, owner in SLICE_HINTS:
        if needle in low:
            return owner
    if low.startswith("mc_skill_"):
        return "Slice C (#4 Codex #2)"
    return "Slice D2 (#6 DeepSeek #2) / 待分诊"


SQUAD_LABELS: dict[str, str] = {
    "nju": "NJU Qwen3.6-35B",
    "glm": "GLM-5.3",
    "ds": "DeepSeek-v4.1-flash",
    "hy4": "HY4-flash-free",
    "lc": "LongCat-2.5-free",
}


def aggregate_by_squad(rows: list[BotRow]) -> list[dict[str, Any]]:
    """Cross-model aggregation: one row per model family (squad)."""
    buckets: defaultdict[str, list[BotRow]] = defaultdict(list)
    for r in rows:
        buckets[r.squad].append(r)
    out: list[dict[str, Any]] = []
    for squad, group in sorted(buckets.items()):
        done = [r for r in group if r.status in ("done", "completed")]
        scores = [r.score for r in done if r.score is not None]
        earned = [r.earned_points for r in done if r.earned_points is not None]
        errors: Counter[str] = Counter()
        for r in group:
            for key, cnt in r.top_errors:
                errors[key] += cnt
        out.append(
            {
                "squad": squad,
                "label": SQUAD_LABELS.get(squad, squad),
                "bots": len(group),
                "completed": len(done),
                "crashed": sum(1 for r in group if r.status == "crash"),
                "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
                "avg_baseline": round(
                    sum(int(r.baseline_points or 0) for r in done) / len(done), 1
                ) if done else 0.0,
                "avg_earned": round(sum(earned) / len(earned), 1) if earned else 0.0,
                "total_deaths": sum(r.deaths for r in group),
                "avg_deaths": round(sum(r.deaths for r in group) / len(group), 2) if group else 0.0,
                "http_429": sum(r.http_429 for r in group),
                "http_429_retries": sum(r.http_429_retries for r in group),
                "top_error": errors.most_common(1)[0][0] if errors else "-",
            }
        )
    return out


def _write_markdown(
    wave: str,
    rows: list[BotRow],
    err_counts: Counter[str],
    err_bots: defaultdict[str, set[str]],
    slow_tools: list[tuple[float, str, str]],
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Export a ready-to-file Markdown triage report with auto-generated work orders."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_done = sum(1 for r in rows if r.status in ("done", "completed"))
    n_crash = sum(1 for r in rows if r.status == "crash")
    n_incomplete = sum(1 for r in rows if r.status == "incomplete")
    lines: list[str] = [
        f"# FLEET TRIAGE — `{wave}`",
        "",
        f"> 自动生成于 {time.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"bot 目录 `{len(rows)}` · completed `{n_done}` · crash `{n_crash}` · incomplete `{n_incomplete}`",
        "",
        "## 1. 各路成绩总览（baseline = 场景 loadout 预授分，earned = 实机净增分）",
        "",
        "| Squad | Bot | Stage | Status | Score | Baseline | Earned | Eps | Deaths | ID-less deaths | Respawn/spawn | Connection changes | Mixed guardian | Guard | Preempt | 429 Failures | 429 Retries | Gained |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in sorted(rows, key=lambda x: (x.squad, x.name)):
        g = ", ".join(r.gained_short[:4])
        score = r.score if r.score is not None else "—"
        earned = r.earned_points if r.earned_points is not None else "—"
        lines.append(
            f"| {r.squad} | `{r.name}` | {r.stage} | {r.status} | {score} | "
            f"{int(r.baseline_points or 0)} | {earned} | {r.episodes_done} | "
            f"{r.deaths} | {r.death_receipts} | {r.respawns} | {r.connection_changes} | "
            f"{r.mixed_guardian_incidents} | {r.guardian_incidents} | {r.preemptions} | "
            f"{r.http_429} | {r.http_429_retries} | {g} |"
        )
    lines.extend(
        [
            "",
            "> `Score` = `final_score`（含 loadout 预授）；`Baseline` = 场景 loadout 预授予的成就分；"
            "`Earned` = Score - Baseline，跨场景横向比较请看 Earned。`—` 表示无可用分数。",
            "> `Deaths` 为按 Bot 和源事件 ID 去重的死亡，加上无 ID 的明确死亡回执。"
            "`Connection changes` 仅统计带类型的连接变化，`Mixed guardian` 记录旧的混合告警。",
        ]
    )
    lines.extend(["", "## 2. 模型维度聚合（跨模型对比）", ""])
    agg = aggregate_by_squad(rows)
    lines.extend(
        [
            "| 模型 | Bots | Completed | Crash | 平均分 | 平均 Baseline | 平均 Earned | 总死亡 | 场均死亡 | 429 Failures | 429 Retries | 头号错误 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for a in agg:
        lines.append(
            f"| {a['label']} | {a['bots']} | {a['completed']} | {a['crashed']} | {a['avg_score']} | "
            f"{a['avg_baseline']} | {a['avg_earned']} | {a['total_deaths']} | {a['avg_deaths']} | "
            f"{a['http_429']} | {a['http_429_retries']} | `{a['top_error']}` |"
        )
    scene_bad = [r for r in rows if r.scene_ok is False]
    lines.extend(["", "## 3. 场景入口校验", ""])
    if scene_bad:
        lines.append("| Bot | Stage | Anchor | 问题 |")
        lines.append("|---|---|---|---|")
        for r in scene_bad:
            problem = "启动未就绪" if r.status == "not_ready" else "场景入口未通过"
            lines.append(
                f"| `{r.name}` | {r.scene_stage or r.stage} | {r.scene_anchor} | `{problem}` |"
            )
    else:
        lines.append("*（所有已收官 bot 场景入口正常）*")
    wd_rows = [r for r in rows if r.scene_ticks is not None]
    if wd_rows:
        lines.extend(["", "| Bot | watchdog ticks | 反射抢占跳过 |", "|---|---:|---:|"])
        for r in wd_rows:
            lines.append(f"| `{r.name}` | {r.scene_ticks} | {r.scene_preempt_skips} |")

    lines.extend(["", "## 4. 跨模型 P0/P1 错误榜单", ""])
    ranked = sorted(err_counts.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))
    if ranked:
        lines.extend(
            [
                "| # | 影响 bot 数 | 次数 | 错误码 | 样本 bot | 建议归属 |",
                "|---:|---:|---:|---|---|---|",
            ]
        )
        for i, (key, cnt) in enumerate(ranked[:20], 1):
            bots = sorted(err_bots[key])
            lines.append(f"| {i} | {len(bots)} | {cnt} | `{key}` | {', '.join(bots[:4])} | {_suggest_owner(key)} |")
    else:
        lines.append("*（无工具失败事件）*")
    lines.extend(["", "## 5. 自动工单（Work Orders）", ""])
    ticket_no = 0
    for r in rows:
        if r.status == "not_ready":
            ticket_no += 1
            lines.extend(
                [
                    f"### FLEET-{ticket_no:03d} | P1 | 启动未就绪 | 状态: OPEN",
                    f"- **影响 bot**：`{r.name}` (stage=`{r.stage}`)",
                    f"- **错误**：`{r.error or 'not_ready'}`",
                    "- **验收标准**：恢复后通过场景入口并进入首集。启动失败时产出 `status=not_ready` summary，"
                    "其余槽位正常运行。",
                    "",
                ]
            )
        if r.status == "crash":
            ticket_no += 1
            lines.extend(
                [
                    f"### FLEET-{ticket_no:03d} | P0 | 进程崩溃 | 状态: OPEN",
                    f"- **影响 bot**：`{r.name}` (stage=`{r.stage}`)",
                    "- **证据**：`stdout.log` traceback tail：",
                    "```text",
                    (r.crash_tail or "<no tail>"),
                    "```",
                    "- **验收标准**：同场景重放触发步骤，进程正常收官并产出 summary。",
                    "",
                ]
            )
    for key, cnt in ranked[:10]:
        if cnt < 2 and len(err_bots[key]) < 2:
            continue
        ticket_no += 1
        bots = sorted(err_bots[key])
        lines.extend(
            [
                f"### FLEET-{ticket_no:03d} | P1 | `{key}` | 状态: OPEN",
                f"- **影响范围**：{len(bots)} bots / {cnt} 次 — {', '.join('`'+b+'`' for b in bots[:8])}",
                f"- **建议归属**：{_suggest_owner(key)}",
                "- **验收标准**：对应工具失败次数归零或返回可恢复错误码，补离线回归单测并跑通全量 `pytest`。",
                "",
            ]
        )
    if slow_tools:
        lines.extend(["## 6. 超时穿透工具（≥{:.0f}s）".format(SLOW_TOOL_THRESHOLD_S), "",
                      "| 耗时 | Bot | 工具 |", "|---:|---|---|"])
        for dt, nm, tool in sorted(slow_tools, reverse=True)[:15]:
            lines.append(f"| {dt:.0f}s | `{nm}` | `{tool}` |")
        lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def cmd_triage(wave: str, markdown: str | bool | None = None, runs_root: pathlib.Path | None = None) -> int:
    root = runs_root or RUNS_ROOT
    wave_dir = root / wave
    bot_dirs = sorted(p for p in wave_dir.iterdir() if p.is_dir()) if wave_dir.is_dir() else []
    if not bot_dirs:
        print(f"No run directories found under {wave_dir}")
        return 1

    rows: list[BotRow] = []
    err_counts: Counter[str] = Counter()
    err_bots: defaultdict[str, set[str]] = defaultdict(set)
    slow_tools: list[tuple[float, str, str]] = []

    for bd in bot_dirs:
        row = triage_bot_dir(bd)
        rows.append(row)
        for key, cnt in row.top_errors:
            err_counts[key] += cnt
            err_bots[key].add(row.name)
        slow_tools.extend(row.slow_calls)

    print(f"\n================ FLEET TRIAGE: {wave} ({len(rows)} bots) ================")
    for r in rows:
        score = r.score if r.score is not None else "—"
        earned = r.earned_points if r.earned_points is not None else "—"
        print(
            f"  [{r.squad:3s}] {r.name:30s} {r.status:10s} score={score:>3} "
            f"base={int(r.baseline_points or 0):3d} earned={earned:>3} "
            f"eps={r.episodes_done:2d} "
            f"deaths={r.deaths:2d} (idless={r.death_receipts}) respawn/spawn={r.respawns} "
            f"connection_changes={r.connection_changes} guard_mixed={r.mixed_guardian_incidents} "
            f"guard={r.guardian_incidents:2d} preempt={r.preemptions:2d} "
            f"429_failed={r.http_429:2d} 429_retries={r.http_429_retries:2d} gained=[{','.join(r.gained_short[:4])}]"
            + (" scene=BAD" if r.scene_ok is False else "")
            + (f" wd={r.scene_ticks}skips={r.scene_preempt_skips}" if r.scene_ticks is not None else "")
        )
        if r.status == "crash" and r.crash_tail:
            tail = r.crash_tail.splitlines()[-1]
            print(f"        crash: {tail[:150]}")
        if r.status == "not_ready":
            print(f"        not_ready: {(r.error or 'unknown')[:150]}")

    print("\n--- CROSS-MODEL AGGREGATE (per model family) ---")
    for a in aggregate_by_squad(rows):
        print(
            f"  {a['label']:22s} bots={a['bots']:2d} done={a['completed']:2d} crash={a['crashed']:d} "
            f"avg_score={a['avg_score']:6.1f} avg_base={a['avg_baseline']:5.1f} "
            f"avg_earned={a['avg_earned']:6.1f} deaths={a['total_deaths']:3d} "
            f"avg_deaths={a['avg_deaths']:4.2f} 429_failed={a['http_429']:d} "
            f"429_retries={a['http_429_retries']:d} top={a['top_error']}"
        )

    print("\n--- CROSS-MODEL P0/P1 BUG LEADERBOARD (ranked by affected_bots x count) ---")
    ranked = sorted(err_counts.items(), key=lambda kv: (-len(err_bots[kv[0]]), -kv[1]))
    if ranked:
        for key, cnt in ranked[:15]:
            bots = sorted(err_bots[key])
            print(f"  {len(bots):2d} bots | {cnt:3d}x | {key:48s} | sample={bots[:4]}")
    else:
        print("  (no tool-failure events parsed)")

    if slow_tools:
        print(f"\n--- SLOW TOOL CALLS (>={SLOW_TOOL_THRESHOLD_S:.0f}s) ---")
        for dt, nm, tool in sorted(slow_tools, reverse=True)[:10]:
            print(f"  {dt:5.1f}s | {nm:28s} | {tool}")

    if markdown is not False:
        out_path = (
            pathlib.Path(markdown).expanduser()
            if isinstance(markdown, str)
            else wave_dir / f"triage-{wave}.md"
        )
        try:
            written = _write_markdown(wave, rows, err_counts, err_bots, slow_tools, out_path)
            print(f"\n[Markdown 工单] {written}")
        except OSError as exc:
            print(f"\n[Markdown 工单] 导出失败: {exc}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("provision")
    sub.add_parser("sites")
    lp = sub.add_parser("launch")
    lp.add_argument("--wave", required=True)
    lp.add_argument("--duration", type=float, default=1200.0)
    lp.add_argument("--slot-durations", action="store_true", help="Use each FleetSlot's wall-clock duration")
    lp.add_argument("--min-slot-duration", type=int, default=None, help="Only launch slots with duration_s >= N (decouples long runs)")
    lp.add_argument("--max-slot-duration", type=int, default=None, help="Only launch slots with duration_s <= N (decouples short/mid runs)")
    lp.add_argument("--max-episodes", type=int, default=0, help="Optional per-bot episode cap (0 runs to the wall-clock deadline)")
    lp.add_argument("--squads", default="nju,glm,hy4")
    lp.add_argument("--bots", default=None, help="Optional comma-separated bot names filter (e.g. HY_2,HY_5,HY_6,HY_9)")
    lp.add_argument("--rpm-override", type=int, default=None, help="Optional per-bot RPM budget override for selected slots")
    lp.add_argument("--stage-override", choices=KNOWN_STAGES, help="Override all selected slots (bare uses portal-safe Day 1 setup)")
    lp.add_argument("--stagger", type=float, default=1.5)
    tp = sub.add_parser("triage")
    tp.add_argument("--wave", required=True)
    tp.add_argument("--runs-root", default=None, help="Override runs/ root (default: MC_FLEET_RUNS_ROOT or <worktree>/runs)")
    tp.add_argument("--markdown", nargs="?", const=True, default=None, help="Export Markdown work orders (optional path)")
    tp.add_argument("--no-markdown", action="store_true", help="Skip Markdown export")
    args = ap.parse_args()
    if args.cmd == "provision":
        return cmd_provision()
    if args.cmd == "sites":
        return cmd_sites()
    if args.cmd == "launch":
        if args.max_episodes < 0:
            lp.error("--max-episodes must be zero or positive")
        bots_filter = {b.strip().lower() for b in args.bots.split(",") if b.strip()} if args.bots else None
        return cmd_launch(
            args.wave,
            args.duration,
            args.max_episodes,
            {s.strip() for s in args.squads.split(",") if s.strip()},
            args.stagger,
            use_slot_durations=args.slot_durations,
            min_slot_duration=args.min_slot_duration,
            max_slot_duration=args.max_slot_duration,
            bots_filter=bots_filter,
            rpm_override=args.rpm_override,
            stage_override=args.stage_override,
        )
    md: str | bool | None = False if args.no_markdown else args.markdown
    return cmd_triage(args.wave, md, pathlib.Path(args.runs_root).expanduser() if args.runs_root else None)



if __name__ == "__main__":
    raise SystemExit(main())
