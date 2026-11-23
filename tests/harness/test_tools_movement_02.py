from tests.harness._common import *  # noqa: F401,F403

def test_silent_clamping_pillar_up_folding_and_hard_tool_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) 静默钳位：mc_skill_tunnel(steps=10 / steps=10.0)、mc_chat('/hi' + 'a'*150)，且 bad_args 不污染后续同坐标方向
    clamp_calls: list[tuple[str, Any]] = []
    clamp_state: dict[str, Any] = {"pos": [-171.5, 53.0, -386.5], "blocks": {(-172, 53, -387): "air", (-172, 54, -387): "air"}}

    def handle_clamp(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        clamp_calls.append((path, body))
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(clamp_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 64}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": clamp_state["blocks"].get(pt, "stone")}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            clamp_state["blocks"][pt] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/move_to":
            clamp_state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(clamp_state["pos"])}})
        if path == "/chat":
            return httpx.Response(200, json={"ok": True, "result": {"sent": body.get("message")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_clamp)) as bot_clamp:
        ts_clamp = McToolset(bot_clamp)
        reg_clamp = ts_clamp.registry(profile="compact")
        prep = reg_clamp.prepare(
            {"id": "p1", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"west","steps":10.0}'}}
        )
        assert not isinstance(prep, str)
        assert type(prep.arguments["steps"]) is int and prep.arguments["steps"] == 10
        tun_clamped = json.loads(prep.invoke())
        assert tun_clamped["ok"] is True
        assert tun_clamped["steps_requested"] == 8
        assert tun_clamped["steps_done"] == 8

        chat_out = json.loads(ts_clamp.mc_chat("/hi" + "a" * 150))
        assert chat_out["ok"] is True
        sent_msg = [b["message"] for p, b in clamp_calls if p == "/chat"][-1]
        assert not sent_msg.startswith("/") and len(sent_msg) == 100

    # bad_args 不入黑名单 + extract_failed_coords 跳过 bad_args 并对 tunnel 带方向标注
    bad_turn = 0
    clamp_state["pos"] = [-171.5, 53.0, -386.5]

    def chat_bad_then_valid(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal bad_turn
        bad_turn += 1
        if bad_turn == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "b1", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"invalid","steps":8}'}}
                ],
            }
        if bad_turn == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "b2", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"west","steps":8}'}}
                ],
            }
        assert json.loads(messages[-1]["content"].splitlines()[0])["ok"] is True
        return {"role": "assistant", "content": "bad_args 未污染有效方向，已成功西进。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_clamp)) as bot_bad:
        rep_bad = run_mc_agent(
            "bad_args 豁免测试",
            bot_bad,
            chat_fn=chat_bad_then_valid,
            max_calls=4,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_bad["status"] == "answered"

    extracted_tunnel = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "e1", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"invalid","steps":8}'}}]},
            {"role": "tool", "tool_call_id": "e1", "content": json.dumps({"ok": False, "error": "bad_args", "pos": [-171.5, 53, -386.5]})},
            {"role": "assistant", "tool_calls": [{"id": "e2", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}]},
            {
                "role": "tool",
                "tool_call_id": "e2",
                "content": json.dumps(
                    {
                        "ok": False,
                        "direction": "south",
                        "stop_reason": "unsafe_or_unknown_floor",
                        "viable_tunnel_directions": ["west"],
                        "viable_up_directions": ["west"],
                        "pos_after": [-171.5, 53.0, -386.5],
                    }
                ),
            },
        ]
    )
    assert extracted_tunnel == ["[-172,53,-387:south]"]

    # 2) 四向水平与斜向阶梯全水封时探测 viable_pillar_up=True，且 vertical='pillar' 完成挖顶跳跃垫脚直上
    #    3) 同时验证连续 3 次重复失败触发硬工具屏蔽，改走 pillar 升层后原工具在下一轮 Schema 中恢复
    pillar_blocks: dict[tuple[int, int, int], str] = {
        (-172, 53, -387): "air",
        (-172, 54, -387): "air",
        # 四周 y=54 水平头高处有水，同时封死 4 向 tunnel 与 4 向斜向上阶梯；南面 y=52 地板为空
        (-172, 54, -388): "water",
        (-172, 54, -386): "water",
        (-171, 54, -387): "water",
        (-173, 54, -387): "water",
        (-172, 52, -386): "air",
    }
    pillar_state: dict[str, Any] = {
        "pos": [-171.5, 53.0, -386.5],
        "jump": False,
        "cobblestone": 32,
        "dug_overhead": 0,
        "blocked_by_self_hits": 0,
        "posture_calls": [],
    }

    def handle_pillar(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(pillar_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": not pillar_state["jump"]}},
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": pillar_state["cobblestone"]}]}},
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": pillar_blocks.get(pt, "stone")}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            pillar_blocks[pt] = "air"
            pillar_state["dug_overhead"] += 1
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/posture":
            pillar_state["jump"] = bool(body.get("jump"))
            pillar_state["posture_calls"].append(bool(body.get("jump")))
            return httpx.Response(200, json={"ok": True, "result": {"jump": pillar_state["jump"]}})
        if path == "/place":
            pt = tuple(int(v) for v in body["pos"])
            cur_y = float(pillar_state["pos"][1])
            if pillar_state["jump"] and math.floor(cur_y) == pt[1]:
                pillar_state["blocked_by_self_hits"] += 1
                pillar_state["pos"][1] = float(pt[1]) + 1.15
                return httpx.Response(400, json={"ok": False, "error": {"code": "blocked_by_self", "message": "bot is standing in target cell"}})
            pillar_blocks[pt] = str(body.get("item", "cobblestone"))
            pillar_state["cobblestone"] -= 1
            pillar_state["pos"][1] = float(pt[1]) + 1.0
            return httpx.Response(200, json={"ok": True, "result": {"placed": body.get("item"), "pos": list(pt)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_pillar)) as bot_pil:
        skills_pil = MacroSkills(bot_pil)
        dead_tun = skills_pil.dig_level_tunnel("south", 6)
        assert dead_tun["ok"] is False
        assert dead_tun["viable_tunnel_directions"] == []
        assert dead_tun["viable_up_directions"] == []
        assert dead_tun["viable_pillar_up"] is True
        assert "vertical='pillar'" in dead_tun["hint"]

        up_res = skills_pil.dig_staircase(direction="pillar", steps=3, vertical="pillar")
        assert up_res["ok"] is True
        assert up_res["steps_done"] == 3
        assert up_res["placed_count"] == 3
        assert up_res["dug_count"] >= 3
        assert math.floor(float(up_res["pos_after"][1])) == 56
        assert pillar_state["blocked_by_self_hits"] == 3
        assert pillar_state["jump"] is False

    # 重置在 [-172, 53, -387] 测试：连续 3 次同向 tunnel 后硬屏蔽 mc_skill_tunnel，改走 pillar 升 2 格后自动解封，且历史被折叠为 1 轮
    pillar_blocks.clear()
    pillar_blocks.update(
        {
            (-172, 53, -387): "air",
            (-172, 54, -387): "air",
            (-172, 54, -388): "water",
            (-172, 54, -386): "water",
            (-171, 54, -387): "water",
            (-173, 54, -387): "water",
            (-172, 52, -386): "air",
        }
    )
    pillar_state["pos"] = [-171.5, 53.0, -386.5]
    pillar_state["jump"] = False
    pillar_state["cobblestone"] = 32
    seen_tool_sets: list[set[str]] = []
    mask_turn = 0

    def chat_mask_recovery(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal mask_turn
        mask_turn += 1
        tool_names = {t["function"]["name"] for t in (tools or [])}
        seen_tool_sets.append(tool_names)
        if mask_turn <= 3:
            assert "mc_skill_tunnel" in tool_names
            return {
                "role": "assistant",
                "content": "意图：向南平推。",
                "tool_calls": [
                    {"id": f"m{mask_turn}", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}
                ],
            }
        if mask_turn == 4:
            # 第 3 次连续零进展后，第 4 轮请求必须将 mc_skill_tunnel 从工具 Schema 中临时屏蔽，且前 3 轮已被折叠为 1 轮活跃记录
            assert "mc_skill_tunnel" not in tool_names
            assert "mc_skill_staircase" in tool_names
            assert sum(1 for m in messages if m.get("role") == "assistant") == 1
            assert any(m.get("name") == "ledger" and "连续重复失败×2已折叠" in str(m.get("content", "")) for m in messages)
            return {
                "role": "assistant",
                "content": "意图：垂直垫脚直上脱困。",
                "tool_calls": [
                    {"id": "m4", "type": "function", "function": {"name": "mc_skill_staircase", "arguments": '{"vertical":"pillar","steps":2}'}}
                ],
            }
        assert mask_turn == 5
        assert "mc_skill_tunnel" in tool_names
        return {"role": "assistant", "content": "已通过原地垫脚升层并解除工具屏蔽。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_pillar)) as bot_mask:
        rep_mask = run_mc_agent(
            "防复读工具屏蔽与上下文折叠测试",
            bot_mask,
            chat_fn=chat_mask_recovery,
            max_calls=7,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_mask["status"] == "answered"
    assert len(seen_tool_sets) == 5
    assert math.floor(float(pillar_state["pos"][1])) == 55
    # 验证不含独立的 role=user [Harness 防卡死提醒] 消息，且内联提醒允许 6 连重复失败完全折叠进账本
    fold_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 7):
        fold_hist.append(
            {
                "role": "assistant",
                "content": "意图：向南平推。",
                "tool_calls": [{"id": f"f{i}", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}],
            }
        )
        t_txt = json.dumps({"ok": False, "stop_reason": "unsafe_or_unknown_floor", "pos_before": [-172, 53, -387]})
        if i >= 2:
            t_txt += f"\n[Harness 防卡死提醒] 第 {i} 次拦截"
        fold_hist.append({"role": "tool", "tool_call_id": f"f{i}", "content": t_txt})
    built_fold = RequestContext(3200, mode="sliding", keep_recent_turns=6).build(fold_hist, record=True)
    assert sum(1 for m in built_fold if m.get("role") == "assistant") == 1
    assert any(m.get("name") == "ledger" and "连续重复失败×5已折叠" in str(m.get("content", "")) for m in built_fold)


def test_selective_thinking_dedup_extra_tools_shiny_gap_and_trunk_canopy(monkeypatch: pytest.MonkeyPatch) -> None:
    from mc_agent.harness.agent import _infer_extra_tools_for_task, _should_think_this_turn
    from mc_agent.harness.context import RequestContext, _compact_tool_outcome
    from mc_agent.harness.hud import compute_goal_gap_hint
    from mc_agent.skills.library import MacroSkills

    # 1) _should_think_this_turn opens thinking on Req1/fail/milestone/every-12 and closes on routine progress
    assert _should_think_this_turn(1, task_changed=False, had_incident=False, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(3, task_changed=False, had_incident=True, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(12, task_changed=False, had_incident=False, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(1, task_changed=True, had_incident=True, force_no_think=True, messages=[]) is False

    routine_msgs = [
        {"role": "assistant", "content": "下探一步", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": json.dumps({"ok": True, "steps_done": 5, "stop_reason": "completed"})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=routine_msgs) is False

    fail_msgs = [
        {"role": "assistant", "content": "合成", "tool_calls": [{"id": "t2"}]},
        {"role": "tool", "tool_call_id": "t2", "content": json.dumps({"ok": False, "error": "missing_ingredients"})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=fail_msgs) is True

    smelt_msgs = [
        {"role": "assistant", "content": "熔炼", "tool_calls": [{"id": "t3"}]},
        {"role": "tool", "tool_call_id": "t3", "content": json.dumps({"ok": True, "collected": [{"name": "iron_ingot", "count": 1}]})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=smelt_msgs) is True
    assert _compact_tool_outcome(smelt_msgs[-1]["content"]) == "ok iron_ingot+1"

    # 2) RequestContext strips reasoning_content from history and dedups repeated assistant slogans in sliding window
    ctx_hist = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        ctx_hist.append(
            {
                "role": "assistant",
                "content": "重复口号！",
                "reasoning_content": f"内部思考_{i}",
                "tool_calls": [{"id": f"d{i}", "type": "function", "function": {"name": "mc_dig", "arguments": json.dumps({"x": i, "y": 64, "z": 0})}}],
            }
        )
        ctx_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"d{i}",
                "content": json.dumps({"ok": True, "gained": {"stone": 1}}),
            }
        )
    built_ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4).build(ctx_hist)
    assert all("reasoning_content" not in m for m in built_ctx)
    asst_texts = [m["content"] for m in built_ctx if m.get("role") == "assistant"]
    assert asst_texts == ["", "", "", "重复口号！"]
    assert all("reasoning_content" in m for m in ctx_hist if m.get("role") == "assistant")

    # 3) _infer_extra_tools_for_task keeps shiny_gear concise (0 extra tools => 19 compact core tools)
    clean_task = "自主推进长程生存，达成「钻石护体」(story/shiny_gear) 成就，全程保持存活。"
    assert _infer_extra_tools_for_task(clean_task) == set()

    # 4) shiny_gear compute_goal_gap_hint preserves original concise hint without spoon-feeding steps
    gap_no_iron = compute_goal_gap_hint(
        {"stone_pickaxe": 1, "iron_ingot": 1, "coal": 8},
        task=clean_task,
        bot_y=70,
    )
    assert "diamond_boots" in gap_no_iron
    assert "iron_pickaxe" in gap_no_iron

    # 5) descend_canopy cuts through leaves standing directly above a tree trunk (*_log)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    trunk_blocks = {
        (-156, 77, -383): "birch_leaves",
        (-156, 76, -383): "birch_log",
        (-156, 75, -383): "birch_log",
        (-156, 74, -383): "grass_block",
    }
    trunk_pos = [-155.5, 78.0, -382.5]

    def trunk_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(trunk_pos), "in_water": False}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            bname = trunk_blocks.get(pt, "air")
            return httpx.Response(200, json={"ok": True, "result": {"name": bname, "diggable": bname != "air"}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            dug_blk = trunk_blocks.pop(pt, "air")
            trunk_pos[1] = float(pt[1])
            return httpx.Response(200, json={"ok": True, "result": {"dug": dug_blk}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(trunk_handler)) as bot_trunk:
        canopy_res = MacroSkills(bot_trunk).descend_canopy()
    assert canopy_res["ok"] is True
    assert canopy_res["on_canopy"] is True
    assert len(canopy_res["dug"]) == 3
    assert trunk_pos[1] == 75.0


def test_bug155_move_to_within_range_and_bug121_forced_final_status() -> None:
    """Regression for BUG-155 (mc_move_to within real float range stays ok=True; outside range fails truthfully)
    and BUG-121 (force_final turn with 0 tools reports limit_reached instead of answered)."""
    pos_now = [219427.5, 69.0, 152037.5]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": list(pos_now), "health": 20.0, "food": 20, "on_ground": True,
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(pos_now)}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    seen_results: list[dict[str, Any]] = []
    turn = 0

    def chat_move(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal turn
        turn += 1
        if turn == 1:
            # Same cell / within range=2 (dist ~0.71m <= 2.0) -> must stay ok=True, already_within_range=True
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m1", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219427,"z":152037,"range":2}',
                }}],
            }
        if turn == 2:
            seen_results.append(json.loads(messages[-1]["content"].splitlines()[0]))
            # Outside real float range=2 (219427.5, 152037.5 -> 219425, 152037 = 2.55m > 2.0) -> must fail truthfully
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m2", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219425,"z":152037,"range":2}',
                }}],
            }
        if turn == 3:
            seen_results.append(json.loads(messages[-1]["content"].splitlines()[0]))
            # Same XZ but Y=14 (dist=55m > range=2) -> must fail with 3D distance, not claim arrived
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m3", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219427,"y":14,"z":152037,"range":2}',
                }}],
            }
        last_tool_msg = next(m for m in reversed(messages) if m.get("role") == "tool")
        seen_results.append(json.loads(last_tool_msg["content"].splitlines()[0]))
        # Turn 4 is force_final (max_calls=4 -> tools=[]): plain text must report limit_reached
        assert tools == [] or tools is None
        return {"role": "assistant", "content": "预算耗尽总结"}

    with BotSession("tok", bot_username="MoveBug155", transport=httpx.MockTransport(handler)) as bot_m:
        rep = run_mc_agent(
            "测试 move_to 范围与末轮状态",
            bot_m,
            chat_fn=chat_move,
            max_calls=4,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep["status"] == "limit_reached"
    assert len(seen_results) == 3
    assert seen_results[0]["ok"] is True and seen_results[0].get("already_within_range") is True
    assert seen_results[1]["ok"] is False and seen_results[1]["error"]["code"] == "movement_not_confirmed"
    assert "已在目标" not in seen_results[1]["error"]["message"]
    assert seen_results[2]["ok"] is False and seen_results[2]["error"]["code"] == "movement_not_confirmed"
    assert float(seen_results[2]["dist_remaining"]) > 50.0


def test_api_v1_3_20_21_find_blocks_128_and_move_to_allow_partial() -> None:
    """Adaptation for upstream mc-agent-api v1.3.20 (typed find_blocks radius<=128 + <=1.3.19 fallback)
    and v1.3.21 (move_to range=0 precise alignment + allow_partial)."""
    seen: list[tuple[str, dict[str, Any]]] = []
    legacy_mode = {"on": False}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        seen.append((path, body))
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20, "on_ground": True,
            }})
        if path == "/find_blocks":
            if legacy_mode["on"] and int(body.get("radius", 0)) > 64:
                return httpx.Response(400, json={"ok": False, "error": {"code": "bad_args", "message": "radius must be 1..64"}})
            return httpx.Response(200, json={"ok": True, "result": {"total": 1, "blocks": [{"name": "bell", "pos": [100, 65, 0], "dist": 100.0}]}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [10.5, 64.0, 0.5], "dist": 0.1, "partial": bool(body.get("allow_partial"))}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="ApiAdapt", transport=httpx.MockTransport(handler)) as bot_a:
        ts = McToolset(bot_a)
        # 1) Typed find_blocks allows radius=128; summary mode stays capped at 64
        r128 = json.loads(ts.mc_find_blocks(radius=128, types=["bell"]))
        assert r128["ok"] is True
        assert [b["radius"] for p, b in seen if p == "/find_blocks"][-1] == 128

        r_sum = json.loads(ts.mc_find_blocks(radius=128, types=None))
        assert r_sum["ok"] is True
        assert [b["radius"] for p, b in seen if p == "/find_blocks"][-1] == 64

        # 2) Legacy <1.3.20 server rejecting radius>64 transparently retries at 64
        legacy_mode["on"] = True
        r_leg = json.loads(ts.mc_find_blocks(radius=128, types=["bell"]))
        assert r_leg["ok"] is True
        assert [b["radius"] for p, b in seen if p == "/find_blocks"][-2:] == [128, 64]

        # 3) move_to supports range=0 precise alignment & allow_partial=True
        rm = json.loads(ts.mc_move_to(x=10.5, z=0.5, range=0.0, allow_partial=True))
        assert rm["ok"] is True and rm["result"]["partial"] is True
        mv_body = [b for p, b in seen if p == "/move_to"][-1]
        assert mv_body["range"] == 0.0 and mv_body["allow_partial"] is True

