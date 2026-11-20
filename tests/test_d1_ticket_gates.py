from __future__ import annotations

import json
from pathlib import Path

from scripts.d1_ticket_gates import ticket_gates


def _write_episode(bot: Path, number: int, events: list[dict], *, status: str = "answered") -> dict:
    stem = f"episode-{number}"
    (bot / f"{stem}.json").write_text(json.dumps({"budget_timeout_s": 90}))
    (bot / f"{stem}.events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    return {
        "episode": number, "status": status, "actor_requests": 1,
        "budget_class": "deep", "report_file": f"/old/location/{stem}.json",
    }


def _tool(call_id: str, name: str, start: float, end: float, args: dict, result: dict) -> list[dict]:
    return [
        {"type": "tool.started", "call_id": call_id, "elapsed": start,
         "function": {"name": name, "arguments": json.dumps(args)}},
        {"type": "tool.completed", "call_id": call_id, "elapsed": end, "result": json.dumps(result)},
    ]


def test_final_summary_and_episode_local_gate_metrics(tmp_path):
    bot = tmp_path / "glm_glm_2_nether_bastion"
    bot.mkdir()
    first = _write_episode(bot, 1, [
        {"type": "model.request", "request": 1},
        {"type": "model.http_retry", "request": 1, "status_code": 429, "attempt": 1, "wait_s": 5},
        {"type": "model.failed", "request": 1, "error": "Qwen API 返回 HTTP 429"},
        {"type": "tool.started", "call_id": "reused", "elapsed": 40,
         "function": {"name": "mc_move_to"}},
        {"type": "run.finished", "elapsed": 45},
    ], status="runtime_error")
    second_events = [
        {"type": "tool.completed", "call_id": "reused", "elapsed": 1, "result": {"error": {"code": "busy"}}},
        {"type": "model.request", "request": 1},
        {"type": "model.completed", "request": 1},
        {"type": "mc.notable_events", "text": "[受伤 health=19 by hot_floor]×11; [死亡 pos={} hot_floor]×4"},
    ]
    second_events += _tool("second", "mc_skill_unstick", 1, 36, {}, {"ok": True})
    second_events += _tool("third", "mc_get_entities", 37, 38, {"radius": 4096}, {"ok": True})
    for index in range(3):
        second_events += _tool(str(index), "mc_skill_mine_dfs", 39 + index * 10, 40 + index * 10,
                               {"action": "climb_out"}, {"stop_reason": "climb_progress"})
    second_events += [{"type": "run.finished", "elapsed": 101, "status": "timeout"}]
    second = _write_episode(bot, 2, second_events, status="timeout")
    (bot / "curriculum-stale.json").write_text(json.dumps({"episodes": []}))
    (bot / "wild-endurance-final.json").write_text(json.dumps({"curriculum_summary": {
        "duration_s": 14_400, "elapsed_s": 200, "episodes": [first, second],
    }}))

    results = {gate.metric: gate for gate in ticket_gates(tmp_path)}
    assert results["sustained 429 attempts"].actual == "2 (n=1)"
    assert results["sustained 429 attempts"].status == "FAIL"
    assert results["GLM cohort HTTP retry share"].actual == "33.33%"
    assert results["~4h minimum elapsed/duration"].status == "FAIL"
    assert results["unstick max"].actual == "35.000s (n=1)"
    assert results["move_to busy max"].status == "PENDING"
    assert results["episode elapsed minus budget max"].actual == "11.000s (n=2)"
    assert results["max hot_floor damage receipts per episode"].actual == "11"
    assert results["wave hot_floor deaths"].actual == "4"
    assert results["wave hot_floor deaths"].evidence.endswith("episode-2.events.jsonl:4")
    assert results["episodes with >=3 consecutive climb_progress"].actual == "1"
    assert results["deep return timeouts"].status == "PENDING"
    assert results["deep return timeouts"].actual == "1 candidates / 1 deep climbs"
    assert results["get_entities radius >=4096"].status == "FAIL"


def test_in_progress_snapshot_has_provisional_findings(tmp_path):
    bot = tmp_path / "hy4_hy_9_deep_water"
    bot.mkdir()
    ep = _write_episode(bot, 1, [
        {"type": "guardian.action.started", "action": "boundary_escape_water", "elapsed": 10},
        {"type": "guardian.action.completed", "action": "boundary_escape_water", "elapsed": 55, "duration": 1},
        {"type": "run.finished", "elapsed": 56},
    ])
    (bot / "curriculum-live.json").write_text(json.dumps({"episodes": [ep]}))

    results = ticket_gates(tmp_path)
    assert all(gate.status == "PENDING" and "0/1 final summaries" in gate.evidence for gate in results)
    assert next(gate.actual for gate in results if gate.metric == "boundary_escape_water max") == "45.000s (n=1)"


def test_skipped_tool_completion_is_excluded_from_timing(tmp_path):
    bot = tmp_path / "hy4_hy_9_deep_water"
    bot.mkdir()
    events = _tool("executed", "mc_move_to", 1, 15, {}, {"ok": False})
    events.extend([
        {"type": "tool.skipped", "call_id": "skipped", "reason": "previous_action_failed", "elapsed": 15},
        {"type": "tool.completed", "call_id": "skipped", "result": {"ok": False}, "elapsed": 15},
        {"type": "run.finished", "elapsed": 16},
    ])
    ep = _write_episode(bot, 1, events)
    (bot / "wild-endurance-final.json").write_text(json.dumps({"curriculum_summary": {
        "duration_s": 1200, "elapsed_s": 1200, "episodes": [ep],
    }}))

    metric = next(gate for gate in ticket_gates(tmp_path) if gate.metric == "tool p99")
    assert (metric.status, metric.actual) == ("PASS", "14.000s (n=1)")
