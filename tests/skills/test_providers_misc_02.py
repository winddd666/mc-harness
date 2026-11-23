from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

def test_event_cursor_pagination_wall_los_and_dig_tier_guard() -> None:
    from mc_agent.harness.mc_tools import McToolset

    all_events = [
        {"seq": i, "type": "hurt" if i % 2 else "death", "health": 0, "cause": "arrow", "pos": [-150, 72, -390]}
        for i in range(1, 71)
    ]
    event_queries: list[int] = []
    dug_calls: list[list[int]] = []
    equipped: list[str] = []
    placed_calls: list[dict] = []
    state: dict[str, Any] = {
        "pos": [-166.3, 63.0, -392.1],
        "held": None,
        "items": [
            {"name": "raw_iron", "count": 2, "slot": 0},
            {"name": "furnace", "count": 1, "slot": 1},
            {"name": "coal", "count": 1, "slot": 2},
        ],
        "blocks": {
            # Roofed air pocket ceiling
            (-167, 65, -393): "stone",
            (-167, 66, -393): "stone",
            # Solid wall around (-167, 63, -393) blocking path toward skeleton at (-166.5, 72, -382.5)
            (-167, 64, -392): "stone",
            (-167, 65, -392): "stone",
            (-167, 66, -391): "stone",
            # Target ore & closed pocket walls for carved furnace
            (-165, 63, -392): "iron_ore",
            (-166, 62, -393): "stone",
            (-166, 63, -393): "stone",
        },
        "window_ops": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/events":
            since = int(request.url.params.get("since", "0"))
            limit = int(request.url.params.get("limit", "50"))
            event_queries.append(since)
            page = [ev for ev in all_events if ev["seq"] > since][:limit]
            page_last = page[-1]["seq"] if page else 70
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": page_last, "events": page}})
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state["pos"]),
                        "health": 20.0,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "in_water": False,
                        "nearest_hostile": {"id": 88, "name": "skeleton", "dist": 12.6},
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [
                            {"id": 88, "type": "hostile", "name": "skeleton", "pos": [-166.5, 72.0, -382.5], "dist": 12.6}
                        ]
                    },
                },
            )
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            name = state["blocks"].get(key)
            if name is None:
                # Default rock layer above/around pocket unless open air in pocket
                name = "stone" if key[1] >= 65 or key in {(-167, 62, -393), (-166, 62, -393)} else "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": list(state["items"]), "held": state["held"]}},
            )
        if path == "/equip":
            equipped.append(body["item"])
            state["held"] = {"name": body["item"], "count": 1}
            return httpx.Response(200, json={"ok": True, "result": {"held": state["held"]}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            dug_calls.append(list(key))
            bname = state["blocks"].pop(key, "stone")
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": bname, "gained": {}}})
        if path == "/place":
            placed_calls.append(dict(body))
            if "pos" not in body:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_surface", "message": "enclosed"}})
            key = tuple(int(v) for v in body["pos"])
            cur_blk = state["blocks"].get(key, "air")
            if key != (-166, 63, -393) or cur_blk != "air":
                return httpx.Response(400, json={"ok": False, "error": {"code": "blocked", "message": "solid"}})
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/window/act":
            state["window_ops"].append(body)
            done = [{"took": {"iron_ingot": 2}}] if body["ops"][0]["op"] == "take" else True
            return httpx.Response(200, json={"ok": True, "result": {"done": done}})
        if path == "/window/view":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"sections": {"input": [], "output": [{"name": "iron_ingot", "count": 2}]}, "info": {"burning": False}}},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-pocket",
        bot_username="BeastBot_01",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. Event pagination drains all 70 stale death/hurt events and persists cursor across toolsets
        ts1 = McToolset(bot)
        assert ts1.sync_event_cursor() == 70
        assert event_queries == [0, 50]
        assert ts1.drain_notable_events() == ""

        ts2 = McToolset(bot)
        assert ts2.sync_event_cursor() == 70
        assert event_queries[-1] == 70

        # 2. mc_status() enriches roofed pocket enclosure and ray-marched wall barrier assessment
        st = json.loads(ts2.mc_status())["result"]
        assert st["enclosure"]["roofed_pocket"] is True
        assert st["threat_assessment"]["hostile_blocked_by_wall"] is True
        assert st["threat_assessment"]["safe_in_place"] is False
        assert st["threat_assessment"]["all_checked_hostiles_occluded"] is True

        # 3. mc_dig blocks bare-hand iron_ore mining and auto-equips stone_pickaxe once present
        no_pick = json.loads(ts2.mc_dig(-165, 63, -392))
        assert no_pick["ok"] is False
        assert no_pick["error"]["code"] == "tool_tier_insufficient"
        assert not dug_calls

        state["items"].append({"name": "stone_pickaxe", "count": 1, "slot": 3})
        with_pick = json.loads(ts2.mc_dig(-165, 63, -392))
        assert with_pick["ok"] is True
        assert equipped == ["stone_pickaxe"]
        assert dug_calls == [[-165, 63, -392]]

        # 4. smelt_items carves a furnace pocket in a closed stone room when direct place fails
        smelt_res = ts2.skills.smelt_items("raw_iron", count=2)
        assert smelt_res["ok"] is True
        assert [-166, 63, -393] in dug_calls
        assert any(p.get("item") == "furnace" and p.get("pos") == [-166, 63, -393] for p in placed_calls)
    finally:
        bot.close()


@pytest.mark.parametrize("release", [True, False])
def test_move_to_busy_retries_only_after_lease_release(release) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "move_to" and calls.count(path) == 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "busy", "message": "running dig"}})
        if path == "move_to":
            result = {"pos": [4.5, 64, 0.5]}
        elif path == "stop":
            result = {"stopped": True, "released": release, "action": "dig"}
        elif path == "status":
            result = {"pos": [0.5, 64, 0.5], "dimension": "overworld"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot, episode_deadline_mono=time.monotonic() + (30 if release else 6.5))
        tools._is_in_water = lambda: False
        result = json.loads(tools.mc_move_to(x=4.5, z=0.5))
    assert result["ok"] is release
    assert calls.count("stop") == 1
    assert calls.count("move_to") == (2 if release else 1)
    if not release:
        assert calls == ["status", "move_to", "stop"]
        assert result["requires_replan"] is True
        assert result["error"]["code"] == "busy"
        assert "动作锁仍被" in result["error"]["message"]


@pytest.mark.parametrize("timed_out", [False, True])
def test_move_to_far_coordinates_reports_one_waypoint_and_remaining_distance(timed_out) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []
    pos = [0.0, 70.0, 0.0]

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((route, body))
        if route == "status":
            result = {"pos": list(pos), "dimension": "overworld"}
        elif route == "move_to":
            pos[:] = [6.0, 71.0, 8.0] if timed_out else [body["x"], 71.0, body["z"]]
            if timed_out:
                raise httpx.ReadTimeout("long walk", request=request)
            result = {"pos": list(pos)}
        elif route == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, episode_deadline_mono=time.monotonic() + 60)
        response = json.loads(toolset.mc_move_to(x=96, y=74, z=128))

    move_calls = [body for route, body in calls if route == "move_to"]
    assert len(move_calls) == 1
    assert move_calls[0]["x"] == pytest.approx(19.2)
    assert move_calls[0]["z"] == pytest.approx(25.6)
    assert "y" not in move_calls[0]
    assert response["ok"] is not timed_out
    result = response["result"]
    assert result["partial_move"] is True
    assert result["pos"] == pos
    assert result["target_xz"] == [96, 128]
    assert result["remaining_dist"] == pytest.approx(150.0 if timed_out else 128.0)
    if timed_out:
        assert response["error"]["code"] == "move_timeout"
        assert response["requires_replan"] is True


@pytest.mark.parametrize("preflight_has_position", [True, False])
def test_budgeted_coordinate_move_with_entity_placeholder_never_enters_multihop(monkeypatch, preflight_has_position) -> None:
    from mc_agent.harness.mc_tools import McToolset

    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    origin = [3031.3, 64, -10005.5]
    target = [2978, -10076]
    calls = []
    moves = []

    def handler(request):
        nonlocal now
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        timeout = request.extensions["timeout"]["read"]
        if route == "status" and len(calls) == 1:
            status = {"dimension": "overworld"}
            if preflight_has_position:
                status["pos"] = origin
            return httpx.Response(200, json={"ok": True, "result": status})
        if route == "status":
            assert timeout <= 1.0
            now += timeout
            raise httpx.ReadTimeout("offline slow status after failed move", request=request)
        assert route == "move_to"
        assert timeout <= 25.0
        moves.append(json.loads(request.content))
        now += timeout
        return httpx.Response(200, json={
            "ok": False, "error": {"code": "path_not_found", "message": "blocked", "pos": origin},
        })

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("episode move must not start legacy multi-hop recovery")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, episode_deadline_mono=44.1327)
        monkeypatch.setattr(toolset.skills, "navigate_segmented", unexpected_fallback)
        result = json.loads(toolset.mc_move_to(x=target[0], y=0, z=target[1], range=4, entity_id=0))

    assert len(moves) == 1
    assert "entity_id" not in moves[0] and "y" not in moves[0]
    assert now <= 26.0
    assert result["error"]["code"] == "path_not_found"
    if preflight_has_position:
        assert math.hypot(moves[0]["x"] - origin[0], moves[0]["z"] - origin[2]) == pytest.approx(32)
        assert result["result"]["target_xz"] == target
    else:
        assert [moves[0]["x"], moves[0]["z"]] == target


def test_move_to_counts_waypoint_and_direct_no_path_in_one_episode() -> None:
    from mc_agent.harness.mc_tools import McToolset

    moves = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={
                "ok": True, "result": {"pos": [0, 64, 0], "dimension": "overworld"},
            })
        assert path == "move_to"
        moves.append(json.loads(request.content)["x"])
        return httpx.Response(200, json={
            "ok": False, "error": {"code": "path_not_found", "message": "blocked", "pos": [0, 64, 0]},
        })

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, episode_deadline_mono=time.monotonic() + 60)
        direct = json.loads(toolset.mc_move_to(x=35, z=0))
        waypoint = json.loads(toolset.mc_move_to(x=40, z=0))
        later = json.loads(toolset.mc_move_to(x=0, z=25))

    assert moves == [35, 32, 0]
    assert [out["path_not_found_count"] for out in (direct, waypoint, later)] == [1, 2, 3]
    assert waypoint["requires_replan"] is True
    assert waypoint["result"]["waypoint_xz"] == [32, 0]
    assert waypoint["result"]["target_xz"] == [40, 0]
    assert "近邻目标重复 2 次" in waypoint["result"]["hint"]
    assert later["requires_replan"] is True


def test_move_to_skips_a_retried_waypoint_within_the_failed_range(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    pos = [-5156.5, 58, -7644.3]
    moves = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            return httpx.Response(200, json={
                "ok": True, "result": {"pos": pos, "dimension": "the_nether"},
            })
        assert route == "move_to"
        moves.append(json.loads(request.content))
        return httpx.Response(200, json={
            "ok": False, "error": {"code": "path_not_found", "message": "blocked", "pos": pos},
        })

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, episode_deadline_mono=60)
        toolset.mc_move_to(x=-5168, z=-7578, range=3)
        now = 3.54
        waypoint = json.loads(toolset.mc_move_to(x=-5168, z=-7578, range=5))
        assert len(moves) == 2 and waypoint["path_not_found_count"] == 2

        now = 6.38
        retry = json.loads(toolset.mc_move_to(x=-5162, z=-7613, range=4))
        assert len(moves) == 2
        assert retry["error"]["code"] == "move_route_cooldown"
        assert retry["requires_replan"] is True

        wider = json.loads(toolset.mc_move_to(x=-5162, z=-7613, range=6))
        assert wider["path_not_found_count"] == 3
        pos[0] += 1.0
        toolset.mc_move_to(x=-5162, z=-7613, range=4)
        assert len(moves) == 4
        pos[0] -= 1.0
        now = 9.0
        toolset.mc_move_to(x=-5162, z=-7613, range=4)
        assert len(moves) == 5


def test_move_to_fails_fast_when_action_lease_is_busy() -> None:
    """r12 P2-16: busy move_to blocked ~101s before returning."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "busy": "dig"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=4.5, z=0.5))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "action_busy"
    assert result["error"]["action"] == "dig"
    assert calls == ["status"]


def test_move_to_already_in_range_is_success_not_failure() -> None:
    """r15 top Slice C error: movement_not_confirmed at 33/10 bots."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.7, 64.0, 1000.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "movement_not_confirmed", "message": "no movement"}},
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=1000.8, z=1000.5, range=2.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True
    assert result["result"]["distance"] < 0.2
    assert calls.count("move_to") == 1


def test_move_to_trusts_server_already_in_range_despite_client_desync() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1002.4, 64.0, 1000.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {
                        "code": "movement_not_confirmed",
                        "message": "当前站位已在目标 range=1m 半径内，未发生实际位移",
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=1000.8, z=1000.5, range=1.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True


def test_move_to_entity_trusts_server_already_in_range() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "pos_before": [0.5, 64.0, 0.5],
                    "pos_after": [0.5, 64.0, 0.5],
                    "error": {"code": "movement_not_confirmed", "message": "当前站位已在目标 range=1m 半径内，未发生实际位移"},
                },
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(entity_id=77, range=1.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True
    assert result["result"]["entity_id"] == 77


def test_move_to_entity_reports_stale_id_with_replan_hint() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(200, json={"ok": False, "error": {"code": "no_such_entity", "message": "entity 214998 not visible"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(entity_id=214998, range=15))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert "mc_get_entities" in result["error"]["message"]


def test_move_to_rejects_z_swapped_into_out_of_range_y() -> None:
    """r12 P2-17: y=-2814 was silently dropped, wasting a 90s path search."""
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError("invalid y must be rejected before any API call")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=-7828, y=-2814, z=-2744))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "bad_args"
    assert "y=-2814" in result["error"]["message"]


@pytest.mark.parametrize(
    "item,level,changed,used,gained,filled",
    [
        ("glass_bottle", 1, False, {}, {}, False),
        ("glass_bottle", 8, False, {}, {}, False),
        ("glass_bottle", 0, False, {"glass_bottle": 1}, {}, False),
        ("glass_bottle", 0, True, {"glass_bottle": 1}, {"kelp": 1}, False),
        ("glass_bottle", 0, False, {"glass_bottle": 1}, {"potion": 1}, True),
        ("minecraft:glass_bottle", 0, False, {"glass_bottle": 1}, {"potion": 1}, True),
    ],
)
def test_glass_bottle_interact_with_water_requires_confirmed_fill(
    item, level, changed, used, gained, filled
) -> None:
    from mc_agent.harness.mc_tools import McToolset

    receipt = {
        "block": "water",
        "before": {"level": level},
        "after": {"name": "water", "properties": {"level": level}},
        "changed": changed,
    }
    if used:
        receipt["used"] = used
    if gained:
        receipt["gained"] = gained

    def handler(request):
        assert request.url.path.endswith("/interact")
        assert json.loads(request.content)["item"] == item
        return httpx.Response(200, json={"ok": True, "result": receipt})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item=item))

    assert result["ok"] is filled
    assert result["result"] == receipt
    if filled:
        assert "error" not in result
    else:
        assert result["requires_replan"] is True
        assert result["error"]["code"] == "interact_no_effect"
        assert "level=0" in result["error"]["message"]


@pytest.mark.parametrize(
    "item,interact_target",
    [
        ("fishing_rod", {"x": 1, "y": 62, "z": 3}),
        ("fishing_rod", {}),
        ("minecraft:fishing_rod", {"x": 1, "y": 62, "z": 3}),
    ],
)
def test_interact_with_fishing_rod_recommends_cast_at_open_water(item, interact_target) -> None:
    from mc_agent.harness.mc_tools import McToolset

    routes = []
    receipt = {"caught": {"cod": 1}, "casts": 1}

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        routes.append(route)
        if route == "interact":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"block": "water", "changed": False}},
            )
        assert route == "use_item"
        assert json.loads(request.content) == {"item": "fishing_rod", "target": {"pos": [1, 62, 3]}}
        return httpx.Response(200, json={"ok": True, "result": receipt})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        rejected = json.loads(toolset.mc_interact(item=item, **interact_target))
        assert rejected["ok"] is False
        assert rejected["requires_replan"] is True
        assert rejected["error"]["code"] == "use_item_api"
        assert "mc_use_item(item='fishing_rod'" in rejected["error"]["message"]
        assert routes == []

        cast = json.loads(toolset.mc_use_item(item="fishing_rod", x=1, y=62, z=3))

    assert routes == ["use_item"]
    assert cast == {"ok": True, "result": receipt}


def test_window_view_rejects_entity_at_12m_before_no_window() -> None:
    """r16 BUG-027: goNear silently failed, then no_window from 12m."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 81197, "pos": [100, 64, 100], "dist": 12.2}]}},
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_window_view(entity_id=81197))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "out_of_reach"
    assert result["error"]["dist"] == pytest.approx(12.2)
    assert "mc_move_to" in result["error"]["message"]
    assert calls == ["get_entities"]


def test_window_view_allows_near_entity() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 5, "pos": [1, 64, 1], "dist": 2.0}]}},
            )
        if path == "view":
            return httpx.Response(
                200, json={"ok": True, "result": {"window": {"type": "minecraft:merchant"}, "target": {"entity_id": 5}}}
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_window_view(entity_id=5))
    assert result["ok"] is True


@pytest.mark.parametrize("item", ["crafting_table", "furnace"])
@pytest.mark.parametrize("has_recess", [True, False])
def test_new_utility_uses_side_recess_and_keeps_both_passages_open(item, has_recess) -> None:
    from mc_agent.harness.mc_tools import McToolset

    side = [0, -54, 1]
    placements = []
    uses = []
    placed = False

    def handler(request):
        nonlocal placed
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, -54.0, 0.5], "dimension": "overworld", "health": 20, "on_ground": True}
        elif path == "inventory":
            result = {"items": [
                {"name": item, "count": int(not placed)}, {"name": "coal", "count": 1},
                {"name": "raw_iron", "count": 1}, {"name": "cobbled_deepslate", "count": 3},
                {"name": "stick", "count": 2}, {"name": "stone_pickaxe", "count": 1},
            ]}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "get_block":
            x, y, z = body["pos"]
            if placed and body["pos"] == side:
                name = item
            elif (z == 0 or (has_recess and x == 0 and z == 1)) and y in (-54, -53):
                name = "air"
            else:
                name = "deepslate"
            result = {"name": name}
        elif path == "place":
            placements.append(body)
            assert body == {"item": item, "pos": side} and has_recess
            placed = True
            result = {"placed": item, "pos": side}
        elif path == "craft":
            uses.append(path)
            assert placed
            result = {"crafted": "stone_pickaxe", "gained": 1}
        elif path == "equip":
            result = {"held": "stone_pickaxe"}
        elif path == "dig":
            assert item == "crafting_table" and body["pos"] == side
            result = {"dug": item, "gained": {item: 1}}
            placed = False
        elif path == "view":
            assert body["target"]["pos"] == side and placed
            result = {"window": "furnace", "info": {"burning": True}, "sections": {
                "output": [{"name": "iron_ingot", "count": 1}], "input": [], "fuel": [],
            }}
        elif path == "act":
            uses.append(path)
            assert body["target"]["pos"] == side and placed
            done = [{"took": {"iron_ingot": 1}}] if body["ops"][0]["op"] == "take" else True
            result = {"done": done}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        raw = tools.mc_skill_craft_gear("stone_pickaxe") if item == "crafting_table" else tools.mc_skill_smelt("raw_iron", 1)
        result = json.loads(raw)
        for x in (-1, 1):
            for y in (-54, -53):
                assert bot.call("get_block", {"pos": [x, y, 0]})["name"] == "air"

    if has_recess:
        assert result["ok"] and placements and uses
        assert placed is (item == "furnace")  # The active furnace stays off the return lane.
    else:
        assert result["error"] == "no_safe_utility_position"
        assert not placements and not uses

