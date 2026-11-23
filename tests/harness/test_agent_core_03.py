from tests.harness._common import *  # noqa: F401,F403

def test_early_run_friction_fixes_max_tokens_stick_mine_dedup_and_mature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.model import QwenModel, QwenSpec

    # 1) QwenModel.chat sets max_tokens >= 4096 when enable_thinking=True
    seen_payloads: list[dict[str, Any]] = []

    def model_handler(req: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    env_p = tmp_path / ".env.test"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.setattr(
        "mc_agent.harness.model._make_http_client",
        lambda *_args, **_kw: httpx.Client(transport=httpx.MockTransport(model_handler)),
    )
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None, max_tokens=1024, enable_thinking=False)
    model = QwenModel(spec)
    model.chat([{"role": "user", "content": "hi"}], stream=False, enable_thinking=True, control=lambda: False)
    model.chat([{"role": "user", "content": "hi2"}], stream=False, enable_thinking=False, control=lambda: False)
    assert seen_payloads[0]["max_tokens"] == 4096
    assert seen_payloads[1]["max_tokens"] == 1024

    # 2) _RepeatFailureGuard._canonical_sig_args deduplicates mine_blocks across count/radius tweaks
    s1 = _RepeatFailureGuard._canonical_sig_args("mc_skill_mine_blocks", {"block_types": ["iron_ore", "coal_ore"], "count": 10, "radius": 16})
    s2 = _RepeatFailureGuard._canonical_sig_args("mc_skill_mine_blocks", {"block_types": ["coal_ore", "iron_ore"], "count": 2, "radius": 32})
    assert s1 == s2

    # 3) craft_gear("stick") converts oak_log -> oak_planks -> stick; empty inventory returns missing_ingredients
    inv_items: list[dict[str, Any]] = [{"name": "oak_log", "count": 2, "slot": 9}]
    crafted_calls: list[tuple[str, int]] = []

    def craft_handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items), "empty_slots": 35 - len(inv_items)}})
        if action == "craft":
            item_name = str(body["item"])
            cnt = int(body["count"])
            crafted_calls.append((item_name, cnt))
            if item_name == "oak_planks":
                inv_items[:] = [{"name": "oak_planks", "count": cnt, "slot": 9}]
            elif item_name == "stick":
                inv_items[:] = [{"name": "stick", "count": cnt, "slot": 9}]
            return httpx.Response(200, json={"ok": True, "result": {"crafted": item_name, "count": cnt}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(craft_handler)) as bot_craft:
        skills_craft = MacroSkills(bot_craft)
        stick_ok = skills_craft.craft_gear("stick", count=4)
        assert stick_ok["ok"] is True
        assert ("oak_planks", 4) in crafted_calls
        assert ("stick", 4) in crafted_calls

        inv_items.clear()
        stick_miss = skills_craft.craft_gear("stick", count=4)
        assert stick_miss["ok"] is False
        assert stick_miss["error"] == "missing_ingredients"
        assert "planks_or_log" in stick_miss["missing_materials"]

    # 4) mine_blocks 0-target message + mc_find_blocks dropping mature on non-crops
    find_payloads: list[dict[str, Any]] = []

    def find_handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5], "dimension": "overworld", "biome": "plains", "health": 20}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1, "slot": 9}], "empty_slots": 35}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": "iron_pickaxe"}}})
        if action == "find_blocks":
            find_payloads.append(dict(body))
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(find_handler)) as bot_find:
        skills_find = MacroSkills(bot_find)
        zero_res = skills_find.mine_blocks(["iron_ore"], count=5, radius=12)
        assert zero_res["ok"] is False
        assert zero_res["error"] == "no_targets_available"
        assert "未扫描到任何匹配目标方块" in zero_res["message"]

        ts_find = McToolset(bot_find)
        ts_find.mc_find_blocks(radius=12, types=["iron_ore"], mature=True)
        ts_find.mc_find_blocks(radius=12, types=["wheat"], mature=True)
    assert "mature" not in find_payloads[-2]
    assert find_payloads[-1]["mature"] is True


def test_target_drop_verification_cobble_cap_turn_away_and_jump_pickup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    base_items = (
        [{"name": "iron_pickaxe", "count": 1, "slot": 9}]
        + [{"name": "cobblestone", "count": 64, "slot": 10 + i} for i in range(14)]
        + [{"name": "cobblestone", "count": 4, "slot": 24}]
        + [{"name": "stone_sword", "count": 1, "slot": 25 + i} for i in range(15)]
    )
    state_a: dict[str, Any] = {
        "items": list(base_items),
        "actions": [],
        "drops": [],
        "moves": [],
        "controls": [],
        "pickup_raw_iron": False,
        "entities": [
            {"id": 10, "item": "dirtx64", "pos": [0.5, 40.0, 0.5], "dist": 0.2},
            {"id": 11, "item": "cobblestonex64", "pos": [0.5, 40.0, 0.5], "dist": 0.3},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        state_a["actions"].append((action, body))
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5], "yaw": 0.0, "in_water": False, "alive": True}})
        if action == "inventory":
            items = list(state_a["items"])
            return httpx.Response(200, json={"ok": True, "result": {"items": items, "empty_slots": max(0, 36 - len(items))}})
        if action == "look":
            return httpx.Response(200, json={"ok": True, "result": {"yaw": body["yaw"]}})
        if action == "drop":
            dname = body["item"]
            dcnt = int(body["count"])
            state_a["drops"].append((dname, dcnt))
            rem = dcnt
            new_items = []
            for row in state_a["items"]:
                if row["name"] == dname and rem > 0:
                    take = min(rem, int(row["count"]))
                    rem -= take
                    left = int(row["count"]) - take
                    if left > 0:
                        new_items.append({**row, "count": left})
                else:
                    new_items.append(row)
            state_a["items"] = new_items
            return httpx.Response(200, json={"ok": True, "result": {"dropped": dname, "count": dcnt}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": [2, 41, 0], "dist": 2.0}]}})
        if action == "dig":
            return httpx.Response(200, json={"ok": True, "result": {"dug": "iron_ore", "gained": {"dirt": 1}}})
        if action == "get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": list(state_a["entities"])}})
        if action == "move_to":
            state_a["moves"].append(dict(body))
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5]}})
        if action == "control":
            state_a["controls"].append(dict(body))
            if state_a["pickup_raw_iron"] and body.get("forward") and body.get("jump"):
                state_a["items"].append({"name": "raw_iron", "count": 1, "slot": 20})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1.5, 41.0, 0.5]}})
        raise AssertionError(action)

    # A) Cobble capped <=192 after turning away (look yaw=pi); incidental dirt does NOT yield ok=True
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_mine:
        skills_mine = MacroSkills(bot_mine)
        res_a = skills_mine.mine_blocks(["iron_ore"], count=1, radius=16)

    look_idx = next(i for i, (act, _) in enumerate(state_a["actions"]) if act == "look")
    drop_idx = next(i for i, (act, _) in enumerate(state_a["actions"]) if act == "drop")
    assert look_idx < drop_idx
    assert abs(state_a["actions"][look_idx][1]["yaw"] - math.pi) < 1e-4
    cobble_left = sum(int(r["count"]) for r in state_a["items"] if r["name"] == "cobblestone")
    assert cobble_left <= 192
    assert state_a["moves"] == []  # Auto-dropped clutter dirt/cobblestone at feet were skipped
    assert res_a["ok"] is False
    assert res_a["error"] == "no_drops_collected"
    assert res_a["incidental_gained"] == {"dirt": 1}

    # B) Elevated raw_iron in 1x1 alcove (dy=+1.2) skips closer dirtx64, jumps to collect, and returns ok=True
    state_a["items"] = list(base_items)
    state_a["drops"].clear()
    state_a["moves"].clear()
    state_a["controls"].clear()
    state_a["pickup_raw_iron"] = True
    state_a["entities"] = [
        {"id": 20, "item": "dirtx64", "pos": [0.5, 40.0, 0.5], "dist": 0.2},
        {"id": 21, "item": "raw_ironx1", "pos": [2.0, 41.2, 0.5], "dist": 1.9},
    ]
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_mine_b:
        res_b = MacroSkills(bot_mine_b).mine_blocks(["iron_ore"], count=1, radius=16)
    assert all(m.get("entity_id") != 20 for m in state_a["moves"])
    assert any(c.get("forward") is True and c.get("jump") is True for c in state_a["controls"])
    assert res_b["ok"] is True
    assert res_b["target_gained"] == {"raw_iron": 1}


def test_night_double_death_triggers_emergency_shelter_before_third_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.harness.agent import run_mc_agent

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.bot.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda dt: clock.__setitem__(0, clock[0] + float(dt)))

    world_state: dict[str, Any] = {
        "pos": [0.5, 70.0, 0.5],
        "on_ground": True,
        "placed": [],
        "dug": [],
        "seq_calls": 0,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            since = int(req.url.params.get("since", 0)) if req.method == "GET" else int(body.get("since", 0))
            world_state["seq_calls"] += 1
            if since == 0 and world_state["seq_calls"] == 1:
                return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
            if since < 2:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "last_seq": 2,
                            "events": [
                                {"seq": 1, "type": "death", "pos": [0, 70, 0], "cause": "mob_attack"},
                                {"seq": 2, "type": "death", "pos": [1, 70, 1], "cause": "mob_attack"},
                            ],
                        },
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 2}})
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(world_state["pos"]),
                        "health": 20,
                        "food": 20,
                        "is_day": False,
                        "alive": True,
                        "ready": True,
                        "on_ground": world_state["on_ground"],
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "items": [
                            {"name": "cobblestone", "count": 32, "slot": 9},
                            {"name": "stone_pickaxe", "count": 1, "slot": 10},
                        ],
                        "empty_slots": 34,
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": [], "summary": {"completed": 0}}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in world_state["placed"]:
                return httpx.Response(200, json={"ok": True, "result": {"name": "cobblestone", "diggable": True}})
            if pt in world_state["dug"]:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air", "diggable": False}})
            if pt[1] >= 70:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air", "diggable": False}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(world_state["pos"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            world_state["dug"].append(pt)
            if pt == (0, 69, 0):
                world_state["pos"] = [0.5, 69.0, 0.5]
            elif pt == (0, 68, 0):
                world_state["pos"] = [0.5, 68.0, 0.5]
            elif pt == (0, 67, 0):
                world_state["pos"] = [0.5, 67.0, 0.5]
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/place":
            world_state["placed"].append(tuple(int(v) for v in body["pos"]))
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"]}})
        raise AssertionError(path)

    turn_num = 0
    seen_shelter_msg = False

    def chat(messages, tools=None, **kwargs):
        nonlocal turn_num, seen_shelter_msg
        turn_num += 1
        if turn_num == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
            }
        seen_shelter_msg = any("[防死亡循环保护]" in str(m.get("content") or "") for m in messages)
        return {"role": "assistant", "content": "已在掩体安全躲过 60s 连死窗口。"}

    run_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_s:
        rep = run_mc_agent(
            "survive night",
            bot_s,
            chat_fn=chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=run_events.append,
        )
    assert rep["status"] == "answered"
    assert seen_shelter_msg is True
    shelter_evs = [e for e in run_events if e["type"] == "guardian.death_loop_shelter"]
    assert len(shelter_evs) == 1
    assert shelter_evs[0]["sheltered"] is True
    assert shelter_evs[0]["held_s"] == 55.0
    assert clock[0] >= 1055.0


def test_v1314_control_wait_and_shoot_charge_passthrough() -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        recorded.append((path, body))
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10.0, 64.0, 10.0],
                        "ready": True,
                        "held_controls": {"keys": ["forward"], "remaining_ms": 250},
                    },
                },
            )
        if path == "/control":
            if body == {"wait": False}:
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"held": [], "released": ["forward"], "pos": [10.0, 64.0, 10.0]}},
                )
            return httpx.Response(
                200,
                json={"ok": True, "result": {"held": ["forward"], "release_in_ms": body.get("ms", 300)}},
            )
        if path == "/shoot":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"fired": True, "weapon": body.get("weapon"), "charge_ms": body.get("charge_ms")}},
            )
        raise AssertionError(f"unexpected path: {path}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_s:
        ts = McToolset(bot_s)
        res_start = json.loads(ts.mc_control(forward=True, ms=400, wait=False))
        assert res_start["ok"] is True
        res_stop = json.loads(ts.mc_control(wait=False))
        assert res_stop["ok"] is True
        st_res = json.loads(ts.mc_status())
        assert st_res["result"]["held_controls"] == {"keys": ["forward"], "remaining_ms": 250}
        res_shoot = json.loads(ts.mc_shoot(entity_id=1, weapon="bow", charge_ms=1000))
        assert res_shoot["ok"] is True

    control_calls = [body for path, body in recorded if path == "/control"]
    assert control_calls == [{"forward": True, "ms": 400, "wait": False}, {"wait": False}]
    shoot_calls = [body for path, body in recorded if path == "/shoot"]
    assert shoot_calls == [{"weapon": "bow", "entity_id": 1, "charge_ms": 1000}]

