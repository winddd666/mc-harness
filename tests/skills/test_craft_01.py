from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

def test_unstick_uses_floor_on_negative_coords_and_clears_hitbox() -> None:
    solid_blocks = {
        (-4, 103, -6): "dirt",
        (-4, 102, -5): "dirt",
        (-3, 103, -5): "dirt",
    }
    dug_coords: list[tuple[int, int, int]] = []
    controls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-3.3, 102.0, -5.3], "health": 20, "food": 20}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            name = solid_blocks.get(pos, "stone" if pos[1] == 101 else "air")
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
        if path == "/dig":
            pos = tuple(body["pos"])
            dug_coords.append(pos)
            solid_blocks.pop(pos, None)
            return httpx.Response(200, json={"ok": True, "result": {"dug": "dirt", "gained": {"dirt": 1}}})
        if path == "/control":
            controls.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-2.2, 102.0, -5.3], "moved": 1.1}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    res = MacroSkills(bot).unstick()
    assert res["ok"] is True
    assert res["foot_block_before"] == [-4, 102, -6]
    assert res["hitbox_xs"] == [-4, -3]
    assert res["hitbox_zs"] == [-6, -5]
    # (-4, 103, -6) and (-4, 102, -5) must be cleared because floor(-3.3) == -4 and floor(-5.3) == -6!
    assert (-4, 103, -6) in dug_coords
    assert (-4, 102, -5) in dug_coords
    assert (-3, 103, -5) in dug_coords
    assert controls and controls[0]["jump"] is False


@pytest.mark.parametrize("ore_layout", ["exposed_iron", "exposed_diamond", "far", "behind_wall", "adjacent_water"])
def test_staircase_up_down_and_tight_table_pocket(ore_layout) -> None:
    from mc_agent.harness.mc_tools import McToolset

    ore_pos = {
        "exposed_iron": [-2, 92, -6],
        "exposed_diamond": [-2, 92, -6],
        "far": [-3, 92, -16],
        "behind_wall": [-1, 92, -6],
        "adjacent_water": [-2, 92, -6],
    }[ore_layout]
    ore_name = "deepslate_diamond_ore" if ore_layout == "exposed_diamond" else "iron_ore"
    state: dict[str, Any] = {
        "pos": [-2.3, 95.0, -2.7],
        "items": [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
            {"name": "cobblestone", "count": 5, "slot": 1},
            {"name": "stick", "count": 4, "slot": 2},
            {"name": "crafting_table", "count": 1, "slot": 3},
        ],
        "blocks": {},
        "dug": [],
        "placed": [],
        "mode": "down",
        "pocket_carved": False,
    }

    # Pre-seed stone underground, air corridor for up filler check, and tight walls around [-3, 95, -3]
    def _default_block(key: tuple[int, int, int]) -> str:
        if key in state["blocks"]:
            return state["blocks"][key]
        x, y, z = key
        if state["mode"] == "down":
            if key == tuple(ore_pos):
                return ore_name
            if ore_layout == "adjacent_water" and key == (-1, 92, -6):
                return "water"
        if state["mode"] == "up" and (x, y, z) == (-3, 93, -4):
            return "grass_block"  # Actual floor after step two, not just a scan hint above a cave.
        if state["mode"] == "up" and x == -3 and z == -4 and y >= 94:
            return "air"
        if state["mode"] == "up" and y >= 94 and z <= -4 and (x, y, z) in {(-3, 93, -3)}:
            return "air"
        if state["mode"] == "up" and y == 92 and z == -5:
            return "air"  # missing step support at nx=-3, by=92, nz=-5 when stepping up
        if y <= 100:
            return "stone"
        return "air"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": _default_block(key), "pos": list(key)}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(list(key))
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/move_to":
            state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        if path == "/place":
            pos = body.get("pos")
            if pos is None:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_support", "message": "tight pocket"}})
            key = tuple(int(v) for v in pos)
            if body["item"] == "crafting_table" and _default_block(key) != "air":
                return httpx.Response(400, json={"ok": False, "error": {"code": "occupied", "message": "wall occupied"}})
            if body["item"] == "crafting_table":
                state["pocket_carved"] = True
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/find_blocks":
            types = set(body.get("types") or [])
            if "crafting_table" in types:
                # Unreachable crafting_table 5 blocks above current y=95 -> must be ignored!
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"blocks": [{"name": "crafting_table", "pos": [-3, 100, -3], "dist": 5.0}]}},
                )
            if "iron_ore" in types:
                assert body["radius"] == 4
                # Still return distant mock results to exercise the coordinate check.
                if state["mode"] == "down" and state["pos"][1] <= 92.0:
                    return httpx.Response(
                        200,
                        json={"ok": True, "result": {"blocks": [{"name": ore_name, "pos": ore_pos, "dist": 2.1}]}},
                    )
                return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
            if "grass_block" in types:
                # Detect surface after 2 upward steps (from y=92 up to y=94)
                if state["mode"] == "up" and state["pos"][1] >= 94.0:
                    return httpx.Response(
                        200,
                        json={"ok": True, "result": {"blocks": [{"name": "grass_block", "pos": [-3, 94, -5], "dist": 1.5}]}},
                    )
                return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/craft":
            return httpx.Response(200, json={"ok": True, "result": {"crafted": body["item"], "gained": 1}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-stair",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        # (c) Distant Y=100 table ignored; open placements blocked -> carves pocket at [-2, 95, -3]
        state["blocks"][(-3, 95, -3)] = "air"
        state["blocks"][(-3, 96, -3)] = "air"
        table_res = skills.ensure_crafting_table()
        assert table_res["ok"] is True
        assert table_res["reused"] is False
        assert table_res["carved_pocket"] == [-2, 95, -3]
        assert [-2, 95, -3] in state["dug"]
        assert ("crafting_table", [-2, 95, -3]) in state["placed"]

        # (a) dig_staircase down stops at step 3 on iron_ore detection
        state["pos"] = [-2.3, 95.0, -2.7]
        state["dug"].clear()
        down_res = skills.dig_staircase(direction="north", steps=8, vertical="down", stop_on_ore=True)
        assert down_res["ok"] is True
        assert state["dug"][:3] == [[-3, 96, -4], [-3, 95, -4], [-3, 94, -4]]
        if ore_layout not in {"exposed_iron", "exposed_diamond"}:
            assert down_res["steps_done"] == 8
            assert down_res["stop_reason"] == "completed"
            assert down_res["ores_nearby"] == []
            assert "ores_detected" not in down_res
            return
        assert down_res["steps_done"] == 3
        assert down_res["stop_reason"] == "ore_detected"
        assert down_res["ores_detected"][0]["pos"] == ore_pos
        assert down_res["ores_detected"][0]["name"] == ore_name
        assert down_res["pos_after"] == [-2.5, 92.0, -5.5]
        if ore_layout == "exposed_diamond":
            return

        # (b) dig_staircase up places filler cobblestone step at [-3, 92, -5] and exits on surface
        state["mode"] = "up"
        state["blocks"].clear()
        state["placed"].clear()
        # Ensure step 1 sky check sees air at [-3, 96, -4] -> then grass_block triggers reached_surface
        state["blocks"][(-3, 96, -4)] = "air"
        up_res = skills.dig_staircase(direction="south", steps=6, vertical="up", stop_on_ore=False)
        assert up_res["ok"] is True
        assert up_res["steps_done"] == 2
        assert up_res["stop_reason"] == "reached_surface"
        assert ("cobblestone", [-3, 92, -5]) in state["placed"]

        # (d) Toolset registration
        names = [s["function"]["name"] for s in McToolset(bot).registry().schemas()]
        assert "mc_skill_staircase" in names
    finally:
        bot.close()


def test_unstick_ignores_suffocation_hurt_and_harvest_logs_inv_delta() -> None:
    state: dict[str, Any] = {
        "pos": [-2.3, 96.0, -1.5],
        "gravel_hits": 0,
        "dig_bodies": [],
        "items": [],
        "logs_dug": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 10.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            if key == (-3, 96, -2):
                # Gravel refills the cell once after the first dig, then clears on the second dig
                name = "gravel" if state["gravel_hits"] < 2 else "air"
                return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
            name = "stone" if key[1] == 95 else "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": False}})
        if path == "/dig":
            state["dig_bodies"].append(body)
            pos = tuple(int(v) for v in body["pos"])
            if pos == (-3, 96, -2):
                # Suffocation hurt would interrupt unless stop_on=["death"]
                if body.get("stop_on") != ["death"]:
                    return httpx.Response(409, json={"ok": False, "error": {"code": "interrupted", "cause": "hurt"}})
                state["gravel_hits"] += 1
                return httpx.Response(200, json={"ok": True, "result": {"dug": "gravel", "gained": {}}})
            state["logs_dug"] += 1
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_log", "gained": {}}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-1.2, 96.0, -1.5], "moved": 1.1}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "oak_log", "pos": [-2, 97, -2], "dist": 1.4},
                            {"name": "oak_log", "pos": [-2, 98, -2], "dist": 2.2},
                        ]
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 701, "name": "item", "item": "oak_logx2", "pos": [-2.0, 96.0, -2.0]}]}},
            )
        if path == "/move_to":
            # Moving over item entity silently puts 2 oak_log in inventory
            state["items"] = [{"name": "oak_log", "count": 2, "slot": 0}]
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-2.0, 96.0, -2.0]}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-suff",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        u_res = skills.unstick()
        assert u_res["ok"] is True
        assert state["gravel_hits"] == 2
        assert all(d.get("stop_on") == ["death"] for d in state["dig_bodies"])

        h_res = skills.harvest_logs(target_logs=2, radius=16)
        assert h_res["ok"] is True
        assert h_res["gained"] == {"oak_log": 2}
    finally:
        bot.close()


def test_text_tool_calls_fallback_and_window_open_adjacent_table_recovery() -> None:
    import io

    from mc_agent.harness.model import _extract_text_tool_calls, _stream_message

    sse_text = (
        "先查桶的合成配方。\n\ntool_calls:\n"
        '```json\n[{"name": "mc_kb_lookup", "arguments": {"query": "bucket"}}]\n```'
    )
    chunk1 = json.dumps({"choices": [{"delta": {"content": sse_text}, "finish_reason": None}]})
    chunk2 = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    raw_sse = "\n".join([f"data: {chunk1}", "", f"data: {chunk2}", "", "data: [DONE]", ""]).encode("utf-8")
    events: list[dict[str, Any]] = []
    msg = _stream_message(io.BytesIO(raw_sse), events.append)
    assert msg["role"] == "assistant"
    assert msg["content"] == "先查桶的合成配方。"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "mc_kb_lookup"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"query": "bucket"}

    # Normal non-mc prose JSON code block must NOT be parsed as tool_calls
    prose = '示例：\n```json\n{"status": "ok"}\n```'
    cleaned_prose, extracted_none = _extract_text_tool_calls(prose)
    assert cleaned_prose == prose
    assert extracted_none == []

    state: dict[str, Any] = {
        "pos": [0.5, 101.0, -2.5],
        "items": [
            {"name": "cobblestone", "count": 6, "slot": 0},
            {"name": "stick", "count": 4, "slot": 1},
            {"name": "oak_planks", "count": 4, "slot": 2},
        ],
        "dug": [],
        "placed": [],
        "blocks": {},
        "buried_tables": {
            (-1, 101, -3): 1.8,
            (-2, 101, -3): 2.7,
            (3, 101, -2): 3.0,
        },
        "stone_pick_attempts": 0,
        "table_crafts": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/find_blocks":
            if "crafting_table" in set(body.get("types") or []):
                blocks = [
                    {"name": "crafting_table", "pos": list(k), "dist": d}
                    for k, d in sorted(state["buried_tables"].items(), key=lambda kv: kv[1])
                ]
                return httpx.Response(200, json={"ok": True, "result": {"blocks": blocks}})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            if key in state["blocks"]:
                name = state["blocks"][key]
            elif key[1] <= 100 or key == (1, 101, -3):
                name = "stone"
            else:
                name = "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "pos": list(key)}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            if key in state["buried_tables"]:
                assert body.get("stop_on") == ["death"]
            state["dug"].append(list(key))
            state["buried_tables"].pop(key, None)
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "block", "gained": {}}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        if path == "/place":
            key = tuple(int(v) for v in body.get("pos", [0, 101, -2]))
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            state["items"] = [it for it in state["items"] if it["name"] != body["item"]]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/craft":
            if body["item"] == "crafting_table":
                state["table_crafts"] += 1
                state["items"] = [it for it in state["items"] if it["name"] != "oak_planks"]
                state["items"].append({"name": "crafting_table", "count": 1, "slot": 3})
                return httpx.Response(200, json={"ok": True, "result": {"crafted": "crafting_table", "gained": 1}})
            if body["item"] == "stone_pickaxe":
                state["stone_pick_attempts"] += 1
                if state["stone_pick_attempts"] == 1:
                    return httpx.Response(
                        400,
                        json={"ok": False, "error": {"code": "craft_timeout", "message": "Event windowOpen did not fire within 20000ms"}},
                    )
                state["items"].append({"name": "stone_pickaxe", "count": 1, "slot": 4})
                return httpx.Response(200, json={"ok": True, "result": {"crafted": "stone_pickaxe", "gained": 1}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-table",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        res = skills.craft_gear("stone_pickaxe", count=1)
        assert res["ok"] is True
        assert state["stone_pick_attempts"] == 2
        assert state["table_crafts"] == 1
        assert state["dug"] == []
        assert len(state["buried_tables"]) == 3
        assert any(item == "crafting_table" for item, _ in state["placed"])
    finally:
        bot.close()


def test_staircase_dig_timeout_names_wooden_pickaxe_as_cause() -> None:
    """BUG-041/r22_long: wooden pickaxe + hard rock = ~25s per block, 1 step per call."""

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 36.0, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "wooden_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "wooden_pickaxe"}}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] <= 36 else "air"}
        elif path == "dig":
            raise httpx.ReadTimeout("offline dig too slow", request=request)
        elif path == "move_to":
            result = {"pos": [0.5, 36.0, 0.5]}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 36.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.dig_staircase(direction="north", vertical="down", steps=1)
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["timeout_path"] == "dig"
    assert result["pickaxe_tier"] == 1
    assert result["low_tier_slow_dig"] is True
    assert "stone_pickaxe" in result["error"]["message"]


@pytest.mark.parametrize("episode_remaining", [5.0, 13.0])
def test_dig_shares_episode_budget_across_pickaxe_preflight(monkeypatch, episode_remaining) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        calls.append(route)
        if route == "dig":
            assert timeout == pytest.approx(5.0)
            clock[0] += timeout
            raise httpx.ReadTimeout("offline slow dig", request=request)
        results = {
            "get_block": {"name": "iron_ore"},
            "inventory": {"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}]},
            "equip": {"equipped": "stone_pickaxe"},
            "stop": {"released": True},
        }
        clock[0] += min(1.0, timeout)
        return httpx.Response(200, json={"ok": True, "result": results[route]})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        tools._episode_deadline_mono = episode_remaining
        result = json.loads(tools.mc_dig(1, 64, 1))

    assert result["ok"] is False
    assert clock[0] <= episode_remaining
    if episode_remaining == 5.0:
        assert calls == []
        assert result["stop_reason"] == "episode_deadline_imminent"
    else:
        assert calls == ["get_block", "inventory", "equip", "dig", "stop"]
        assert result["stop_reason"] == "time_budget"
        assert result["stop_confirmed"] is True


@pytest.mark.parametrize("slow_route", ["craft", "equip"])
def test_craft_gear_shares_episode_budget_and_keeps_completed_craft(monkeypatch, slow_route) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        assert 0.0 < timeout <= 8.0 - clock[0]
        calls.append(route)
        if route == slow_route:
            assert timeout == pytest.approx(7.0 - clock[0])
            clock[0] += timeout
            raise httpx.ReadTimeout(f"offline slow {route}", request=request)
        results = {
            "inventory": {"items": [{"name": "oak_planks", "count": 8, "slot": 0}]},
            "craft": {"gained": {"stick": 4}},
            "stop": {"released": True},
        }
        clock[0] += min(1.0, timeout)
        return httpx.Response(200, json={"ok": True, "result": results[route]})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        tools._episode_deadline_mono = 12.0
        result = json.loads(tools.mc_skill_craft_gear("stick", equip_slot="hand"))

    assert calls[-2:] == [slow_route, "stop"]
    assert calls.count("craft") == 1
    assert clock[0] <= 12.0
    if slow_route == "craft":
        assert result["ok"] is False
        assert result["stop_reason"] == "time_budget"
    else:
        assert result["ok"] is True
        assert result["gained"] == {"stick": 4}
        assert result["deadline_hit"] is True
        assert result["equipped_slot"] is None


@pytest.mark.parametrize(
    "mode",
    [
        "equip_shield", "shield_in_slot", "equip_unconfirmed", "equip_shield_alias", "shield_in_slot_alias",
        "owned", "short", "make_sticks", "with_sticks",
        "old_pick_selected", "old_pick_durability", "old_pick_damage", "old_pick_default", "fresh_default", "owned_worn",
    ],
)
def test_craft_gear_reuses_only_after_shortage_and_confirms_equip(mode) -> None:
    from mc_agent.harness.mc_tools import McToolset

    wear = {"durability_used": 130}
    if mode in {"old_pick_durability", "old_pick_default", "owned_worn"}:
        wear = {"durability": [1, 131]}
    elif mode == "old_pick_damage":
        wear = {"damage": 130}
    worn = {"name": "stone_pickaxe", "count": 1, **wear}
    fresh = {"name": "stone_pickaxe", "count": 1, "durability": [131, 131]}
    item, slot, count = "flint_and_steel", None, 1
    bag = [{"name": item, "count": 1}]
    offhand = held = None
    new_pick_request = mode in {"make_sticks", "with_sticks", "fresh_default"} or mode.startswith("old_pick_")
    equip_shield = mode in {"equip_shield", "equip_shield_alias"}
    shield_in_slot = mode in {"shield_in_slot", "shield_in_slot_alias"}
    if equip_shield or shield_in_slot or mode == "equip_unconfirmed":
        item, slot = "shield", "off-hand"
        shield = {"name": item, "count": 1}
        bag = [] if shield_in_slot else [shield]
        offhand = shield if shield_in_slot else None
    elif new_pick_request:
        item, slot, held = "stone_pickaxe", "hand", worn
        wood = "oak_planks" if mode == "make_sticks" else "stick"
        bag = [worn, {"name": "cobblestone", "count": 3}, {"name": wood, "count": 2}]
    elif mode == "owned_worn":
        item, held, bag = "stone_pickaxe", worn, [worn]
    elif mode == "short":
        count = 2
    crafts, equips = [], []

    def handler(request):
        nonlocal offhand, held
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": bag, "offhand": offhand, "held": held}
        elif route == "status":
            result = {"pos": [0.5, 64, 0.5], "dimension": "overworld"}
        elif route == "find_blocks":
            assert body["types"] == ["crafting_table"]
            result = {"blocks": [{"pos": [1, 64, 0], "dist": 1}]}
        elif route == "craft":
            crafts.append(body["item"])
            if body["item"] == "stick":
                assert mode == "make_sticks" and bag.pop(2)["name"] == "oak_planks"
                bag.append({"name": "stick", "count": 4})
                result = {"crafted": "stick", "gained": 4}
            else:
                assert body == {"item": "stone_pickaxe", "count": 1}
                assert bag[1]["count"] == 3 and bag[2]["count"] >= 2
                bag.pop(1)
                bag[1]["count"] -= 2
                bag.append(fresh)
                result = {"crafted": "stone_pickaxe", "gained": 1}
        elif route == "equip":
            assert body == {"item": item, "slot": slot}
            equips.append(body)
            if equip_shield:
                offhand = bag.pop()
            elif new_pick_request and not mode.startswith("old_pick_"):
                assert bag[-1] == fresh
                held = fresh
            result = {"held": held}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        requested_slot = None if mode.endswith("_default") else slot
        if mode.endswith("_alias"):
            requested_slot = "offhand"
        result = json.loads(tools.mc_skill_craft_gear(item, count=count, equip_slot=requested_slot))

    if mode == "short":
        assert result["error"] == "missing_ingredients" and not crafts and not equips
    elif new_pick_request:
        assert result["ok"] is True and result["gained"] == 1 and not result.get("already_owned")
        assert crafts == (["stick", "stone_pickaxe"] if mode == "make_sticks" else ["stone_pickaxe"])
        assert len(equips) == 1
        if mode.startswith("old_pick_"):
            assert result["equipped_slot"] is None and result["equipped_item"] == worn
            assert result["equip_error"]["code"] == "equip_unconfirmed"
        else:
            assert result["equipped_slot"] == "hand" and result["equipped_item"] == fresh
    else:
        assert result["already_owned"] is True and result["owned_count"] == 1 and result["gained"] == 0 and not crafts
        assert len(equips) == int(equip_shield or mode == "equip_unconfirmed")
        if mode == "equip_unconfirmed":
            assert result["ok"] is False and result["equip_error"]["code"] == "equip_unconfirmed"
        else:
            assert result["ok"] is True and result["equipped_slot"] == slot
            if slot:
                assert offhand["name"] == "shield"
            if mode == "owned_worn":
                assert result["owned_items"] == [worn]


@pytest.mark.parametrize("scene,table_count,plank_count,expected_actions", [
    ("above", 1, 0, ["place", "craft:iron_pickaxe", "equip"]),
    ("stalled", 1, 0, ["move_to", "place", "craft:iron_pickaxe", "equip"]),
    ("no_window", 0, 4, ["craft:iron_pickaxe", "craft:crafting_table", "place", "craft:iron_pickaxe", "equip"]),
    ("fresh_table_no_window", 1, 0, ["place", "craft:iron_pickaxe", "craft:iron_pickaxe", "equip"]),
    ("reached", 1, 0, ["move_to", "craft:iron_pickaxe", "equip"]),
    ("above", 0, 3, ["move_to"]),
])
def test_craft_gear_uses_a_local_table_without_removing_the_old_one(scene, table_count, plank_count, expected_actions) -> None:
    from mc_agent.harness.mc_tools import McToolset

    pos = [0.5, 68, 0.5]
    old_table = [4, 68, 0]
    if scene in {"above", "fresh_table_no_window"}:
        old_table[1] += 14
    elif scene == "no_window":
        old_table[0] = 1
    bag = {"crafting_table": table_count, "oak_planks": plank_count, "iron_ingot": 3, "stick": 2}
    held = None
    placed = False
    actions = []

    def handler(request):
        nonlocal pos, held, placed
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": item, "count": count} for item, count in bag.items() if count], "held": held}
        elif route == "status":
            result = {"pos": pos, "health": 20, "dimension": "overworld", "held": held}
        elif route == "find_blocks":
            distance = math.dist(pos, [coord + 0.5 for coord in old_table])
            result = {"blocks": [{"pos": old_table, "dist": distance}] if distance <= body["radius"] else []}
        elif route == "move_to":
            actions.append(route)
            if scene == "reached":
                pos = [3.5, 68, 0.5]
            result = {"pos": pos}
        elif route == "place":
            actions.append(route)
            assert body["item"] == "crafting_table" and bag["crafting_table"] == 1
            bag["crafting_table"] -= 1
            placed = True
            result = {"pos": [0, 68, 1]}
        elif route == "craft":
            item = body["item"]
            actions.append(f"craft:{item}")
            if item == "crafting_table":
                bag["oak_planks"] -= 4
            elif scene == "no_window" and not placed:
                return httpx.Response(200, json={"ok": False, "error": {"code": "no_window"}})
            elif scene == "fresh_table_no_window" and actions.count("craft:iron_pickaxe") == 1:
                assert placed
                return httpx.Response(200, json={"ok": False, "error": {"code": "no_window"}})
            else:
                assert placed or scene == "reached"
                bag["iron_ingot"] -= 3
            bag[item] = bag.get(item, 0) + 1
            result = {"crafted": item, "gained": {item: 1}}
        elif route == "equip":
            actions.append(route)
            assert body == {"item": "iron_pickaxe", "slot": "hand"}
            held = {"name": "iron_pickaxe", "slot": 36}
            result = {"equipped": "iron_pickaxe"}
        else:
            raise AssertionError(route)  # In particular, the old table must never be dug.
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_craft_gear("iron_pickaxe", equip_slot="hand"))

    assert actions == expected_actions
    if not table_count and plank_count < 4:
        assert result["error"] == "need_crafting_table" and bag["iron_ingot"] == 3
    else:
        assert result["ok"] is True and result["equipped_slot"] == "hand"
        assert bag["iron_pickaxe"] == 1

