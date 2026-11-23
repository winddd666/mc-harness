from tests.harness._common import *  # noqa: F401,F403

def test_void_geofence_blocks_out_of_bounds_actions_and_allows_inside() -> None:
    gf = VoidGeofence(center_x=100.0, center_z=-200.0, radius=60.0, margin=5.0)
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        called_paths.append(path)
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [150.5, 64.0, -200.0], "ready": True}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=gf)

        mv_out = json.loads(ts.mc_move_to(x=160.0, z=-200.0))
        assert mv_out["ok"] is False
        assert mv_out["error"]["code"] == "void_geofence_blocked"

        dig_out = json.loads(ts.mc_dig(160, 64, -200))
        assert dig_out["ok"] is False
        assert dig_out["error"]["code"] == "void_geofence_blocked"

        stair_out = json.loads(ts.mc_skill_staircase(direction="east", steps=8, vertical="down"))
        assert stair_out["ok"] is False
        assert stair_out["error"]["code"] == "void_geofence_blocked"

        tunnel_out = json.loads(ts.mc_skill_tunnel(direction="east", steps=6))
        assert tunnel_out["ok"] is False
        assert tunnel_out["error"]["code"] == "void_geofence_blocked"

        assert "/move_to" not in called_paths
        assert "/dig" not in called_paths

        mv_in = json.loads(ts.mc_move_to(x=120.0, z=-200.0))
        assert mv_in["ok"] is True
        assert "/move_to" in called_paths


def test_bug048_geofence_clamps_mild_overshoot_and_escalates_repeated_or_implausible_blocks() -> None:
    gf = VoidGeofence(center_x=100.0, center_z=-200.0, radius=220.0, margin=8.0)  # safe_radius = 212.0
    move_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [308.5, 64.0, -200.0], "ready": True}})
        if path == "/move_to":
            move_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=gf)

        # 1) Mild overshoot (dist=240 <= 212 * 1.25 = 265) clamps inside safe_radius-1 and executes.
        clamped_out = json.loads(ts.mc_move_to(x=340.0, z=-200.0))
        assert clamped_out["ok"] is True
        assert clamped_out["geofence_clamped"] is True
        assert clamped_out["geofence_clamped_xz"] == [311.0, -200.0]
        assert move_payloads[-1]["x"] == 311.0
        assert move_payloads[-1]["z"] == -200.0

        # 2) Implausible coordinate (> 3 * safe_radius) immediately demands replan.
        far_out = json.loads(ts.mc_move_to(x=1500.0, z=-200.0))
        assert far_out["ok"] is False
        assert far_out["requires_replan"] is True
        assert far_out["error"]["code"] == "void_geofence_blocked"
        assert far_out["error"]["reason"] == "implausible_distance"
        assert far_out["geofence_blocked_count"] == 1

        # 3) Distinct moderate out-of-bounds targets escalate on 3rd hit; tunnel suggests inward dirs.
        b2 = json.loads(ts.mc_move_to(x=450.0, z=-200.0))
        assert b2["ok"] is False
        assert b2.get("requires_replan") is not True
        assert b2["geofence_blocked_count"] == 2

        b3 = json.loads(ts.mc_skill_tunnel(direction="east", steps=6))
        assert b3["ok"] is False
        assert b3["error"]["code"] == "void_geofence_blocked"
        assert b3["geofence_blocked_count"] == 3
        assert b3["requires_replan"] is True
        assert "west" in b3["suggested_safe_directions"]

        # 4) Episode rebind resets the counter.
        ts.rebind_runtime_hooks(geofence=gf)
        b_reset = json.loads(ts.mc_move_to(x=451.0, z=-200.0))
        assert b_reset["geofence_blocked_count"] == 1
        assert b_reset.get("requires_replan") is not True


def test_bug064_scan_ceiling_surfaces_clamp_and_stops_radius_doubling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}},
            )
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [], "total": 0}})
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))

        first = json.loads(ts.mc_find_blocks(radius=256, types=["lava"]))
        assert first["result"]["radius_clamped_from"] == 256
        assert first["result"]["radius_effective"] == 128
        assert first.get("requires_replan") is not True

        second = json.loads(ts.mc_find_blocks(radius=512, types=["lava"]))
        assert second["requires_replan"] is True
        assert second["scan_exhausted"] is True
        assert second["empty_max_scan_count"] == 2

        e1 = json.loads(ts.mc_get_entities(radius=2048, types=["wolf"]))
        assert e1["result"]["radius_clamped_from"] == 2048
        assert e1.get("requires_replan") is not True

        e2 = json.loads(ts.mc_get_entities(radius=4096, types=["wolf"]))
        assert e2["requires_replan"] is True
        assert e2["scan_exhausted"] is True

        ts.rebind_runtime_hooks()
        e3 = json.loads(ts.mc_get_entities(radius=64, types=["wolf"]))
        assert e3.get("requires_replan") is not True


def test_bug047_move_to_clamps_timeout_and_stops_cleanly_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [2000.0]
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.monotonic", lambda: now[0])
    seen_calls: list[tuple[str, float | None]] = []

    class FakeBot:
        def call(self, path: str, body=None, **kwargs):
            seen_calls.append((path, kwargs.get("timeout_s")))
            if path == "status":
                return {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}
            if path == "move_to":
                raise httpx.ReadTimeout("move timed out")
            return {"ok": True}

    bot = FakeBot()
    ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))  # type: ignore[arg-type]

    # 1) Tight episode deadline (11s remaining -> 8s budget) stops cleanly without raising.
    ts.rebind_runtime_hooks(episode_deadline_mono=2011.0)
    out = json.loads(ts.mc_move_to(x=120.0, z=200.0))
    assert out["ok"] is False
    assert out["requires_replan"] is True
    assert out["error"]["code"] == "move_timeout"
    assert ("move_to", 8.0) in seen_calls
    assert seen_calls[-1][0] == "stop"

    # 2) Imminent deadline (4s remaining) short-circuits before issuing move_to.
    ts.rebind_runtime_hooks(episode_deadline_mono=2004.0)
    before_n = len(seen_calls)
    out2 = json.loads(ts.mc_move_to(x=120.0, z=200.0))
    assert out2["ok"] is False
    assert out2["error"]["code"] == "episode_deadline_imminent"
    assert len(seen_calls) == before_n or all(p != "move_to" for p, _ in seen_calls[before_n:])


def test_compact_tool_schemas_fit_sub_3k_budget() -> None:
    def dummy(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(dummy)) as bot:
        ts = McToolset(bot)
        s_core = ts.registry(profile="compact", extra_tools=()).schemas()
        s_ext = ts.registry(
            profile="compact",
            extra_tools=("mc_interact", "mc_use_item", "mc_posture"),
        ).schemas()
        assert len(s_core) == 15
        assert len(s_ext) == 18
        assert len(json.dumps(s_core, ensure_ascii=False)) < 4300
        assert len(json.dumps(s_ext, ensure_ascii=False)) < 5250

        compact_by_name = {sc["function"]["name"]: sc["function"] for sc in s_core}
        assert "mc_attack" in compact_by_name
        assert "mc_skill_flee" in compact_by_name
        assert "mc_craft" not in compact_by_name
        assert "mc_equip" not in compact_by_name
        assert "mc_skill_collect_drops" not in compact_by_name
        assert "mc_skill_bunker" not in compact_by_name
        assert compact_by_name["mc_skill_staircase"]["parameters"]["properties"]["direction"]["description"] == "north/south/east/west"
        assert compact_by_name["mc_skill_tunnel"]["parameters"]["properties"]["direction"]["description"] == "north/south/east/west"

        called_dirs: list[tuple[str, int, str]] = []
        ts.skills.dig_staircase = lambda direction="north", steps=12, vertical="down", stop_on_ore=True: (
            called_dirs.append((direction, steps, vertical)) or {"ok": True, "direction": direction, "vertical": vertical}
        )
        coerced = json.loads(ts.mc_skill_staircase(direction="down", steps=2, vertical="down"))
        assert coerced["ok"] is True
        assert called_dirs == [("north", 2, "down")]

        full_by_name = {sc["function"]["name"]: sc["function"] for sc in ts.registry().schemas()}
        assert "speed" in full_by_name["mc_status"]["description"]
        assert full_by_name["mc_status"]["parameters"]["additionalProperties"] is False


def test_zombie_counter_attack_and_knockback_immune_flee(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1) Armed 1v1 zombie counter-attack in GenerationGuard
    calls_1: list[tuple[str, dict[str, Any]]] = []
    state_1: dict[str, Any] = {"killed": False, "hurt_emitted": False}

    def handle_armed(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            nh = None if state_1["killed"] else {"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.0, 64.0, 0.0],
                        "health": 17,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": True,
                        "nearest_hostile": nh,
                    },
                },
            )
        if action == "events":
            rows = (
                [
                    {
                        "seq": 1,
                        "type": "hurt",
                        "health": 17,
                        "source": "mob_attack",
                        "source_entity": {"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]},
                    }
                ]
                if not state_1["hurt_emitted"]
                else []
            )
            state_1["hurt_emitted"] = True
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": 1}})
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1}]}},
            )
        if action == "get_entities":
            ents = [] if state_1["killed"] else [{"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]}]
            return httpx.Response(200, json={"ok": True, "result": {"entities": ents}})
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "equip":
            calls_1.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "attack":
            calls_1.append((action, body))
            state_1["killed"] = True
            return httpx.Response(200, json={"ok": True, "result": {"killed": True}})
        raise AssertionError(f"unexpected action in armed counter-attack: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_armed)) as bot1:
        guard1 = GenerationGuard(bot1, EventSink(lambda _: None), checkpoint=lambda: None)
        incident1 = guard1.poll()
    assert incident1 is not None
    assert incident1["actions"][0]["action"] == "combat_or_flee"
    assert incident1["actions"][0]["ok"] is True
    assert incident1["actions"][0]["result"]["mode"] == "counter_attack"
    assert incident1["actions"][0]["result"]["weapon"] == "stone_sword"
    assert incident1["actions"][0]["result"]["killed"] is True
    assert [c[0] for c in calls_1] == ["equip", "attack"]
    assert calls_1[0][1]["item"] == "stone_sword"
    assert calls_1[1][1]["entity_id"] == 77

    # 2) Unarmed airborne knockback flee in GenerationGuard (respecting VoidGeofence)
    calls_2: list[tuple[str, dict[str, Any]]] = []
    state_2: dict[str, Any] = {"pos": [0.0, 64.0, 0.0], "hurt_emitted": False}

    def handle_flee(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            dist = abs(state_2["pos"][2] - (-2.0))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state_2["pos"]),
                        "health": 15,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": False,
                        "nearest_hostile": {"id": 77, "name": "zombie", "dist": dist, "pos": [0.0, 64.0, -2.0]},
                    },
                },
            )
        if action == "events":
            rows = (
                [
                    {
                        "seq": 1,
                        "type": "hurt",
                        "health": 15,
                        "source": "mob_attack",
                        "source_entity": {"id": 77, "name": "zombie", "dist": 2.0, "pos": [0.0, 64.0, -2.0]},
                    }
                ]
                if not state_2["hurt_emitted"]
                else []
            )
            state_2["hurt_emitted"] = True
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": 1}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "get_entities":
            dist = abs(state_2["pos"][2] - (-2.0))
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 77, "name": "zombie", "dist": dist, "pos": [0.0, 64.0, -2.0]}]}},
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "control":
            calls_2.append((action, body))
            assert body["sprint"] is True
            assert body["jump"] is True
            assert body["stop_on"] == ["death"]
            state_2["pos"] = [0.0, 64.0, 6.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_2["pos"])}})
        if action == "move_to":
            calls_2.append((action, body))
            assert body["stop_on"] == ["death"]
            state_2["pos"] = [0.0, 64.0, 14.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_2["pos"])}})
        if action == "attack":
            raise AssertionError("unarmed bot must not attack")
        raise AssertionError(f"unexpected action in airborne flee: {action}")

    gf = VoidGeofence(center_x=0.0, center_z=0.0, radius=55.0, margin=5.0)
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_flee)) as bot2:
        guard2 = GenerationGuard(bot2, EventSink(lambda _: None), checkpoint=lambda: None, geofence=gf)
        incident2 = guard2.poll()
    assert incident2 is not None
    assert incident2["actions"][0]["action"] == "combat_or_flee"
    assert incident2["actions"][0]["ok"] is True
    assert incident2["actions"][0]["result"]["mode"] == "flee"
    assert incident2["actions"][0]["result"]["moved_dist"] >= 5.0
    assert not any(a == "attack" for a, _ in calls_2)
    assert any(a == "control" for a, _ in calls_2)

    # 3) Creeper override even when armed (iron_sword + health=20)
    calls_3: list[tuple[str, dict[str, Any]]] = []
    state_3: dict[str, Any] = {"pos": [0.0, 64.0, 0.0]}

    def handle_creeper(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            dist = abs(state_3["pos"][2] - 3.0)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state_3["pos"]),
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 88, "name": "creeper", "dist": dist, "pos": [0.0, 64.0, 3.0]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_sword", "count": 1}]}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 88, "name": "creeper", "dist": 3.5, "pos": [0.0, 64.0, 3.0]}]}},
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "control":
            calls_3.append((action, body))
            state_3["pos"] = [0.0, 64.0, -7.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_3["pos"])}})
        if action == "move_to":
            calls_3.append((action, body))
            state_3["pos"] = [0.0, 64.0, -14.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_3["pos"])}})
        if action == "attack":
            raise AssertionError("must refuse /attack near creeper")
        raise AssertionError(f"unexpected action near creeper: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_creeper)) as bot3:
        res3 = MacroSkills(bot3).defend_or_flee(
            attacker={"id": 88, "name": "creeper", "dist": 3.5, "pos": [0.0, 64.0, 3.0]},
            urgent=True,
        )
    assert res3["mode"] == "flee"
    assert res3["ok"] is True
    assert not any(a == "attack" for a, _ in calls_3)

    # 4) McToolset.mc_attack auto-equip & WorldHUD #id rendering
    calls_4: list[tuple[str, dict[str, Any]]] = []

    def handle_tool(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.0, 64.0, 0.0],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 77, "name": "zombie", "dist": 1.8},
                    },
                },
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [{"id": 77, "name": "zombie", "type": "hostile", "dist": 1.8, "pos": [0.0, 64.0, -1.8]}]
                    },
                },
            )
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "held": {"name": "stone_pickaxe"},
                        "items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1}],
                    },
                },
            )
        if action == "equip":
            calls_4.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "attack":
            calls_4.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"killed": True}})
        raise AssertionError(f"unexpected action in mc_attack: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_tool)) as bot4:
        ts4 = McToolset(bot4)
        atk_out = json.loads(ts4.mc_attack(77))
        hud_txt = build_world_hud(ts4)
    assert atk_out["ok"] is True
    assert atk_out["result"]["killed"] is True
    assert [a for a, _ in calls_4] == ["equip", "attack"]
    assert calls_4[0][1] == {"item": "stone_sword", "slot": "hand"}
    assert calls_4[1][1] == {"entity_id": 77, "timeout": 20000, "feedback": True}
    assert "zombie#77@1.8m" in hud_txt
    assert "近敌(无遮挡!)" in hud_txt

