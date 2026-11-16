"""Tests for read-only `mc-live` server, replay engine, safety redlines, and live WS tailing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from mc_agent.auth import BotTokenRecord, TokenStore
from mc_agent.config import Settings
from mc_agent.live.server import _COORD_KEY, ReadOnlyMcClient, build_parser, create_app
from mc_agent.live.state import VoxelCache, list_runs, parse_replay_bundle, resolve_safe_run_path

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RUNS_DIR = REPO_ROOT / "runs"


def test_voxel_cache_cap_and_eviction() -> None:
    cache = VoxelCache(max_blocks=120)
    batch = [{"pos": [i, 64, 0], "name": "stone"} for i in range(180)]
    batch.append({"pos": [179, 65, 0], "name": "crafting_table"})
    added, removed = cache.put_blocks(batch, anchor_pos=[175.0, 64.0, 0.0])
    assert len(added) == 181
    assert removed == []
    assert len(cache) == 120

    current_positions = {tuple(b["pos"]) for b in cache.to_list()}
    # Blocks near anchor [175, 64, 0] and crafting_table station should survive eviction
    assert (175, 64, 0) in current_positions
    assert (179, 65, 0) in current_positions

    rem = cache.remove_positions([[175, 64, 0], {"pos": [174, 64, 0]}])
    assert [175, 64, 0] in rem
    assert (175, 64, 0) not in {tuple(b["pos"]) for b in cache.to_list()}


def test_synthetic_replay_bundle_parsing(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    log_path = runs_dir / "synthetic.events.jsonl"

    initial_task = (
        "采集铁矿并烧制铁锭\n\n[开局实时状态快照]\n"
        'status: {"ok": true, "result": {"username": "SynthBot", "pos": [10.5, 70.0, -5.5], '
        '"yaw": -1.57, "dimension": "overworld", "health": 20, "food": 19, '
        '"held": {"name": "stone_pickaxe", "count": 1, "slot": 36}, '
        '"last_death": {"pos": [2.0, 64.0, 2.0], "dropped_items_at": [2.0, 64.0, 2.0], "cause": "Creeper"}}}\n'
        'inventory: {"ok": true, "result": {"items": [{"name": "stone_pickaxe", "count": 1, "slot": 36}], '
        '"armor": {}, "offhand": null, "held": {"name": "stone_pickaxe", "count": 1, "slot": 36}, "empty_slots": 35}}\n'
        'nearby_blocks_summary(r=16): {"ok": true, "result": {"blocks": '
        '{"crafting_table": {"count": 1, "nearest": [12, 70, -5], "dist": 1.5}, '
        '"iron_ore": {"count": 2, "nearest": [14, 69, -4], "dist": 4.1}}}}'
    )

    events = [
        {
            "seq": 1,
            "time": "2026-11-14T08:00:00+00:00",
            "elapsed": 0.1,
            "type": "run.started",
            "task": initial_task,
            "bot": "SynthBot",
            "model": "default@https://api.nju-aia.com/v1",
            "max_calls": 6,
        },
        {"seq": 2, "time": "2026-11-14T08:00:00+00:00", "elapsed": 0.2, "type": "model.started", "request": 1, "stream": True},
        {"seq": 3, "time": "2026-11-14T08:00:00+00:00", "elapsed": 0.25, "type": "model.rate_wait", "request": 1, "wait_s": 1.25},
        {"seq": 4, "time": "2026-11-14T08:00:01+00:00", "elapsed": 1.5, "type": "model.text.delta", "request": 1, "text": "先去挖铁矿"},
        {"seq": 5, "time": "2026-11-14T08:00:01+00:00", "elapsed": 1.7, "type": "model.text.delta", "request": 1, "text": "再烧铁。"},
        {
            "seq": 6,
            "time": "2026-11-14T08:00:02+00:00",
            "elapsed": 2.0,
            "type": "model.usage",
            "request": 1,
            "usage": {"prompt_tokens": 5000, "completion_tokens": 50, "total_tokens": 5050, "prompt_tokens_details": {"cached_tokens": 4000}},
        },
        {
            "seq": 7,
            "time": "2026-11-14T08:00:02+00:00",
            "elapsed": 2.05,
            "type": "model.completed",
            "request": 1,
            "duration": 1.85,
            "message": {
                "role": "assistant",
                "content": "先去挖铁矿再烧铁。",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "mc_move_to", "arguments": '{"x": 14, "y": 69, "z": -4}'}},
                    {"id": "c2", "type": "function", "function": {"name": "mc_dig", "arguments": '{"x": 14, "y": 69, "z": -4}'}},
                ],
            },
        },
        {"seq": 8, "time": "2026-11-14T08:00:02+00:00", "elapsed": 2.06, "type": "tool.ready", "call_id": "c1", "concurrency": "exclusive"},
        {"seq": 9, "time": "2026-11-14T08:00:02+00:00", "elapsed": 2.07, "type": "tool.ready", "call_id": "c2", "concurrency": "exclusive"},
        {
            "seq": 10,
            "time": "2026-11-14T08:00:02+00:00",
            "elapsed": 2.1,
            "type": "tool.started",
            "call_id": "c1",
            "function": {"name": "mc_move_to", "arguments": '{"x": 14, "y": 69, "z": -4}'},
        },
        {
            "seq": 11,
            "time": "2026-11-14T08:00:03+00:00",
            "elapsed": 3.0,
            "type": "tool.completed",
            "call_id": "c1",
            "result": json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "path_not_found",
                        "message": "No path",
                        "pos": [11.0, 70.0, -5.0],
                        "kb_hint": "请省略 y 坐标水平寻路。",
                    },
                },
                ensure_ascii=False,
            )
            + "\n[期间新事件] [受伤 health=16.5 by freeze]",
        },
        {"seq": 12, "time": "2026-11-14T08:00:03+00:00", "elapsed": 3.01, "type": "tool.skipped", "call_id": "c2", "reason": "previous_action_failed"},
        {
            "seq": 13,
            "time": "2026-11-14T08:00:03+00:00",
            "elapsed": 3.02,
            "type": "tool.completed",
            "call_id": "c2",
            "result": "错误：本项工具尚未启动，已取消执行。原因：previous_action_failed",
        },
        {"seq": 14, "time": "2026-11-14T08:00:04+00:00", "elapsed": 4.2, "type": "run.finished", "status": "answered", "actor_requests": 1},
    ]
    log_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    bundle = parse_replay_bundle(runs_dir, "synthetic.events.jsonl")
    assert bundle["id"] == "synthetic.events.jsonl"
    assert bundle["meta"]["bot"] == "SynthBot"
    assert bundle["meta"]["status"] == "answered"
    assert bundle["meta"]["actor_requests"] == 1
    assert len(bundle["trajectory"]) >= 2
    assert bundle["trajectory"][0]["pos"] == [10.5, 70.0, -5.5]

    kinds = [f["kind"] for f in bundle["frames"]]
    assert "rate_wait" in kinds
    assert "thought_delta" in kinds
    assert "usage" in kinds
    assert "tool_skipped" in kinds
    assert "finished" in kinds

    usage_frame = next(f for f in bundle["frames"] if f["kind"] == "usage")
    assert usage_frame["data"]["cached_tokens"] == 4000
    assert usage_frame["data"]["cache_hit_pct"] == 80.0

    completed_frame = next(f for f in bundle["frames"] if f["kind"] == "tool_completed")
    assert completed_frame["data"]["ok"] is False
    assert completed_frame["data"]["kb_hint"] == "请省略 y 坐标水平寻路。"
    assert completed_frame["state_patch"]["health"] == 16.5

    skipped_frame = next(f for f in bundle["frames"] if f["kind"] == "tool_skipped")
    assert skipped_frame["data"]["name"] == "mc_dig"
    assert skipped_frame["data"]["reason"] == "previous_action_failed"

    marker_kinds = {m["kind"] for m in bundle["markers"]}
    assert {"death", "station", "hurt"}.issubset(marker_kinds)


@pytest.mark.skipif(not (REAL_RUNS_DIR / "expedition-20261114").is_dir(), reason="Real expedition logs not present")
def test_real_expedition_logs_replay_bundle() -> None:
    runs = list_runs(REAL_RUNS_DIR)
    assert len(runs) >= 10
    ids = {r["id"] for r in runs}
    stage5b = "expedition-20261114/20261113-224831-stage5b-nju-kb-live.events.jsonl"
    stage5a = "expedition-20261114/20261113-224445-stage5a-iron-and-husbandry.events.jsonl"
    assert stage5b in ids
    assert stage5a in ids

    b5b = parse_replay_bundle(REAL_RUNS_DIR, stage5b)
    assert b5b["meta"]["bot"] == "5Ease_Bot"
    assert b5b["meta"]["status"] == "answered"
    assert b5b["meta"]["actor_requests"] == 8
    assert len(b5b["trajectory"]) >= 4
    marker_kinds_5b = {m["kind"] for m in b5b["markers"]}
    assert "protected_region" in marker_kinds_5b
    assert "death" in marker_kinds_5b
    assert "hurt" in marker_kinds_5b
    assert any((f.get("data") or {}).get("kb_hint") for f in b5b["frames"] if f["kind"] == "tool_completed")
    assert b5b["final_state"]["advancements"]["completed_count"] == 5
    assert b5b["final_state"]["advancements"]["total_catalog"] == 125
    # Verify #10 item 2b: Bot death & `inv_after == {}` clear inventory and held/offhand slots
    assert b5b["final_state"]["inventory"]["items"] == []
    assert b5b["final_state"]["inventory"]["held"] is None
    assert b5b["final_state"]["status"].get("held") is None
    death_frames_5b = [
        f
        for f in b5b["frames"]
        if any(m.get("kind") == "death" for m in (f.get("state_patch") or {}).get("markers") or [])
        and f["kind"] == "tool_completed"
    ]
    assert len(death_frames_5b) >= 1
    for df in death_frames_5b:
        assert df["state_patch"]["inventory"]["items"] == []
        assert df["state_patch"]["held"] is None
        assert df["state_patch"]["offhand"] is None

    b5a = parse_replay_bundle(REAL_RUNS_DIR, stage5a)
    assert b5a["meta"]["actor_requests"] == 30
    assert len(b5a["blocks"]) >= 5
    assert any(b["name"] == "iron_ore" for b in b5a["blocks"])
    # Verify #10 item 2a: stage5a advancements reconstructed from summary + catalog parent chain + mid-run unlock
    assert b5a["frames"][0]["state_patch"]["advancements"]["completed_count"] == 4
    assert "minecraft:adventure/kill_a_mob" not in b5a["frames"][0]["state_patch"]["advancements"]["completed_ids"]
    assert b5a["final_state"]["advancements"]["completed_count"] == 5
    assert "minecraft:adventure/kill_a_mob" in b5a["final_state"]["advancements"]["completed_ids"]
    assert any(f.get("how_to") for f in b5a["final_state"]["advancements"]["frontier"])
    assert len(b5a["final_state"]["inventory"]["items"]) == 14

    # Every expedition run with a `.summary.json` completed_count must match exactly
    for r in runs:
        if r["id"].startswith("expedition-20261114/") and isinstance(r.get("summary"), dict):
            expected_cc = r["summary"].get("completed_count")
            if isinstance(expected_cc, int):
                rb = parse_replay_bundle(REAL_RUNS_DIR, r["id"])
                assert rb["final_state"]["advancements"]["completed_count"] == expected_cc, r["id"]


def test_path_traversal_rejected(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    for bad in (
        "",
        "../pyproject.toml",
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../outside.events.jsonl",
        "..\\secret.events.jsonl",
        "not_an_event_log.json",
    ):
        with pytest.raises(ValueError):
            resolve_safe_run_path(runs_dir, bad)


def test_readonly_mc_client_blocks_mutations() -> None:
    async def _run() -> None:
        called = False

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = ReadOnlyMcClient(
            "http://127.0.0.1:18900/api/v1",
            transport=httpx.MockTransport(handler),
        )
        try:
            for method, forbidden_path in (
                ("POST", "/auth/login"),
                ("POST", "/connect"),
                ("POST", "/disconnect"),
                ("POST", "/move_to"),
                ("POST", "/dig"),
                ("POST", "/place"),
            ):
                with pytest.raises(PermissionError):
                    await client.observe(method, forbidden_path, token="tok")
            assert called is False
        finally:
            await client.close()

    asyncio.run(_run())


def test_401_never_triggers_login_or_exposes_token(tmp_path: Path) -> None:
    async def _run() -> None:
        state_dir = tmp_path / ".state-test"
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        store = TokenStore(state_dir / "tokens.json")
        secret_token = "SUPER_SECRET_BEARER_TOKEN_12345"
        store.put(BotTokenRecord(bot_username="GuardBot", token=secret_token))

        seen_paths: list[tuple[str, str]] = []

        def mc_handler(req: httpx.Request) -> httpx.Response:
            seen_paths.append((req.method, req.url.path))
            if req.url.path.endswith("/auth/login"):
                raise AssertionError("CRITICAL VIOLATION: /auth/login must NEVER be called!")
            return httpx.Response(
                401,
                json={"ok": False, "error": "unauthorized", "message": "Invalid or expired token"},
            )

        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            skin_password="SUPER_SECRET_PASSWORD_999",
            default_bot_username="GuardBot",
            state_dir=state_dir,
        )

        app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            mc_transport=httpx.MockTransport(mc_handler),
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            status_interval_s=0.05,
            tail_interval_s=0.05,
        )

        async with TestClient(TestServer(app)) as client:
            await asyncio.sleep(0.12)
            resp = await client.get("/api/info")
            assert resp.status == 200
            info_text = await resp.text()
            assert secret_token not in info_text
            assert "SUPER_SECRET_PASSWORD_999" not in info_text
            info = json.loads(info_text)
            assert info["bot_username"] == "GuardBot"
            assert info["has_cached_token"] is True
            assert info["online"] is False
            assert info["ready"] is False
            assert info["prismarine_viewer"]["available"] is False

            # Path traversal check over HTTP
            bad_resp = await client.get("/api/replay", params={"id": "../tokens.json"})
            assert bad_resp.status == 400

            bad_resp2 = await client.get("/api/replay", params={"id": "../../pyproject.toml.events.jsonl"})
            assert bad_resp2.status == 400

            missing_resp = await client.get("/api/replay", params={"id": "missing.events.jsonl"})
            assert missing_resp.status == 404

        # Confirm ONLY GET /status was attempted and cached token on disk was untouched
        assert all(p == ("GET", "/api/v1/status") for p in seen_paths)
        assert store.get("GuardBot") is not None
        assert store.get("GuardBot").token == secret_token

    asyncio.run(_run())


def test_http_and_ws_live_tailing_and_telemetry_polling(tmp_path: Path) -> None:
    async def _run() -> None:
        state_dir = tmp_path / ".state-live"
        runs_dir = tmp_path / "runs"
        static_dir = tmp_path / "static"
        runs_dir.mkdir()
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<!doctype html><title>Test UI</title>", encoding="utf-8")
        (static_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

        # Initial run file before server start
        run1 = runs_dir / "stage1.events.jsonl"
        run1.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "time": "2026-11-14T08:00:00+00:00",
                    "elapsed": 0.1,
                    "type": "run.started",
                    "task": "Stage 1 wood",
                    "bot": "LiveBot",
                    "model": "qwen",
                    "max_calls": 5,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        store = TokenStore(state_dir / "tokens.json")
        store.put(BotTokenRecord(bot_username="LiveBot", token="valid-live-token"))

        events_call_count = 0
        bot_pos = [0.5, 65.0, 0.5]
        find_blocks_calls = 0

        def mc_handler(req: httpx.Request) -> httpx.Response:
            nonlocal events_call_count, find_blocks_calls
            path = req.url.path
            if path.endswith("/status"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "username": "LiveBot",
                            "pos": list(bot_pos),
                            "yaw": -1.57,
                            "dimension": "overworld",
                            "health": 19.5,
                            "food": 18,
                            "oxygen": 20,
                            "ready": True,
                            "alive": True,
                            "busy": "dig",
                        },
                    },
                )
            if path.endswith("/events"):
                events_call_count += 1
                since = int(req.url.params.get("since", "0"))
                if events_call_count == 1:
                    # Pre-existing historical death at seq=10 must NOT flash as live alert!
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": {
                                "since": since,
                                "last_seq": 10,
                                "total": 1,
                                "events": [{"seq": 10, "type": "death", "pos": [99, 64, 99]}],
                            },
                        },
                    )
                if events_call_count == 2:
                    assert since == 10
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": {
                                "since": 10,
                                "last_seq": 11,
                                "total": 1,
                                "events": [{"seq": 11, "type": "hurt", "health": 17.0, "source": "zombie"}],
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"since": since, "last_seq": 11, "total": 0, "events": []}},
                )
            if path.endswith("/get_entities"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "entities": [
                                {"id": 7, "type": "hostile", "name": "zombie", "pos": [4.0, 65.0, 1.0], "dist": 3.5}
                            ]
                        },
                    },
                )
            if path.endswith("/inventory"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "items": [{"name": "oak_log", "count": 4, "slot": 36}],
                            "armor": {},
                            "offhand": None,
                            "held": {"name": "oak_log", "count": 4, "slot": 36},
                            "empty_slots": 35,
                        },
                    },
                )
            if path.endswith("/advancements"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "advancements": [
                                {"id": "minecraft:story/root", "status": "completed"},
                                {"id": "minecraft:story/mine_stone", "status": "completed"},
                            ]
                        },
                    },
                )
            if path.endswith("/find_blocks"):
                find_blocks_calls += 1
                body = json.loads(req.content.decode("utf-8"))
                types = body.get("types") or []
                if "crafting_table" in types:
                    return httpx.Response(
                        200,
                        json={"ok": True, "result": {"blocks": [{"name": "crafting_table", "pos": [1, 65, 1], "dist": 1.0}]}},
                    )
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"blocks": [{"name": "grass_block", "pos": [0, 64, 0], "dist": 1.0}]}},
                )
            raise AssertionError(f"Unexpected endpoint: {path}")

        def viewer_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "note": "Cam online"})

        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            default_bot_username="LiveBot",
            state_dir=state_dir,
        )

        app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            static_dir=static_dir,
            mc_transport=httpx.MockTransport(mc_handler),
            viewer_transport=httpx.MockTransport(viewer_handler),
            tail_interval_s=0.04,
            status_interval_s=0.08,
            entities_interval_s=0.08,
            inv_adv_interval_s=0.1,
            voxel_min_interval_s=0.1,
            voxel_min_move_blocks=4.0,
        )

        async with TestClient(TestServer(app)) as client:
            # Verify static routes
            idx_resp = await client.get("/")
            assert idx_resp.status == 200
            assert "Test UI" in await idx_resp.text()

            js_resp = await client.get("/static/app.js")
            assert js_resp.status == 200

            # Connect WebSocket and check initial snapshot
            ws = await client.ws_connect("/ws")
            first_msg = await ws.receive_json(timeout=2.0)
            assert first_msg["type"] == "snapshot"
            assert first_msg["info"]["prismarine_viewer"]["available"] is True
            assert ":8766" in first_msg["info"]["prismarine_viewer"]["embed_url"]
            assert first_msg["active_run"]["id"] == "stage1.events.jsonl"

            # Append live lines to stage1.events.jsonl (including partial write then newline)
            with run1.open("a", encoding="utf-8") as fh:
                fh.write('{"seq": 2, "time": "2026-11-14T08:00:01+00:00", "elapsed": 1.1, "type": "tool.started", ')
                fh.flush()
            await asyncio.sleep(0.06)
            with run1.open("a", encoding="utf-8") as fh:
                fh.write('"call_id": "t1", "function": {"name": "mc_place", "arguments": "{\\"item\\":\\"furnace\\",\\"x\\":2,\\"y\\":65,\\"z\\":2}"}}\n')
                fh.write(
                    json.dumps(
                        {
                            "seq": 3,
                            "time": "2026-11-14T08:00:02+00:00",
                            "elapsed": 1.6,
                            "type": "tool.completed",
                            "call_id": "t1",
                            "result": json.dumps({"ok": True, "result": {"pos": [2, 65, 2], "item": "furnace"}}),
                        }
                    )
                    + "\n"
                )
                fh.flush()

            # Collect WS messages over ~0.35s
            received: list[dict[str, Any]] = []
            deadline = time.monotonic() + 0.45
            while time.monotonic() < deadline:
                try:
                    msg = await ws.receive_json(timeout=0.08)
                    received.append(msg)
                except asyncio.TimeoutError:
                    pass

            msg_types = {m["type"] for m in received}
            assert "harness_frame" in msg_types
            assert "telemetry" in msg_types
            assert "inventory_advancements" in msg_types
            assert "blocks_patch" in msg_types
            assert "mc_events" in msg_types

            # Ensure historical seq=10 death was NOT broadcast as a live mc_event, only seq=11 hurt!
            mc_ev_msgs = [m for m in received if m["type"] == "mc_events"]
            all_seqs = [ev["seq"] for m in mc_ev_msgs for ev in m["events"]]
            assert 10 not in all_seqs
            assert 11 in all_seqs

            # Check /api/telemetry-pos
            tpos = await (await client.get("/api/telemetry-pos")).json()
            assert tpos["ok"] is True
            assert tpos["pos"] == [0.5, 65.0, 0.5]
            assert tpos["dimension"] == "overworld"

            # Verify initial voxel sample ran 2 queries, and does NOT re-run until Bot moves >= 4 blocks
            assert find_blocks_calls == 2
            await asyncio.sleep(0.15)
            assert find_blocks_calls == 2
            bot_pos[:] = [10.5, 65.0, 0.5]  # Move 10 blocks (>= 4)
            await asyncio.sleep(0.2)
            assert find_blocks_calls == 4

            # Create a newer run in a subdirectory -> verify smooth `run_switched` broadcast!
            sub = runs_dir / "expedition-next"
            sub.mkdir()
            run2 = sub / "stage2.events.jsonl"
            run2.write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "time": "2026-11-14T08:05:00+00:00",
                        "elapsed": 0.05,
                        "type": "run.started",
                        "task": "Stage 2 iron",
                        "bot": "LiveBot",
                        "model": "qwen",
                        "max_calls": 10,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            now_ts = time.time() + 5.0
            os.utime(run2, (now_ts, now_ts))

            switched_msg = None
            deadline2 = time.monotonic() + 0.6
            while time.monotonic() < deadline2:
                try:
                    m = await ws.receive_json(timeout=0.1)
                    if m.get("type") == "run_switched":
                        switched_msg = m
                        break
                except asyncio.TimeoutError:
                    pass

            assert switched_msg is not None
            assert switched_msg["run"]["id"] == "expedition-next/stage2.events.jsonl"

            await ws.close()

    asyncio.run(_run())


def test_window_capture_ws_relay_and_mjpeg_stream(tmp_path: Path) -> None:
    async def _run() -> None:
        state_dir = tmp_path / ".state-cap"
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            default_bot_username="CapBot",
            state_dir=state_dir,
        )
        app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            enable_mc_polling=False,
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            tail_interval_s=0.05,
        )

        sample_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00testframe\xff\xd9"

        async with TestClient(TestServer(app)) as client:
            info0 = await (await client.get("/api/info")).json()
            assert info0["capture_stream"]["active"] is False
            assert (await client.get("/api/capture/frame.jpg")).status == 404
            assert (await client.get("/api/capture/stream.mjpg")).status == 404

            # Invalid image bytes via HTTP POST must be rejected with 400
            bad_post = await client.post("/api/capture/frame", data=b"not-an-image")
            assert bad_post.status == 400

            viewer_ws = await client.ws_connect("/ws")
            snap = await viewer_ws.receive_json(timeout=2.0)
            assert snap["type"] == "snapshot"
            assert snap["info"]["capture_stream"]["active"] is False

            pub_ws = await client.ws_connect("/ws-capture")
            await pub_ws.send_str(json.dumps({"type": "meta", "width": 640, "height": 360, "fps": 15}))
            await pub_ws.send_bytes(sample_jpeg)

            cap_on = await viewer_ws.receive_json(timeout=2.0)
            assert cap_on["type"] == "capture_status"
            assert cap_on["capture_stream"]["active"] is True
            assert cap_on["capture_stream"]["width"] == 640
            assert cap_on["capture_stream"]["height"] == 360
            assert cap_on["capture_stream"]["stream_url"] == "/api/capture/stream.mjpg"

            # Verify invalid binary payload over WS is ignored and preserves valid frame
            await pub_ws.send_bytes(b"not-an-image")
            await asyncio.sleep(0.05)

            frame_resp = await client.get("/api/capture/frame.jpg")
            assert frame_resp.status == 200
            assert frame_resp.content_type == "image/jpeg"
            assert await frame_resp.read() == sample_jpeg

            async with client.get("/api/capture/stream.mjpg") as mjpeg_resp:
                assert mjpeg_resp.status == 200
                assert "multipart/x-mixed-replace" in mjpeg_resp.headers["Content-Type"]
                chunk = await asyncio.wait_for(mjpeg_resp.content.readany(), timeout=2.0)
                assert b"--mcliveframe" in chunk
                assert sample_jpeg in chunk

            await pub_ws.close()
            cap_off = await viewer_ws.receive_json(timeout=2.0)
            assert cap_off["type"] == "capture_status"
            assert cap_off["capture_stream"]["active"] is False
            assert (await client.get("/api/capture/frame.jpg")).status == 404

            await viewer_ws.close()

    asyncio.run(_run())


def test_camera_mode_director_and_ws_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MC_CAPTURE_DIRECTOR_URL", "http://127.0.0.1:18767/")
    monkeypatch.setenv("MC_UPSTREAM_LIVE_URL", "http://10.50.248.70:8765/")
    parsed = build_parser().parse_args([])
    assert parsed.director_url == "http://127.0.0.1:18767/"
    assert parsed.upstream_live_url == "http://10.50.248.70:8765/"
    monkeypatch.delenv("MC_CAPTURE_DIRECTOR_URL", raising=False)
    monkeypatch.delenv("MC_UPSTREAM_LIVE_URL", raising=False)

    async def _run() -> None:
        state_dir = tmp_path / ".state-dir"
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            default_bot_username="DirBot",
            state_dir=state_dir,
        )
        sample_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00dirframe\xff\xd9"

        # 1) Unconfigured director: 503 on POST, 400 on invalid mode, /ws-capture meta live broadcast
        unconf_app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            enable_mc_polling=False,
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            tail_interval_s=0.05,
        )
        async with TestClient(TestServer(unconf_app)) as client:
            info0 = await (await client.get("/api/info")).json()
            assert info0["capture_stream"]["camera_mode"] == "third_person"
            assert info0["capture_stream"]["director_available"] is False

            mode_get0 = await client.get("/api/capture/camera-mode")
            assert mode_get0.status == 200
            assert await mode_get0.json() == {
                "ok": True,
                "mode": "third_person",
                "director_available": False,
            }

            bad_mode = await client.post("/api/capture/camera-mode", json={"mode": "free_orbit"})
            assert bad_mode.status == 400

            bad_json = await client.post("/api/capture/camera-mode", data=b"{not-json")
            assert bad_json.status == 400

            unconf_post = await client.post("/api/capture/camera-mode", json={"mode": "first_person"})
            assert unconf_post.status == 503
            unconf_body = await unconf_post.json()
            assert unconf_body["ok"] is False

            viewer_ws = await client.ws_connect("/ws")
            await viewer_ws.receive_json(timeout=2.0)  # initial snapshot

            pub_ws = await client.ws_connect("/ws-capture")
            await pub_ws.send_bytes(sample_jpeg)
            cap_on = await viewer_ws.receive_json(timeout=2.0)
            assert cap_on["type"] == "capture_status"
            assert cap_on["capture_stream"]["active"] is True
            assert cap_on["capture_stream"]["director_available"] is False

            await pub_ws.send_str(
                json.dumps(
                    {
                        "type": "meta",
                        "camera_mode": "first_person",
                        "director_available": True,
                        "width": 1280,
                        "height": 720,
                        "fps": 12,
                        "source": "beast_cam",
                    }
                )
            )
            meta_msg = await viewer_ws.receive_json(timeout=2.0)
            assert meta_msg["type"] == "capture_status"
            cs = meta_msg["capture_stream"]
            assert cs["camera_mode"] == "first_person"
            assert cs["director_available"] is True
            assert cs["width"] == 1280
            assert cs["height"] == 720
            assert cs["fps"] == 12
            assert cs["source"] == "beast_cam"

            mode_get1 = await (await client.get("/api/capture/camera-mode")).json()
            assert mode_get1 == {"ok": True, "mode": "first_person", "director_available": True}

            await pub_ws.close()
            cap_off = await viewer_ws.receive_json(timeout=2.0)
            assert cap_off["type"] == "capture_status"
            assert cap_off["capture_stream"]["active"] is False
            assert cap_off["capture_stream"]["director_available"] is False
            await viewer_ws.close()

        # 2) Configured --director-url: happy path + WS broadcast + 502 error handling
        director_calls: list[dict[str, Any]] = []
        director_fail = False

        def director_handler(req: httpx.Request) -> httpx.Response:
            if director_fail:
                return httpx.Response(500, json={"ok": False, "error": "rcon_down"})
            if req.method == "POST" and req.url.path == "/mode":
                body = json.loads(req.content.decode("utf-8"))
                director_calls.append(body)
                return httpx.Response(200, json={"ok": True, "mode": body["mode"]})
            return httpx.Response(404)

        dir_app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            director_url="http://127.0.0.1:8767",
            director_transport=httpx.MockTransport(director_handler),
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            enable_mc_polling=False,
            tail_interval_s=0.05,
        )
        async with TestClient(TestServer(dir_app)) as client:
            info_d = await (await client.get("/api/info")).json()
            assert info_d["capture_stream"]["director_available"] is True
            assert info_d["capture_stream"]["camera_mode"] == "third_person"

            viewer_ws = await client.ws_connect("/ws")
            snap = await viewer_ws.receive_json(timeout=2.0)
            assert snap["info"]["capture_stream"]["director_available"] is True

            sw_resp = await client.post("/api/capture/camera-mode", json={"mode": "first_person"})
            assert sw_resp.status == 200
            assert await sw_resp.json() == {"ok": True, "mode": "first_person", "director_available": True}
            assert director_calls == [{"mode": "first_person"}]

            ws_ev = await viewer_ws.receive_json(timeout=2.0)
            assert ws_ev["type"] == "capture_status"
            assert ws_ev["capture_stream"]["camera_mode"] == "first_person"
            assert ws_ev["capture_stream"]["director_available"] is True

            get_d = await (await client.get("/api/capture/camera-mode")).json()
            assert get_d == {"ok": True, "mode": "first_person", "director_available": True}

            director_fail = True
            fail_resp = await client.post("/api/capture/camera-mode", json={"mode": "third_person"})
            assert fail_resp.status == 502

            await viewer_ws.close()

    asyncio.run(_run())


def test_upstream_live_url_proxy_mode_with_second_server(tmp_path: Path) -> None:
    async def _run() -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            default_bot_username="ProxyBot",
            state_dir=tmp_path / ".state-proxy",
        )

        director_modes: list[str] = []

        def director_handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/mode":
                payload = json.loads(req.content.decode("utf-8"))
                director_modes.append(payload["mode"])
                return httpx.Response(200, json={"ok": True, "mode": payload["mode"]})
            return httpx.Response(404)

        upstream_app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            director_url="http://127.0.0.1:18767",
            director_transport=httpx.MockTransport(director_handler),
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            enable_mc_polling=False,
            tail_interval_s=0.05,
        )

        frame_1 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00upstream1\xff\xd9"
        frame_2 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00upstream2\xff\xd9"

        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        upstream_client = TestClient(upstream_server)
        try:
            pub_ws = await upstream_client.ws_connect("/ws-capture")
            await pub_ws.send_str(
                json.dumps(
                    {
                        "type": "meta",
                        "width": 1280,
                        "height": 720,
                        "fps": 20,
                        "source": "beast_cam",
                        "camera_mode": "third_person",
                        "director_available": True,
                    }
                )
            )
            await pub_ws.send_bytes(frame_1)
            await asyncio.sleep(0.05)

            upstream_base = str(upstream_server.make_url("/")).rstrip("/")
            proxy_app = create_app(
                settings=settings,
                runs_dir=runs_dir,
                upstream_live_url=upstream_base,
                viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                enable_mc_polling=False,
                tail_interval_s=0.05,
                upstream_poll_interval_s=0.05,
            )
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()
            proxy_client = TestClient(proxy_server)
            proxy_closed = False
            try:
                info_resp = await proxy_client.get("/api/info")
                assert info_resp.status == 200
                info_json = await info_resp.json()
                cs = info_json["capture_stream"]
                assert cs["active"] is True
                assert cs["stream_url"] == "/api/capture/stream.mjpg"
                assert cs["snapshot_url"] == "/api/capture/frame.jpg"
                assert cs["width"] == 1280
                assert cs["height"] == 720
                assert cs["fps"] == 20
                assert cs["source"] == "beast_cam"
                assert cs["seq"] >= 1
                assert cs["camera_mode"] == "third_person"
                assert cs["director_available"] is True

                cam_get = await proxy_client.get("/api/capture/camera-mode")
                assert cam_get.status == 200
                assert await cam_get.json() == {
                    "ok": True,
                    "mode": "third_person",
                    "director_available": True,
                }

                frame_resp = await proxy_client.get("/api/capture/frame.jpg")
                assert frame_resp.status == 200
                assert frame_resp.content_type == "image/jpeg"
                assert await frame_resp.read() == frame_1

                viewer_ws = await proxy_client.ws_connect("/ws")
                snap = await viewer_ws.receive_json(timeout=2.0)
                assert snap["info"]["capture_stream"]["active"] is True

                # Proxy MJPEG streaming across consecutive frames
                async with proxy_client.get("/api/capture/stream.mjpg") as mjpeg_resp:
                    assert mjpeg_resp.status == 200
                    assert "multipart/x-mixed-replace" in mjpeg_resp.headers["Content-Type"]
                    chunk1 = await asyncio.wait_for(mjpeg_resp.content.readany(), timeout=2.0)
                    assert b"--mcliveframe" in chunk1
                    assert frame_1 in chunk1

                    await pub_ws.send_bytes(frame_2)
                    buf = b""
                    deadline = time.monotonic() + 2.0
                    while frame_2 not in buf and time.monotonic() < deadline:
                        buf += await asyncio.wait_for(mjpeg_resp.content.readany(), timeout=1.0)
                    assert frame_2 in buf

                # Forward POST /api/capture/camera-mode through proxy -> upstream -> director
                post_mode = await proxy_client.post(
                    "/api/capture/camera-mode",
                    json={"mode": "first_person"},
                )
                assert post_mode.status == 200
                assert await post_mode.json() == {
                    "ok": True,
                    "mode": "first_person",
                    "director_available": True,
                }
                assert director_modes == ["first_person"]

                # Connected WS client on proxy receives capture_status update
                ws_update = await viewer_ws.receive_json(timeout=2.0)
                assert ws_update["type"] == "capture_status"
                assert ws_update["capture_stream"]["camera_mode"] == "first_person"
                assert ws_update["capture_stream"]["director_available"] is True

                info_after = await (await proxy_client.get("/api/info")).json()
                assert info_after["capture_stream"]["camera_mode"] == "first_person"

                await viewer_ws.close()

                # Verify active stream.mjpg proxy exits immediately on server shutdown without hanging
                async def _hold_mjpeg() -> None:
                    with contextlib.suppress(BaseException):
                        async with proxy_client.get("/api/capture/stream.mjpg") as stream_resp:
                            await stream_resp.content.readany()
                            await stream_resp.content.readany()

                stream_task = asyncio.create_task(_hold_mjpeg())
                await asyncio.sleep(0.1)
                await asyncio.wait_for(proxy_client.close(), timeout=2.0)
                proxy_closed = True
                assert proxy_app[_COORD_KEY].shutting_down is True
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(stream_task, timeout=1.0)
            finally:
                if not proxy_closed:
                    await proxy_client.close()

            await pub_ws.close()
        finally:
            await upstream_client.close()

        # Verify graceful fallback when upstream is unreachable
        down_app = create_app(
            settings=settings,
            runs_dir=runs_dir,
            upstream_live_url="http://127.0.0.1:19999",
            upstream_transport=httpx.MockTransport(
                lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            ),
            viewer_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            enable_mc_polling=False,
        )
        async with TestClient(TestServer(down_app)) as down_client:
            down_info = await (await down_client.get("/api/info")).json()
            assert down_info["capture_stream"]["active"] is False
            assert down_info["capture_stream"]["director_available"] is False
            assert (await down_client.get("/api/capture/frame.jpg")).status == 502

    asyncio.run(_run())


def test_tcp_viewer_proxy_relay_and_clean_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import web

    monkeypatch.setenv("MC_VIEWER_PROXY_TARGET", "100.93.211.71:8766")
    monkeypatch.setenv("MC_VIEWER_PROXY_PORT", "8776")
    monkeypatch.delenv("MC_VIEWER_PROXY_HOST", raising=False)

    env_args = build_parser().parse_args([])
    assert env_args.viewer_proxy_target == "100.93.211.71:8766"
    assert env_args.viewer_proxy_port == 8776
    assert env_args.viewer_proxy_host is None

    captured_apps: list[web.Application] = []
    monkeypatch.setattr("mc_agent.live.server.web.run_app", lambda app, **kw: captured_apps.append(app))
    empty_env = tmp_path / ".env.test"
    empty_env.write_text("", encoding="utf-8")

    from mc_agent.live.server import main as live_main

    live_main(["--env-file", str(empty_env), "--runs-dir", str(tmp_path)])
    assert captured_apps[-1][_COORD_KEY].viewer_proxy_host == "0.0.0.0"
    assert captured_apps[-1][_COORD_KEY].viewer_proxy_port == 8776

    live_main(["--env-file", str(empty_env), "--runs-dir", str(tmp_path), "--host", "127.0.0.1"])
    assert captured_apps[-1][_COORD_KEY].viewer_proxy_host == "127.0.0.1"

    async def _run() -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(exist_ok=True)
        settings = Settings(
            mc_api_base_url="http://127.0.0.1:18900/api/v1",
            default_bot_username="RelayBot",
            state_dir=tmp_path / ".state-relay",
        )

        backend_app = web.Application()

        async def handle_health(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "note": "Remote Prismarine (:8766)"})

        async def handle_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await ws.send_str(f"echo:{msg.data}")
            return ws

        backend_app.router.add_get("/health", handle_health)
        backend_app.router.add_get("/ws", handle_ws)

        backend_server = TestServer(backend_app)
        await backend_server.start_server()
        idle_writer: asyncio.StreamWriter | None = None
        client: TestClient | None = None
        try:
            target = f"127.0.0.1:{backend_server.port}"
            app = create_app(
                settings=settings,
                runs_dir=runs_dir,
                viewer_proxy_target=target,
                viewer_proxy_port=0,
                viewer_proxy_host="127.0.0.1",
                enable_mc_polling=False,
            )
            coord = app[_COORD_KEY]
            assert coord.viewer_proxy_host == "127.0.0.1"

            client = TestClient(TestServer(app))
            await client.start_server()

            bound_port = coord.viewer_proxy_bound_port
            assert bound_port is not None and bound_port > 0

            # Auto-detected prismarine_viewer should probe and advertise the local relay port
            info_resp = await client.get("/api/info", headers={"Host": "teammate-box.local:8765"})
            assert info_resp.status == 200
            info = await info_resp.json()
            assert info["prismarine_viewer"]["available"] is True
            assert info["prismarine_viewer"]["embed_url"] == f"http://teammate-box.local:{bound_port}"
            assert info["prismarine_viewer"]["note"] == "Remote Prismarine (:8766)"

            # Bidirectional HTTP + WebSocket traffic through TCP relay
            async with httpx.AsyncClient(trust_env=False) as http_client:
                proxied_health = await http_client.get(f"http://127.0.0.1:{bound_port}/health")
                assert proxied_health.status_code == 200
                assert proxied_health.json()["ok"] is True

            async with client.session.ws_connect(f"http://127.0.0.1:{bound_port}/ws") as ws:
                await ws.send_str("ping-relay")
                reply = await ws.receive_str()
                assert reply == "echo:ping-relay"

            # Keep an idle TCP connection open through the relay, then verify shutdown does not hang
            idle_reader, idle_writer = await asyncio.open_connection("127.0.0.1", bound_port)
            idle_writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")
            await idle_writer.drain()
            chunk = await asyncio.wait_for(idle_reader.read(4096), timeout=2.0)
            assert b"200 OK" in chunk

            await asyncio.wait_for(client.close(), timeout=2.0)
            client = None

            # Relay closed the active socket cleanly upon shutdown
            eof = await asyncio.wait_for(idle_reader.read(1024), timeout=2.0)
            assert eof == b""
        finally:
            if idle_writer is not None:
                idle_writer.close()
            if client is not None:
                await client.close()
            await backend_server.close()

    asyncio.run(_run())


def test_live_projection_includes_reasoning_content(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / "reasoning.events.jsonl"
    events = [
        {"seq": 1, "time": "2026-11-16T08:00:00+00:00", "elapsed": 0.0, "type": "run.started", "bot": "SynthBot"},
        {"seq": 2, "time": "2026-11-16T08:00:01+00:00", "elapsed": 1.0, "type": "model.text.delta", "request": 1, "text": "先合成铁镐。"},
        {
            "seq": 3,
            "time": "2026-11-16T08:00:02+00:00",
            "elapsed": 2.0,
            "type": "model.completed",
            "request": 1,
            "duration": 1.5,
            "message": {
                "role": "assistant",
                "reasoning_content": "需要至少3个铁锭，先下探收集铁矿并熔炼。",
                "content": "先合成铁镐。",
                "tool_calls": [],
            },
        },
        {"seq": 4, "time": "2026-11-16T08:00:03+00:00", "elapsed": 2.5, "type": "run.finished", "status": "answered", "actor_requests": 1},
    ]
    log_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    bundle = parse_replay_bundle(runs_dir, "reasoning.events.jsonl")
    thought_frames = [f for f in bundle["frames"] if f["kind"] == "thought"]
    assert len(thought_frames) == 1
    data = thought_frames[0]["data"]
    assert data["replace"] is True
    assert "[思考]" in data["text"]
    assert "需要至少3个铁锭" in data["text"]
    assert "先合成铁镐。" in data["text"]
    assert thought_frames[0]["seq"] == 3
