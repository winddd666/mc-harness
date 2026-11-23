from tests.harness._common import *  # noqa: F401,F403

def test_bug058_path_not_found_suggests_dig_toward_and_escalates_repeats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}},
            )
        if path == "/move_to":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "path_not_found", "message": "No path"}},
            )
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))

        first = json.loads(ts.mc_move_to(x=105.0, y=64.0, z=200.0, range=1.0))
        assert first["ok"] is False
        assert first["suggested"]["mode"] == "dig_toward"
        assert first["path_not_found_count"] == 1
        assert first.get("requires_replan") is not True

        # Second failure within 8m escalates to requires_replan=True
        second = json.loads(ts.mc_move_to(x=106.0, y=64.0, z=201.0, range=1.0))
        assert second["ok"] is False
        assert second["path_not_found_count"] == 2
        assert second["requires_replan"] is True

        # Rebinding episode clears the counter
        ts.rebind_runtime_hooks()
        third = json.loads(ts.mc_move_to(x=120.0, y=64.0, z=200.0, range=1.0))
        assert third["path_not_found_count"] == 1
        assert third.get("requires_replan") is not True


def test_world_hud_renders_compact_coords_durability_and_gaps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [-3.4, 72.0, -5.8],
                        "dimension": "overworld",
                        "biome": "birch_forest",
                        "is_day": True,
                        "health": 18.0,
                        "food": 17,
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
                            {"name": "stone_pickaxe", "count": 1, "durability_used": 31},
                            {"name": "iron_ingot", "count": 2},
                            {"name": "raw_iron", "count": 4},
                            {"name": "oak_log", "count": 3},
                        ],
                        "offhand": {"name": "shield", "count": 1},
                        "empty_slots": 31,
                    },
                },
            )
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "iron_ore", "pos": [-1, 70, -4], "dist": 3.2},
                            {"name": "crafting_table", "pos": [-3, 72, -5], "dist": 1.1},
                            {"name": "oak_log", "pos": [1, 72, -6], "dist": 4.4},
                        ]
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(center_x=0, center_z=0, radius=100, margin=5))
        hud = build_world_hud(
            ts,
            task="制作铁镐",
            target_advancement="minecraft:story/shiny_gear",
            failed_coords=["[-10,70,-10]"],
        )
    assert len(hud) < 650
    assert "[-4,72,-6]" in hud
    assert "剩100" in hud
    assert "iron_ore" in hud
    assert "diamond_boots" in hud
    assert "[-10,70,-10]" in hud


def test_sliding_context_compresses_old_turns_and_ephemeral_hud_never_mutates_history() -> None:
    history: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for idx in range(1, 10):
        history.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{idx}", "type": "function", "function": {"name": "mc_dig", "arguments": json.dumps({"x": idx, "y": 64, "z": 0})}}
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": f"c{idx}",
                "content": json.dumps({"ok": idx != 2, "error": {"code": "path_not_found"}} if idx == 2 else {"ok": True, "gained": {"stone": 1}}),
            }
        )

    before_len = len(history)
    before_last = history[-1]["content"]
    ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4, max_ledger_entries=16)
    built = ctx.build(history, ephemeral_hud="[实时HUD]\n位置=[9,64,0]")

    assert len(history) == before_len
    assert history[-1]["content"] == before_last
    assert "[已完成动作账本" in built[2]["content"]
    assert "#1 mc_dig" in built[2]["content"]
    assert "ERR:path_not_found" in built[2]["content"]
    assert built[-1]["content"].endswith("[实时HUD]\n位置=[9,64,0]")
    assert len(built) == 2 + 1 + 4 * 2


def test_canopy_descend_and_occluded_ore_tunnel() -> None:
    blocks: dict[tuple[int, int, int], str] = {
        (0, 79, 0): "oak_leaves",
        (0, 78, 0): "air",
        (0, 77, 0): "oak_leaves",
        (0, 76, 0): "grass_block",
    }
    pos_state = [0.5, 80.0, 0.5]

    def canopy_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content.decode()) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(pos_state), "in_water": False}})
        if path == "/get_block":
            p = tuple(body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": blocks.get(p, "air"), "diggable": blocks.get(p, "air") != "air"}})
        if path == "/dig":
            p = tuple(body["pos"])
            blocks[p] = "air"
            if p == (0, 79, 0):
                pos_state[1] = 78.0
            elif p == (0, 77, 0):
                pos_state[1] = 77.0
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_leaves"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(canopy_handler)) as bot:
        res = MacroSkills(bot).unstick()
        assert res["ok"] is True
        assert res["descended_canopy"] is True
        assert pos_state[1] == 77.0

    # 隔墙矿石：第一次 find_blocks 发现同层 3m 外有 iron_ore 但 dig 报 path_not_found；
    # mine_blocks 自动调 tunnel 后重试 dig 成功！
    wall_dug = False
    ore_gained = False
    ore_pos = [3, 64, 0]
    bot_pos = [0.5, 64.0, 0.5]

    def ore_handler(req: httpx.Request) -> httpx.Response:
        nonlocal wall_dug, ore_gained, bot_pos
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content.decode()) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(bot_pos), "health": 20, "food": 20, "on_ground": True, "ready": True, "alive": True}})
        if path == "/inventory":
            items = [{"name": "stone_pickaxe", "count": 1}]
            if ore_gained:
                items.append({"name": "raw_iron", "count": 1})
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": ore_pos, "dist": 2.8}]}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            if pos == tuple(ore_pos):
                return httpx.Response(200, json={"ok": True, "result": {"name": "iron_ore", "diggable": True}})
            if pos[1] == 63:
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if wall_dug else "stone", "diggable": True}})
        if path == "/dig":
            if body["pos"] == ore_pos and not wall_dug:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "wall"}})
            if body["pos"] == ore_pos:
                ore_gained = True
                return httpx.Response(200, json={"ok": True, "result": {"dug": "iron_ore", "gained": {"raw_iron": 1}}})
            wall_dug = True
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {}}})
        if path == "/move_to":
            wall_dug = True
            bot_pos = [float(body.get("x", 2.5)), 64.0, float(body.get("z", 0.5))]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(bot_pos)}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(ore_handler)) as bot:
        mres = MacroSkills(bot).mine_blocks(["iron_ore"], count=1, radius=16)
        assert mres["ok"] is True
        assert mres["gained"].get("raw_iron", 0) >= 1
        assert "occluded_tunnel" in mres


def test_cave_shaft_structure_occluded_calmness_and_shaft_seal() -> None:
    # 1) Carved shaft + occluded skeleton: HUD shows shaft & 隔墙怪, mc_skill_flee holds inside shaft
    shaft_moves: list[tuple[str, dict[str, Any]]] = []

    def shaft_block(bx: int, by: int, bz: int) -> str:
        # 1x2 pocket at (0, 57..58, 0), roofed at (0, 59..60, 0), east/west/north walls solid stone, south staircase up
        if bx == 0 and bz == 0 and by in (57, 58):
            return "air"
        if bx == 0 and bz == 1 and by in (58, 59):
            return "air"
        return "stone"

    def handle_shaft_occluded(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 57.0, 0.5],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 51, "name": "skeleton", "dist": 6.3, "pos": [6.5, 59.0, 0.5]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 16}]}},
            )
        if action == "find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"blocks": [{"name": "stone", "pos": [1, 57, 0], "dist": 1.0}]}},
            )
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [
                            {"id": 51, "name": "skeleton", "type": "hostile", "dist": 6.3, "pos": [6.5, 59.0, 0.5]}
                        ]
                    },
                },
            )
        if action == "get_block":
            pos = body.get("pos") or [0, 0, 0]
            return httpx.Response(200, json={"ok": True, "result": {"name": shaft_block(int(pos[0]), int(pos[1]), int(pos[2]))}})
        if action in {"control", "move_to", "attack"}:
            shaft_moves.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(f"unexpected action in shaft occluded test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_shaft_occluded)) as bot1:
        ts1 = McToolset(bot1)
        hud1 = build_world_hud(ts1)
        flee1 = json.loads(ts1.mc_skill_flee(distance=20))
    assert "地形=探矿穴(3面岩壁,开:south上行)" in hud1
    assert "隔墙怪=skeleton@6.3m" in hud1
    assert "stone阻隔,勿弃坑" in hud1
    assert "近敌(无遮挡!)" not in hud1
    assert flee1["ok"] is True
    assert flee1["mode"] == "hold_in_shaft"
    assert flee1["moved_dist"] == 0.0
    assert shaft_moves == []

    # 2) Carved shaft + unoccluded intruding creeper: seals north corridor with cobblestone & stays at Y=57
    seal_blocks: dict[tuple[int, int, int], str] = {}
    seal_calls: list[tuple[str, dict[str, Any]]] = []

    def seal_block_at(bx: int, by: int, bz: int) -> str:
        if (bx, by, bz) in seal_blocks:
            return seal_blocks[(bx, by, bz)]
        if bx == 0 and bz == 0 and by in (57, 58):
            return "air"
        if bx == 0 and bz <= -1 and by in (57, 58):
            return "air"
        if bx == 0 and bz == 1 and by in (58, 59):
            return "air"
        return "stone"

    def handle_shaft_seal(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 57.0, 0.5],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 91, "name": "creeper", "dist": 4.0, "pos": [0.5, 57.0, -3.5]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 16}]}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [{"id": 91, "name": "creeper", "type": "hostile", "dist": 4.0, "pos": [0.5, 57.0, -3.5]}]
                    },
                },
            )
        if action == "get_block":
            pos = body.get("pos") or [0, 0, 0]
            return httpx.Response(200, json={"ok": True, "result": {"name": seal_block_at(int(pos[0]), int(pos[1]), int(pos[2]))}})
        if action == "place":
            seal_calls.append((action, body))
            p = body["pos"]
            seal_blocks[(int(p[0]), int(p[1]), int(p[2]))] = str(body["item"])
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"]}})
        if action == "attack":
            raise AssertionError("must not attack creeper")
        raise AssertionError(f"unexpected action in shaft seal test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_shaft_seal)) as bot2:
        seal_res = MacroSkills(bot2).defend_or_flee(
            attacker={"id": 91, "name": "creeper", "dist": 4.0, "pos": [0.5, 57.0, -3.5]},
            urgent=True,
        )
    assert seal_res["ok"] is True
    assert seal_res["mode"] == "seal_shaft"
    assert seal_res["sealed_direction"] == "north"
    assert seal_res["placed_positions"] == [[0, 57, -1], [0, 58, -1]]
    assert seal_res["pos_after"][1] == 57.0
    assert seal_blocks[(0, 57, -1)] == "cobblestone"
    assert seal_blocks[(0, 58, -1)] == "cobblestone"

    # 3) dig_staircase cave_or_drop_ahead reports viable_directions
    def handle_stair_drop(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "get_block":
            pos = body.get("pos") or [0, 64, 0]
            bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
            if (bx, by, bz) == (0, 65, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if (bx, by, bz) == (0, 62, -1):
                # Drop ahead to the north
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if bx == -1 and by == 62 and bz == 0:
                # Lava pit to the west (unsafe)
                return httpx.Response(200, json={"ok": True, "result": {"name": "lava"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        raise AssertionError(f"unexpected action in staircase drop test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_drop)) as bot3:
        stair_res = MacroSkills(bot3).dig_staircase(direction="north", steps=4, vertical="down")
    assert stair_res["stop_reason"] == "cave_or_drop_ahead"
    assert set(stair_res["viable_directions"]) == {"south", "east"}
    assert "实测可换向安全下探" in stair_res["hint"]


def test_leaves_not_wall_all_dirs_blocked_iron_gate_and_no_food_guard() -> None:
    # 1a) birch_leaves 绝不当作实心阻隔墙体
    def handle_leaves(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "get_block":
            pos = tuple(body["pos"])
            if pos[0] == -2:
                return httpx.Response(200, json={"ok": True, "result": {"name": "birch_leaves"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_leaves)) as bot_l:
        assert ray_wall_blocker(bot_l, (0.5, 70.0, 0.5), (-5.5, 70.0, 0.5)) is None

    # 1b) 露天石头暂挡输出“遮挡怪=...露天注意绕行”而非“勿弃坑”，且低血 0 食物输出禁空吃提醒
    def handle_surface_hud(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 70.0, 0.5],
                        "health": 2.0,
                        "food": 17,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 88, "name": "skeleton", "dist": 6.0},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 12}]}},
            )
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"entities": [{"id": 88, "name": "skeleton", "type": "hostile", "pos": [-5.5, 70.0, 0.5]}]},
                },
            )
        if action == "get_block":
            bx, by, bz = (int(v) for v in body["pos"])
            if (bx, by, bz) == (-2, 71, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_surface_hud)) as bot_h:
        hud_text = build_world_hud(McToolset(bot_h))
    assert "遮挡怪=skeleton@" in hud_text
    assert "露天注意绕行" in hud_text
    assert "勿弃坑" not in hud_text
    assert "包内无食物(禁调mc_use_item空吃)" in hud_text

    # 2) 四面悬空时 dig_staircase 返回 viable_directions=[] + all_directions_blocked=True，且 extract_failed_coords 收录浮点坐标
    def handle_all_drop(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [-155.7, 75.0, -381.5], "health": 20, "food": 20, "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "get_block":
            pos = body.get("pos") or [0, 75, 0]
            bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
            if (bx, by, bz) == (-156, 74, -382):
                return httpx.Response(200, json={"ok": True, "result": {"name": "grass_block"}})
            if (bx, by, bz) == (-156, 76, -382):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if by == 73:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "birch_leaves"}})
        raise AssertionError(f"unexpected action in all-drop test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_all_drop)) as bot_d:
        res_drop = MacroSkills(bot_d).dig_staircase(direction="west", steps=6, vertical="down")
    assert res_drop["stop_reason"] == "cave_or_drop_ahead"
    assert res_drop["viable_directions"] == []
    assert res_drop["all_directions_blocked"] is True
    assert "严禁在当前位置换方向继续调 mc_skill_staircase" in res_drop["hint"]

    extracted = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"west"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps(res_drop, ensure_ascii=False)},
        ]
    )
    assert extracted == ["[-156,75,-382]"]

    # 3) 流断连报错被 _is_transient_llm_error 豁免，且 0 铁时即使 bootstrap 冷却也绝不派发 crafters_crafting_crafters / ol_betsy
    assert _is_transient_llm_error({"status": "runtime_error", "error": "模型请求线程意外退出，未取得完整响应。"}) is True

    def handle_zero_iron(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "stick", "count": 4}]}},
            )
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
                            {"id": "minecraft:adventure/root", "status": "completed"},
                            {"id": "minecraft:adventure/kill_a_mob", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:adventure/crafters_crafting_crafters", "status": "unlocked"},
                            {"id": "minecraft:adventure/ol_betsy", "status": "unlocked"},
                            {"id": "minecraft:story/iron_tools", "status": "unlocked"},
                            {"id": "minecraft:adventure/sleep_in_bed", "status": "unlocked"},
                        ]
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_zero_iron)) as bot_c:
        picked, _ = select_next_target(bot_c, cooldowns={"bootstrap_iron_age": 999999.0}, now=1.0)
    assert picked.key not in {"minecraft:adventure/crafters_crafting_crafters", "minecraft:adventure/ol_betsy"}

    # 4) mc_use_item("golden_apple") 在 0 食物背包报错时禁止继续空吃且不提示金苹果配方
    def handle_no_food(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/use_item":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {
                        "code": "missing_item",
                        "message": "no golden_apple in inventory",
                        "inventory": {"cobblestone": 190, "birch_log": 3, "stone_pickaxe": 1},
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_no_food)) as bot_f:
        raw_err = McToolset(bot_f).mc_use_item("golden_apple")
    parsed_err = json.loads(raw_err)["error"]
    assert parsed_err["no_food_in_inventory"] is True
    assert "严禁继续调用 mc_use_item" in parsed_err["kb_hint"]
    assert "gold_ingot" not in parsed_err["kb_hint"]

