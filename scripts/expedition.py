"""Managed multi-stage expedition CLI (`scout`, `stage`, `disconnect`) with L0 safety & diff summaries."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mc_agent.bot import BotManager, BotSession, ProvisionedBot
from mc_agent.config import Settings
from mc_agent.errors import ApiError, DeathLoopCooldownError, McAgentError, RateLimitError
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.events import JsonlEvents
from mc_agent.harness.model import QwenModel, QwenSpec
from mc_agent.skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier

EXPEDITION_DIR = Path("runs/expedition-20261114")


def _inv_map(bot: BotSession) -> dict[str, int]:
    inv = bot.inventory()
    out: dict[str, int] = {}
    for item in inv.get("items") or []:
        n = item.get("name")
        c = int(item.get("count") or 0)
        if n and c > 0:
            out[n] = out.get(n, 0) + c
    for slot, eq in (inv.get("armor") or {}).items():
        if isinstance(eq, dict) and eq.get("name"):
            key = f"armor:{slot}:{eq['name']}"
            out[key] = 1
    off = inv.get("offhand")
    if isinstance(off, dict) and off.get("name"):
        out[f"offhand:{off['name']}"] = int(off.get("count") or 1)
    return out


def _open_bot_resilient(
    mgr: BotManager,
    bot_name: str | None,
    *,
    wait_ready: bool = True,
) -> ProvisionedBot:
    """Open bot session and wait once if blocked briefly by 30s reconnect cooldown."""
    try:
        return mgr.open_bot(bot_name, wait_ready=wait_ready)
    except DeathLoopCooldownError:
        raise
    except ApiError as exc:
        retry_s = getattr(exc, "retry_after_s", None)
        is_cooldown = isinstance(exc, RateLimitError) or any(
            k in f"{exc.code} {exc.message}".lower() for k in ("cooldown", "reconnect", "rate_limit", "30")
        )
        if is_cooldown and (retry_s is None or retry_s <= 35.0):
            wait_s = max(1.0, float(retry_s) if retry_s is not None else 32.0)
            print(f"[RECONNECT COOLDOWN] 等待 {wait_s:.1f}s 后重试连入...", flush=True)
            time.sleep(wait_s)
            return mgr.open_bot(bot_name, wait_ready=wait_ready)
        raise


def _safe_disconnect(session: BotSession) -> dict[str, Any]:
    try:
        res = session.disconnect()
        return {"ok": True, "result": res}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def cmd_scout(args: argparse.Namespace) -> int:
    settings = Settings.load(dotenv_path=args.env_file)
    mgr = BotManager(settings)
    prov = _open_bot_resilient(mgr, args.bot or settings.default_bot_username, wait_ready=True)
    EXPEDITION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        st = prov.status or prov.session.status()
        inv = _inv_map(prov.session)
        adv = compute_advancement_frontier(prov.session, limit=15)
        blocks_summary = prov.session.call("find_blocks", {"radius": 32, "limit": 60})
        entities = prov.session.call("get_entities", {"radius": 48})

        pos = st.get("pos") or [0, 64, 0]
        bx, by, bz = int(round(pos[0])), int(round(pos[1])), int(round(pos[2]))
        vertical = []
        for dy in range(-5, 3):
            try:
                vertical.append(
                    {
                        "dy": dy,
                        "pos": [bx, by + dy, bz],
                        "block": prov.session.call("get_block", {"pos": [bx, by + dy, bz]}).get("name"),
                    }
                )
            except ApiError as exc:
                vertical.append({"dy": dy, "error": exc.code})

        # Also check ladders / water / vines / signs / stairs / portals near platform
        exits = prov.session.call(
            "find_blocks",
            {
                "radius": 24,
                "types": [
                    "water",
                    "ladder",
                    "vine",
                    "oak_stairs",
                    "stone_stairs",
                    "cobblestone_stairs",
                    "nether_portal",
                    "oak_sign",
                    "oak_wall_sign",
                    "chest",
                    "crafting_table",
                    "furnace",
                    "white_bed",
                ],
                "limit": 40,
            },
        )
        out = {
            "timestamp": datetime.now().isoformat(),
            "bot_username": prov.bot_username,
            "mc_api_base_url": settings.mc_api_base_url,
            "status": {
                k: st.get(k)
                for k in (
                    "pos",
                    "yaw",
                    "dimension",
                    "health",
                    "food",
                    "is_day",
                    "time_of_day",
                    "on_ground",
                    "in_water",
                    "in_lava",
                    "held",
                    "offhand",
                    "nearest_hostile",
                )
            },
            "inventory": inv,
            "advancements": {
                "completed_count": adv["completed_count"],
                "completed_ids": adv["completed_ids"],
                "frontier": adv["frontier"],
            },
            "vertical_column": vertical,
            "exits_and_stations": exits,
            "blocks_r32": blocks_summary,
            "entities_r48": {
                "total": entities.get("total"),
                "entities": (entities.get("entities") or [])[:20],
            },
        }
        out_path = EXPEDITION_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-scout.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"SCOUT_SAVED: {out_path}")
        return 0
    finally:
        if not args.keep_online:
            d = _safe_disconnect(prov.session)
            print("DISCONNECT:", json.dumps(d, ensure_ascii=False))
        prov.session.close()


def cmd_stage(args: argparse.Namespace) -> int:
    EXPEDITION_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.name}"
    report_path = EXPEDITION_DIR / f"{stem}.report.json"
    event_path = EXPEDITION_DIR / f"{stem}.events.jsonl"
    summary_path = EXPEDITION_DIR / f"{stem}.summary.json"

    settings = Settings.load(dotenv_path=args.env_file)
    llm = QwenModel(
        QwenSpec(
            dotenv_path=args.env_file,
            fallback_dotenv_path=".env",
            provider=args.provider,
            enable_thinking=args.thinking,
            max_tokens=args.max_tokens,
        )
    )
    mgr = BotManager(settings)
    prov = _open_bot_resilient(mgr, args.bot or settings.default_bot_username, wait_ready=True)
    started = time.monotonic()

    try:
        if args.bunker_first:
            st_boot = prov.session.status()
            if st_boot.get("alive") is False:
                time.sleep(3.0)
            bunker_res = MacroSkills(prov.session).dig_safety_bunker()
            print("BUNKER_FIRST:", json.dumps(bunker_res, ensure_ascii=False))

        try:
            st_before = check_and_stabilize(prov.session, emergency_health=args.min_start_hp)
        except SafetyEmergency as exc:
            raise RuntimeError(f"开局触发安全急停：{exc}") from None

        inv_before = _inv_map(prov.session)
        adv_before = compute_advancement_frontier(prov.session, limit=15)
        completed_before = set(adv_before["completed_ids"])

        print(
            f"[STAGE START] name={args.name} bot={prov.bot_username} provider={llm.model}@{llm.base_url} "
            f"pos={st_before.get('pos')} hp={st_before.get('health')} food={st_before.get('food')}",
            flush=True,
        )

        with llm, JsonlEvents(event_path) as sink:
            report = run_mc_agent(
                args.goal,
                prov.session,
                chat_fn=llm.chat,
                model_name=f"{llm.model}@{llm.base_url}",
                max_calls=args.max_calls,
                run_timeout_s=args.timeout_s,
                stream=True,
                on_event=sink,
                inbox=None,
            )

        # Post-stage stabilize & inventory/advancement diff
        post_emergency = None
        try:
            st_after = check_and_stabilize(prov.session, emergency_health=min(args.min_start_hp, 3.0))
        except SafetyEmergency as exc:
            post_emergency = exc.reason
            st_after = exc.status
        inv_after = _inv_map(prov.session)
        adv_after = compute_advancement_frontier(prov.session, limit=15)
        completed_after = set(adv_after["completed_ids"])
        newly_unlocked = sorted(completed_after - completed_before)

        all_keys = sorted(set(inv_before) | set(inv_after))
        inv_delta = {k: inv_after.get(k, 0) - inv_before.get(k, 0) for k in all_keys if inv_after.get(k, 0) != inv_before.get(k, 0)}

        tool_errors = [
            {"tool": ev.get("name"), "snippet": str(ev.get("result") or "")[:180]}
            for ev in (report.get("events") or [])
            if ev.get("type") == "tool.done" and '"ok": false' in str(ev.get("result") or "")
        ]

        elapsed_s = round(time.monotonic() - started, 2)
        summary = {
            "stage": args.name,
            "elapsed_s": elapsed_s,
            "provider_model": report.get("model"),
            "status": report.get("status"),
            "error": report.get("error"),
            "post_emergency": post_emergency,
            "actor_requests": report.get("actor_requests"),
            "max_calls": args.max_calls,
            "pos_before": st_before.get("pos"),
            "pos_after": st_after.get("pos"),
            "health_after": st_after.get("health"),
            "food_after": st_after.get("food"),
            "inv_delta": inv_delta,
            "inv_after": inv_after,
            "completed_count": adv_after["completed_count"],
            "newly_unlocked": newly_unlocked,
            "frontier_top10": adv_after["frontier"][:10],
            "tool_error_count": len(tool_errors),
            "tool_errors": tool_errors[:12],
            "answer": report.get("answer"),
            "report_path": str(report_path),
            "event_log": str(event_path),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"SUMMARY_SAVED: {summary_path}")
        return 0 if report.get("status") == "answered" and post_emergency is None else 1
    finally:
        llm.close()
        if not args.keep_online:
            d = _safe_disconnect(prov.session)
            print("DISCONNECT:", json.dumps(d, ensure_ascii=False))
        prov.session.close()


def cmd_disconnect(args: argparse.Namespace) -> int:
    settings = Settings.load(dotenv_path=args.env_file)
    mgr = BotManager(settings)
    try:
        prov = mgr.open_bot(args.bot or settings.default_bot_username, wait_ready=False)
    except McAgentError as exc:
        print(f"打开会话失败：{exc}")
        return 1
    try:
        try:
            st = prov.session.status()
            if st.get("alive"):
                try:
                    check_and_stabilize(prov.session)
                except Exception:
                    pass
        except ApiError as exc:
            if exc.code == "not_connected":
                print(json.dumps({"ok": True, "already_offline": True}, ensure_ascii=False))
                return 0
        res = _safe_disconnect(prov.session)
        print(json.dumps({"disconnect": res}, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    finally:
        prov.session.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="expedition")
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_scout = sub.add_parser("scout")
    p_scout.add_argument("--env-file", default=None)
    p_scout.add_argument("--bot", default=None)
    p_scout.add_argument("--keep-online", action="store_true")

    p_stage = sub.add_parser("stage")
    p_stage.add_argument("--name", required=True)
    p_stage.add_argument("--goal", required=True)
    p_stage.add_argument("--provider", choices=["nju", "deepseek", "dashscope"], default="deepseek")
    p_stage.add_argument("--max-calls", type=int, default=25)
    p_stage.add_argument("--timeout-s", type=float, default=600.0)
    p_stage.add_argument("--max-tokens", type=int, default=1024)
    p_stage.add_argument("--thinking", action="store_true")
    p_stage.add_argument("--env-file", default=None)
    p_stage.add_argument("--bot", default=None)
    p_stage.add_argument("--keep-online", action="store_true")
    p_stage.add_argument("--min-start-hp", type=float, default=3.0)
    p_stage.add_argument("--bunker-first", action="store_true")

    p_disc = sub.add_parser("disconnect")
    p_disc.add_argument("--env-file", default=None)
    p_disc.add_argument("--bot", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "scout":
            return cmd_scout(args)
        if args.subcommand == "stage":
            return cmd_stage(args)
        if args.subcommand == "disconnect":
            return cmd_disconnect(args)
        return 2
    except McAgentError as exc:
        print(f"MC_AGENT_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"EXPEDITION_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
