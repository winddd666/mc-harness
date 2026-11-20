from __future__ import annotations

import json
from pathlib import Path

from scripts import d1_acceptance
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

def test_day1_reports_old_failures_and_distinguishes_incidental_portal_entry(tmp_path):
    wave = tmp_path / "r41_day1_test"
    bot = wave / "hy4_hy_6_bare"
    bot.mkdir(parents=True)
    events = _tool("harvest", "mc_skill_harvest_logs", 1, 92, {}, {
        "ok": False, "gained": {"oak_sapling": 2},
    }) + [{"type": "run.finished", "elapsed": 93}]
    bootstrap = _write_episode(bot, 1, events)
    bootstrap.update(target="bootstrap_stone_age", gained_advancements=["minecraft:story/enter_the_nether"])
    nether = _write_episode(bot, 2, [
        {"type": "run.started"}, {"type": "run.finished", "elapsed": 20},
    ])
    nether["target"] = "minecraft:nether/find_fortress"
    (bot / "wild-endurance-final.json").write_text(json.dumps({
        "scene": {"stage": "bare"}, "curriculum_summary": {"episodes": [bootstrap, nether]},
    }))

    gates = {gate.metric: gate for gate in ticket_gates(wave)}
    assert gates["Day 1 harvest_logs >=60s"].status == "FAIL"
    assert gates["Day 1 harvest_logs >=90s with zero log gain"].status == "FAIL"
    assert gates["Day 1 Nether targets before explicit portal dispatch"].status == "FAIL"
    assert gates["Day 1 Nether targets before explicit portal dispatch"].evidence.endswith("episode-2.events.jsonl:1")
    assert gates["Day 1 verified explicit portal crossing"].status == "PENDING"


def test_day1_guards_require_full_scope_and_verify_explicit_portal_crossing(tmp_path):
    portal_target = "minecraft:story/enter_the_nether"
    for index in range(1, 16):
        bot = tmp_path / f"hy4_hy_{index}_bare"
        bot.mkdir()
        first = _write_episode(bot, 1, _tool("harvest", "mc_skill_harvest_logs", 1, 59, {}, {
            "ok": True, "target_gained": {"oak_log": 4},
        }) + [{"type": "run.finished", "elapsed": 60}])
        rows = [first]
        if index == 6:
            rows[0].update(target=portal_target, gained_advancements=[portal_target], objective_verified=True)
            followup = _write_episode(bot, 2, [{"type": "run.finished", "elapsed": 20}])
            followup["target"] = "minecraft:nether/find_fortress"
            rows.append(followup)
        (bot / "wild-endurance-final.json").write_text(json.dumps({
            "scene": {"stage": "bare", "day1": True}, "curriculum_summary": {"episodes": rows},
        }))

    gates = {gate.metric: gate for gate in ticket_gates(tmp_path)}
    assert gates["Day 1 harvest_logs >=60s"].status == "PASS"
    assert gates["Day 1 harvest_logs >=90s with zero log gain"].status == "PASS"
    dispatch = "Day 1 Nether targets before explicit portal dispatch"
    crossing = "Day 1 verified explicit portal crossing"
    assert gates[dispatch].status == "PASS"
    assert gates[crossing].status == "PASS"

    report = tmp_path / "hy4_hy_6_bare" / "wild-endurance-final.json"
    payload = json.loads(report.read_text())
    payload["curriculum_summary"]["episodes"][0]["objective_verified"] = False
    report.write_text(json.dumps(payload))

    gates = {gate.metric: gate for gate in ticket_gates(tmp_path)}
    assert gates[dispatch].status == "PENDING"
    assert gates[crossing].status == "PENDING"


def test_late_staircase_and_shaft_retreat_gates_have_separate_exposure(tmp_path):
    bot = tmp_path / "hy4_hy_6_iron"
    bot.mkdir()
    final = bot / "wild-endurance-final.json"
    normal = _tool("flee", "mc_skill_flee", 10, 15, {}, {"ok": True, "mode": "flee"})
    normal += _tool("early", "mc_skill_staircase", 16, 56, {}, {"ok": True})
    normal += _tool("refused", "mc_skill_staircase", 87, 87.01, {}, {
        "ok": False, "error": {"code": "episode_deadline_imminent"},
    })
    normal.append({"type": "run.finished", "elapsed": 89})
    rows = [_write_episode(bot, 1, normal)]
    final.write_text(json.dumps({"curriculum_summary": {"episodes": rows}}))

    gates = {gate.metric: gate for gate in ticket_gates(tmp_path)}
    assert gates["staircase started in final 10s, max overrun"].status == "PASS"
    assert gates["actor flee max"].status == "PASS"
    assert gates["actor retreat_shaft max"].status == "PENDING"

    late = _tool("shaft", "mc_skill_flee", 0, 79.5, {}, {"ok": True, "mode": "retreat_shaft"})
    late += _tool("staircase", "mc_skill_staircase", 88, 124, {}, {"ok": True})
    late.append({"type": "run.finished", "elapsed": 124.1})
    rows.append(_write_episode(bot, 2, late, status="timeout"))
    final.write_text(json.dumps({"curriculum_summary": {"episodes": rows}}))

    gates = {gate.metric: gate for gate in ticket_gates(tmp_path)}
    assert gates["staircase started in final 10s, max overrun"].actual == "34.000s (n=2)"
    assert gates["staircase started in final 10s, max overrun"].status == "FAIL"
    assert gates["actor flee max"].status == "FAIL"
    assert gates["actor retreat_shaft max"].actual == "79.500s (n=1)"
    assert gates["actor retreat_shaft max"].status == "FAIL"


def test_acceptance_counts_missing_summaries_after_actor_start_as_runner_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(d1_acceptance, "WT", tmp_path)
    wave = tmp_path / "runs" / "fixture"
    startup = wave / "startup"
    actor = wave / "actor"
    thread = wave / "thread"
    for bot in (startup, actor, thread):
        bot.mkdir(parents=True)
    traceback = 'Traceback (most recent call last):\n  File "l3_wild_endurance_runner.py", line 2, in <module>\n'
    (startup / "stdout.log").write_text(traceback)
    (actor / "stdout.log").write_text(traceback)
    (thread / "stdout.log").write_text("Exception in thread watchdog:\nTraceback (most recent call last):\n")
    _write_episode(actor, 1, [
        {"type": "run.started", "scope": "actor", "elapsed": 0},
        {"type": "run.finished", "scope": "actor", "elapsed": 12, "status": "runtime_error"},
    ])

    metrics = d1_acceptance.collect("fixture")

    assert metrics["startup_crash"] == ["startup"]
    assert metrics["runner_abort"] == ["actor"]
    assert metrics["thread_errors"] == ["thread"]
    assert metrics["runtime_error"] == 0


def test_negative_climb_is_detected_even_when_receipt_reports_time_slice(tmp_path):
    bot = tmp_path / "hy4_hy_7_iron"
    bot.mkdir()
    events = _tool("climb", "mc_skill_mine_dfs", 0, 20, {"action": "climb_out"}, {
        "ok": False, "action": "climb_out", "stop_reason": "climb_progress",
        "climb": {"start_y": -9, "end_y": 21},
    }) + _tool("regression", "mc_skill_mine_dfs", 21, 31, {"action": "climb_out"}, {
        "ok": False, "action": "climb_out", "stop_reason": "time_slice",
        "climb": {"start_y": 21, "end_y": 15},
    }) + _tool("return", "mc_skill_mine_dfs", 32, 41, {"action": "return_collar"}, {
        "ok": True, "action": "return_collar", "climb": {"start_y": 15, "end_y": 14},
    }) + [{"type": "run.finished", "elapsed": 45}]
    ep = _write_episode(bot, 1, events)
    (bot / "wild-endurance-final.json").write_text(json.dumps({"curriculum_summary": {"episodes": [ep]}}))

    gate = next(gate for gate in ticket_gates(tmp_path) if gate.metric.startswith("measured climb_out"))
    assert (gate.status, gate.actual) == ("FAIL", "1/2")
    assert gate.evidence.endswith("episode-1.events.jsonl:4")
