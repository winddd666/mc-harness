#!/usr/bin/env python3
"""Zero-RCON official server CurriculumSupervisor runner (reuses cached .state/tokens.json)."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mc_agent import BotManager
from mc_agent.config import Settings
from mc_agent.errors import DeathLoopCooldownError
from mc_agent.harness.curriculum import CurriculumSupervisor
from mc_agent.harness.geofence import VoidGeofence
from mc_agent.harness.mc_tools import McToolset
from mc_agent.harness.model import QwenModel, QwenSpec


def main() -> int:
    ap = argparse.ArgumentParser(description="Run CurriculumSupervisor directly on official MC server (no RCON).")
    ap.add_argument("--bot", required=True, help="Official bot username (e.g. 5Ease_Bot, GLM_1)")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="Env file for official server")
    ap.add_argument("--provider", default="nju", help="LLM provider (nju/opencode/deepseek/dashscope)")
    ap.add_argument("--model", default=None, help="Optional model override (e.g. hy4-flash-free)")
    ap.add_argument("--rpm-budget", type=float, default=None, help="Per-process RPM cap")
    ap.add_argument("--duration-s", type=float, default=1200.0, help="Wall-clock budget in seconds")
    ap.add_argument("--max-episodes", type=int, default=0, help="Optional episode cap (0 = run until deadline)")
    ap.add_argument("--geofence-radius", type=float, default=1200.0, help="Safe geofence radius around spawn")
    ap.add_argument("--tag", default="official", help="Run folder tag under runs/")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_dir = REPO_ROOT / "runs" / args.tag / args.bot
    runs_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings.load(dotenv_path=args.env_file)
    mgr = BotManager(settings)
    t0 = time.monotonic()
    overall_deadline = t0 + max(30.0, float(args.duration_s))
    all_episodes: list[dict] = []
    last_summary: dict = {}
    last_server: str | None = None
    last_model: str = args.model or args.provider
    reconnect_count = 0

    while True:
        remaining_s = overall_deadline - time.monotonic()
        if remaining_s < 15.0:
            break
        try:
            provisioned = mgr.open_bot(args.bot)
        except DeathLoopCooldownError as exc:
            wait_s = max(10.0, float(getattr(exc, "retry_after_s", 60.0) or 60.0) + 3.0)
            wait_s = min(wait_s, max(0.0, overall_deadline - time.monotonic()))
            if wait_s < 1.0:
                break
            print(f"[Official Wait] bot={args.bot} hit death_loop_cooldown ({exc}); waiting {wait_s:.0f}s...", flush=True)
            time.sleep(wait_s)
            continue
        except (httpx.TimeoutException, httpx.TransportError, ConnectionError, OSError) as exc:
            reconnect_count += 1
            wait_s = min(15.0, max(0.0, overall_deadline - time.monotonic()))
            if wait_s < 1.0:
                break
            print(
                f"[Official Reconnect #{reconnect_count}] bot={args.bot} open_bot transient error: "
                f"{type(exc).__name__}: {exc}; retrying in {wait_s:.0f}s...",
                flush=True,
            )
            time.sleep(wait_s)
            continue

        try:
            with provisioned.session as bot:
                st0 = bot.status()
                last_server = st0.get("server")
                pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
                sx, sy, sz = float(pos0[0]), float(pos0[1]), float(pos0[2])
                print(
                    f"[Official Start] bot={args.bot} server={last_server} "
                    f"provider={args.provider} model={args.model or 'default'} "
                    f"spawn=({sx:.1f}, {sy:.1f}, {sz:.1f}) hp={st0.get('health')} "
                    f"tod={st0.get('time_of_day')} remaining_s={remaining_s:.1f}",
                    flush=True,
                )

                geofence = VoidGeofence(center_x=sx, center_z=sz, radius=args.geofence_radius, margin=8.0)
                toolset = McToolset(bot, geofence=geofence, wait_for_cooldown=True)
                qwen = QwenModel(
                    QwenSpec(
                        dotenv_path=args.env_file,
                        fallback_dotenv_path=str(REPO_ROOT / ".env"),
                        provider=args.provider,
                        model_override=args.model,
                        rpm_budget_override=args.rpm_budget,
                    )
                )
                last_model = args.model or qwen.model
                try:
                    sup = CurriculumSupervisor(
                        bot,
                        chat_fn=qwen.chat,
                        model_name=f"{qwen.model}@{qwen.base_url}",
                        runs_dir=runs_dir,
                        duration_s=remaining_s,
                        toolset=toolset,
                        geofence=geofence,
                        enable_pre_ensure=True,
                        enable_island_recon=True,
                        enable_async_world_jobs=True,
                    )
                    summary = sup.run(max_episodes=args.max_episodes or None)
                    last_summary = summary or {}
                    all_episodes.extend(last_summary.get("episodes", []))
                finally:
                    qwen.close()
            break
        except (httpx.TimeoutException, httpx.TransportError, ConnectionError, OSError, DeathLoopCooldownError) as exc:
            reconnect_count += 1
            wait_s = float(getattr(exc, "retry_after_s", 12.0) or 12.0) + 2.0
            wait_s = min(max(10.0, wait_s), max(0.0, overall_deadline - time.monotonic()))
            if wait_s < 1.0:
                break
            print(
                f"[Official Reconnect #{reconnect_count}] bot={args.bot} interrupted by "
                f"{type(exc).__name__}: {exc}; auto-reconnecting in {wait_s:.0f}s...",
                flush=True,
            )
            time.sleep(wait_s)

    elapsed_s = round(time.monotonic() - t0, 1)
    out = {
        "bot": args.bot,
        "server": last_server,
        "provider": args.provider,
        "model": last_model,
        "elapsed_s": elapsed_s,
        "reconnect_count": reconnect_count,
        "curriculum_summary": last_summary,
        "total_episodes": len(all_episodes),
    }
    out_path = runs_dir / f"official-{args.tag}-{args.bot}-{ts}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[Official Done] bot={args.bot} elapsed_s={elapsed_s} reconnects={reconnect_count} "
        f"final_score={last_summary.get('final_score')} episodes={len(all_episodes)} "
        f"report={out_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
