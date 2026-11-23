from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

def test_mine_blocks_out_of_reach_ranking_and_drop_collection() -> None:
    dug_positions: list[list[int]] = []
    moved_to: list[dict] = []
    cobble = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cobble
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 100.0, 0.0], "health": 20, "food": 20}})
        if path == "/inventory":
            items = [{"name": "wooden_pickaxe", "count": 1, "slot": 36}]
            if cobble > 0:
                items.append({"name": "cobblestone", "count": cobble, "slot": 37})
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "stone", "pos": [0, 80, 0], "dist": 20.0},
                            {"name": "stone", "pos": [2, 100, 0], "dist": 2.0},
                            {"name": "stone", "pos": [3, 100, 0], "dist": 3.0},
                        ]
                    },
                },
            )
        if path == "/dig":
            dug_positions.append(body["pos"])
            if body["pos"] == [2, 100, 0]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "blocked"}})
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {}}})
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"entities": [{"id": 9001, "type": "item", "item": "cobblestonex1", "pos": [3.0, 100.0, 0.0]}]},
                },
            )
        if path == "/get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone" if body["pos"][1] < 100 else "air"}})
        if path == "/move_to":
            moved_to.append(body)
            cobble += 1
            return httpx.Response(200, json={"ok": True, "result": {"pos": [3.0, 100.0, 0.0]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)
    res = skills.mine_blocks(["stone"], count=1, radius=24)
    assert res["ok"] is True
    assert res["dug_count"] == 1
    assert res["gained"].get("cobblestone") == 1
    # [0, 80, 0] is penalized vertically; [2, 100, 0] is tried first, blacklisted on out_of_reach, then [3, 100, 0] succeeds
    assert dug_positions == [[2, 100, 0], [3, 100, 0]]
    assert (2, 100, 0) not in skills.protected_positions
    assert skills.temporary_avoidance[(2, 100, 0)]["reason"] == "out_of_reach"
    assert moved_to and [moved_to[0][axis] for axis in ("x", "y", "z")] == [3.0, 100, 0.0]
    assert moved_to[0]["range"] == 0.8


def test_harvest_logs_stops_after_four_unreachable_targets(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    origins = [(x, 64, 0) for x in range(3, 9)]
    attempts = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": list(pos), "name": "oak_log", "dist": pos[0]} for pos in origins]}
        elif route == "get_block":
            result = {"name": "air"}
        elif route == "dig":
            attempts.append(tuple(json.loads(request.content)["pos"]))
            clock[0] += 2.75
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.harvest_logs(target_logs=16)
        assert attempts == origins[:4]
        assert clock[0] <= 12
        clock[0] += 16
        assert all(skills._is_block_avoided(pos) for pos in attempts)
        clock[0] += 45
        assert all(not skills._is_block_avoided(pos) for pos in attempts)

    assert result["stop_reason"] == "unreachable_targets"
    assert result["requires_replan"] is True
    assert result["dug_count"] == 0
    assert result["gained"] == {}


@pytest.mark.parametrize("terrain", ["slope", "far_hill", "water", "unconfirmed", "hurt", "lost_receipt"])
def test_harvest_logs_tries_one_supported_approach_to_elevated_logs(terrain) -> None:
    standing = [0.6, 64.0, 0.5]
    log = [21, 80, 0] if terrain == "far_hill" else [4, 68, 0]
    moves = []
    scans = []
    digs = []
    stops = []
    wood = 0

    def handler(request):
        nonlocal wood
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": standing, "on_ground": True, "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": "oak_log", "count": wood}] if wood else []}
        elif route == "find_blocks":
            scans.append(standing.copy())
            result = {"blocks": [{"pos": log, "name": "oak_log"}]}
        elif route == "get_block":
            x, y, _ = body["pos"]
            if terrain == "water" and x != 0:
                name = "water"
            else:
                name = "dirt" if y <= (63 if x == 0 else 64) else "air"
            result = {"name": name}
        elif route == "move_to":
            moves.append(body)
            if terrain == "lost_receipt":
                raise httpx.RemoteProtocolError("movement receipt lost", request=request)
            destination = [body["x"], body["y"], body["z"]]
            if terrain != "unconfirmed":
                standing[:] = destination
            result = {"pos": destination}
            if terrain == "hurt":
                result["interrupted_by"] = "hurt"
        elif route == "stop":
            stops.append(body)
            result = {"stopped": True, "released": True}
        elif route == "dig":
            digs.append(body["pos"])
            assert math.dist(standing, log) <= 4.0
            wood += 1
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=1, radius=16)

    assert len(moves) == (0 if terrain == "water" else 1)
    if moves:
        assert moves[0]["y"] == 65
        assert moves[0]["stop_on"] == ["hurt", "death"]
        assert moves[0]["timeout"] < 3000
    assert len(stops) == (1 if terrain == "lost_receipt" else 0)
    assert len(scans) == (2 if terrain in {"slope", "far_hill"} else 1)
    assert result["log_approach"]["moved"] is (terrain in {"slope", "far_hill"})
    if terrain == "slope":
        assert digs == [log]
        assert result["ok"] is True
        assert result["target_gained"] == {"oak_log": 1}
    else:
        assert digs == []
        assert result["ok"] is False
        assert result["stop_reason"] == ("approach_interrupted" if terrain == "hurt" else "unreachable_targets")


@pytest.mark.parametrize("actor", [False, True])
def test_harvest_logs_budget_covers_setup_and_preserves_partial_dig(monkeypatch, actor) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    attempts = []
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "status":
            clock[0] += 1.0
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": [1 + len(attempts), 64, 0], "name": "oak_log", "dist": 1.0}]}
        elif route == "dig":
            timeout = request.extensions["timeout"]["read"]
            attempts.append(timeout)
            if len(attempts) == 2:
                clock[0] += timeout
                raise httpx.ReadTimeout("slow dig", request=request)
            clock[0] += 1.0
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        elif route == "stop":
            clock[0] += 0.5
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        if actor:
            tools = McToolset(bot, episode_deadline_mono=5.8975)
            skipped = json.loads(tools.mc_skill_harvest_logs(target_logs=6, radius=24))
            assert skipped["stop_reason"] == "episode_deadline_imminent"
            assert calls == []
            tools = McToolset(bot, episode_deadline_mono=12.0)
            result = json.loads(tools.mc_skill_harvest_logs(target_logs=4))
        else:
            result = MacroSkills(bot).harvest_logs(target_logs=4, max_duration_s=8.0)

    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["gained"] == {"oak_log": 1}
    assert result["dug_count"] == 1
    assert attempts[1] <= 4.0
    assert calls[-1] == "stop"
    assert clock[0] <= 8.0


@pytest.mark.parametrize("cleanup_times_out", [False, True])
def test_actor_harvest_cleanup_respects_the_same_budget(monkeypatch, cleanup_times_out) -> None:
    from mc_agent.harness.mc_tools import McToolset

    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    cobblestone_stacks = 0
    calls = []

    def handler(request):
        nonlocal now, cobblestone_stacks
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [
                {"name": "cobblestone", "count": 64, "slot": slot}
                for slot in range(9, 9 + cobblestone_stacks)
            ]}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": [1, 64, 0], "name": "oak_log", "dist": 1.0}]}
        elif route == "dig":
            now += 5.0
            cobblestone_stacks = 4
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        elif route == "drop":
            timeout = request.extensions["timeout"]["read"]
            assert 0 < timeout <= 2.0
            assert json.loads(request.content) == {"item": "cobblestone", "count": 64}
            if cleanup_times_out:
                now += timeout
                raise httpx.ReadTimeout("slow cleanup", request=request)
            cobblestone_stacks -= 1
            result = {"count": 64}
        elif route == "stop":
            result = {"released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot, episode_deadline_mono=12.0)
        result = json.loads(tools.mc_skill_harvest_logs(target_logs=1))

    assert result["ok"] is True
    assert result["target_gained"] == {"oak_log": 1}
    if cleanup_times_out:
        assert result["cleanup_deadline_hit"] is True
        assert calls[-2:] == ["drop", "stop"]
    else:
        assert result["inventory_cleaned"] == [{"item": "cobblestone", "count": 64}]


@pytest.mark.parametrize("timeout_after_column", [False, True])
def test_harvest_logs_chops_one_column_and_collects_at_the_end(monkeypatch, timeout_after_column) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    standing = [0.5, 64.0, 0.5]
    trunk = [[5, y, 0] for y in range(64, 68)]
    scans = []
    moves = []
    digs = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": standing, "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            scans.append(body)
            cells = [*trunk, [8, 64, 0]] if len(scans) == 1 else [[8, 64, 0]]
            result = {"blocks": [{"name": "oak_log", "pos": p, "dist": float(p[0])} for p in cells]}
        elif route == "move_to":
            moves.append(body)
            standing[:] = [4.1, 64.0, 0.5]
            clock[0] += 2.0
            result = {"pos": standing}
        elif route == "dig":
            digs.append(body)
            if len(digs) > 4:
                clock[0] += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("slow second trunk", request=request)
            clock[0] += 1.0
            collected = 3 if len(digs) == 3 else 1
            result = {"dug": "oak_log", "gained": {"oak_log": collected} if body["collect"] else {}}
        elif route == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        outcome = MacroSkills(bot).harvest_logs(
            target_logs=6 if timeout_after_column else 4, max_duration_s=14.0,
        )

    assert [d["pos"] for d in digs[:4]] == trunk
    assert [d["collect"] for d in digs[:4]] == [False, False, True, True]
    assert len(moves) == 1
    assert [moves[0]["x"], moves[0]["z"]] == [5.5, 0.5]
    assert len(scans) == 1 + timeout_after_column
    assert outcome["ok"] is True
    assert outcome["dug_count"] == 4
    assert outcome["target_gained"] == {"oak_log": 4}
    if timeout_after_column:
        assert outcome["stop_reason"] == "time_budget"
        assert outcome["partial_progress"] is True
    else:
        assert clock[0] == 6.0


@pytest.mark.parametrize("actual_logs", [2, 6])
def test_harvest_logs_checks_inventory_when_cuts_outnumber_reported_pickups(actual_logs) -> None:
    cuts = 0
    pickup_scans = 0

    def handler(request):
        nonlocal cuts, pickup_scans
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": "spruce_log", "count": min(cuts, actual_logs)}]}
        elif route == "find_blocks":
            result = {"blocks": [{"name": "spruce_log", "pos": [cuts + 1, 64, 0], "dist": 1.0}]}
        elif route == "dig":
            cuts += 1
            result = {"dug": "spruce_log", "gained": {"spruce_log": 1} if cuts <= 2 else {}}
        elif route == "get_entities":
            pickup_scans += 1
            result = {"entities": []}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=6)

    assert cuts == 6
    assert result["target_gained"] == {"spruce_log": actual_logs}
    assert result["ok"] is (actual_logs == 6)
    if actual_logs == 2:
        assert result["error"] == "partial_collection"
        assert result["partial_progress"] is True
        assert pickup_scans == 2
    else:
        assert pickup_scans == 0


@pytest.mark.parametrize("terrain", ["same_level", "one_below", "pit", "water", "no_alternative"])
def test_common_mining_batches_skip_unsafe_drops_and_try_a_reachable_face(monkeypatch, terrain) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    position = [0.5, 64.0, 0.5]
    first_cells = [[1, 64, 0], [2, 64, 0]]
    lower_cell = [0, 63, 0]
    replacement = [0, 64, 2]
    drops, moves, digs, scans = [], [], [], []
    cobble = 0

    def handle(request):
        nonlocal cobble
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": list(position), "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": cobble}]}
        elif route == "equip":
            result = {}
        elif route == "find_blocks":
            alternatives = []
            if len(digs) >= 2 and terrain != "no_alternative":
                alternatives = [replacement] if terrain in {"same_level", "one_below"} else [lower_cell, replacement]
            cells = first_cells[len(digs):] if len(digs) < 2 or terrain in {"same_level", "one_below"} else first_cells
            result = {"blocks": [
                {"name": "stone", "pos": pos, "dist": float(index + 1) if pos != lower_cell else 0.2}
                for index, pos in enumerate([*cells, *alternatives])
            ]}
        elif route == "get_block":
            x, y, z = body["pos"]
            floor_y = 62 if terrain == "one_below" and x >= 1 else 63
            result = {"name": "water" if terrain == "water" and x >= 1 and y == 64 else "stone" if y <= floor_y else "air"}
        elif route == "dig":
            digs.append(body["pos"])
            if len(digs) == 2:
                drop_y = 63.0 if terrain == "one_below" else 62.0 if terrain in {"pit", "no_alternative"} else 64.0
                drop_x = 1.72 if terrain == "one_below" else 1.5
                drops.append({"id": 4, "item": "cobblestonex2", "pos": [drop_x, drop_y, 0.5]})
            cobble += int(len(digs) == 3)
            result = {"dug": "stone", "gained": {"cobblestone": 1} if len(digs) == 3 else {}}
        elif route == "get_entities":
            scans.append((len(digs), body))
            result = {"entities": list(drops)}
        elif route == "move_to":
            assert body["range"] == 0.8
            moves.append([body["x"], body["y"], body["z"]])
            position[:] = moves[-1]
            drops.clear()
            cobble += 2
            result = {"pos": list(position)}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = json.loads(McToolset(bot).mc_skill_mine_blocks(["stone"], count=3, radius=8))
    assert scans[0] == (2, {"radius": 5, "types": ["item"]})
    if terrain in {"same_level", "one_below"}:
        assert moves == ([[1.72, 63, 0.5]] if terrain == "one_below" else [[1.5, 64, 0.5]])
        assert result["target_gained"] == {"cobblestone": 3}
    else:
        assert moves == []
        assert result["target_gained"] == ({"cobblestone": 1} if terrain != "no_alternative" else {})
    if terrain == "no_alternative":
        assert digs == first_cells
        assert result["error"] == "no_drops_collected"
        assert "改采同层" in result["approach_hint"]
        assert "mc_skill_staircase" not in result["message"]
    else:
        assert digs == [*first_cells, replacement]
        assert result["ok"] is True


def test_mine_blocks_with_wooden_pick_skips_only_unmineable_types() -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    queries = []
    digs = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": "wooden_pickaxe", "count": 1}]}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5]}
        elif route == "equip":
            assert body["item"] == "wooden_pickaxe"
            result = {}
        elif route == "find_blocks":
            queries.append(body["types"])
            result = {"blocks": [{"name": "stone", "pos": [1, 64, 0], "dist": 1.0}]}
        elif route == "dig":
            digs.append(body["pos"])
            result = {"dug": "stone", "gained": {"cobblestone": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        outcome = json.loads(toolset.mc_skill_mine_blocks(["stone", "iron_ore", "coal_ore"], count=1))

    assert outcome["ok"] is True
    assert outcome["skipped_unmineable"] == ["iron_ore"]
    assert outcome["target_gained"] == {"cobblestone": 1}
    assert queries == [["stone", "coal_ore"]]
    assert digs == [[1, 64, 0]]


@pytest.mark.parametrize("slow_route", [None, "dig", "cleanup"])
def test_actor_mine_blocks_shares_episode_budget_with_cleanup(monkeypatch, slow_route) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        is_cleanup = route == "inventory" and "dig" in calls
        calls.append(route)
        assert 0.0 < timeout <= (9.0 if route == "stop" else 8.0) - clock[0]
        if route == slow_route or (slow_route == "cleanup" and is_cleanup):
            assert timeout == pytest.approx(8.0 - clock[0])
            clock[0] += timeout
            raise httpx.ReadTimeout("offline slow mining request", request=request)
        clock[0] += min(0.5, timeout)
        responses = {
            "inventory": {"items": [{"name": "wooden_pickaxe", "count": 1, "slot": 0}]},
            "status": {"pos": [0.5, 64, 0.5]},
            "equip": {},
            "find_blocks": {"blocks": [{"name": "coal_ore", "pos": [1, 64, 0], "dist": 1}]},
            "dig": {"gained": {"coal": 1}},
            "stop": {"released": True},
        }
        return httpx.Response(200, json={"ok": True, "result": responses[route]})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        remaining = 0.904 if slow_route is None else 13.0
        tools._episode_deadline_mono = remaining
        result = json.loads(tools.mc_skill_mine_blocks(["coal_ore"], count=1))

    assert clock[0] <= remaining
    if slow_route is None:
        assert calls == []
        assert result["stop_reason"] == "episode_deadline_imminent"
    elif slow_route == "dig":
        assert calls[-2:] == ["dig", "stop"]
        assert result["ok"] is False
        assert result["stop_reason"] == "time_budget"
    else:
        assert calls[-1] == "inventory"
        assert result["ok"] is True
        assert result["target_gained"] == {"coal": 1}
        assert result["cleanup_deadline_hit"] is True


def test_mine_blocks_single_dig_cannot_outlive_slice_deadline(monkeypatch) -> None:
    """r12: one un-timed obsidian dig let mine_blocks run ~300s."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    dig_timeouts: list[float] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "find_blocks":
            result = {"blocks": [{"name": "obsidian", "pos": [1, 64, 1], "dist": 1.0}]}
        elif path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 20.0}
        elif path == "dig":
            timeout = request.extensions["timeout"]["read"]
            dig_timeouts.append(timeout)
            clock[0] += timeout
            raise httpx.ReadTimeout("offline dig still pending", request=request)
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills._dig_target_blocks(
            block_types=["obsidian"],
            count=4,
            radius=8,
            protected_msg="protected",
            below_tol=2.0,
            above_tol=2.0,
        )
    assert skills.temporary_avoidance[(1, 64, 1)]["reason"] == "dig_timeout"
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["partial_error"]["code"] == "mine_timeout"
    assert result["dug_count"] == 0
    # r14: simple stone/log digs took 8~12s; the per-dig budget must not be
    # divided down to a few seconds just because count is large.
    assert dig_timeouts and dig_timeouts[0] == pytest.approx(15.0)
    assert clock[0] <= 48.0


def test_targeted_drop_recovery_refreshes_each_stack_and_skips_unsafe_routes(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    monkeypatch.setattr(time, "sleep", lambda _: None)
    origin = [0.5, 64.0, 0.5]
    pos = list(origin)
    stacks = {
        1: {"id": 1, "item": "diamondx2", "pos": [1.5, 64.0, 0.5]},
        2: {"id": 2, "item": "diamond", "pos": [2.5, 64.0, 0.5]},
        3: {"id": 3, "item": "diamond", "pos": [-1.5, 64.0, 0.5]},
        4: {"id": 4, "item": "diamond", "pos": [1.5, 68.5, 1.5]},
        5: {"id": 5, "item": "cobblestonex32", "pos": [0.5, 64.0, 3.5]},
    }
    moves = []
    owned = 0

    def handler(request):
        nonlocal owned
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": list(pos), "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": "diamond", "count": owned}] if owned else []}
        elif route == "get_entities":
            result = {"entities": list(stacks.values())}
        elif route == "get_block":
            x, y, z = body["pos"]
            result = {"name": "stone" if y < 64 or x == -1 else "air"}
        elif route == "move_to":
            dest = [body["x"], body["y"], body["z"]]
            match = next(e for e in stacks.values() if e["pos"] == dest)
            entity_id = match["id"]
            assert entity_id in {1, 2, 4}
            moves.append(entity_id)
            pos[:] = dest
            stacks.pop(entity_id)
            owned += 2 if entity_id == 1 else 1
            if entity_id == 1:
                stacks[2]["pos"] = [3.0, 64.0, 0.5]  # Move after the first approach.
            result = {"pos": list(pos)}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        first = json.loads(toolset.mc_skill_collect_drops(item_types=["diamond"], max_pickups=6))
        assert first["gained"] == {"diamond": 3}
        assert [(row["id"], row["picked_up"]) for row in first["attempts"]] == [(1, 2), (2, 1)]
        assert first["attempts"][1]["item_pos"] == [3.0, 64.0, 0.5]
        assert {row["id"]: row["reason"] for row in first["skipped"]} == {
            3: "unsafe_route", 4: "height_difference", 5: "non_target_item",
        }
        stacks[4]["pos"] = [2.5, 64.0, 1.5]  # A new safe landing invalidates its prior rejection.
        second = json.loads(toolset.mc_skill_collect_drops(item_types=["diamond"], max_pickups=6))
        assert second["gained"] == {"diamond": 1}
        assert moves == [1, 2, 4]
        third = json.loads(toolset.mc_skill_collect_drops(item_types=["diamond"], max_pickups=6))
        assert third["gained"] == {}
        assert any(row["id"] == 3 and row["reason"] == "retry_cooldown" for row in third["skipped"])
        assert moves == [1, 2, 4]

