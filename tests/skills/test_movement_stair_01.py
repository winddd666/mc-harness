from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

def test_harvest_logs_prefers_low_trunk_over_closer_canopy() -> None:
    low = [5, 64, 0]
    high = [2, 67, 0]
    attempted = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.4, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [
                {"pos": high, "name": "oak_log", "dist": 2.0},
                {"pos": low, "name": "oak_log", "dist": 5.0},
            ]}
        elif route == "dig":
            attempted.append(json.loads(request.content)["pos"])
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=1)

    assert attempted == [low]
    assert result["ok"] is True
    assert result["target_gained"] == {"oak_log": 1}


@pytest.mark.parametrize("filler", ["stone", "netherrack", "blackstone", "cobblestone"])
def test_staircase_up_uses_available_solid_filler(filler) -> None:
    pos = [0.5, 30.0, 0.5]
    placed = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": filler, "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placed.append(body)
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert placed == [{"item": filler, "pos": [0, 30, -1]}]


def test_staircase_up_retries_another_filler_when_server_rejects_item() -> None:
    """r13: count listed cobblestone but place returned missing_item."""
    pos = [0.5, 30.0, 0.5]
    placed = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}, {"name": "netherrack", "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placed.append(body["item"])
            if body["item"] == "cobblestone":
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": "missing_item", "message": "no cobblestone in inventory"}}
                )
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert placed == ["cobblestone", "netherrack"]


def test_staircase_cools_down_stand_after_zero_progress_dig_timeout(monkeypatch) -> None:
    """BUG-041/r23: same stand kept burning 45s per direction with 0 progress."""
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "monotonic", lambda: 1000.0)
    pos = [0.5, 64.0, 0.5]
    digs = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "diamond_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "diamond_pickaxe"}}
        elif path == "get_block":
            result = {"name": "basalt" if body["pos"][1] <= 66 else "air"}
        elif path == "dig":
            digs.append(body["pos"])
            raise httpx.ReadTimeout("offline dig stall", request=request)
        elif path == "move_to":
            result = {"pos": list(pos)}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": list(pos), "moved": 0.0}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        first = skills.dig_staircase(direction="north", vertical="up", steps=3)
        digs_after_first = len(digs)
        second = skills.dig_staircase(direction="south", vertical="up", steps=3)
    assert first["stop_reason"] == "time_budget"
    assert digs_after_first >= 1
    assert second["stop_reason"] == "stair_cell_cooldown"
    assert len(digs) == digs_after_first
    assert "8~12 格" in second["hint"]


def test_staircase_fails_fast_on_previously_timed_out_dig_cell(monkeypatch) -> None:
    """BUG-041/r23: retrying the same timed-out cell burned 15-45s each time."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    clock[0] = 1000.0
    digs = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "stone_pickaxe"}}
        elif path == "get_block":
            result = {"name": "blackstone" if body["pos"][1] <= 66 else "air"}
        elif path == "dig":
            digs.append(body["pos"])
            result = {}
        elif path == "move_to":
            result = {"pos": [0.5, 64.0, 0.5]}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 64.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills._avoid_temporarily((0, 65, 0), "dig_timeout", 30.0)
        result = skills.dig_staircase(direction="north", vertical="up", steps=3)
    assert result["ok"] is False
    assert result["stop_reason"] == "dig_cell_avoided"
    assert result["last_error"]["code"] == "dig_cell_avoided"
    assert digs == []
    assert "8~12 格" in result["hint"]


def test_staircase_up_falls_back_to_pillar_when_step_has_no_face(monkeypatch) -> None:
    """BUG-024: a self-dug shaft step cell has no six-face support."""
    pos = [0.5, 30.0, 0.5]
    jumping = [False]
    jumps_from = [30.0]
    places = []
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            y = jumps_from[0] + 1.2 if jumping[0] else pos[1]
            result = {"pos": [pos[0], y, pos[2]], "on_ground": not jumping[0], "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            if jumping[0]:
                jumps_from[0] = pos[1]
            result = {}
        elif path == "place":
            pos[1] += 1
            places.append(body["pos"])
            result = {"pos": body["pos"]}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.dig_staircase(direction="north", vertical="up", steps=1)
        repeated = skills.dig_staircase(direction="east", vertical="up", steps=1)
        blocked = skills.dig_staircase(direction="north", vertical="up", steps=1)
        assert len(places) == 2
        clock[0] = 121.0
        expired = skills.dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["stop_reason"] == "pillar_fallback"
    assert result["fallback"] == "pillar"
    assert result["steps_done"] == 1
    assert result["placed_count"] == 1
    assert result["requires_replan"] is False
    assert repeated["ok"] is True and repeated["requires_replan"] is True
    assert blocked["stop_reason"] == "pillar_fallback_cooldown"
    assert blocked["steps_done"] == 0
    assert blocked["requires_replan"] is True
    assert "120 秒" in blocked["message"] and "mc_skill_unstick" in blocked["message"]
    assert blocked["last_error"]["message"] == blocked["message"]
    assert expired["stop_reason"] == "pillar_fallback"
    assert expired["requires_replan"] is False


@pytest.mark.parametrize("scene", ["auto_east", "explicit_north", "horizontal_exit"])
def test_pillar_cooldown_uses_a_probed_stair_or_safe_lateral_exit(monkeypatch, scene) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    position = [0.5, 30.0, 0.5]
    blocks = {(0, 29, 0): "stone", (0, 31, -1): "stone"}
    if scene == "horizontal_exit":
        blocks[(1, 29, 0)] = "stone"
    else:
        blocks[(1, 30, 0)] = "stone"
    writes = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": list(position), "dimension": "overworld", "on_ground": True, "health": 20}
        elif route == "inventory":
            result = {"items": [] if scene == "horizontal_exit" else [{"name": "cobblestone", "count": 4}]}
        elif route == "get_block":
            result = {"name": blocks.get(tuple(body["pos"]), "air")}
        elif route == "move_to" and scene == "auto_east":
            writes.append(route)
            position[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(position)}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence(radius=10))
        tools.skills._pillar_fallbacks[("overworld", 0, 0)] = (2, 100.0)
        direction = "" if scene == "auto_east" else "north"
        result = json.loads(tools.mc_skill_staircase(direction=direction, vertical="up", steps=1, stop_on_ore=False))

    if scene == "auto_east":
        assert result["ok"] and result["direction"] == "east" and result["steps_done"] == 1
        assert writes == ["move_to"] and position == [1.5, 31, 0.5]
    else:
        assert not result["ok"] and result["stop_reason"] == "pillar_fallback_cooldown"
        assert result["steps_done"] == result["dug_count"] == 0 and not writes
        assert "100 秒" in result["message"]
        if scene == "horizontal_exit":
            assert result["viable_up_directions"] == []
            assert result["recovery_move"] == {"x": 1.5, "y": 30, "z": 0.5, "range": 0.3}
            assert "mc_move_to" in result["message"]
        else:
            assert result["viable_up_directions"] == ["east"]
            assert "direction='east'" in result["message"] and "recovery_move" not in result


def test_staircase_up_retries_transient_place_failed() -> None:
    """r14: up-step filler was refused once, then a retry succeeded."""
    pos = [0.5, 30.0, 0.5]
    placement_attempts = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placement_attempts.append(body)
            if len(placement_attempts) <= 2:
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": "place_failed", "message": "the block is still air"}}
                )
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert len(placement_attempts) == 3


@pytest.mark.parametrize(
    "timeout_path,episode_budget",
    [("get_block", None), ("move_to", None), ("dig", None), ("dig", 9.0)],
)
def test_staircase_deadline_covers_inner_calls_and_stops_pending_action(monkeypatch, timeout_path, episode_budget) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    blocks = {}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        timeout = request.extensions["timeout"]["read"]
        if path == timeout_path:
            # BUG-037/r24: the step-0 dig may use up to 40s of the session
            # budget instead of the old flat 15s cap; nothing may exceed it.
            limit = 40 if timeout_path == "dig" else 15
            if episode_budget is not None:
                limit = episode_budget - 3.0 - clock[0]
            assert 0 < timeout <= limit + 0.01
            if path == "get_block":
                # Reading safety probes also consumes the same total budget.
                clock[0] += 3
            else:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline pending action", request=request)
        if path == "status":
            result = {"pos": [0.5, 30.0, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": blocks.get(tuple(body["pos"]), "stone")}
        elif path == "dig":
            blocks[tuple(body["pos"])] = "air"
            result = {}
        elif path == "stop":
            assert timeout <= 1
            result = {"stopped": True, "released": True}
        elif path == "move_to":
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        original_call = bot.call
        if episode_budget is None:
            skills = MacroSkills(bot)
            result = skills.dig_staircase(direction="north", steps=25, stop_on_ore=False)
        else:
            toolset = McToolset(bot, geofence=VoidGeofence())
            toolset._episode_deadline_mono = episode_budget
            skills = toolset.skills
            result = json.loads(toolset.mc_skill_staircase(direction="north", steps=6, stop_on_ore=False))
        assert skills.bot is bot
        assert bot.call == original_call
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["action_released"] is True
    assert clock[0] <= 45
    assert calls[-1] == "stop"
    assert calls.count("stop") == 1
    if timeout_path == "dig":
        assert result["dig_target"]
        if episode_budget is None:
            assert "mc_skill_unstick" in result["error"]["message"]
        else:
            assert clock[0] == episode_budget - 3.0
            assert skills.failed_stair_dirs == {}
            assert skills.temporary_avoidance == {}


def test_staircase_retries_one_slow_read_before_budget_failure(monkeypatch) -> None:
    """r12: a slow first status aborted as time_budget with zero steps."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls = [0]
    read_timeouts: list[float] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls[0] += 1
        assert path == "status"
        read_timeouts.append(request.extensions["timeout"]["read"])
        if calls[0] == 1:
            clock[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("slow status", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 30.0, 0.5]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        session = library._StairSession(bot, 10.0)
        status = session.status()
        session.call("status", timeout_s=1.5)
    assert status["pos"] == [0.5, 30.0, 0.5]
    assert calls[0] == 3
    assert session.last_timeout_path is None
    assert session.last_retry_path == "status"
    assert read_timeouts[-1] == pytest.approx(1.5)


def test_staircase_time_budget_reports_timed_out_read(monkeypatch) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "status":
            clock[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("slow status", request=request)
        if path == "stop":
            return httpx.Response(200, json={"ok": True, "result": {"stopped": True, "released": True}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=1, stop_on_ore=False)
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["timeout_path"] == "status"
    assert result["retried_path"] == "status"
    assert result["action_released"] is True


@pytest.mark.parametrize("refills", [1, 100])
def test_staircase_down_verifies_refilled_floor_before_moving(monkeypatch, refills) -> None:
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    pos = [0.5, 30.0, 0.5]
    digs = []
    moves = []
    floor = [0, 29, -1]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            cell = body["pos"]
            name = "stone" if cell[1] <= 28 else "air"
            if cell == floor:
                name = "gravel" if len(digs) <= refills else "air"
            result = {"name": name}
        elif path == "dig":
            assert body["pos"] == floor
            digs.append(body)
            result = {}
        elif path == "move_to":
            moves.append(body)
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=1, stop_on_ore=False)
    if refills == 1:
        assert result["ok"] is True
        assert len(digs) == 2
        assert len(moves) == 1
    else:
        assert result["ok"] is False
        assert result["stop_reason"] == "dig_failed"
        assert result["last_error"]["code"] == "stair_cell_not_cleared"
        assert len(digs) == 3
        assert moves == []


@pytest.mark.parametrize("target,segments", [(None, 5), ("dirt_path", 5), ("bell", 2), ("villager", 2)])
def test_surface_scout_continues_and_scans_until_target(target, segments) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    backend.target = target
    arguments = {"target_types": ["minecraft:villager"]} if target == "villager" else {}
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        registry = toolset.registry(profile="compact", extra_tools=("mc_skill_scout_surface",))
        result = json.loads(registry.execute({
            "function": {"name": "mc_skill_scout_surface", "arguments": json.dumps(arguments)},
        }))
        assert toolset._last_block_pos == [28 * segments, 64, 0]

    assert result["ok"] is True
    hit = target in {"bell", "villager"}
    assert result["stop_reason"] == ("target_found" if hit else "segment_limit")
    assert result["segments_completed"] == len(backend.moves) == segments
    assert result["distance_traveled"] == 28 * segments
    assert result["final_pos"] == [28 * segments + 0.5, 64, 0.5]
    assert sum(route == "get_entities" for route, _, _ in backend.calls) == segments + 1
    assert [row["name"] for row in result["detected_pois"]].count("obsidian") == 1
    if hit:
        key = "detected_entities" if target == "villager" else "detected_pois"
        match = next(row for row in result[key] if row["name"] == target)
        assert match["pos"] == [62, 64, 1]
        assert match["dist"] == 5.5
        assert match["biome"] == "meadow"
        assert match["observer_biome"] == "plains"


@pytest.mark.parametrize("vector,direction", [
    ([1, 0], "east"), ([1, 1], "southeast"), ([-0.1, 0.8], "south"),
    ([-0.6, 0.5], "southwest"), ([-1, 0], "west"), ([-1, -1], "northwest"),
    ([0, -1], "north"), ([0.6, -0.5], "northeast"),
    ([0, 0], None), (None, None), ([True, 1], None),
])
def test_ender_eye_reports_scout_direction_from_observed_vector(vector, direction) -> None:
    from mc_agent.harness.mc_tools import McToolset

    throw = {"thrown": True, "projectile": "eye_of_ender", "bearing_deg": 6}
    if vector is not None:
        throw["eye_vector"] = vector

    def handle(request):
        assert request.url.path.endswith("/use_item")
        assert json.loads(request.content) == {"item": "minecraft:ender_eye"}
        return httpx.Response(200, json={"ok": True, "result": throw})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        receipt = json.loads(McToolset(bot).mc_use_item("minecraft:ender_eye"))

    assert receipt["ok"] is True
    result = receipt["result"]
    assert result["scout_direction"] == direction
    assert all(result[key] == value for key, value in throw.items())
    if direction is None:
        assert "重新测向" in result["scout_hint"]
    else:
        assert f"mc_skill_scout_surface(direction='{direction}', track_ender_eye=True)" in result["scout_hint"]


@pytest.mark.parametrize("slow_route", ["imminent", "dig", "cleanup"])
def test_actor_tunnel_shares_episode_deadline_across_preflight_and_cleanup(monkeypatch, slow_route) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    pos = [0.5, 64, 0.5]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(route)
        allowed = (2.1 if route == "stop" else 1.575) - clock[0]
        timeout = request.extensions["timeout"]["read"]
        assert 0.0 < timeout <= allowed + 1e-6
        if route == slow_route or (slow_route == "cleanup" and route == "inventory" and calls.count(route) == 3):
            clock[0] += timeout
            raise httpx.ReadTimeout("offline tunnel timeout", request=request)
        clock[0] += min(0.02, timeout)
        if route == "get_block":
            name = "stone" if body["pos"][1] == 63 or (slow_route == "dig" and body["pos"] == [1, 65, 0]) else "air"
            return _SurfaceScoutBackend.ok({"name": name})
        if route == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            return _SurfaceScoutBackend.ok({"pos": pos})
        responses = {
            "inventory": {"items": [{"name": "stone_pickaxe", "count": 1}]},
            "status": {"pos": pos, "held": "stone_pickaxe", "on_ground": True, "health": 20},
            "stop": {"released": True},
        }
        return _SurfaceScoutBackend.ok(responses[route])

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence(radius=30))
        tools._episode_deadline_mono = 6.0 if slow_route == "imminent" else 6.1
        result = json.loads(tools.mc_skill_tunnel("east", steps=1))

    assert clock[0] <= 2.1 + 1e-6
    if slow_route == "imminent":
        assert calls == []
        assert result["stop_reason"] == "episode_deadline_imminent"
    elif slow_route == "dig":
        assert calls[0] == "status"
        assert calls[-2:] == ["dig", "stop"]
        assert result["stop_reason"] == "time_slice"
        assert result["stop_confirmed"] is True
    else:
        assert calls[-1] == "inventory" and calls.count("inventory") == 3
        assert result["ok"] is True and result["steps_done"] == 1
        assert result["cleanup_deadline_hit"] is True


@pytest.mark.parametrize(
    "target_types,chest_y,village_marker,expected_segments",
    [(None, 15, None, 5), (None, 64, None, 5), (None, 15, "bell", 2), (["chest"], 15, None, 0)],
)
def test_surface_scout_keeps_chest_observations_without_ending_village_search(
    target_types, chest_y, village_marker, expected_segments,
) -> None:
    backend = _SurfaceScoutBackend()
    backend.target = village_marker
    chest_queries = []

    def handle(request):
        if request.url.path.endswith("/find_blocks"):
            types = json.loads(request.content)["types"]
            if "chest" in types:
                chest_queries.append(types)
                return backend.ok({"blocks": [{"name": "chest", "pos": [1, chest_y, 0]}]})
            if types == ["dirt_path"]:
                return backend.ok({"blocks": [{"name": "dirt_path", "pos": [2, 64, 0]}]})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east", target_types=target_types)

    assert result["segments_completed"] == len(backend.moves) == expected_segments
    assert chest_queries and all(types == ["chest"] for types in chest_queries)
    observed = {row["name"]: row for row in result["detected_pois"]}
    assert observed["chest"]["height_delta"] == chest_y - 64
    assert result["stop_reason"] == ("target_found" if target_types or village_marker else "segment_limit")
    if target_types is None:
        assert "dirt_path" in observed


@pytest.mark.parametrize("valid_targets", [[], ["bell", "villager"]])
def test_surface_scout_recovers_from_unknown_block_in_target_list(valid_targets) -> None:
    backend = _SurfaceScoutBackend()
    backend.target = "bell"
    rejected = "d食物_path"
    queries = []

    def handle(request):
        if request.url.path.endswith("/find_blocks"):
            types = json.loads(request.content)["types"]
            queries.append(types)
            if rejected in types:
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "bad_args", "message": f"unknown block {rejected}"},
                })
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(target_types=[rejected, *valid_targets])

    assert result["ignored_types"] == [rejected]
    assert all(rejected not in query for query in queries[1:])
    if valid_targets:
        assert result["stop_reason"] == "target_found"
        assert result["segments_completed"] == 2
        assert any(row["name"] == "bell" for row in result["detected_pois"])
    else:
        assert result["stop_reason"] == "invalid_target_types"
        assert not backend.moves


def test_surface_scout_detours_around_fence_and_server_timeout() -> None:
    from mc_agent.harness.geofence import VoidGeofence

    backend = _SurfaceScoutBackend()
    backend.pos = [15.5, 64, 0.5]
    fence = VoidGeofence(radius=42, margin=0)

    def move(request, body):
        if len(backend.moves) == 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "path timeout"}})
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot, geofence=fence).scout_surface(direction="east")

    assert result["segments_completed"] >= 1
    assert backend.moves[0]["z"] < 0 < backend.moves[1]["z"]
    assert all(fence.check_point(body["x"], body["z"]) is None for body in backend.moves)

