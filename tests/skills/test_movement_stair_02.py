from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("near_turns_clear", [True, False])
def test_scout_novelty_cannot_skip_legal_turns_after_path_failure(monkeypatch, near_turns_clear) -> None:
    from mc_agent.harness.world_memory import WorldMemory

    backend = _SurfaceScoutBackend()
    monkeypatch.setattr(
        WorldMemory, "score_direction_novelty",
        lambda self, pos, direction, **kwargs: 10 if direction in {"northeast", "southeast"} else 0,
        raising=False,
    )

    def move(request, body):
        if len(backend.moves) == 1 or not near_turns_clear:
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert all(body["x"] > 0.5 for body in backend.moves)
    if near_turns_clear:
        assert result["segments_completed"] == 5
        assert len(backend.moves) == 6
    else:
        assert len(backend.moves) == 3
        assert result["stop_reason"] == "path_not_found"
        assert result["distance_traveled"] == 0


def test_surface_scout_skips_recent_failed_routes_near_same_origin(monkeypatch) -> None:
    from mc_agent.skills import library

    now = 0.0
    monkeypatch.setattr(library.time, "monotonic", lambda: now)
    backend = _SurfaceScoutBackend()

    def move(request, body):
        return httpx.Response(200, json={
            "ok": False, "error": {"code": "path_not_found", "message": "blocked"},
        })

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        skills = MacroSkills(bot)
        assert skills.scout_surface(direction="east", step_dist=30)["stop_reason"] == "path_not_found"
        assert len(backend.moves) == 3

        now = 139.0
        backend.pos[1] += 1
        skills.scout_surface(direction="north", step_dist=30)
        assert len(backend.moves) == 5
        assert all(body["x"] <= 0.5 and body["z"] < 0.5 for body in backend.moves[3:])

        result = skills.scout_surface(direction="east", step_dist=30)
        assert result["stop_reason"] == "scout_route_cooldown"
        assert result["requires_replan"] is True
        assert len(backend.moves) == 5

        now = 181.0
        skills.scout_surface(direction="east", step_dist=30)
        assert len(backend.moves) == 8

        backend.pos[0] += 3
        skills.scout_surface(direction="east", step_dist=30)
        assert len(backend.moves) == 11


def test_surface_scout_checks_for_water_before_moving() -> None:
    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"] == [14, 63, 0]:
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["segments_completed"] == 5
    assert backend.moves[0]["z"] < 0
    assert len(backend.moves) == 5


@pytest.mark.parametrize("direction", ["auto", "east"])
def test_surface_scout_turns_inward_at_geofence_edge(direction) -> None:
    from mc_agent.harness.geofence import VoidGeofence

    backend = _SurfaceScoutBackend()
    backend.pos = [39.5, 64, 0.5]
    fence = VoidGeofence(radius=42, margin=0)
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot, geofence=fence).scout_surface(direction=direction)

    assert result["segments_completed"] > 0
    assert backend.moves[0]["x"] < 39.5
    assert all(fence.check_point(move["x"], move["z"]) is None for move in backend.moves)
    if direction == "auto":
        assert backend.moves[0]["z"] == 0.5


def test_surface_scout_turns_ninety_degrees_along_dry_bank() -> None:
    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"][0] > 2:
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["segments_completed"] == 5
    assert backend.moves[0]["x"] == 0.5
    assert backend.moves[0]["z"] == -27.5


@pytest.mark.parametrize("submerged", [False, True])
def test_surface_scout_can_take_one_short_step_out_of_shallow_water(submerged) -> None:
    backend = _SurfaceScoutBackend()
    backend.wet = True
    backend.submerged = submerged
    surfacing = []

    def handle(request):
        if request.url.path.endswith("/control"):
            surfacing.append(json.loads(request.content))
            backend.submerged = False
            return backend.ok({})
        if request.url.path.endswith("/find_blocks") and json.loads(request.content)["radius"] == 10:
            return backend.ok({"blocks": [{"name": "grass_block", "pos": [0, 63, 7]}]})
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"] in ([0, 64, 7], [0, 65, 7]):
            return backend.ok({"name": "air"})
        return backend.handle(request)

    def move(request, body):
        backend.wet = False
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert backend.moves[0]["range"] == 0.5
    assert [backend.moves[0][axis] for axis in ("x", "y", "z")] == [0.5, 64, 7.5]
    assert bool(surfacing) is submerged
    assert len(surfacing) <= 1
    assert result["shore_recoveries"] == 1
    assert result["segments_completed"] == 5
    assert result["distance_traveled"] == 147


@pytest.mark.parametrize("profile", ["full", "compact"])
def test_surface_scout_tool_registry_accepts_numeric_strings(profile) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    backend.biome = "deep_ocean"
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        registry = McToolset(bot, geofence=VoidGeofence()).registry(
            profile=profile, extra_tools=("mc_skill_scout_surface",),
        ).map_tools(lambda tool: tool)
        result = json.loads(registry.execute({
            "function": {
                "name": "mc_skill_scout_surface",
                "arguments": '{"direction":"east","max_duration_s":"45","step_dist":"30"}',
            },
        }))

    assert result["segments_completed"] == 5
    assert result["distance_traveled"] == 150
    assert result["biome"] == "deep_ocean"
    assert "方向" in result["biome_hint"]


@pytest.mark.parametrize("released", [True, False])
def test_surface_scout_confirmed_stop_before_retry_after_read_timeout(released) -> None:
    backend = _SurfaceScoutBackend()
    backend.released = released

    def move(request, body):
        if len(backend.moves) == 1:
            raise httpx.ReadTimeout("offline movement timeout", request=request)
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    actions = [route for route, _, _ in backend.calls if route in {"move_to", "stop"}]
    if released:
        assert actions[:3] == ["move_to", "stop", "move_to"]
        assert backend.moves[1]["z"] < 0
        assert result["segments_completed"] == 5
    else:
        assert actions == ["move_to", "stop"]
        assert result["stop_reason"] == "move_timeout_unreleased"
        assert result["requires_replan"] is True


@pytest.mark.parametrize("interrupted", [False, True])
def test_surface_scout_water_retreat_yields_to_hurt(interrupted) -> None:
    backend = _SurfaceScoutBackend()

    def move(request, body):
        backend.wet = len(backend.moves) == 1
        if backend.wet:
            body = {**body, "x": 3.5}
        receipt = backend.arrive(request, body)
        if backend.wet and interrupted:
            return backend.ok({"pos": backend.pos, "interrupted_by": "hurt"})
        return receipt

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    if interrupted:
        assert result["stop_reason"] == "hurt"
        assert len(backend.moves) == 1
    else:
        assert [backend.moves[1][axis] for axis in ("x", "z")] == [0.5, 0.5]
        assert backend.moves[2]["z"] < 0
        assert result["segments_completed"] == 5


def test_surface_scout_stops_for_hostile_at_landing() -> None:
    backend = _SurfaceScoutBackend()

    def move(request, body):
        backend.hostile = {"dist": 6.0, "name": "zombie"}
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["stop_reason"] == "nearby_hostile"
    assert result["distance_traveled"] == 28
    assert len(backend.moves) == 1
    assert all(pos[0] == 0.5 for route, pos, _ in backend.calls if route == "find_blocks")


@pytest.mark.parametrize("episode_remaining,cap", [(None, 50.0), (18.0, 14.0), (5.0, 0.0)])
def test_surface_scout_clamps_entire_tool_budget(monkeypatch, episode_remaining, cap) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])

    def handle(request):
        timeout = request.extensions["timeout"]["read"]
        assert 0 < timeout <= cap - clock[0]
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            assert body["timeout"] / 1000 < timeout
            step_time = min(10.0, body["timeout"] / 1000)
            clock[0] += step_time
            if step_time < 10.0:
                return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "short hop"}})
        else:
            clock[0] += min(0.2, timeout)
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        toolset._episode_deadline_mono = episode_remaining
        result = json.loads(toolset.mc_skill_scout_surface(max_duration_s=120.0))

    assert clock[0] <= cap
    if cap == 0:
        assert result["stop_reason"] == "episode_deadline_imminent"
        assert backend.calls == []
    else:
        assert result["stop_reason"] == "time_budget"
        assert result["segments_completed"] == (4 if cap == 50 else 1)


def test_staircase_first_step_uses_total_budget(monkeypatch) -> None:
    """BUG-037: a 20s first step must not die at the 15s per-step cap."""
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    pos = [0.5, 30.0, 0.5]
    delayed = [False]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            if body["pos"] == [0, 29, -1] and not delayed[0]:
                delayed[0] = True
                clock[0] += 20.0
                result = {"name": "stone"}
            else:
                result = {"name": "air"}
        elif path == "place":
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


def test_staircase_tool_caps_long_steps() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        captured = {}
        toolset.skills.dig_staircase = lambda **kwargs: captured.update(kwargs) or {"ok": True, "steps_done": 10}
        result = json.loads(toolset.mc_skill_staircase(direction="north", steps=40))
    assert captured["steps"] == 10
    assert result["steps_capped_from"] == 40
    assert result["steps_clamped"] is True


def test_staircase_tool_auto_retries_viable_direction_on_zero_progress(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset._episode_deadline_mono = 109.0
        calls = []

        def fake_dig(**kwargs):
            assert kwargs["deadline"] == 106.0
            calls.append(kwargs["direction"])
            if kwargs["direction"] == "north":
                clock[0] += 4.0
                return {
                    "ok": False, "stop_reason": "movement_not_confirmed",
                    "steps_done": 0, "dug_count": 0,
                    "viable_directions": ["south", "east"],
                }
            return {"ok": True, "steps_done": 1, "stop_reason": "completed"}

        toolset.skills.dig_staircase = fake_dig
        result = json.loads(toolset.mc_skill_staircase(direction="north", steps=6))
    assert calls == ["north", "south"]
    assert result["ok"] is True
    assert result["auto_retry_direction"] == "south"


def test_staircase_tool_skips_action_with_no_episode_budget(monkeypatch) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: 100.0)

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence(radius=100))
        toolset._episode_deadline_mono = 105.0
        result = json.loads(toolset.mc_skill_staircase(vertical="pillar"))

    assert result["stop_reason"] == "episode_deadline_imminent"
    assert result["steps_done"] == 0


@pytest.mark.parametrize("direction,vertical", [(None, "up"), ("pillar", "pillar")])
def test_staircase_tool_shares_episode_budget_with_direction_and_fence_probes(monkeypatch, direction, vertical) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    paths = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        paths.append(path)
        assert path in {"status", "inventory", "get_block"}
        timeout = request.extensions["timeout"]["read"]
        assert timeout <= 106.0 - clock[0]
        clock[0] += timeout if direction == "pillar" else min(1.5, timeout)
        result = {
            "status": {"pos": [0.5, 30, 0.5], "on_ground": True},
            "inventory": {"items": [{"name": "cobblestone", "count": 64}]},
            "get_block": {"name": "stone"},
        }
        if direction == "pillar":
            raise httpx.ReadTimeout("offline preflight timeout", request=request)
        return httpx.Response(200, json={"ok": True, "result": result[path]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence(radius=100))
        toolset._episode_deadline_mono = 109.0
        result = json.loads(toolset.mc_skill_staircase(direction=direction, vertical=vertical))

    assert result["stop_reason"] == "time_budget"
    assert result["steps_done"] == 0
    assert clock[0] == 106.0
    assert ("get_block" in paths) is (direction is None)


def test_tunnel_triggers_retention_cleanup() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state = {"cobblestone": 300}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5]}})
        if path == "inventory":
            rows = []
            left = state["cobblestone"]
            while left > 0:
                rows.append({"name": "cobblestone", "count": min(64, left)})
                left -= min(64, left)
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "drop":
            state["cobblestone"] -= int(body.get("count") or 0)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.skills.dig_level_tunnel = lambda *args, **kwargs: {"ok": True, "steps_done": 1}
        result = json.loads(toolset.mc_skill_tunnel("north", 1))
    assert result["inventory_cleaned"]
    assert state["cobblestone"] == 192


def test_staircase_step0_dig_uses_budget_beyond_15s(monkeypatch) -> None:
    """BUG-037/r24: step 0 stalled ~28s in dig even with a stone pickaxe."""
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    dig_timeouts = []
    pos = [0.5, 36.0, 0.5]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "stone_pickaxe"}}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] <= 36 else "air"}
        elif path == "dig":
            dig_timeouts.append(request.extensions["timeout"]["read"])
            if len(dig_timeouts) == 1:
                # The first dig stalls ~28s: only the raised step-0 cap can wait.
                clock[0] += 28.0
            result = {}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": list(pos), "moved": 1.0}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="down", steps=1)
    assert dig_timeouts and dig_timeouts[0] > 15.0
    assert result["steps_done"] >= 1


@pytest.mark.parametrize("outer_budget,step_budget,expected", [(2.0, 10.0, 2.0), (45.0, 1.0, 1.0)])
def test_staircase_inherits_parent_deadline_and_caps_single_step(monkeypatch, outer_budget, step_budget, expected) -> None:
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 30, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            clock[0] += min(0.5, request.extensions["timeout"]["read"])
            result = {"name": "stone"}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=25, deadline=100.0 + outer_budget, per_step_timeout=step_budget)
    assert result["stop_reason"] == "time_budget"
    assert clock[0] == pytest.approx(100.0 + expected)
    assert calls[-1] == "stop"
    assert "dig" not in calls


@pytest.mark.parametrize("filler", ["stone", "netherrack", "tuff"])
def test_stair_direction_hints_share_filler_rules_and_avoid_recent_failure(filler) -> None:
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        assert path == "get_block"
        return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills._record_failed_stair_dir((0, 30, 0, "up"), "north", "movement_not_confirmed")
        result = skills._probe_viable_up_stair_dirs(0, 30, 0, inv_counts={filler: 8})
        assert result == ["south", "east", "west"]
        # A down-step over open air is rejected by dig_staircase even with
        # filler available, so a hint must not claim that route is executable.
        down = skills._probe_viable_down_stair_dirs(0, 30, 0, inv_counts={filler: 8})
        assert down == []


def test_pillar_deadline_clears_persistent_jump_posture(monkeypatch) -> None:
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    posture = []
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            if posture and posture[-1]["jump"]:
                clock[0] += request.extensions["timeout"]["read"]
            result = {"pos": [0.5, 30, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            posture.append(body)
            result = {}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(vertical="pillar", deadline=100.5, per_step_timeout=0.2)
    assert result["stop_reason"] == "time_budget"
    assert clock[0] == pytest.approx(100.2)
    assert [item["jump"] for item in posture] == [True, False]
    assert calls[-2:] == ["stop", "posture"]


@pytest.mark.parametrize("failure_code", ["place_failed", "no_support"])
def test_pillar_retries_transient_place_failures(monkeypatch, failure_code) -> None:
    """r14/BUG-024: first one-step pillar jump-place failed, next attempt worked."""
    import mc_agent.skills.library as library

    clock = [500.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    jumping = [False]
    placed = [False]
    place_calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            y = 31.2 if jumping[0] else (31.0 if placed[0] else 30.0)
            result = {"pos": [0.5, y, 0.5], "on_ground": not jumping[0], "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            result = {}
        elif path == "place":
            place_calls.append(body)
            if len(place_calls) <= 2:
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": failure_code, "message": "placement refused"}}
                )
            placed[0] = True
            result = {"pos": body["pos"]}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(vertical="pillar", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert result["placed_count"] == 1
    assert len(place_calls) == 3


def test_failed_pillar_blocks_landing_cell_not_airborne_cell(monkeypatch) -> None:
    """r12: a failed jump-place was remembered at y+1, so the next call still
    advertised viable_pillar_up=true at the same grounded cell."""
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "monotonic", lambda: 500.0)
    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    jumping = [False]
    status_reads = [0]
    place_calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            # First read is the grounded start; the r12 failure returned while
            # the jump/place attempt was still at y=31.2.
            y = 30.0 if status_reads[0] == 0 else 31.2
            status_reads[0] += 1
            result = {
                "pos": [0.5, y, 0.5],
                "on_ground": status_reads[0] == 1,
                "alive": True,
                "ready": True,
                "health": 20.0,
            }
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            result = {}
        elif path == "place":
            place_calls.append(body)
            return httpx.Response(
                409, json={"ok": False, "error": {"code": "place_failed", "message": "the block is still air"}}
            )
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.dig_staircase(vertical="pillar", steps=1)
        assert result["stop_reason"] == "place_failed"
        assert place_calls
        assert skills._probe_viable_pillar_up(0, 30, 0, inv_counts={"cobblestone": 8}) is False


def test_staircase_timeout_preserves_confirmed_partial_excavation() -> None:
    blocks = {}
    dig_count = [0]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 30, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": blocks.get(tuple(body["pos"]), "stone")}
        elif path == "dig":
            dig_count[0] += 1
            if dig_count[0] == 2:
                raise httpx.ReadTimeout("second dig pending", request=request)
            blocks[tuple(body["pos"])] = "air"
            result = {"dug": "stone"}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=3, stop_on_ore=False)
    assert result["stop_reason"] == "time_budget"
    assert result["dug_count"] == 1
    assert result["steps_done"] == 0
    assert result["pos_before"] == [0.5, 30, 0.5]
    assert result["pos_after"] == [0.5, 30, 0.5]

