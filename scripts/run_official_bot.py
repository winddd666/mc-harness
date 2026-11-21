#!/usr/bin/env python3
"""Zero-RCON official server CurriculumSupervisor runner (reuses cached .state/tokens.json)."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime

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
    while True:
        try:
            provisioned = mgr.open_bot(args.bot)
            break
        except DeathLoopCooldownError as exc:
            wait_s = max(10.0, float(getattr(exc, "retry_after_s", 60.0) or 60.0) + 3.0)
            print(f"[Official Wait] bot={args.bot} hit death_loop_cooldown ({exc}); waiting {wait_s:.0f}s...", flush=True)
            time.sleep(wait_s)

    with provisioned.session as bot:
        st0 = bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        sx, sy, sz = float(pos0[0]), float(pos0[1]), float(pos0[2])
        print(
            f"[Official Start] bot={args.bot} server={st0.get('server')} "
            f"provider={args.provider} model={args.model or 'default'} "
            f"spawn=({sx:.1f}, {sy:.1f}, {sz:.1f}) hp={st0.get('health')} "
            f"tod={st0.get('time_of_day')} duration_s={args.duration_s}",
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
        t0 = time.monotonic()
        try:
            sup = CurriculumSupervisor(
                bot,
                chat_fn=qwen.chat,
                model_name=f"{qwen.model}@{qwen.base_url}",
                runs_dir=runs_dir,
                duration_s=args.duration_s,
                toolset=toolset,
                geofence=geofence,
                enable_pre_ensure=True,
                enable_island_recon=True,
                enable_async_world_jobs=True,
            )
            summary = sup.run(max_episodes=args.max_episodes or None)
        finally:
            qwen.close()

        elapsed_s = round(time.monotonic() - t0, 1)
        out = {
            "bot": args.bot,
            "server": st0.get("server"),
            "provider": args.provider,
            "model": args.model or qwen.model,
            "elapsed_s": elapsed_s,
            "curriculum_summary": summary,
        }
        out_path = runs_dir / f"official-{args.tag}-{args.bot}-{ts}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[Official Done] bot={args.bot} elapsed_s={elapsed_s} "
            f"final_score={summary.get('final_score')} episodes={len(summary.get('episodes', []))} "
            f"report={out_path}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
