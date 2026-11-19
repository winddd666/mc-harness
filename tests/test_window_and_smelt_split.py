"""Offline tests for window cache self-healing and the non-blocking multi-furnace split."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.errors import ApiError
from mc_agent.harness.mc_tools import McToolset
from mc_agent.harness.world_memory import WorldMemory
from mc_agent.skills.library import MacroSkills


# ---------------------------------------------------------------------------
# WorldMemory ledger
# ---------------------------------------------------------------------------


def test_conversion_job_ledger_persists_across_instances(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "wm.json"
    wm = WorldMemory(path)
    job = wm.register_conversion_job(
        pos=[10, 64, 10], input_item="cobblestone", output_item="stone", count=12, ready_at=time.time() + 90
    )
    assert wm.get_in_conversion_counts() == {"stone": 12}
    assert path.is_file()

    reloaded = WorldMemory(path)
    assert reloaded.get_in_conversion_counts() == {"stone": 12}
    assert reloaded.conversion_jobs[job["id"]]["pos"] == [10, 64, 10]
    reloaded.complete_conversion_job(job["id"], collected=12)
    assert reloaded.get_in_conversion_counts() == {}


def test_invalidate_container_clears_both_tables_and_jobs() -> None:
    wm = WorldMemory()
    wm.update_container([1, 64, 1], {"redstone": 2}, block_name="chest")
    wm.record_poi("chest", [1, 64, 1])
    job = wm.register_conversion_job(
        pos=[1, 64, 1], input_item="cobblestone", output_item="stone", count=3
    )
    res = wm.invalidate_container([1, 64, 1], block_name="chest")
    assert res["container_removed"] is True
    assert res["poi_removed"] is True
    assert res["jobs_removed"] == [job["id"]]
    assert ("overworld", 1, 64, 1) not in wm.containers
    assert ("overworld", 1, 64, 1) not in wm.pois
    assert wm.get_in_conversion_counts() == {}


def test_window_snapshot_with_empty_container_wipes_phantom_cache() -> None:
    wm = WorldMemory()
    wm.update_container([5, 64, 0], {"redstone": 1, "iron_ingot": 3}, block_name="chest")
    synced = wm.update_container_from_window(
        {"window": "generic_9x3", "target": "chest", "sections": {"container": []}},
        fallback_pos=[5, 64, 0],
        fallback_block="chest",
    )
    assert synced is True
    assert wm.containers[("overworld", 5, 64, 0)]["items"] == {}


# ---------------------------------------------------------------------------
# MacroSkills.container_batch error-branch self-healing
# ---------------------------------------------------------------------------


class _WinBot:
    def __init__(self, error: ApiError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def status(self) -> dict[str, Any]:
        return {"pos": [0.0, 64.0, 0.0], "dimension": "overworld"}

    def inventory(self) -> dict[str, Any]:
        return {"items": [], "empty_slots": 36}

    def call(self, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        body = body or {}
        self.calls.append((path, dict(body)))
        if path == "window/act" and self.error is not None:
            raise self.error
        if path == "window/act":
            return {
                "done": [{"op": "take", "took": {"stone": 1}}],
                "window": {
                    "window": "generic_9x3",
                    "target": "chest",
                    "pos": [3, 64, 0],
                    "sections": {"container": []},
                },
            }
        if path == "get_block":
            return {"name": "chest", "pos": body.get("pos")}
        return {}


def test_container_batch_success_syncs_empty_snapshot() -> None:
    bot = _WinBot()
    wm = WorldMemory()
    wm.update_container([3, 64, 0], {"stone": 9}, block_name="chest")
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.container_withdraw({"stone": 9}, x=3, y=64, z=0)
    assert res["ok"] is True
    assert res["memory_synced"] is True
    assert wm.containers[("overworld", 3, 64, 0)]["items"] == {}


def test_container_batch_missing_item_error_heals_cache_from_context() -> None:
    err = ApiError(
        "missing_item",
        "no stone in chest",
        context={
            "failed_op": 0,
            "done": [],
            "window": {
                "window": "generic_9x3",
                "target": "chest",
                "pos": [3, 64, 0],
                "sections": {"container": []},
            },
        },
    )
    bot = _WinBot(error=err)
    wm = WorldMemory()
    wm.update_container([3, 64, 0], {"stone": 5}, block_name="chest")
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.container_withdraw({"stone": 5}, x=3, y=64, z=0)
    assert res["ok"] is False
    assert res["error"] == "missing_item"
    assert res["failed_op"] == 0
    assert res["memory_synced"] is True
    assert res["context_keys"] == ["done", "failed_op", "window"]
    assert wm.containers[("overworld", 3, 64, 0)]["items"] == {}


def test_probe_unknown_nearby_containers_skips_spawner_and_caches() -> None:
    class _Bot(_WinBot):
        def call(self, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            body = body or {}
            self.calls.append((path, dict(body)))
            if path == "find_blocks":
                if "spawner" in (body.get("types") or []):
                    return {"blocks": [{"name": "spawner", "pos": [8, 65, 1], "dist": 3.0}]}
                return {
                    "blocks": [
                        {"name": "chest", "pos": [8, 64, 0], "dist": 2.0},
                        {"name": "barrel", "pos": [-4, 64, 0], "dist": 4.0},
                    ]
                }
            if path == "window/view":
                pos = (body.get("target") or {}).get("pos") or [0, 64, 0]
                if pos[0] < 0:
                    return {"window": "generic_9x3", "target": "barrel", "pos": pos, "sections": {"container": [{"name": "coal", "count": 5}]}}
                return {"window": "generic_9x3", "target": "chest", "pos": pos, "sections": {"container": []}}
            if path == "get_block":
                return {"name": "chest", "pos": body.get("pos")}
            return {}

    bot = _Bot()
    wm = WorldMemory()
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.probe_unknown_nearby_containers(radius=8.0, max_probes=2)
    assert [p["pos"] for p in res["probed"]] == [[-4, 64, 0]]
    assert res["skipped"][0]["reason"] == "dungeon_spawner"
    assert wm.containers[("overworld", -4, 64, 0)]["items"] == {"coal": 5}


# ---------------------------------------------------------------------------
# McToolset: ok/error window sync + dig invalidation
# ---------------------------------------------------------------------------


def _toolset(handler: Any) -> McToolset:
    transport = httpx.MockTransport(handler)
    session = BotSession("t", bot_username="WinTest", base_url="http://test.local/api/v1", transport=transport)
    return McToolset(session, world_memory=WorldMemory())


def test_mc_window_act_error_payload_syncs_world_memory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/window/act"):
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {
                        "code": "missing_item",
                        "message": "chest has no redstone",
                        "failed_op": 0,
                        "done": [],
                        "window": {
                            "window": "generic_9x3",
                            "target": "chest",
                            "pos": [6, 64, 0],
                            "sections": {"container": []},
                        },
                    },
                },
            )
        if url.endswith("/get_block"):
            return httpx.Response(200, json={"ok": True, "result": {"name": "chest", "pos": [6, 64, 0]}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    ts = _toolset(handler)
    ts.world_memory.update_container([6, 64, 0], {"redstone": 1}, block_name="chest")
    raw = ts.mc_window_act([{"op": "take", "from": "container", "item": "redstone", "count": 1}], x=6, y=64, z=0)
    parsed = json.loads(raw)
    assert parsed["ok"] is False
    assert ts.world_memory.containers[("overworld", 6, 64, 0)]["items"] == {}


def test_mc_dig_invalidates_container_and_poi() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/dig"):
            return httpx.Response(200, json={"ok": True, "result": {"dug": "chest", "gained": {"chest": 1}}})
        if url.endswith("/get_block"):
            return httpx.Response(200, json={"ok": True, "result": {"name": "chest", "pos": [7, 64, 0], "diggable": True}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    ts = _toolset(handler)
    ts.world_memory.update_container([7, 64, 0], {"iron_ingot": 4}, block_name="chest")
    ts.world_memory.record_poi("chest", [7, 64, 0])
    raw = ts.mc_dig(7, 64, 0)
    assert json.loads(raw)["ok"] is True
    assert ("overworld", 7, 64, 0) not in ts.world_memory.containers
    assert ("overworld", 7, 64, 0) not in ts.world_memory.pois


# ---------------------------------------------------------------------------
# Non-blocking smelt split
# ---------------------------------------------------------------------------


class SmeltBot:
    def __init__(self, items: list[dict[str, Any]], *, furnaces: list[list[int]] | None = None) -> None:
        self.items = [dict(r) for r in items]
        self.furnaces = furnaces if furnaces is not None else [[3, 64, 0], [4, 64, 0]]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.empty_slots = 30
        self.dimension = "overworld"
        self.furnace_output: dict[tuple[int, int, int], dict[str, int]] = {}

    def status(self) -> dict[str, Any]:
        return {"pos": [0.5, 64.0, 0.0], "dimension": self.dimension, "on_ground": True}

    def inventory(self) -> dict[str, Any]:
        return {"items": [dict(r) for r in self.items], "empty_slots": self.empty_slots}

    def _take(self, name: str, count: int) -> None:
        left = count
        kept = []
        for row in self.items:
            if row["name"] == name and left > 0:
                take = min(int(row["count"]), left)
                row["count"] -= take
                left -= take
            if int(row["count"]) > 0:
                kept.append(row)
        self.items = kept

    def _add(self, name: str, count: int) -> None:
        for row in self.items:
            if row["name"] == name:
                row["count"] += count
                return
        self.items.append({"name": name, "count": count, "slot": 20})

    def call(self, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        body = body or {}
        self.calls.append((path, dict(body)))
        if path == "find_blocks":
            return {"blocks": [{"name": "furnace", "pos": p, "dist": 2.0} for p in self.furnaces]}
        if path == "window/act":
            pos = tuple(int(v) for v in (body.get("target") or {}).get("pos", []))
            for op in body.get("ops") or []:
                if op.get("op") == "put":
                    self._take(str(op["item"]), int(op.get("count") or 1))
                    if str(op.get("to")) == "input":
                        self.furnace_output.setdefault(pos, {})["pending"] = int(op.get("count") or 1)
                elif op.get("op") == "take":
                    out = self.furnace_output.get(pos, {})
                    for name, cnt in out.items():
                        if name != "pending":
                            self._add(name, cnt)
                    self.furnace_output[pos] = {}
            return {"done": [{"op": "put"}], "window": {"window": "furnace", "sections": {"input": [], "fuel": [], "output": []}}}
        if path == "window/view":
            pos = tuple(int(v) for v in (body.get("target") or {}).get("pos", []))
            out = self.furnace_output.get(pos, {})
            outputs = [{"name": k, "count": v} for k, v in out.items() if k != "pending" and v > 0]
            return {
                "window": "furnace",
                "sections": {"input": [], "fuel": [], "output": outputs},
                "info": {"burning": False, "progress": 1.0, "fuel_left": 0.0},
            }
        if path == "craft":
            self._take("cobblestone", 8 * int(body.get("count") or 1))
            self._add(str(body.get("item")), int(body.get("count") or 1))
            return {"crafted": body.get("item"), "gained": body.get("count")}
        if path == "place":
            return {"placed": body.get("item"), "pos": [3, 64, 1]}
        if path == "move_to":
            return {"pos": [body.get("x"), 64.0, body.get("z")]}
        if path == "get_block":
            pos = body.get("pos") or [0, 0, 0]
            if int(pos[1]) >= 64:
                return {"name": "air", "pos": pos}
            return {"name": "stone", "pos": pos}
        return {}


def _cobble_items(count: int, coal: int) -> list[dict[str, Any]]:
    return [
        {"name": "cobblestone", "count": count, "slot": 0},
        {"name": "coal", "count": coal, "slot": 1},
    ]


def test_smelt_start_splits_12_across_two_existing_furnaces_without_blocking() -> None:
    bot = SmeltBot(_cobble_items(24, 4))
    wm = WorldMemory()
    skills = MacroSkills(bot)
    skills.world_memory = wm
    started = time.monotonic()
    res = skills.smelt_start("cobblestone", count=12)
    assert time.monotonic() - started < 2.0
    assert res["ok"] is True, res
    assert res["furnace_count"] == 2
    assert res["non_blocking"] is True
    assert res["output_item"] == "stone"
    assert res["in_conversion_counts"] == {"stone": 12}
    assert sorted(f["input_count"] for f in res["furnaces"]) == [6, 6]
    assert wm.get_in_conversion_counts() == {"stone": 12}
    # both furnaces got exactly one coal each
    puts = [b for p, b in bot.calls if p == "window/act"]
    assert len(puts) == 2


def test_smelt_start_builds_extra_furnaces_only_with_cobble_buffer() -> None:
    bot = SmeltBot(_cobble_items(40, 8), furnaces=[])
    wm = WorldMemory()
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.smelt_start("cobblestone", count=12)
    assert res["ok"] is True, res
    assert res["furnace_count"] >= 1
    assert any(p == "craft" and b.get("item") == "furnace" for p, b in bot.calls)
    craft_calls = [b for p, b in bot.calls if p == "craft" and b.get("item") == "furnace"]
    assert craft_calls and int(craft_calls[0]["count"]) == 2
    # 40 - 8*2 = 24 cobble left after paying for the furnaces (>=16 buffer honoured)
    assert 40 - 8 * int(craft_calls[0]["count"]) >= 16


def test_smelt_start_rejects_unsafe_fuel() -> None:
    bot = SmeltBot(
        [
            {"name": "cobblestone", "count": 12, "slot": 0},
            {"name": "white_bed", "count": 1, "slot": 1},
        ]
    )
    skills = MacroSkills(bot)
    skills.world_memory = WorldMemory()
    res = skills.smelt_start("cobblestone", count=12, fuel_item="white_bed")
    assert res["ok"] is False
    assert res["error"] == "missing_fuel"


def test_smelt_collect_takes_output_and_settles_ledger() -> None:
    bot = SmeltBot(_cobble_items(24, 4))
    wm = WorldMemory()
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.smelt_start("cobblestone", count=12)
    for fur in res["furnaces"]:
        bot.furnace_output[tuple(fur["pos"])] = {"stone": fur["input_count"]}
    collected = skills.smelt_collect(furnaces=res["furnaces"])
    assert collected["ok"] is True, collected
    total_stone = sum(int(r["count"]) for r in bot.items if r["name"] == "stone")
    assert total_stone == 12
    assert wm.get_in_conversion_counts() == {}
    assert collected["jobs_remaining"] == 0


def test_smelt_items_signature_still_backward_compatible() -> None:
    import inspect

    params = inspect.signature(MacroSkills.smelt_items).parameters
    assert list(params)[:4] == ["self", "input_item", "count", "fuel_item"]


def test_smelt_collect_credits_only_the_amount_the_server_really_took() -> None:
    bot = SmeltBot(_cobble_items(24, 4))
    wm = WorldMemory()
    skills = MacroSkills(bot)
    skills.world_memory = wm
    res = skills.smelt_start("cobblestone", count=12)
    for fur in res["furnaces"]:
        bot.furnace_output[tuple(fur["pos"])] = {"stone": fur["input_count"]}
    # server lies: it only hands over 2 stone even though 6 were requested
    original_call = bot.call

    def _call(path: str, body: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        if path == "window/act":
            body = body or {}
            ops = body.get("ops") or []
            if ops and ops[0].get("op") == "take":
                pos = tuple(int(v) for v in (body.get("target") or {}).get("pos", []))
                bot.furnace_output[pos] = {"stone": 4}
                return {"done": [{"op": "take", "took": {"stone": 2}}], "window": {"window": "furnace"}}
        return original_call(path, body, **kw)

    bot.call = _call  # type: ignore[assignment]
    out = skills.smelt_collect(furnaces=res["furnaces"])
    assert out["ok"] is True
    # 2 + 2 collected => 4 credited, the remaining 8 must STILL be in the ledger
    assert wm.get_in_conversion_counts() == {"stone": 8}
