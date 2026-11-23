from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend, _stream_message

def test_deepseek_provider_and_direct_routing(monkeypatch, tmp_path) -> None:
    assert should_trust_env("https://api.deepseek.com/chat/completions") is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-placeholder")
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    ds = resolve_qwen_endpoint(
        QwenSpec(provider="deepseek", dotenv_path=str(empty_env), fallback_dotenv_path=None)
    )
    assert ds.endpoint == "https://api.deepseek.com/chat/completions"
    assert ds.model == DEEPSEEK_DEFAULT_MODEL
    assert ds.rpm_budget == 0
    assert ds.extra_body(enable_thinking=False) == {"thinking": {"type": "disabled"}}
    assert ds.extra_body(enable_thinking=True) == {"thinking": {"type": "enabled"}}

    monkeypatch.setenv("NJU_API_KEY", "nju-test-placeholder")
    nju = resolve_qwen_endpoint(
        QwenSpec(provider="nju", dotenv_path=str(empty_env), fallback_dotenv_path=None)
    )
    assert nju.endpoint == "https://api.nju-aia.com/v1/chat/completions"
    assert nju.model == "default"
    assert nju.rpm_budget == 24
    assert nju.extra_body(enable_thinking=False) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_request_context_quantized_kv_prefix() -> None:
    ctx = RequestContext(tool_output_chars=400, archive_after_tools=4, archive_step=4)
    big = "X" * 1200
    history: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]

    for i in range(7):
        history.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"#{i}:" + big})
        built = ctx.build(history, record=False)
        # Fewer than 4+4 tools: tool #0 stays at full tool_output_chars=400 cap, no step shift yet
        assert len(built[2]["content"]) <= 400
        assert "旧轮次工具输出已折叠" not in built[2]["content"]

    # 8th tool triggers quantised folding of tools 0..3
    history.append({"role": "tool", "tool_call_id": "t7", "content": "#7:" + big})
    built8 = ctx.build(history, record=False)
    for idx in range(2, 6):
        assert "旧轮次工具输出已折叠" in built8[idx]["content"]
    snap_prefix = [built8[idx]["content"] for idx in range(6)]

    # Adding 9th tool must keep tools 0..3 byte-identical for KV cache stability
    history.append({"role": "tool", "tool_call_id": "t8", "content": "#8:" + big})
    built9 = ctx.build(history, record=False)
    assert [built9[idx]["content"] for idx in range(6)] == snap_prefix


def test_stream_reasoning_and_event_drain() -> None:
    lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"step1 "}}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"step2","content":"好的"}}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    msg = _stream_message(lines, None)
    assert msg["content"] == "好的"
    assert msg["reasoning_content"] == "step1 step2"

    seq_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seq_calls
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"username": "5Ease_Bot", "ready": True, "pos": [0, 64, 0]}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})
        if path == "/events":
            seq_calls += 1
            if seq_calls == 1:
                return httpx.Response(200, json={"ok": True, "result": {"last_seq": 10, "events": [{"seq": 9, "type": "chat", "text": "<old> stale"}]}})
            if seq_calls == 2:
                return httpx.Response(200, json={"ok": True, "result": {"last_seq": 12, "events": [{"seq": 11, "type": "chat", "text": "<Alice> 来西边矿洞"}]}})
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 12, "events": []}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    turn = 0

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal turn
        turn += 1
        if turn == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
            }
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert any("不可信玩家公屏消息" in m["content"] and "来西边矿洞" in m["content"] for m in tool_msgs)
        assert not any("stale" in m["content"] for m in tool_msgs)
        return {"role": "assistant", "content": "已收到队友聊天提示。"}

    report = run_mc_agent("测试事件同步", bot, chat_fn=fake_chat, max_calls=4, stream=False, on_event=lambda _: None)
    assert report["status"] == "answered"


def test_macro_skills_protection_tier_safety_and_frontier() -> None:
    dug_calls: list[list[int]] = []
    posture_calls: list[dict] = []
    used_items: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10, 64, 10],
                        "health": 20,
                        "food": 12,
                        "in_water": True,
                        "submerged": True,
                        "oxygen": 10,
                        "jumping": False,
                        "on_ground": False,
                        "alive": True,
                        "ready": True,
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
                            {"name": "wooden_pickaxe", "count": 1, "slot": 36},
                            {"name": "bread", "count": 3, "slot": 37},
                        ]
                    },
                },
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "total": 2,
                        "blocks": [
                            {"name": "oak_log", "pos": [1, 64, 1], "dist": 12.0},
                            {"name": "oak_log", "pos": [60, 64, 60], "dist": 70.0},
                        ],
                    },
                },
            )
        if path == "/dig":
            dug_calls.append(body["pos"])
            if body["pos"] == [1, 64, 1]:
                return httpx.Response(
                    200,
                    json={"ok": False, "error": {"code": "protected_region", "message": "protected"}},
                )
            return httpx.Response(
                200,
                json={"ok": True, "result": {"dug": "oak_log", "gained": {"oak_log": 1}}},
            )
        if path == "/posture":
            posture_calls.append(body)
            return httpx.Response(200, json={"ok": True, "result": body})
        if path == "/use_item":
            used_items.append(body["item"])
            return httpx.Response(200, json={"ok": True, "result": {"used": {body["item"]: 1}}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"advancements": [{"id": "minecraft:story/root", "status": "completed"}]}},
            )
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)

    # 1. Pickaxe tier gate blocks wooden_pickaxe from attempting iron_ore
    tier_err = skills.mine_blocks(["iron_ore"], count=1)
    assert tier_err["ok"] is False
    assert tier_err["error"] == "tool_tier_insufficient"
    assert not dug_calls

    # 2. Protected dig blacklists [1, 64, 1] and falls back to [60, 64, 60]
    chop = skills.harvest_logs(target_logs=1)
    assert chop["ok"] is True
    assert chop["dug_count"] == 1
    assert (1, 64, 1) in skills.protected_positions

    # 3. L0 safety triggers water float + auto-eat bread
    stab = check_and_stabilize(bot)
    assert "posture_water_float" in stab["actions"]
    assert "ate:bread" in stab["actions"]
    assert posture_calls and posture_calls[0]["jump"] is True
    assert used_items == ["bread"]

    # 4. Advancement frontier lists story/mine_stone after story/root
    frontier = compute_advancement_frontier(bot, limit=15)
    assert frontier["completed_count"] == 1
    assert any(row["id"] == "minecraft:story/mine_stone" for row in frontier["frontier"])

    # 5. Low health raises SafetyEmergency
    danger_bot = BotSession(
        "tok",
        bot_username="5Ease_Bot",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"ok": True, "result": {"health": 4.0, "food": 20, "in_water": False, "in_lava": False, "alive": True}},
            )
        ),
    )
    with pytest.raises(SafetyEmergency):
        check_and_stabilize(danger_bot)


@pytest.mark.parametrize("path", ["status", "move_to"])
def test_nested_skill_transport_and_stop_use_initiating_budget(monkeypatch, path) -> None:
    from mc_agent.skills.library import _SkillBudgetSession, _SkillDeadline

    now = [100.0]
    requests = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        requests.append(route)
        timeouts = request.extensions["timeout"]
        now[0] += sum(timeouts.values()) - 0.01
        if route == "stop":
            return httpx.Response(200, json={"ok": True, "result": {"released": True}})
        raise httpx.RemoteProtocolError("offline response lost", request=request)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        outer = _SkillBudgetSession(bot, 30.0)
        inner = _SkillBudgetSession(outer, 6.0, bound_transport_retries=True)
        with pytest.raises(_SkillDeadline):
            inner.call(path, {"x": 1.5, "z": 0.5, "timeout": 2000} if path == "move_to" else None)
        if path == "move_to":
            assert inner.stop_pending() is True
        assert not outer.pending_action and not outer.timed_out

    assert requests == [path, path] + (["stop"] if path == "move_to" else [])
    assert now[0] <= 106.0


def test_kb_lookup_jit_hints_and_path_not_found_recovery() -> None:
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.kb import build_jit_error_hint, lookup_kb

    # 1. Alias resolution & recipe/ore queries
    res_bed = lookup_kb("bed", category="recipe")
    assert res_bed["resolved_alias"] == "white_bed"
    assert res_bed["results"][0]["id"] == "white_bed"
    assert res_bed["results"][0]["table_3x3"] is True

    res_iron = lookup_kb("铁矿", category="ore")
    assert any(r["id"] == "iron_ore" and "16" in r["peak_y"] for r in res_iron["results"])

    # 2. JIT error hints
    h_use = build_jit_error_hint("use_item", {"item": "water_bucket"}, "need_block_target", "need block")
    assert h_use and "mc_interact" in h_use
    h_move = build_jit_error_hint("move_to", {"x": 50, "y": 102, "z": -10}, "path_not_found", "No path")
    assert h_move and "省略 y" in h_move
    h_rec = build_jit_error_hint("craft", {"item": "steak"}, "no_recipe", "unknown")
    assert h_rec and "cooked_beef" in h_rec

    # 3. Toolset integrates JIT hint on ApiError + mc_kb_lookup + frontier playbooks + path_not_found blacklist
    dug_pos: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0], "health": 20, "food": 20}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/use_item":
            return httpx.Response(200, json={"ok": False, "error": {"code": "need_block_target", "message": "need block"}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "oak_log", "pos": [10, 64, 0], "dist": 10.0},
                            {"name": "oak_log", "pos": [3, 64, 0], "dist": 12.0},
                        ]
                    },
                },
            )
        if path == "/dig":
            dug_pos.append(body["pos"])
            if body["pos"] == [10, 64, 0]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "No path"}})
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_log", "gained": {"oak_log": 1}}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": [{"id": "minecraft:story/root", "status": "completed"}]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    ts = McToolset(bot)
    assert "mc_kb_lookup" in {s["function"]["name"] for s in ts.registry().schemas()}

    use_err = json.loads(ts.mc_use_item("bucket"))
    assert use_err["ok"] is False
    assert "mc_interact" in use_err["error"]["kb_hint"]

    # harvest_logs skips [10, 64, 0] after path_not_found and chops [3, 64, 0]
    chop = ts.skills.harvest_logs(target_logs=1)
    assert chop["ok"] is True
    assert dug_pos == [[10, 64, 0], [3, 64, 0]]
    assert (10, 64, 0) not in ts.skills.protected_positions
    assert ts.skills.temporary_avoidance[(10, 64, 0)]["reason"] == "path_not_found"

    frontier = compute_advancement_frontier(bot, limit=10)
    mine_stone = next(row for row in frontier["frontier"] if row["id"] == "minecraft:story/mine_stone")
    assert mine_stone.get("difficulty") == "easy"
    assert "how_to" in mine_stone


@pytest.mark.parametrize("roof,reason", [(None, "no_roof_block"), ("oak_log", "no_roof_block"), ("dirt", "unsafe_floor"), ("oak_planks", "unsafe_floor")])
def test_urgent_bunker_requires_ready_roof_material_before_probing(roof, reason) -> None:
    routes = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        routes.append(route)
        if route == "inventory":
            result = {"items": [{"name": roof, "count": 1}] if roof else []}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 4, "on_ground": True}
        elif route == "get_block":
            assert json.loads(request.content)["pos"] == [0, 60, 0]
            result = {"name": "air"}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_safety_bunker(urgent=True)

    assert result["ok"] is False and result["reason"] == reason
    assert routes == (["inventory"] if reason == "no_roof_block" else ["inventory", "status", "get_block"])


def test_dig_safety_bunker_and_registration() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state: dict[str, Any] = {
        "pos": [59.5, 112.0, -25.5],
        "items": [{"name": "oak_log", "count": 2, "slot": 0}],
        "blocks": {
            (59, 108, -26): "water",
            (59, 111, -26): "stone",
            (59, 110, -26): "stone",
            (59, 109, -26): "stone",
        },
        "dug": [],
        "placed": [],
        "crafted": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 10.0, "food": 18, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": state["blocks"].get(key, "air"), "pos": list(key)}})
        if path == "/craft":
            item = body["item"]
            cnt = int(body.get("count", 1))
            state["crafted"].append((item, cnt))
            if item == "oak_planks":
                state["items"] = [
                    {"name": "oak_log", "count": 1, "slot": 0},
                    {"name": "oak_planks", "count": 4, "slot": 1},
                ]
            return httpx.Response(200, json={"ok": True, "result": {"item": item, "count": cnt}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(list(key))
            state["blocks"][key] = "air"
            state["pos"][1] = float(key[1])
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        if path == "/place":
            key = tuple(int(v) for v in body["pos"])
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-bunker",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        unsafe = skills.dig_safety_bunker()
        assert unsafe["ok"] is False
        assert unsafe["reason"] == "unsafe_floor"
        assert unsafe["floor_pos"] == [59, 108, -26]
        assert state["dug"] == []

        state["blocks"][(59, 108, -26)] = "stone"
        ok_res = skills.dig_safety_bunker()
        assert ok_res["ok"] is True
        assert ok_res["floor_pos"] == [59, 108, -26]
        assert ok_res["crafted_planks"] == "oak_planks"
        assert state["dug"] == [[59, 111, -26], [59, 110, -26], [59, 109, -26]]
        assert state["placed"] == [("oak_planks", [59, 111, -26])]
        assert ok_res["roof_placed"] is True
        assert ok_res["pos_after"] == [59.5, 109.0, -25.5]

        names = [s["function"]["name"] for s in McToolset(bot).registry().schemas()]
        assert "mc_skill_bunker" in names
    finally:
        bot.close()


@pytest.mark.parametrize("block_types,next_tool", [
    (["iron_ore", "deepslate_iron_ore"], "mc_skill_mine_dfs(action='explore', target='iron')"),
    (["stone"], "mc_skill_mine_blocks"),
    (["iron_ore", "stone"], "mc_skill_mine_blocks"),
])
def test_underwater_mining_replans_from_dry_ground_without_manual_descent(block_types, next_tool) -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "find_blocks":
            assert body["types"] == block_types
            result = {"blocks": [{"name": block_types[0], "pos": [1, 62, 0], "dist": 2.2}]}
        elif route == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif route == "status":
            result = {"pos": [0.5, 64, 0.5], "in_water": False, "on_ground": True}
        elif route == "get_block":
            result = {"name": "water"}
        elif route == "equip":
            result = {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_mine_blocks(block_types, count=1))

    assert result["error"] == "underwater_targets_skipped"
    assert result["water_skipped"] == 1 and result["dug_count"] == 0
    assert "干燥" in result["message"] and next_tool in result["message"]
    assert "mc_skill_staircase" not in result["message"]
    assert ("mc_skill_mine_dfs" in result["message"]) == next_tool.startswith("mc_skill_mine_dfs")


def test_water_escape_and_underwater_mining_protection() -> None:
    state: dict[str, Any] = {
        "pos": [-169.3, 62.5, -390.7],
        "in_water": True,
        "submerged": False,
        "on_ground": False,
        "jumping": False,
        "oxygen": 230,
        "blocks": {
            # East/West/North around [-170, 62, -391] are open water
            (-169, 62, -391): "water",
            (-171, 62, -391): "water",
            (-170, 62, -392): "water",
            # South bank has solid stone base at y=62 and dirt blocking head at y=63
            (-170, 61, -390): "stone",
            (-170, 62, -390): "stone",
            (-170, 63, -390): "dirt",
            (-170, 64, -390): "air",
            # Underwater ore covered by water
            (-168, 59, -393): "iron_ore",
            (-168, 60, -393): "water",
            (-168, 61, -393): "water",
            # Dry ore
            (-160, 63, -390): "iron_ore",
            (-160, 64, -390): "air",
            # Hurt-interrupted ore
            (-160, 62, -389): "iron_ore",
            (-160, 63, -389): "air",
        },
        "dug": [],
        "controls": [],
        "postures": [],
        "items": [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
        ],
        "fail_interrupted_on": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state["pos"]),
                        "health": 16.0,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "in_water": state["in_water"],
                        "submerged": state["submerged"],
                        "on_ground": state["on_ground"],
                        "jumping": state["jumping"],
                        "oxygen": state["oxygen"],
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/posture":
            state["postures"].append(dict(body))
            state["jumping"] = bool(body.get("jump", False))
            return httpx.Response(200, json={"ok": True, "result": dict(body)})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            name = state["blocks"].get(key, "air")
            diggable = name not in {"air", "water", "flowing_water", "bedrock"}
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": diggable}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(key)
            if state["fail_interrupted_on"] == key:
                state["pos"] = [-169.3, 62.5, -390.7]
                state["in_water"] = True
                state["on_ground"] = False
                return httpx.Response(
                    409,
                    json={"ok": False, "error": {"code": "interrupted", "message": "stopped because of hurt"}},
                )
            bname = state["blocks"].pop(key, "stone")
            gained = {"raw_iron": 1} if bname == "iron_ore" else {}
            if gained:
                state["items"].append({"name": "raw_iron", "count": 1, "slot": 1})
            return httpx.Response(200, json={"ok": True, "result": {"dug": bname, "gained": gained}})
        if path == "/control":
            state["controls"].append(dict(body))
            if body.get("forward") and body.get("jump"):
                state["pos"] = [-169.5, 63.0, -389.5]
                state["in_water"] = False
                state["on_ground"] = True
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"]), "moved": 1.2}})
        if path == "/find_blocks":
            dists = {
                (-168, 59, -393): 2.0,
                (-160, 63, -390): 18.0,
                (-160, 62, -389): 20.0,
            }
            avail = [
                {"name": "iron_ore", "pos": list(k), "dist": dists[k]}
                for k in ((-168, 59, -393), (-160, 63, -390), (-160, 62, -389))
                if state["blocks"].get(k) == "iron_ore"
            ]
            return httpx.Response(200, json={"ok": True, "result": {"blocks": avail}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-water",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. check_and_stabilize triggers float on 300-tick oxygen scale (oxygen=230 < 240)
        stab = check_and_stabilize(bot)
        assert "posture_water_float" in stab["actions"]
        assert state["jumping"] is True

        # 2. unstick() in water keeps solid bank step (-170, 62, -390), digs (-170, 63, -390), hops onto dry land & resets jump
        skills = MacroSkills(bot)
        u_res = skills.unstick()
        assert u_res["ok"] is True
        assert u_res["escaped_water"] is True
        assert u_res["in_water"] is False
        assert (-170, 62, -390) not in state["dug"]
        assert (-170, 63, -390) in state["dug"]
        assert state["controls"] and abs(abs(state["controls"][0]["yaw"]) - 3.14159) < 0.35
        assert state["postures"][-1]["jump"] is False

        # 3. mine_blocks() skips water-covered [-168, 59, -393] (blacklisting it) and mines dry [-160, 63, -390]
        state["dug"].clear()
        state["pos"] = [-166.5, 61.0, -392.5]
        state["in_water"] = False
        state["on_ground"] = True
        m_res = skills.mine_blocks(["iron_ore"], count=1, radius=16)
        assert m_res["ok"] is True
        assert (-168, 59, -393) not in skills.protected_positions
        assert skills.temporary_avoidance[(-168, 59, -393)]["reason"] == "water_covered"
        assert (-168, 59, -393) not in state["dug"]
        assert (-160, 63, -390) in state["dug"]

        # 4. interrupted error blacklists [-160, 62, -389] and escapes water if knocked in
        state["dug"].clear()
        state["blocks"].pop((-160, 63, -390), None)
        state["fail_interrupted_on"] = (-160, 62, -389)
        m_err = skills.mine_blocks(["iron_ore"], count=1, radius=16)
        assert m_err["ok"] is False
        assert m_err["partial_error"]["code"] == "interrupted"
        assert (-160, 62, -389) not in skills.protected_positions
        assert skills.temporary_avoidance[(-160, 62, -389)]["reason"] == "interrupted"
        assert m_err.get("water_escape", {}).get("in_water") is False
    finally:
        bot.close()

