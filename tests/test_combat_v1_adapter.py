"""Regression tests for `combat_v1` integration via `combat_adapter` and `McToolset.mc_skill_combat`."""

from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.errors import AuthError
from mc_agent.harness.agent import _infer_extra_tools_for_task
from mc_agent.harness.events import EventSink
from mc_agent.harness.geofence import VoidGeofence
from mc_agent.harness.guardian import GenerationGuard, ReflexPreempted
from mc_agent.harness.mc_tools import McToolset
from mc_agent.skills import combat_v1
from mc_agent.skills.combat_adapter import BotSessionCombatAdapter, run_combat_skill


class _MockArena:
    def __init__(
        self,
        *,
        mob_name: str = "zombie",
        mob_dist: float = 2.4,
        hits_to_kill: int = 1,
        has_shield: bool = True,
    ) -> None:
        self.mob_name = mob_name
        self.mob_id = 9001
        self.mob_pos = [100.5 + mob_dist, 64.0, 200.5]
        self.bot_pos = [100.5, 64.0, 200.5]
        self.hits_to_kill = hits_to_kill
        self.hits_taken = 0
        self.has_shield = has_shield
        self.posture = {"sneak": False, "shield": False, "jump": False}
        self.held = {"name": "iron_sword"}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        ep = request.url.path.removeprefix("/api/v1/").strip("/")
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        self.calls.append((ep, body))

        if ep == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "ready": True,
                        "alive": True,
                        "pos": list(self.bot_pos),
                        "vel": [0.0, -0.1, 0.0],
                        "on_ground": True,
                        "health": 20.0,
                        "food": 20,
                        "in_water": False,
                        "in_lava": False,
                        "effects": [],
                        "posture": dict(self.posture),
                        "held": dict(self.held),
                    },
                },
            )
        if ep == "inventory":
            items = [
                {"name": "iron_sword", "count": 1},
                {"name": "iron_axe", "count": 1},
                {"name": "bow", "count": 1},
                {"name": "arrow", "count": 16},
                {"name": "cobblestone", "count": 32},
            ]
            if self.has_shield:
                items.append({"name": "shield", "count": 1})
            return httpx.Response(200, json={"ok": True, "result": {"held": dict(self.held), "items": items}})
        if ep == "get_entities":
            ents = []
            if self.hits_taken < self.hits_to_kill:
                ents.append(
                    {
                        "id": self.mob_id,
                        "name": self.mob_name,
                        "type": "hostile",
                        "pos": list(self.mob_pos),
                        "dist": round(math.dist(self.bot_pos, self.mob_pos), 2),
                        "health": 20.0,
                    }
                )
            return httpx.Response(200, json={"ok": True, "result": {"entities": ents}})
        if ep == "equip":
            if (body or {}).get("slot", "hand") == "hand":
                self.held = {"name": str((body or {}).get("item") or "iron_sword")}
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        if ep == "posture":
            for k in ("sneak", "shield", "jump"):
                if body and k in body:
                    self.posture[k] = bool(body[k])
            return httpx.Response(200, json={"ok": True, "result": {"posture": dict(self.posture)}})
        if ep == "control":
            hit = bool((body or {}).get("hit"))
            if hit:
                self.hits_taken += 1
            return httpx.Response(
                200,
                json={"ok": True, "result": {"ok": True, "hits": [{"hit": True, "entity_id": self.mob_id}] if hit else []}},
            )
        if ep == "shoot":
            self.hits_taken += 1
            return httpx.Response(
                200,
                json={"ok": True, "result": {"ok": True, "hit": True, "target_health_before": 20.0, "target_health_after": 10.0}},
            )
        if ep in ("look", "stop", "events"):
            return httpx.Response(200, json={"ok": True, "result": {"ok": True, "events": []}})
        if ep == "move_to":
            return httpx.Response(200, json={"ok": True, "result": {"ok": True, "pos": list(self.bot_pos)}})
        return httpx.Response(404, json={"ok": False, "error": {"code": "not_found", "message": ep}})


def test_combat_skill_modes_and_posture_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(combat_v1.time, "sleep", lambda _: None)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    for mode, mob, has_shield, expected_mode in [
        ("auto", "creeper", True, "creeper"),
        ("auto", "skeleton", True, "rush"),
        ("auto", "skeleton", False, "skeleton_bow"),
        ("auto", "zombie", False, "melee"),
        ("skeleton_shield", "skeleton", True, "skeleton_shield"),
        ("crit", "zombie", True, "crit"),
        ("wtap", "zombie", True, "wtap"),
        ("gap_foot", "zombie", True, "gap_foot"),
    ]:
        srv = _MockArena(mob_name=mob, mob_dist=2.4, hits_to_kill=1, has_shield=has_shield)
        with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(srv.handler)) as bot:
            out = run_combat_skill(bot, mode=mode, max_time_s=5.0)
            assert out["ok"] is True
            assert out["mode"] == expected_mode
            assert not any(ep == "auth/login" for ep, _ in srv.calls)
            # Lingering shield/sneak/jump posture from skel_shield/creeper must be cleared on exit.
            assert srv.posture == {"sneak": False, "shield": False, "jump": False}


def test_combat_adapter_enforces_geofence_on_entity_id_chase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(combat_v1.time, "sleep", lambda _: None)
    srv = _MockArena(mob_name="zombie", mob_dist=18.0, hits_to_kill=1)
    gf = VoidGeofence(center_x=100.5, center_z=200.5, radius=20.0, margin=5.0)  # safe_radius=15
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(srv.handler)) as bot:
        out = run_combat_skill(bot, mode="melee", target_name="zombie", max_time_s=3.0, geofence=gf)
        assert out["ok"] is False
        assert out["error"]["code"] == "void_geofence_blocked"
        assert not any(ep == "move_to" for ep, _ in srv.calls)


def test_combat_adapter_bounds_pillar_and_episode_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(combat_v1.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + max(0.05, float(s))))
    monkeypatch.setattr(combat_v1.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    srv = _MockArena(mob_name="zombie", mob_dist=2.4, hits_to_kill=999)
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(srv.handler)) as bot:
        monkeypatch.setattr(
            combat_v1,
            "build_pillar",
            lambda _b, **kw: {
                "pillar_ok": True,
                "height": 2,
                "layers_completed": 2,
                "start_pos": [100.5, 64.0, 200.5],
                "top_pos": [100.5, 66.0, 200.5],
                "pillar_blocks": [[100, 64, 200], [100, 65, 200]],
                "column": [100, 200],
            },
        )
        srv.bot_pos = [100.5, 66.0, 200.5]
        out = run_combat_skill(bot, mode="pillar", pillar_height=2, max_time_s=6.0)
        assert out["ok"] is False
        assert out["error"]["code"] == "time_budget"
        assert clock[0] <= 1009.0  # Bounded far below combat_v1's 180s internal ceiling

        # Imminent episode deadline short-circuits immediately
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))
        ts.rebind_runtime_hooks(episode_deadline_mono=clock[0] + 4.0)
        imminent = json.loads(ts.mc_skill_combat(mode="melee"))
        assert imminent["ok"] is False
        assert imminent["error"]["code"] == "episode_deadline_imminent"


def test_combat_adapter_lets_auth_and_guardian_preemption_bubble(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(combat_v1.time, "sleep", lambda _: None)

    def auth_fail(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": {"code": "unauthorized", "message": "expired"}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(auth_fail)) as bot:
        with pytest.raises(AuthError):
            run_combat_skill(bot, mode="melee", target_name="zombie", max_time_s=5.0)

    srv = _MockArena(mob_name="zombie", mob_dist=2.4, hits_to_kill=50)
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(srv.handler)) as bot:
        _guard = GenerationGuard(bot, EventSink(lambda _e: None), checkpoint=lambda: None)
        adapter = BotSessionCombatAdapter(bot)
        orig_call = bot.call
        call_n = [0]

        def preempt_on_5(path: str, body=None, **kwargs):
            call_n[0] += 1
            if call_n[0] == 5:
                raise ReflexPreempted({"source": "lava", "primary_type": "in_lava"})  # type: ignore[arg-type]
            return orig_call(path, body, **kwargs)

        monkeypatch.setattr(bot, "call", preempt_on_5)
        with pytest.raises(ReflexPreempted):
            combat_v1.action_melee(adapter, target_name="zombie", max_time_s=15.0)
        assert call_n[0] == 5


def test_compact_extra_tools_unlocks_mc_skill_combat_for_combat_tasks() -> None:
    assert "mc_skill_combat" in _infer_extra_tools_for_task("探索下界要塞并击杀烈焰人获取烈焰棒")
    assert "mc_skill_combat" in _infer_extra_tools_for_task("洞穴战斗：用盾牌弹反骷髅箭矢")
    assert "mc_skill_combat" not in _infer_extra_tools_for_task("收集原木并合成工作台")
