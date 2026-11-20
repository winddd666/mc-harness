"""Read-only acceptance metrics for the assigned D1 tickets."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from .fleet_lifecycle import LifecycleCounter
else:
    from fleet_lifecycle import LifecycleCounter


_HOT_FLOOR_HURT = re.compile(r"^\[受伤 [^\]]* by hot_floor\](?:×(\d+))?$")
_HTTP_FAILURE = re.compile(r"\bHTTP (429|502)\b|failed: \[(429|502)\]", re.I)
_FOUR_HOUR_WINDOW = (14_100, 14_700)
_PORTAL_ENTRY = "minecraft:story/enter_the_nether"
_DAY1_AFFECTED_BOTS = {"hy4_hy_6_bare", "hy4_hy_7_bare"}


@dataclass(frozen=True)
class TicketGate:
    ticket: str
    metric: str
    status: str
    actual: str
    threshold: str
    evidence: str


@dataclass
class Episode:
    ref: str
    bot: str
    status: str
    requests: int | None
    budget_class: str
    first_lines: dict[str, int] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)
    durations: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    hard_429: bool = False
    lost_to_http: bool = False
    attempts_429: list[tuple[int, int]] = field(default_factory=list)
    backoff_429: float = 0.0
    complete_trace: bool = False
    target: str = ""
    portal_verified: bool = False

    def count(self, key: str, line: int, count: int = 1) -> None:
        self.first_lines.setdefault(key, line)
        self.counts[key] += count

    def seconds(self, key: str, value: float, line: int) -> None:
        self.durations.setdefault(key, []).append((value, line))

    def at(self, line: int) -> str:
        return f"{self.ref}:{line}" if line else self.ref


def _object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _read(path: Path) -> dict:
    try:
        return _object(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _number(value) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _trace(path: Path, report: dict):
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line, raw in enumerate(handle, 1):
                yield line, _object(raw)
    else:
        for event in report.get("events") or ():
            yield 0, _object(event)


def _episode(bot_dir: Path, row: dict, seen_ids: set[tuple[str, str]]) -> Episode:
    basename = Path(row["report_file"]).name if row.get("report_file") else ""
    report_path = bot_dir / basename
    report = _read(report_path) if basename else {}
    trace = report_path.with_suffix(".events.jsonl") if basename else bot_dir / "missing"
    bot = bot_dir.name
    metrics = Episode(
        f"{bot} EP{row.get('episode')} {trace.name if trace.is_file() else basename}",
        bot, row.get("status", ""), row.get("actor_requests"), row.get("budget_class", ""),
    )
    metrics.target = str(row.get("target") or "")
    metrics.portal_verified = metrics.target == _PORTAL_ENTRY and (
        _PORTAL_ENTRY in (row.get("gained_advancements") or ())
        and (row.get("objective_verified") is True or metrics.status == "goal_satisfied")
    )
    life = LifecycleCounter(bot, seen_ids=seen_ids)
    starts: dict[str, tuple[str, float | None]] = {}
    skipped_calls: set[str] = set()
    water_starts: dict[str, float] = {}
    retries: dict[tuple[str, int], tuple[int, int]] = {}
    climb_streak = 0
    completed = None

    for line, event in _trace(trace, report):
        kind = event.get("type")
        scope = event.get("scope", "")
        elapsed = _number(event.get("elapsed"))
        death_count = life.deaths
        life.observe(event)
        if life.deaths > death_count:
            hot_floor_deaths = sum(
                count for cause, count in life.death_causes.items() if re.search(r"\bhot_floor\b", cause)
            )
            added = hot_floor_deaths - metrics.counts["hot_floor_deaths"]
            if added:
                metrics.count("hot_floor_deaths", line, added)
        if kind == "tool.started":
            fn = _object(event.get("function"))
            tool = str(fn.get("name") or event.get("tool") or "")
            args = _object(fn.get("arguments"))
            if event.get("call_id"):
                skipped_calls.discard(event["call_id"])
                starts[event["call_id"]] = (tool, elapsed)
            else:
                metrics.count("untimed_tool", line)
            if tool in ("mc_move_to", "mc_dig", "mc_place"):
                metrics.count("path_tool", line)
            if tool == "mc_get_entities":
                metrics.count("entity_scan", line)
                if (_number(args.get("radius")) or 0) >= 4096:
                    metrics.count("large_entity_scan", line)
            elif tool == "mc_move_to" and args.get("entity_id") is not None:
                metrics.count("entity_move", line)
        elif kind == "tool.skipped":
            call_id = event.get("call_id")
            starts.pop(call_id, None)
            if call_id:
                skipped_calls.add(call_id)
        elif kind == "tool.completed":
            call_id = event.get("call_id")
            # The scheduler also emits a synthetic completion for skipped tools.
            if call_id in skipped_calls:
                skipped_calls.remove(call_id)
                continue
            tool, start_time = starts.pop(call_id, (str(event.get("tool") or ""), None))
            duration = elapsed - start_time if elapsed is not None and start_time is not None else None
            result = _object(event.get("result"))
            raw_error = result.get("error")
            error = raw_error.get("code") if isinstance(raw_error, dict) else raw_error
            if duration is not None and duration >= 0:
                metrics.seconds("tool", duration, line)
                if tool == "mc_skill_unstick" or (tool == "mc_move_to" and error == "busy"):
                    metrics.seconds("unstick" if tool == "mc_skill_unstick" else "move_busy", duration, line)
            else:
                metrics.count("untimed_tool", line)
                if tool == "mc_skill_unstick":
                    metrics.count("untimed_unstick", line)
                if tool == "mc_move_to" and error == "busy":
                    metrics.count("untimed_move_busy", line)
            if tool == "mc_skill_harvest_logs":
                metrics.count("harvest_logs", line)
                if duration is None or duration < 0:
                    metrics.count("untimed_harvest_logs", line)
                elif duration >= 60:
                    metrics.count("slow_harvest_logs", line)
                    if duration >= 90:
                        target_gains = result.get("target_gained")
                        gains = result.get("gained")
                        if not isinstance(target_gains, dict) and isinstance(gains, dict):
                            target_gains = {
                                name: count for name, count in gains.items()
                                if name.endswith(("_log", "_stem", "_hyphae"))
                            }
                        if not isinstance(target_gains, dict):
                            metrics.count("unknown_harvest_gain", line)
                        elif not any((_number(count) or 0) > 0 for count in target_gains.values()):
                            metrics.count("empty_harvest_logs_90s", line)
            if error in ("path_not_found", "no_such_entity"):
                metrics.count(error, line)
            if tool == "mc_skill_mine_dfs":
                reason = result.get("stop_reason")
                climb_streak = climb_streak + 1 if reason == "climb_progress" else 0
                if reason in ("climb_progress", "climb_blocked", "surface_reached"):
                    metrics.count("climb", line)
                    metrics.count(reason, line)
                if climb_streak == 3:
                    metrics.count("climb_streak", line)
        elif kind == "guardian.action.started" and event.get("action") == "boundary_escape_water":
            if elapsed is not None:
                water_starts[scope] = elapsed
        elif kind == "guardian.action.completed" and event.get("action") == "boundary_escape_water":
            start_time = water_starts.pop(scope, None)
            duration = elapsed - start_time if elapsed is not None and start_time is not None else None
            if duration is not None and duration >= 0:
                metrics.seconds("escape_water", duration, line)
            else:
                metrics.count("untimed_water", line)
        elif kind == "mc.notable_events":
            for part in str(event.get("text") or "").split("; "):
                if match := _HOT_FLOOR_HURT.fullmatch(part):
                    metrics.count("hot_floor_hurt", line, int(match[1] or 1))
        elif kind == "model.request":
            metrics.count("model_request", line)
        elif kind == "model.http_retry":
            metrics.count("retry", line)
            request = (scope, event.get("request"))
            if event.get("status_code") == 429:
                metrics.backoff_429 += _number(event.get("wait_s")) or 0.0
                retries[request] = (retries.get(request, (0, 0))[0] + 1, line)
            else:
                retries.pop(request, None)
        elif kind in ("model.completed", "model.failed"):
            error = str(event.get("error") or "")
            match = _HTTP_FAILURE.search(error)
            status = next((code for code in match.groups() if code), "") if match else ""
            if status == "429":
                metrics.hard_429 = True
                metrics.first_lines.setdefault("hard_429", line)
            if status in ("429", "502") and metrics.status == "runtime_error":
                metrics.lost_to_http = True
                metrics.first_lines.setdefault("http_loss", line)
            attempt, retry_line = retries.pop((scope, event.get("request")), (0, line))
            if status == "429" or attempt >= 5 and kind == "model.completed":
                metrics.attempts_429.append((attempt + 1, retry_line))
        elif kind == "run.started":
            metrics.first_lines.setdefault("run_started", line)
        elif kind == "run.finished":
            completed = elapsed
            metrics.first_lines["run_finished"] = line

    budget = _number(report.get("budget_timeout_s"))
    if budget is not None and completed is not None:
        metrics.seconds("episode_overrun", completed - budget, metrics.first_lines["run_finished"])
    elif completed is not None:
        metrics.count("missing_budget", metrics.first_lines["run_finished"])
    if starts:
        metrics.count("untimed_tool", 0, len(starts))
        for tool, _ in starts.values():
            if tool == "mc_skill_unstick":
                metrics.count("untimed_unstick", 0)
            if tool == "mc_move_to":
                metrics.count("untimed_move_busy", 0)
            if tool == "mc_skill_harvest_logs":
                metrics.count("untimed_harvest_logs", 0)
    if water_starts:
        metrics.count("untimed_water", 0, len(water_starts))
    metrics.complete_trace = completed is not None or metrics.requests == 0 and bool(report)
    return metrics


def ticket_gates(wave_dir: Path) -> list[TicketGate]:
    """Evaluate thresholds from BUG_BOARD for the selected wave."""
    episodes: list[Episode] = []
    final: dict[str, dict] = {}
    final_scenes: dict[str, dict] = {}
    missing: list[str] = []
    seen_ids: set[tuple[str, str]] = set()
    for bot_dir in sorted(path for path in wave_dir.iterdir() if path.is_dir()):
        summaries = sorted(bot_dir.glob("wild-endurance-*.json"))
        report = _read(summaries[-1]) if summaries else {}
        summary = _object(report.get("curriculum_summary"))
        if summary:
            final[bot_dir.name] = summary
            final_scenes[bot_dir.name] = _object(report.get("scene"))
        else:
            missing.append(bot_dir.name)
            snapshots = sorted(bot_dir.glob("curriculum-*.json"))
            summary = _read(snapshots[-1]) if snapshots else {}
        episodes.extend(_episode(bot_dir, ep, seen_ids) for ep in summary.get("episodes") or ())

    results: list[TicketGate] = []
    missing_traces = [ep.ref for ep in episodes if not ep.complete_trace]

    def add(ticket: str, metric: str, value: str, threshold: str, passed: bool | None,
            evidence: str = "", *, needs_trace: bool = True) -> None:
        if missing or not final:
            passed = None
            evidence = f"provisional, {len(final)}/{len(final) + len(missing)} final summaries; {evidence}"
        elif needs_trace and missing_traces and passed is not False:
            passed = None
            evidence = f"{len(missing_traces)} incomplete traces, e.g. {missing_traces[0]}; {evidence}"
        status = "PENDING" if passed is None else "PASS" if passed else "FAIL"
        results.append(TicketGate(ticket, metric, status, value, threshold, evidence.rstrip("; ")))

    def total(group: list[Episode], key: str) -> tuple[int, str]:
        active = [ep for ep in group if ep.counts[key]]
        return sum(ep.counts[key] for ep in active), active[0].at(active[0].first_lines[key]) if active else ""

    def peak(key: str) -> tuple[int, str]:
        ep = max(episodes, key=lambda item: item.counts[key], default=None)
        return (ep.counts[key], ep.at(ep.first_lines.get(key, 0))) if ep and ep.counts[key] else (0, "")

    def timed(ticket: str, key: str, caption: str, limit: float, percentile: float = 1.0) -> None:
        samples = sorted((value, ep.at(line)) for ep in episodes for value, line in ep.durations.get(key, ()))
        if samples:
            value, trace = samples[math.ceil(len(samples) * percentile) - 1]
            missing_key = {
                "escape_water": "untimed_water",
                "episode_overrun": "missing_budget",
            }.get(key, f"untimed_{key}")
            untimed, missing_ref = total(episodes, missing_key)
            passed = value <= limit
            if untimed:
                if passed or percentile < 1.0:
                    passed = None
                trace += f"; {untimed} missing timing samples, e.g. {missing_ref}"
            add(ticket, caption, f"{value:.3f}s (n={len(samples)})", f"<= {limit:g}s", passed, trace)
        else:
            add(ticket, caption, "n=0", f"<= {limit:g}s", None, "no matching timed events")

    durations = {bot: _number(cs.get("duration_s")) for bot, cs in final.items()}
    four_hour = bool(durations) and all(
        d is not None and _FOUR_HOUR_WINDOW[0] <= d <= _FOUR_HOUR_WINDOW[1]
        for d in durations.values()
    )
    hard = [ep for ep in episodes if ep.hard_429]
    hard_sample = hard[0].at(hard[0].first_lines["hard_429"]) if hard else ""
    add("BUG-042", "12-bot ~4h hard 429 episodes", str(len(hard)), "<= 2",
        len(hard) <= 2 if four_hour and len(final) == 12 else None,
        hard_sample if four_hour and len(final) == 12 else "requires 12 bots with ~4h configured duration")
    attempts = [(count, ep.at(line)) for ep in episodes for count, line in ep.attempts_429]
    count, ref = min(attempts, default=(0, "no sustained 429 request"))
    add("BUG-042", "sustained 429 attempts", f"{count} (n={len(attempts)})", ">= 6",
        count >= 6 if attempts else None, ref)
    long_bots = {bot for bot, duration in durations.items() if duration is not None and duration >= 2700}
    losses = [ep for ep in episodes if ep.bot in long_bots and ep.lost_to_http]
    add("BUG-042", "long-slot 429/502 losses", str(len(losses)), "<= 1",
        len(losses) <= 1 if any(ep.bot in long_bots for ep in episodes) else None,
        losses[0].at(losses[0].first_lines["http_loss"]) if losses else f"{len(long_bots)} slots with duration >=2700s")
    bare = next((bot for bot in long_bots if bot == "hy4_hy_1_bare"), None)
    bare_score = _number(final[bare].get("final_score")) if bare else None
    add("BUG-042", "HY4 bare long-slot score", f"{bare_score:g}" if bare_score is not None else "n/a", ">= 60",
        bare_score >= 60 if bare_score is not None else None, bare or "needs comparable HY4 bare long slot", needs_trace=False)

    qwen = [ep for ep in episodes if ep.bot.startswith("glm_")]
    qwen_elapsed = sum(_number(cs.get("elapsed_s")) or 0 for bot, cs in final.items() if bot.startswith("glm_"))
    retries, retry_sample = total(qwen, "retry")
    requests, request_sample = total(qwen, "model_request")
    retry_rate = 100 * retries / (requests + retries) if requests else None
    backoff = 100 * sum(ep.backoff_429 for ep in qwen) / qwen_elapsed if requests and qwen_elapsed else None
    for label, value, evidence in (
        ("GLM cohort HTTP retry share", retry_rate, f"{retries} retries / ({requests} requests + {retries} retries); {retry_sample or request_sample}"),
        ("GLM cohort 429 backoff share", backoff, f"logged wait / {qwen_elapsed:.1f}s final Bot elapsed; {retry_sample or request_sample}"),
    ):
        add("BUG-045", label, f"{value:.2f}%" if value is not None else "n/a", "<= 5%",
            value <= 5 if value is not None else None, evidence)
    add("BUG-045", "wave hard 429 episodes", str(len(hard)), "<= 3",
        len(hard) <= 3 if requests or len(hard) > 3 else None,
        hard_sample or (request_sample if requests else "no GLM cohort requests"))

    ratios = [(elapsed / durations[bot], bot) for bot, cs in final.items()
              if (elapsed := _number(cs.get("elapsed_s"))) is not None and durations[bot]]
    ratio, bot = min(ratios, default=(0.0, "no valid duration/elapsed pair"))
    add("BUG-046", "~4h minimum elapsed/duration", f"{ratio:.4f} ({len(ratios)}/{len(final)} bots)", ">= 0.95",
        ratio >= 0.95 if four_hour and len(ratios) == len(final) else None, bot, needs_trace=False)

    timed("BUG-047", "tool", "tool p99", 60, .99)
    timed("BUG-047", "unstick", "unstick max", 30)
    timed("BUG-047", "move_busy", "move_to busy max", 20)
    timed("BUG-047", "episode_overrun", "episode elapsed minus budget max", 10)
    timed("BUG-056", "escape_water", "boundary_escape_water p99", 25, .99)
    timed("BUG-056", "escape_water", "boundary_escape_water max", 40)
    water = [ep for ep in episodes if ep.bot.startswith(("nju_movetest", "hy4_hy_9"))]
    water_timeouts = [ep for ep in water if ep.requests == 1 and ep.status == "timeout"]
    water_exposure = any(ep.durations.get("escape_water") for ep in water)
    add("BUG-056", "target bots req=1 timeout episodes", str(len(water_timeouts)), "== 0",
        not water_timeouts if water_timeouts or water_exposure else None,
        water_timeouts[0].at(water_timeouts[0].first_lines.get("run_finished", 0)) if water_timeouts
        else f"{len(water)} target-bot episodes; {'escape_water observed' if water_exposure else 'escape_water unobserved'}")

    hurt, hurt_ref = peak("hot_floor_hurt")
    deaths, death_ref = total(episodes, "hot_floor_deaths")
    add("BUG-057", "max hot_floor damage receipts per episode", str(hurt), "<= 10",
        hurt <= 10 if hurt else None, hurt_ref or "no hot_floor damage receipts")
    add("BUG-057", "wave hot_floor deaths", str(deaths), "<= 3",
        deaths <= 3 if hurt or deaths else None, death_ref or hurt_ref or "no hot_floor exposure")

    paths, path_ref = total(episodes, "path_not_found")
    path_peak, path_peak_ref = peak("path_not_found")
    any_tools = any(ep.counts["path_tool"] for ep in episodes)
    add("BUG-058", "path_not_found max per episode", str(path_peak), "<= 3",
        path_peak <= 3 if paths or any_tools else None, path_peak_ref)
    add("BUG-058", "path_not_found per wave", str(paths), "<= 300",
        paths <= 300 if paths or any_tools else None, path_ref)

    climb, climb_ref = total(episodes, "climb")
    progress, progress_ref = total(episodes, "climb_progress")
    reached, _ = total(episodes, "surface_reached")
    streaks = [ep for ep in episodes if ep.counts["climb_streak"]]
    add("BUG-059", "climb_progress / surface_reached", f"{progress}/{reached}", "<= 1:1",
        progress <= reached if progress or reached else None, progress_ref or climb_ref or "no climb outcomes")
    add("BUG-059", "episodes with >=3 consecutive climb_progress", str(len(streaks)), "<= 5",
        len(streaks) <= 5 if climb else None, streaks[0].at(streaks[0].first_lines["climb_streak"]) if streaks else climb_ref)
    deep_returns = [ep for ep in episodes if ep.budget_class in ("deep", "expedition") and ep.counts["climb"]]
    suspect = [ep for ep in deep_returns if ep.status == "timeout"]
    add("BUG-059", "deep return timeouts", f"{len(suspect)} candidates / {len(deep_returns)} deep climbs", "== 0",
        True if deep_returns and not suspect else None,
        f"trace causality at {suspect[0].at(suspect[0].first_lines['climb'])}" if suspect
        else deep_returns[0].at(deep_returns[0].first_lines["climb"]) if deep_returns else "requires deep climb exposure")

    stale, stale_ref = total(episodes, "no_such_entity")
    entity_move, entity_ref = total(episodes, "entity_move")
    big, big_ref = total(episodes, "large_entity_scan")
    scans, scan_ref = total(episodes, "entity_scan")
    add("BUG-060", "no_such_entity per wave", str(stale), "<= 5",
        stale <= 5 if stale or entity_move else None, stale_ref or entity_ref or "no entity move exposure")
    add("BUG-060", "get_entities radius >=4096", str(big), "== 0",
        big == 0 if scans else None, big_ref or scan_ref or "no get_entities calls")
    bare = {bot for bot, scene in final_scenes.items() if scene.get("stage") == "bare"}
    guarded = {bot for bot in bare if final_scenes[bot].get("day1") is True}
    if guarded or "day1" in wave_dir.name.lower():
        day1_bots = bare | {bot for bot in missing if bot.endswith("_bare")}
        day1 = [ep for ep in episodes if ep.bot in day1_bots]
        full_day1 = len(bare) == 15 and bare == {ep.bot for ep in day1}
        harvest, _ = total(day1, "harvest_logs")
        untimed, _ = total(day1, "untimed_harvest_logs")
        unknown_gain, _ = total(day1, "unknown_harvest_gain")
        harvest_scope = f"{len(bare)}/15 sealed bare slots, {harvest} harvest receipts, {untimed} untimed"
        for key, caption, missing_gain in (
            ("slow_harvest_logs", "Day 1 harvest_logs >=60s", 0),
            ("empty_harvest_logs_90s", "Day 1 harvest_logs >=90s with zero log gain", unknown_gain),
        ):
            count, sample = total(day1, key)
            passed = False if count else True if full_day1 and harvest and not (untimed or missing_gain) else None
            add("BUG-064", caption, str(count), "== 0", passed,
                sample or f"{harvest_scope}, {missing_gain} missing gain reports")

        # Dispatch and verified crossing are separate: an incidental advancement
        # during a bootstrap episode is neither an explicit dispatch nor a legal control.
        portal_dispatched: set[str] = set()
        portal_verified: set[str] = set()
        early_nether = []
        ambiguous_nether = []
        for ep in day1:
            if ep.target == _PORTAL_ENTRY:
                portal_dispatched.add(ep.bot)
                if ep.portal_verified:
                    portal_verified.add(ep.bot)
            elif ep.target.startswith("minecraft:nether/"):
                if ep.bot not in portal_dispatched:
                    early_nether.append(ep)
                elif ep.bot not in portal_verified:
                    ambiguous_nether.append(ep)
        guarded_scope = full_day1 and guarded == bare and _DAY1_AFFECTED_BOTS <= guarded
        scope_info = f"{len(guarded)}/15 final scene.day1=true, affected HY4_6/7 required"
        if ambiguous_nether:
            scope_info += f"; portal crossing unresolved before {ambiguous_nether[0].ref}"
        add("BUG-066", "Day 1 Nether targets before explicit portal dispatch",
            str(len(early_nether)), "== 0",
            False if early_nether else True if guarded_scope and not ambiguous_nether else None,
            early_nether[0].at(early_nether[0].first_lines.get("run_started", 0)) if early_nether else scope_info)
        legal_crossings = [ep for ep in day1 if ep.bot in guarded and ep.portal_verified]
        add("BUG-066", "Day 1 verified explicit portal crossing", str(len(legal_crossings)), ">= 1",
            True if legal_crossings else None,
            legal_crossings[0].at(legal_crossings[0].first_lines.get("run_started", 0)) if legal_crossings
            else "no guarded episode verified enter_the_nether under the explicit portal target")
    return results
