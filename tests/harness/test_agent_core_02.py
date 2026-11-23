from tests.harness._common import *  # noqa: F401,F403

def test_long_horizon_anti_wedge_context_tunnel_blocker_and_repeat_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) _compact_tool_outcome 带尾随 [期间新事件] 仍精准保留 stop_reason/blocker/viable_directions
    raw_with_events = (
        json.dumps(
            {
                "ok": False,
                "stop_reason": "cave_or_drop_ahead",
                "blocker": {"pos": [-170, 64, -381], "block": "air", "relation": "missing_floor_support"},
                "viable_directions": ["north", "east"],
            },
            ensure_ascii=False,
        )
        + "\n[期间新事件] [受伤 health=17 by mob_attack]"
    )
    compact = _compact_tool_outcome(raw_with_events)
    assert "cave_or_drop_ahead" in compact
    assert "air@[-170,64,-381]" in compact
    assert "viable=north,east" in compact

    # 2) RequestContext(1600, mode="sliding") 不在 1000 字符处误截断 1360 字符工具输出，并折叠连续重复失败
    payload_1360 = json.dumps(
        {
            "ok": False,
            "stop_reason": "cave_or_drop_ahead",
            "blocker": {"pos": [-170, 64, -381], "block": "air"},
            "viable_directions": ["north"],
            "pad": "x" * 1200,
        },
        ensure_ascii=False,
    )
    assert 1250 < len(payload_1360) < 1550
    dup_args = json.dumps({"direction": "south", "steps": 10, "vertical": "down"})
    dup_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        dup_hist.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"d{i}", "type": "function", "function": {"name": "mc_skill_staircase", "arguments": dup_args}}],
            }
        )
        dup_hist.append({"role": "tool", "tool_call_id": f"d{i}", "content": payload_1360})

    ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4)
    built_dup = ctx.build(dup_hist, record=True)
    assert built_dup[2]["role"] == "user" and built_dup[2]["name"] == "ledger"
    assert "连续重复失败×3已折叠" in built_dup[2]["content"]
    assert "ERR:cave_or_drop_ahead(air@[-170,64,-381],viable=north)" in built_dup[2]["content"]
    assert sum(1 for m in built_dup if m.get("role") == "assistant") == 1
    assert ctx.requests[-1]["truncated_tool_results"] == []
    assert built_dup[-1]["content"] == payload_1360

    # 3) dig_level_tunnel 报错补齐 blocker/hint 且诊断字段排在 inventory_after 之前；dig_staircase 诊断排在 ores_nearby 之前
    class _TunnelMock:
        def __init__(self, overrides: dict[tuple[int, int, int], str], pick: str = "stone_pickaxe") -> None:
            self.overrides = overrides
            self.pick = pick

        def status(self) -> dict[str, Any]:
            return {"pos": [-157.5, 50.0, -383.5], "health": 20, "ready": True, "alive": True, "on_ground": True}

        def inventory(self) -> dict[str, Any]:
            return {"items": [{"name": self.pick, "count": 1}, {"name": "cobblestone", "count": 64}, {"name": "coal", "count": 8}]}

        def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            if action == "get_block":
                pt = tuple(int(v) for v in (body or {})["pos"])
                return {"name": self.overrides.get(pt, "stone")}
            if action in {"equip"}:
                return {}
            raise AssertionError(action)

    tun_unsup = MacroSkills(_TunnelMock({(-158, 50, -385): "oak_planks"})).dig_level_tunnel("north", 4)
    assert tun_unsup["stop_reason"] == "unsupported_face_block"
    assert tun_unsup["blocker"] == {"pos": [-158, 50, -385], "block": "oak_planks", "relation": "unsupported_face_block"}
    assert "oak_planks" in tun_unsup["hint"]
    keys_unsup = list(tun_unsup.keys())
    assert keys_unsup.index("blocker") < keys_unsup.index("inventory_after")
    assert keys_unsup.index("hint") < keys_unsup.index("inventory_after")

    tun_tier = MacroSkills(_TunnelMock({(-158, 50, -385): "diamond_ore"}, pick="stone_pickaxe")).dig_level_tunnel(
        "north", 4, stop_on_ore=False
    )
    assert tun_tier["stop_reason"] == "insufficient_pickaxe_tier"
    assert tun_tier["blocker"]["block"] == "diamond_ore"
    assert tun_tier["blocker"]["required_tier"] == 3

    def handle_stair_order(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 66.0, -381.5], "health": 20, "ready": True, "alive": True, "on_ground": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "copper_ore", "pos": [-164, 65, -383], "dist": 5.9}]}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in {(-170, 66, -382), (-170, 67, -382), (-170, 68, -382), (-170, 64, -381)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_order)) as bot_order:
        stair_res = MacroSkills(bot_order).dig_staircase(direction="south", steps=6, vertical="down")
    stair_keys = list(stair_res.keys())
    assert stair_keys.index("blocker") < stair_keys.index("ores_nearby")
    assert stair_keys.index("viable_directions") < stair_keys.index("ores_nearby")
    assert stair_keys.index("hint") < stair_keys.index("ores_nearby")

    # 4) mc_attack 零命中暴露失败 + GenerationGuard 反射后排空新死亡事件
    def handle_zero_hit(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_sword", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": "stone_sword"}})
        if path == "/attack":
            return httpx.Response(200, json={"ok": True, "result": {"killed": False, "hits": 0, "ms": 5004}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_zero_hit)) as bot_atk:
        atk_out = json.loads(McToolset(bot_atk).mc_attack(entity_id=50790))
    assert atk_out["ok"] is False
    assert atk_out["error"]["code"] == "attack_zero_hits"

    guard_clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: guard_clock[0])
    g_state = {"fled": False, "events_calls": 0}

    def handle_guard_drain(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [-169.5, 73.0, -388.5] if g_state["fled"] else [-155.4, 45.5, -383.7],
                        "health": 20 if g_state["fled"] else 14,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": True,
                        "nearest_hostile": None if g_state["fled"] else {"id": 9, "name": "zombie", "dist": 1.8, "pos": [-155.0, 45.5, -382.0]},
                    },
                },
            )
        if action == "events":
            g_state["events_calls"] += 1
            since = int(req.url.params.get("since", 0))
            if g_state["events_calls"] == 1:
                rows = [{"seq": 10, "type": "hurt", "health": 14, "source": "mob_attack", "source_entity": {"id": 9, "name": "zombie", "dist": 1.8}}]
            elif g_state["fled"] and since < 12:
                rows = [{"seq": 11, "type": "death", "pos": [-155.4, 45.5, -383.7]}, {"seq": 12, "type": "spawn", "pos": [-169.5, 73.0, -388.5]}]
            else:
                rows = []
            max_seq = max([since] + [r["seq"] for r in rows])
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": max_seq}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if action == "get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": [] if g_state["fled"] else [{"id": 9, "name": "zombie", "dist": 1.8, "pos": [-155.0, 45.5, -382.0]}]}})
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action in {"control", "move_to"}:
            g_state["fled"] = True
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 73.0, -388.5]}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_guard_drain)) as bot_g:
        guard = GenerationGuard(bot_g, EventSink(lambda _: None), checkpoint=lambda: None)
        inc1 = guard.poll()
        guard_clock[0] += 5.0
        inc2 = guard.poll()
    assert inc1 is not None and "death_or_connection_changed" in inc1["reasons"]
    assert guard.cursor == 12
    assert inc2 is None  # 死亡与重生事件已被当场排空，不会重复打断下一轮生成

    # 5) run_mc_agent Harness 零进展重复失败熔断器：同一坐标第 2 次完全相同调用直接短路拦截
    api_calls: list[str] = []
    equip_calls = 0

    def handle_circuit(req: httpx.Request) -> httpx.Response:
        nonlocal equip_calls
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 66.0, -381.5], "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in {(-170, 66, -382), (-170, 67, -382), (-170, 68, -382), (-170, 64, -381)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/equip":
            equip_calls += 1
            api_calls.append(path)
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path in {"/dig", "/move_to"}:
            api_calls.append(path)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    llm_turn = 0
    seen_blocked_payload = False

    def chat_repeat(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal llm_turn, seen_blocked_payload
        llm_turn += 1
        if llm_turn in (1, 2):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"rc{llm_turn}",
                        "type": "function",
                        "function": {
                            "name": "mc_skill_staircase",
                            "arguments": json.dumps({"direction": "south", "steps": 6, "vertical": "down"}),
                        },
                    }
                ],
            }
        assert llm_turn == 3
        seen_blocked_payload = any(
            m.get("role") == "tool" and "repeated_identical_failure_blocked" in str(m.get("content", ""))
            for m in messages
        )
        assert any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages)
        return {"role": "assistant", "content": "已收到防卡死拦截并准备换向。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_circuit)) as bot_c:
        rep_c = run_mc_agent(
            "防卡死测试",
            bot_c,
            chat_fn=chat_repeat,
            max_calls=5,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_c["status"] == "answered"
    assert seen_blocked_payload is True
    # 第 1 次 south 调用执行了 1 次 /equip，第 2 次在 Harness 层直接短路所以 /equip 仍仅调用 1 次
    assert equip_calls == 1


def test_anti_ping_pong_cul_de_sac_probe_and_multi_step_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) 实机双格水封死胡同 [-172,50,-384] <-> [-172,50,-385]：
    #    北面有 1 格既有空气 (-172,50,-385)，其后 (-172,50,-387) 为水；仅南面 (-172,50,-383) 可向上阶梯退回
    pocket_blocks: dict[tuple[int, int, int], str] = {
        (-172, 50, -384): "air",
        (-172, 51, -384): "air",
        (-172, 52, -384): "air",
        (-172, 50, -385): "air",
        (-172, 51, -385): "air",
        # north 阻断（y=53 不妨碍平推进 -385，仅阻断北向上探）
        (-172, 48, -385): "water",
        (-172, 50, -387): "water",
        (-172, 53, -385): "water",
        # east 阻断
        (-171, 49, -384): "water",
        (-171, 49, -385): "water",
        (-171, 52, -384): "water",
        # west 阻断
        (-173, 49, -384): "water",
        (-173, 49, -385): "water",
        (-174, 50, -384): "water",
        (-173, 52, -384): "water",
        # south 仅地板下方水阻塞下探与平推，但 y=50..53 为稳固石头允许 vertical='up'
        (-172, 49, -383): "water",
    }
    pocket_state = {"pos": [-171.5, 50.0, -383.5], "moves": 0}

    def handle_cul_de_sac(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(pocket_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 32}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": pocket_blocks.get(pt, "stone")}})
        if path == "/move_to":
            pocket_state["moves"] += 1
            target = [float(body["x"]), float(body["y"]), float(body["z"])]
            pocket_state["pos"] = target
            return httpx.Response(200, json={"ok": True, "result": {"pos": target}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_cul_de_sac)) as bot_p:
        skills_p = MacroSkills(bot_p)
        stair_p = skills_p.dig_staircase(direction="north", steps=6, vertical="down")
        assert stair_p["stop_reason"] == "fluid_or_bedrock"
        assert stair_p["viable_directions"] == []
        # 北侧虽有 1 格既有空气 (-172,50,-385)，但 2 步隧道预检看穿后面的 water@[-172,50,-387]，不再假阳性指向 north
        assert stair_p["viable_tunnel_directions"] == []
        assert stair_p["viable_up_directions"] == ["south"]
        assert "mc_skill_staircase(direction='south', vertical='up'" in stair_p["hint"]

        # 若直接从 -384 朝 north 平推：空走 1 格空气到达 -385 后被水阻断（dug==[]），起终点双坐标均记入 failed_tunnel_dirs
        tun_p = skills_p.dig_level_tunnel("north", 6)
        assert tun_p["ok"] is False
        assert tun_p["steps_done"] == 1
        assert tun_p["dug"] == []
        assert tun_p["stop_reason"] == "fluid_adjacent"
        assert "north" in skills_p.failed_tunnel_dirs[(-172, 50, -385)]
        assert "north" in skills_p.failed_tunnel_dirs[(-172, 50, -384)]
        assert tun_p["viable_tunnel_directions"] == []
        tkeys = list(tun_p.keys())
        assert tkeys.index("viable_tunnel_directions") < tkeys.index("inventory_after")
        assert tkeys.index("hint") < tkeys.index("inventory_after")

    # 2) 4 步跨坐标乒乓熔断：-384 tunnel(north,steps=6) -> -385 stair(south) -> -385 tunnel(south,steps=4) -> -384 stair(north)
    #    第 5 轮回到 -384 再发 tunnel(north,steps=8)（刻意改变 steps），仍被 _RepeatFailureGuard 当场短路拦截
    ping_state = {"pos": [-171.5, 50.0, -383.5], "moves": 0}
    ping_blocks = dict(pocket_blocks)
    # 为 [-172,50,-385] 周围也补齐地板水体边界使 4 步均能走入旧空气后零掘进失败
    ping_blocks[(-171, 49, -385)] = "water"
    ping_blocks[(-173, 49, -385)] = "water"
    ping_blocks[(-172, 48, -384)] = "water"

    def handle_ping_pong(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(ping_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": ping_blocks.get(pt, "stone")}})
        if path == "/move_to":
            ping_state["moves"] += 1
            target = [float(body["x"]), float(body["y"]), float(body["z"])]
            ping_state["pos"] = target
            return httpx.Response(200, json={"ok": True, "result": {"pos": target}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    cycle_calls = [
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}),
        ("mc_skill_tunnel", {"direction": "south", "steps": 4}),
        ("mc_skill_staircase", {"direction": "north", "steps": 6, "vertical": "down"}),
        ("mc_skill_tunnel", {"direction": "north", "steps": 8}),
    ]
    cycle_turn = 0
    blocked_err_obj: dict[str, Any] = {}
    saw_guard_notice = False

    def chat_ping_pong(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal cycle_turn, blocked_err_obj, saw_guard_notice
        cycle_turn += 1
        if cycle_turn <= len(cycle_calls):
            tname, targs = cycle_calls[cycle_turn - 1]
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"pp{cycle_turn}",
                        "type": "function",
                        "function": {"name": tname, "arguments": json.dumps(targs)},
                    }
                ],
            }
        assert cycle_turn == len(cycle_calls) + 1
        saw_guard_notice = any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages)
        for m in messages:
            if m.get("role") == "tool" and "repeated_identical_failure_blocked" in str(m.get("content", "")):
                parsed_blk = json.loads(str(m["content"]).splitlines()[0])
                blocked_err_obj = parsed_blk.get("error") or {}
        return {"role": "assistant", "content": "已识破四步乒乓死循环并准备改走向上退路。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_ping_pong)) as bot_pp:
        rep_pp = run_mc_agent(
            "四步乒乓熔断测试",
            bot_pp,
            chat_fn=chat_ping_pong,
            max_calls=8,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_pp["status"] == "answered"
    assert saw_guard_notice is True
    assert blocked_err_obj.get("code") == "repeated_identical_failure_blocked"
    assert len(blocked_err_obj.get("nearby_failed_actions") or []) >= 2
    # Turn 1 与 Turn 3 各移动 1 次，Turn 5 在 Harness 层被直接拦截未触发任何 move_to
    assert ping_state["moves"] == 2

    # 3) 滑动上下文对周期性 A -> B -> A -> B 无成功间隔失败段去重并折叠进账本
    cyc_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    cycle_pattern = [
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}, [-171.5, 50.0, -383.5], "fluid_adjacent"),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}, [-171.5, 50.0, -384.5], "fluid_or_bedrock"),
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}, [-171.5, 50.0, -383.5], "fluid_adjacent"),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}, [-171.5, 50.0, -384.5], "fluid_or_bedrock"),
    ]
    for idx, (cname, cargs, cpos, creason) in enumerate(cycle_pattern, start=1):
        cyc_hist.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"cyc{idx}", "type": "function", "function": {"name": cname, "arguments": json.dumps(cargs)}}],
            }
        )
        cyc_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"cyc{idx}",
                "content": json.dumps(
                    {"ok": False, "stop_reason": creason, "pos_before": cpos, "viable_tunnel_directions": [], "viable_up_directions": ["south"]},
                    ensure_ascii=False,
                ),
            }
        )

    cyc_ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4)
    built_cyc = cyc_ctx.build(cyc_hist, record=True)
    assert built_cyc[2]["role"] == "user" and built_cyc[2]["name"] == "ledger"
    assert "连续重复失败×1已折叠" in built_cyc[2]["content"]
    assert "up=south" in built_cyc[2]["content"]
    assert sum(1 for m in built_cyc if m.get("role") == "assistant") == 2


def test_underground_wood_shortage_and_coordinate_drift_stagnation_masking() -> None:
    from mc_agent.harness.context import RequestContext
    from mc_agent.harness.hud import compute_goal_gap_hint
    from mc_agent.skills.library import MacroSkills

    # 1) 地下 y=29 且圆石 533、原木/木板/木棍为 0 时，缺口提示不得再误报“还差圆石0个”
    gap_under = compute_goal_gap_hint(
        {"wooden_pickaxe": 1, "cobblestone": 533},
        task="继续推进生存与地下采矿，收集铁矿石并熔炼合成装备 iron_pickaxe。",
        bot_y=29,
    )
    assert "还差圆石0个" not in gap_under
    assert "地下(y=29)无树" in gap_under
    assert "mc_skill_staircase" in gap_under

    # 2) harvest_logs 在地下 y=29 返回 no_targets_available 时明确指出地下无树并给出 viable_up_directions
    state: dict[str, Any] = {
        "pos": [-172.3, 29.0, -334.3],
        "dig_round": 0,
    }

    def handle_world(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20, "food": 20, "ready": True, "in_water": False}},
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "held": {"name": "wooden_pickaxe", "count": 1, "durability_used": 39},
                        "items": [
                            {"name": "wooden_pickaxe", "count": 1, "durability_used": 39},
                            {"name": "cobblestone", "count": 533},
                        ],
                    },
                },
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if path == "/get_block":
            bpos = body.get("pos") or [0, 0, 0]
            bx, by, bz = int(bpos[0]), int(bpos[1]), int(bpos[2])
            if (bx, bz) == (math.floor(state["pos"][0]), math.floor(state["pos"][2])) and by in (29, 30):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            state["dig_round"] += 1
            if state["dig_round"] <= 3:
                jitter_pos = (
                    [-172.4, 29.0, -335.4]
                    if state["dig_round"] < 3
                    else [-172.3, 29.0, -334.5]
                )
                state["pos"] = list(jitter_pos)
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error": {
                            "code": "out_of_reach",
                            "message": f"cannot reach stone at {body.get('pos')}",
                            "pos": jitter_pos,
                        },
                    },
                )
            # staircase 上行时逐块掘进成功
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/move_to":
            state["pos"] = [float(body.get("x", state["pos"][0])), float(body.get("y", state["pos"][1])), float(body.get("z", state["pos"][2]))]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        raise AssertionError(f"unexpected path: {path}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_world)) as bot_logs:
        h_res = MacroSkills(bot_logs).harvest_logs(target_logs=4, radius=10)
    assert h_res["ok"] is False
    assert h_res.get("underground_no_trees") is True
    assert h_res.get("viable_up_directions")
    assert "mc_skill_staircase" in str(h_res.get("message"))
    assert "严禁" in str(h_res.get("message"))

    # 3) 连续 3 轮沿不同坐标递推调用 mc_dig(out_of_reach) 且伴随亚格微漂移，第 4 轮必须硬屏蔽 mc_dig 并折叠历史
    state["pos"] = [-172.4, 29.0, -335.4]
    state["dig_round"] = 0
    seen_tools: list[set[str]] = []

    def chat_drift(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        req_no = len(seen_tools) + 1
        tool_names = {
            str(((t.get("function") or {}).get("name") or ""))
            for t in (tools or [])
            if isinstance(t, dict)
        }
        seen_tools.append(tool_names)
        if req_no <= 3:
            assert "mc_dig" in tool_names
            tx = -176 - req_no
            tz = -339 - req_no
            return {
                "role": "assistant",
                "content": f"意图：向西南盲挖寻找原木 {req_no}",
                "tool_calls": [
                    {
                        "id": f"drift_{req_no}",
                        "type": "function",
                        "function": {"name": "mc_dig", "arguments": json.dumps({"x": tx, "y": 29, "z": tz, "collect": True})},
                    }
                ],
            }
        if req_no == 4:
            assert "mc_dig" not in tool_names
            assert "mc_skill_staircase" in tool_names
            assert sum(1 for m in messages if m.get("role") == "assistant") == 1
            assert any(m.get("name") == "ledger" and "连续重复失败×2已折叠" in str(m.get("content", "")) for m in messages)
            assert any("严禁向更远坐标连续递推调用 mc_dig" in str(m.get("content", "")) for m in messages if m.get("role") == "tool")
            assert any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages if m.get("role") == "tool")
            return {
                "role": "assistant",
                "content": "意图：改用阶梯上行回地表。",
                "tool_calls": [
                    {
                        "id": "escape_up",
                        "type": "function",
                        "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"north","vertical":"up","steps":3}'},
                    }
                ],
            }
        assert req_no == 5
        assert "mc_dig" in tool_names
        return {"role": "assistant", "content": "已止损递推盲挖并升层脱困。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_world)) as bot_drift:
        rep_drift = run_mc_agent(
            "收集铁矿石并合成 iron_pickaxe",
            bot_drift,
            chat_fn=chat_drift,
            max_calls=6,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_drift["status"] == "answered"
    assert len(seen_tools) == 5

    # 4) 验证 path_not_found 坐标序列漂移同样被 sliding 上下文完全折叠
    pnf_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        pnf_hist.append(
            {
                "role": "assistant",
                "content": "继续西南挖",
                "tool_calls": [
                    {
                        "id": f"pnf_{i}",
                        "type": "function",
                        "function": {"name": "mc_dig", "arguments": json.dumps({"x": -193 - i, "y": 29, "z": -356 - i, "collect": True})},
                    }
                ],
            }
        )
        pnf_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"pnf_{i}",
                "content": json.dumps(
                    {
                        "ok": False,
                        "error": {"code": "path_not_found", "from": [-172.3 - 0.1 * i, 29.0, -334.5]},
                    }
                ),
            }
        )
    built_pnf = RequestContext(3200, mode="sliding", keep_recent_turns=6).build(pnf_hist, record=True)
    assert sum(1 for m in built_pnf if m.get("role") == "assistant") == 1
    assert any(m.get("name") == "ledger" and "连续重复失败×3已折叠" in str(m.get("content", "")) for m in built_pnf)

