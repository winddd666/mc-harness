from tests.harness._common import *  # noqa: F401,F403

def test_rules_md_table_matches_curriculum_points_exactly() -> None:
    rules_path = Path(__file__).resolve().parents[2] / "RULES.md"
    assert rules_path.is_file()
    row_re = re.compile(r"^\|\s*`(minecraft:[^`]+)`\s*\|\s*[^|]+\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")

    parsed_points: dict[str, int] = {}
    parsed_unconditional: set[str] = set()
    for line in rules_path.read_text(encoding="utf-8").splitlines():
        m = row_re.match(line.strip())
        if not m:
            continue
        adv_id, pts_raw, cond = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if pts_raw.isdigit():
            parsed_points[adv_id] = int(pts_raw)
            if cond == "计分":
                parsed_unconditional.add(adv_id)

    assert len(parsed_points) == 99
    assert parsed_points == RULES_ADVANCEMENT_POINTS
    assert len(parsed_unconditional) == 45
    assert parsed_unconditional == set(UNCONDITIONAL_ADVANCEMENTS)


def test_bug047_mine_dfs_clamps_to_55s_and_episode_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.monotonic", lambda: now[0])
    seen_budgets: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))
        monkeypatch.setattr(
            ts.skills,
            "mine_dfs",
            lambda **kwargs: (seen_budgets.append(float(kwargs["max_duration_s"])) or {"ok": True}),
        )
        monkeypatch.setattr(ts.skills, "_ensure_free_slots", lambda min_free=3: (True, []))

        # 1) Unbounded episode caps oversized request to 90s.
        json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=150.0))
        assert seen_budgets[-1] == 90.0

        # 2) Tight episode deadline (20s remaining) clamps to 16s (reserve 4s).
        ts.rebind_runtime_hooks(episode_deadline_mono=1020.0)
        json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=90.0))
        assert seen_budgets[-1] == 16.0

        # 3) Imminent deadline (5s remaining) short-circuits without calling mine_dfs.
        ts.rebind_runtime_hooks(episode_deadline_mono=1005.0)
        count_before = len(seen_budgets)
        out = json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=90.0))
        assert out["ok"] is False
        assert out["error"]["code"] == "episode_deadline_imminent"
        assert len(seen_budgets) == count_before


def test_model_length_recovery_http_retries_and_thread_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. 流式 finish_reason=length：保留完整前缀 tool_calls，丢弃截断尾巴
    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"c0","type":"function","function":{"name":"mc_dig","arguments":"{\\"x\\":1,\\"y\\":64,\\"z\\":2}"}},{"index":1,"id":"c1","type":"function","function":{"name":"mc_place","arguments":"{\\"item\\":\\"st"}}]},"finish_reason":"length"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    msg = _stream_message(io.BytesIO("\r\n".join(sse_lines).encode("utf-8")), None)
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "mc_dig"

    # 2. 流式 finish_reason=length 且无完整 tool_calls：抛 OutputTruncatedError，run_mc_agent 内可自愈一次
    sse_bad = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"长篇思考未完"},"finish_reason":"length"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    with pytest.raises(OutputTruncatedError):
        _stream_message(io.BytesIO("\r\n".join(sse_bad).encode("utf-8")), None)

    def handle_bot(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    turn = 0

    def chat_trunc_then_ok(messages, tools=None, **kwargs):
        nonlocal turn
        turn += 1
        assert tools is not None
        if turn == 1:
            raise OutputTruncatedError("truncated")
        assert any("上轮输出超长被截断" in str(m.get("content", "")) for m in messages)
        return {"role": "assistant", "content": "已完成。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot:
        rep = run_mc_agent(
            "恢复截断",
            bot,
            chat_fn=chat_trunc_then_ok,
            max_calls=4,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
            include_initial_snapshot=False,
        )
    assert rep["status"] == "answered"
    assert rep["actor_requests"] == 2

    # 3. _chat_direct 对 524 / 429 退避重试 3 次成功
    monkeypatch.setattr("mc_agent.harness.model.time.sleep", lambda _: None)
    env_p = tmp_path / ".env.test"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None)
    calls_http = 0

    def retry_transport(req: httpx.Request) -> httpx.Response:
        nonlocal calls_http
        calls_http += 1
        if calls_http == 1:
            return httpx.Response(524, text="cf timeout")
        if calls_http == 2:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="rate limit")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(retry_transport))
    try:
        out = _chat_direct(spec, [{"role": "user", "content": "hi"}], stream=False, client=client)
    finally:
        client.close()
    assert out["content"] == "ok"
    assert calls_http == 3

    # 4. 线程化 _process_request 可被 control 立即取消并触发 on_cancel
    cancelled_cb = False

    def slow_transport(req: httpx.Request) -> httpx.Response:
        import time as _t
        _t.sleep(0.5)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "late"}}]})

    slow_client = httpx.Client(transport=httpx.MockTransport(slow_transport))

    def on_cancel() -> None:
        nonlocal cancelled_cb
        cancelled_cb = True

    checks = 0

    def control_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    with pytest.raises(RequestCancelled):
        _process_request(
            spec,
            [{"role": "user", "content": "hi"}],
            None,
            False,
            on_event=None,
            control=control_cancel,
            client=slow_client,
            on_cancel=on_cancel,
        )
    slow_client.close()
    assert cancelled_cb is True


def test_curriculum_target_priority_and_two_strike_cooldown(tmp_path: Path) -> None:
    inv_items = [{"name": "iron_pickaxe", "count": 1}]
    adv_rows = [
        {"id": "minecraft:story/root", "status": "completed"},
        {"id": "minecraft:husbandry/root", "status": "completed"},
        {"id": "minecraft:adventure/root", "status": "completed"},
        {"id": "minecraft:story/mine_stone", "status": "completed"},
        {"id": "minecraft:story/upgrade_tools", "status": "completed"},
        {"id": "minecraft:story/smelt_iron", "status": "completed"},
        {"id": "minecraft:story/iron_tools", "status": "completed"},
        {"id": "minecraft:story/lava_bucket", "status": "not_started"},
        {"id": "minecraft:adventure/use_lodestone", "status": "not_started"},
        {"id": "minecraft:husbandry/make_a_sign_glow", "status": "not_started"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items)}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": list(adv_rows)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(path)

    targets_seen: list[str] = []
    tool_counts_seen: list[int] = []

    def chat_ep(messages, tools=None, **kwargs):
        tool_counts_seen.append(len(tools or []))
        return {"role": "assistant", "content": "本轮尝试结束。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        first_target, _ = select_next_target(bot)
        assert first_target.key == "minecraft:story/shiny_gear"
        assert first_target.points == 60

        sup = CurriculumSupervisor(
            bot,
            chat_fn=chat_ep,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            max_calls_per_episode=2,
            cooldown_s=600.0,
            strike_limit=2,
        )
        summary = sup.run(max_episodes=3)
        targets_seen = [ep["target"] for ep in summary["episodes"]]

    # 前两次 shiny_gear 无进展后第 2 次打标冷却，第 3 次自动切换至下一个最高分主世界目标（make_a_sign_glow 30分）
    assert targets_seen[0] == "minecraft:story/shiny_gear"
    assert targets_seen[1] == "minecraft:story/shiny_gear"
    assert summary["episodes"][1]["cooled_down"] is True
    assert targets_seen[2] == "minecraft:husbandry/make_a_sign_glow"
    # compact profile 下每局工具数严格精简（<=22，不含全量 28 个工具）
    assert all(cnt <= len(COMPACT_CORE_TOOLS) + 5 for cnt in tool_counts_seen)


def test_curriculum_exempts_transient_429_from_target_strikes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mc_agent.harness.curriculum.time.sleep", lambda _: None)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(path)

    turn = 0

    def chat_ep(messages, tools=None, **kwargs):
        nonlocal turn
        turn += 1
        if turn <= 2:
            raise RuntimeError('Qwen API 返回 HTTP 429：{"error":{"message":"Harness group concurrency limit: 2 simultaneous requests"}}')
        return {"role": "assistant", "content": "暂无进展。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        sup = CurriculumSupervisor(
            bot,
            chat_fn=chat_ep,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            max_calls_per_episode=1,
            cooldown_s=600.0,
            strike_limit=2,
            api_error_backoff_s=8.0,
        )
        summary = sup.run(max_episodes=4)

    eps = summary["episodes"]
    assert len(eps) == 4
    assert eps[0]["target"] == "bootstrap_stone_age"
    assert eps[0]["transient_api_error"] is True
    assert eps[0]["cooled_down"] is False
    assert eps[1]["target"] == "bootstrap_stone_age"
    assert eps[1]["transient_api_error"] is True
    assert eps[1]["cooled_down"] is False
    # 非 429 的正常无进展两次才触发 2-strike 冷却
    assert eps[2]["target"] == "bootstrap_stone_age"
    assert eps[2]["transient_api_error"] is False
    assert eps[2]["cooled_down"] is False
    assert eps[3]["target"] == "bootstrap_stone_age"
    assert eps[3]["cooled_down"] is True


def test_kb_lookup_covers_all_125_advancements_and_scoring_metadata() -> None:
    catalog = _load_full_advancement_kb()
    assert len(catalog) == 125

    res_trident = lookup_kb("trident")
    assert res_trident["ok"] is True
    assert res_trident["count"] >= 2
    by_id = {item["id"]: item for item in res_trident["results"]}
    assert "drowned_trident" in by_id
    assert by_id["drowned_trident"]["kind"] == "mob"
    assert "minecraft:adventure/throw_trident" in by_id
    throw_entry = by_id["minecraft:adventure/throw_trident"]
    assert throw_entry["kind"] == "advancement"
    assert throw_entry["points"] == 60
    assert throw_entry["scored"] is True
    ids_in_order = [item["id"] for item in res_trident["results"]]
    if "minecraft:adventure/very_very_frightening" in ids_in_order:
        assert ids_in_order.index("minecraft:adventure/throw_trident") < ids_in_order.index(
            "minecraft:adventure/very_very_frightening"
        )

    res_zh = lookup_kb("抖包袱", category="advancement")
    assert res_zh["count"] >= 1
    assert res_zh["results"][0]["id"] == "minecraft:adventure/throw_trident"
    assert res_zh["results"][0]["points"] == 60

    res_glow = lookup_kb("make_a_sign_glow")
    assert res_glow["count"] >= 1
    top_glow = res_glow["results"][0]
    assert top_glow["id"] == "minecraft:husbandry/make_a_sign_glow"
    assert top_glow["points"] == 30
    assert top_glow["unconditional"] is True
    assert "minecraft:story/mine_stone" not in {r["id"] for r in res_glow["results"]}

    res_lode = lookup_kb("lodestone")
    assert res_lode["count"] >= 2
    lode_ids = {item["id"]: item for item in res_lode["results"]}
    assert "lodestone" in lode_ids
    assert lode_ids["lodestone"]["kind"] == "recipe"
    assert "chiseled_stone_bricks" in lode_ids["lodestone"]["ingredients"]
    assert "minecraft:adventure/use_lodestone" in lode_ids
    assert lode_ids["minecraft:adventure/use_lodestone"]["points"] == 15


def test_craft_gear_underground_path_not_found_recovery_and_unreachable_ore_hint() -> None:
    # 1) craft_gear 在地下遇到 path_not_found（试图寻路地表工作台）时就地掏墙龛放置工作台并重试合成 stone_pickaxe
    craft_state: dict[str, Any] = {
        "inv": [
            {"name": "cobblestone", "count": 64},
            {"name": "stick", "count": 4},
            {"name": "crafting_table", "count": 1},
            {"name": "wooden_pickaxe", "count": 1},
        ],
        "table_placed_at": None,
        "craft_attempts": 0,
    }

    def handle_craft_recovery(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 50.0, 0.5], "in_water": False, "alive": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(craft_state["inv"])}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            if pos == (1, 49, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
            if pos == (1, 50, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air" if craft_state["table_placed_at"] == [1, 50, 0] else "stone"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/dig":
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/place":
            if body.get("pos") is None or body.get("pos") != [1, 50, 0]:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_place", "message": "cramped"}})
            craft_state["table_placed_at"] = [1, 50, 0]
            craft_state["inv"] = [r for r in craft_state["inv"] if r["name"] != "crafting_table"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": "crafting_table", "pos": [1, 50, 0]}})
        if path == "/craft":
            craft_state["craft_attempts"] += 1
            if craft_state["craft_attempts"] == 1:
                return httpx.Response(
                    400,
                    json={"ok": False, "error": {"code": "path_not_found", "message": "No path to the goal!", "from": [0.5, 50, 0.5], "to": [3, 72, 0]}},
                )
            craft_state["inv"].append({"name": "stone_pickaxe", "count": 1})
            return httpx.Response(200, json={"ok": True, "result": {"crafted": "stone_pickaxe", "gained": 1}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_craft_recovery)) as bot_cg:
        cg_res = MacroSkills(bot_cg).craft_gear("stone_pickaxe", count=1)
    assert cg_res["ok"] is True
    assert cg_res["crafted"] == "stone_pickaxe"
    assert craft_state["craft_attempts"] == 2
    assert craft_state["table_placed_at"] == [1, 50, 0]

    # 2) mine_blocks 目标下方 out_of_reach 且掘进失败时附带 nearest_unreachable_target 与 approach_hint
    def handle_unreachable_ore(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 56.0, 0.5], "in_water": False, "alive": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": [0, 48, 4], "dist": 8.9}]}},
            )
        if path == "/get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            return httpx.Response(400, json={"ok": False, "error": {"code": "out_of_reach", "message": "too far"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_unreachable_ore)) as bot_mine:
        m_res = MacroSkills(bot_mine).mine_blocks(["iron_ore"], count=3, radius=16)
    assert m_res["ok"] is False
    assert m_res["nearest_unreachable_target"]["pos"] == [0, 48, 4]
    assert m_res["nearest_unreachable_target"]["bearing"] == "south"
    assert "mc_skill_staircase(direction='south', vertical='down'" in m_res["approach_hint"]


def test_quarantine_recovery_adjacent_fluid_guard_and_staircase_dir_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.bot.time.sleep", lambda _: None)

    # 1) action_quarantined 自动 disconnect -> wait not_connected -> connect -> ready 并重试；
    #    action_timeout 清理隔离重连后仍抛出 action_timeout（绝不重跑超时动作）
    q_state: dict[str, Any] = {
        "quarantined": True,
        "connected": True,
        "events": [],
        "dig_calls": 0,
    }

    def handle_quarantine(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        q_state["events"].append(path)
        if path == "/disconnect":
            q_state["connected"] = False
            q_state["quarantined"] = False
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        if path == "/status":
            if not q_state["connected"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 50.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True}})
        if path == "/connect":
            q_state["connected"] = True
            return httpx.Response(200, json={"ok": True, "result": {"ok": True, "reconnect": True}})
        if path == "/dig":
            q_state["dig_calls"] += 1
            if q_state["dig_calls"] == 1:
                return httpx.Response(200, json={"ok": False, "error": {"code": "action_quarantined", "message": "previous action timed out; disconnect and reconnect before resuming"}})
            if q_state["dig_calls"] == 2:
                return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
            if q_state["dig_calls"] == 3:
                q_state["quarantined"] = True
                return httpx.Response(200, json={"ok": False, "error": {"code": "action_timeout", "message": "action cancelled: action_timeout"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", base_url="http://100.93.211.71:18900/api/v1", transport=httpx.MockTransport(handle_quarantine)) as bot_q:
        res_ok = bot_q.call("dig", {"pos": [1, 50, 0]})
        assert res_ok == {"dug": "stone"}
        assert q_state["dig_calls"] == 2
        assert q_state["events"][:5] == ["/dig", "/disconnect", "/status", "/connect", "/status"]

        q_state["events"].clear()
        with pytest.raises(ApiError) as exc_info:
            bot_q.call("dig", {"pos": [1, 50, 1]})
        assert exc_info.value.code == "action_timeout"
        assert q_state["dig_calls"] == 3
        assert q_state["quarantined"] is False
        assert q_state["connected"] is True
        assert q_state["events"] == ["/dig", "/disconnect", "/status", "/connect", "/status"]

    # 2 & 3) dig_staircase 侧面藏水不破壁（dug==[]）+ 同格多方向失败记忆彻底消解跨方向死循环
    mine_state: dict[str, Any] = {"dug": []}

    def handle_stair_guard(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [-157.5, 50.0, -383.4], "health": 20, "food": 20, "ready": True, "alive": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/get_block":
            pos = tuple(int(v) for v in body["pos"])
            if pos in {(-158, 50, -384), (-158, 51, -384)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if pos in {(-160, 49, -384), (-158, 49, -382)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "water"}})
            if pos == (-157, 48, -384):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            mine_state["dug"].append(body.get("pos"))
            return httpx.Response(400, json={"ok": False, "error": {"code": "dig_blocked", "message": "cannot break", "pos": body.get("pos")}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_guard)) as bot_s:
        skills = MacroSkills(bot_s)
        # west 前方石墙侧面(-160,49,-384)藏水：未敲任何方块即提前拦停
        west_res = skills.dig_staircase(direction="west", steps=4, vertical="down")
        assert west_res["stop_reason"] == "fluid_or_bedrock"
        assert mine_state["dug"] == []
        assert west_res["blocker"]["block"] == "water"
        assert west_res["blocker"]["relation"] == "adjacent_fluid"
        assert west_res["blocker"]["pos"] == [-160, 49, -384]
        # north 探测正常石墙但 dig_blocked 失败：记入失败方向记忆
        north_res = skills.dig_staircase(direction="north", steps=4, vertical="down")
        assert north_res["stop_reason"] == "dig_failed"
        assert north_res["viable_directions"] == []
        # east 遇 cave_or_drop_ahead 时不再回指刚刚失败的 north，返回 all_directions_blocked + 地下隧道/上退指引
        east_res = skills.dig_staircase(direction="east", steps=4, vertical="down")
        assert east_res["stop_reason"] == "cave_or_drop_ahead"
        assert east_res["viable_directions"] == []
        assert east_res["all_directions_blocked"] is True
        assert east_res["direction_status"]["north"].startswith("failed_recently(")
        assert "water" in east_res["direction_status"]["south"]
        assert "water" in east_res["direction_status"]["west"]
        assert "mc_skill_tunnel" in east_res["hint"]
        assert "vertical='up'" in east_res["hint"]

    # 4) extract_failed_coords 对可换向阶梯标注方向，对 all_directions_blocked 标注整格
    extracted_dir = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "s1", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"south"}'}}]},
            {
                "role": "tool",
                "tool_call_id": "s1",
                "content": json.dumps(
                    {
                        "ok": False,
                        "direction": "south",
                        "viable_directions": ["north"],
                        "pos_after": [-157.5, 50.0, -383.4],
                    },
                    ensure_ascii=False,
                ),
            },
            {"role": "assistant", "tool_calls": [{"id": "s2", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"east"}'}}]},
            {"role": "tool", "tool_call_id": "s2", "content": json.dumps(east_res, ensure_ascii=False)},
        ]
    )
    assert "[-158,50,-384:south]" in extracted_dir
    assert "[-158,50,-384]" in extracted_dir


def test_death_loop_cooldown_recovery_guard_status_and_api_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.agent import run_mc_agent
    from mc_agent.harness.events import EventSink
    from mc_agent.harness.guardian import GenerationGuard

    clock = [500.0]
    monkeypatch.setattr("mc_agent.bot.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.bot.time.sleep", lambda dt: clock.__setitem__(0, clock[0] + float(dt)))
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    # 1) GenerationGuard.poll() degrades cleanly when post_status() hits not_connected after reflex
    guard_calls = {"status": 0}

    def guard_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            guard_calls["status"] += 1
            if guard_calls["status"] == 1:
                return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 10.0, "food": 20, "ready": True, "alive": True, "on_ground": True}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "kicked"}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 2, "events": [{"seq": 2, "type": "hurt", "health": 10.0, "source": "arrow", "source_entity": {"id": 99}}]}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "shield", "count": 1}]}})
        if path in ("/look", "/posture"):
            return httpx.Response(200, json={"ok": True, "result": {"blocking": True}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(guard_handler)) as bot_g:
        bot_g._last_event_seq = 1
        guard = GenerationGuard(bot_g, EventSink(lambda _: None), checkpoint=lambda: None)
        inc = guard.poll()
    assert inc is not None
    assert inc["status"]["ready"] is False
    assert "death_or_connection_changed" in inc["reasons"]
    assert inc["requires_replan"] is True

    # 2) run_mc_agent auto-waits death_loop_cooldown and recovers without aborting the run
    cd_state = {"kicked": True, "connects": 0}

    def cd_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if path == "/connect":
            cd_state["connects"] += 1
            if cd_state["connects"] == 1:
                return httpx.Response(200, json={"ok": False, "error": {"code": "death_loop_cooldown", "message": "cooldown", "cooldown_remaining_seconds": 3}})
            cd_state["kicked"] = False
            return httpx.Response(200, json={"ok": True, "result": {"connected": True}})
        if path == "/status":
            if cd_state["kicked"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True}})
        if path == "/inventory":
            if cd_state["kicked"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1}], "empty_slots": 35}})
        raise AssertionError(path)

    cd_turns = 0

    def cd_chat(messages, tools=None, **kwargs):
        nonlocal cd_turns
        cd_turns += 1
        if cd_turns == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "mc_inventory", "arguments": "{}"}}],
            }
        last_tool = next(m for m in reversed(messages) if m.get("role") == "tool")
        assert '"iron_pickaxe"' in last_tool["content"]
        return {"role": "assistant", "content": "冷却重连恢复，继续任务。"}

    cd_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(cd_handler)) as bot_cd:
        rep = run_mc_agent(
            "test cooldown",
            bot_cd,
            chat_fn=cd_chat,
            max_calls=4,
            survival_guard=False,
            include_initial_snapshot=False,
            on_event=cd_events.append,
        )
    assert rep["status"] == "answered"
    assert cd_state["connects"] == 2
    assert any(e["type"] == "bot.cooldown_wait" and e["reason"] == "death_loop_cooldown" for e in cd_events)

    # 3) Direct ApiError escaping run_mc_agent sets runtime_error in run.finished instead of leaving "running"
    err_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(cd_handler)) as bot_err:
        rep_err = run_mc_agent(
            "fail api",
            bot_err,
            chat_fn=lambda *a, **kw: (_ for _ in ()).throw(ApiError("not_connected", "boom")),
            max_calls=2,
            survival_guard=False,
            include_initial_snapshot=False,
            on_event=err_events.append,
        )
    assert rep_err["status"] == "runtime_error"
    finished_ev = [e for e in err_events if e["type"] == "run.finished"][-1]
    assert finished_ev["status"] == "runtime_error"


def test_curriculum_two_stone_picks_and_late_grind_husbandry_demoted() -> None:
    """bootstrap_iron_age ensures 2 stone pickaxes; rare 80m-island achievements stay at tier=3."""
    from mc_agent.harness.curriculum import CURRICULUM_PLAYBOOKS, select_next_target

    assert ("stone_pickaxe", 2) in CURRICULUM_PLAYBOOKS["bootstrap_iron_age"].pre_ensure

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0.5, 70.0, 0.5], "dimension": "minecraft:overworld", "time_of_day": 2000, "in_water": False, "alive": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1}]}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "advancements": [
                            {"id": "minecraft:story/root", "status": "completed"},
                            {"id": "minecraft:story/mine_stone", "status": "completed"},
                            {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                            {"id": "minecraft:story/smelt_iron", "status": "completed"},
                            {"id": "minecraft:story/iron_tools", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:husbandry/obtain_sniffer_egg", "status": "not_started"},
                            {"id": "minecraft:husbandry/allay_deliver_item_to_player", "status": "not_started"},
                            {"id": "minecraft:husbandry/ride_a_boat_with_a_goat", "status": "not_started"},
                            {"id": "minecraft:husbandry/breed_an_animal", "status": "not_started"},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_c:
        picked, _ = select_next_target(
            bot_c,
            cooldowns={"minecraft:story/shiny_gear": 999999.0},
            now=1.0,
            enable_island_recon=False,
        )
    assert picked is not None
    assert picked.key == "minecraft:husbandry/make_a_sign_glow"
    assert picked.key not in {
        "minecraft:husbandry/obtain_sniffer_egg",
        "minecraft:husbandry/allay_deliver_item_to_player",
        "minecraft:husbandry/ride_a_boat_with_a_goat",
    }

