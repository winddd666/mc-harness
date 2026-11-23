from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_r18_lava_airborne_swim_timeout_and_creeper_flee_timeout_hardening(monkeypatch):
    """Reproduces r18_22bot hy4_hy_6_nether_bastion & glm_glm_4_brewing_alchemy:
    1) Airborne bob above lava (`in_lava=False, on_ground=False` over lava) must NOT exit ok=True prematurely; continue to place filler block.
    2) Transient ReadTimeout in lava `control` or creeper `flee.move_to` must `/stop` and continue to cobblestone place / creeper shield latch.
    """
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)

    # 1) Airborne bob above lava + ReadTimeout during lava_surface_swim -> /stop & bridge cobblestone before returning ok=True
    state_l: dict[str, Any] = {
        "pos": [237.5, 34.3, 196.3],
        "in_lava": True,
        "on_ground": False,
        "placed_solid": set(),
    }
    lava_actions: list[str] = []

    class _AirborneLavaBot:
        def status(self):
            return {
                "pos": list(state_l["pos"]),
                "health": 16.0,
                "alive": True,
                "ready": True,
                "in_lava": state_l["in_lava"],
                "on_ground": state_l["on_ground"],
                "yaw": 0.64,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            lava_actions.append(action)
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in state_l["placed_solid"]:
                    return {"name": "cobblestone"}
                if p == (237, 35, 196) and state_l["in_lava"]:
                    return {"name": "lava"}
                if p[1] == 34:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "stop" or (action == "control" and pl == {"wait": False}):
                assert kwargs["timeout_s"] <= 0.5
                assert kwargs["allow_reauth"] is False
            if action == "control":
                # First swim call simulates transient ReadTimeout; jump-sprint launches mid-air above lava!
                if pl.get("ms") in {950, 1400}:
                    assert pl["ms"] / 1000 < kwargs["timeout_s"] <= 2.0
                    assert kwargs["allow_reauth"] is False
                if pl.get("ms") == 950 and lava_actions.count("control") == 1:
                    assert pl["jump"] is True
                    assert pl["forward"] is False and pl["sprint"] is False
                    raise httpx.ReadTimeout("simulated swim control timeout")
                if pl.get("forward"):
                    state_l["pos"] = [234.7, 36.0, 193.8]
                    state_l["in_lava"] = False
                    state_l["on_ground"] = False
                return {"ok": True}
            if action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                state_l["placed_solid"].add(p)
                state_l["in_lava"] = False
                if p in {(234, 35, 193), (234, 34, 193)}:
                    state_l["pos"] = [234.5, float(p[1] + 1), 193.5]
                    state_l["on_ground"] = True
                return {"ok": True}
            return {"ok": True}

    out_l = CombatEnv(_AirborneLavaBot()).escape_lava()
    assert out_l["ok"] is True and out_l["in_lava"] is False
    assert "lava_surface_swim_timeout" in out_l["actions"]
    assert "lava_jump_sprint" in out_l["actions"]
    assert "lava_place_cobblestone" in out_l["actions"]
    assert "stop" in lava_actions
    assert state_l["placed_solid"]  # Did NOT exit ok=True while airborne at y=36 above y=34 lava!

    # 2) Creeper evade: move_to ReadTimeout inside flee() must /stop and still latch shield (`latched_guard=True`)
    creeper_calls: list[tuple[str, dict]] = []

    class _CreeperFleeTimeoutBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 19.0,
                "food": 17,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "blocking": False,
                "nearest_hostile": {"id": 88, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0},
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            creeper_calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 88, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0}]}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "move_to":
                raise httpx.ReadTimeout("simulated creeper flee pathfinder timeout")
            return {"ok": True}

    out_c = SubcorticalInstinctController().on_encounter(
        CombatEnv(_CreeperFleeTimeoutBot()),
        {"source": "pre_hurt_creeper", "primary_entity_id": 88, "primary_type": "creeper", "primary_pos": [2.5, 64.0, 0.5], "distance": 2.0},
    )
    assert out_c["mode"] == "creeper_evade"
    assert out_c["latched_guard"] is True
    assert any(a == "stop" for a, _ in creeper_calls)
    assert any(a == "posture" and pl_c.get("shield") is True for a, pl_c in creeper_calls)


@pytest.mark.parametrize("depth", ["deep", "shallow"])
def test_overworld_lava_poll_uses_water_only_after_reaching_the_surface(depth):
    status = {
        "pos": [11105.2, 66 if depth == "deep" else 67, 11111.4], "health": 10, "food": 20,
        "dimension": "overworld", "ready": True, "alive": True, "in_lava": True, "on_ground": False,
    }
    commands = []

    class PoolBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "water_bucket", "count": 1}]}
            if action == "events":
                return {"events": []}
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "lava" if (x, z) == (11105, 11111) and y <= 67 else "air"}
            if action == "control":
                assert body["jump"] is True and body["forward"] is False
                commands.append("swim")
                status["pos"] = [11105.2, 67, 11111.4]
                return {"ok": True}
            if action == "interact":
                commands.append("water")
                assert body == {"pos": [11105, 66, 11111], "item": "water_bucket"}
                status.update(in_lava=False, in_water=True, on_ground=True)
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(PoolBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    escape, = guard.poll()["actions"]

    assert commands == (["swim", "water"] if depth == "deep" else ["water"])
    assert escape["action"] == "escape_lava" and escape["ok"]


def test_deep_lava_keeps_swimming_up_while_the_head_is_submerged():
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 30.0, 0.5], "in_lava": True, "alive": True,
        "ready": True, "on_ground": False, "dimension": "the_nether",
    }
    controls = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (0, 0) and y <= 33:
                    return {"name": "lava"}
                return {"name": "netherrack" if y <= 32 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                controls.append(body["forward"])
                if body["forward"]:
                    assert status["pos"][1] >= 33
                    status.update(in_lava=False, on_ground=True, pos=[2.5, 33.0, 0.5])
                else:
                    status["pos"] = [0.5, status["pos"][1] + 1.0, 0.5]
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()
    assert controls == [False, False, False, True]
    assert result["ok"] is True and result["in_lava"] is False


@pytest.mark.parametrize("threat", ["clear", "nearest_drowned", "other_nearest", "scan_timeout"])
def test_recent_trident_hit_defers_food_until_the_projectile_threat_passes(monkeypatch, threat):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [9261.3, 64, -11343.7], "health": 5.07, "food": 17,
        "dimension": "overworld", "on_ground": True, "alive": True, "ready": True, "blocking": False,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "trident"}]
    dishes = []
    drowned = {"id": 5922198, "name": "drowned", "dist": 8.5}
    if threat == "nearest_drowned":
        status["nearest_hostile"] = drowned
    if threat == "other_nearest":
        status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 7.0}

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 2}]}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.5
                if threat == "scan_timeout":
                    raise httpx.ReadTimeout("hostile scan unavailable")
                return {"entities": [drowned] if threat == "other_nearest" else []}
            if action == "use_item":
                dishes.append(body["item"])
            return {"ok": True}

    bot = RiverBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()

    def eat():
        return json.dumps(bot.call("use_item", {"item": "cooked_beef"}))

    result = json.loads(guard.run_tool_with_reflex("mc_use_item", eat))
    assert result["error"]["code"] == "unsafe_to_eat"
    assert guard.poll() is None and dishes == []

    clock[0] += 8.1
    if threat != "clear":
        result = json.loads(guard.run_tool_with_reflex("mc_use_item", eat))
        assert result["error"]["code"] == "unsafe_to_eat"
        assert guard.poll() is None and dishes == []
        clock[0] += 22.0
    assert guard.poll()["actions"][0]["action"] == "eat"
    assert dishes == ["cooked_beef"]


@pytest.mark.parametrize("bridge_probe", ["normal", "timeout", "respawned"])
def test_lava_terrain_probe_reserves_time_for_a_bridge_when_swimming_stalls(monkeypatch, bridge_probe):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {"pos": [3183.5, 64.4, -2045.3], "dimension": "the_nether", "health": 20,
              "in_lava": True, "on_ground": False, "alive": True, "ready": True}
    actions = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_block":
                timeout = kwargs.get("timeout_s", 0.5)
                if not actions:
                    clock[0] += timeout
                elif bridge_probe != "normal" and status["in_lava"]:
                    assert kwargs["allow_reauth"] is False and timeout <= 0.35
                    clock[0] += timeout
                    if bridge_probe == "respawned":
                        status.update(pos=[-415.5, 64.1, -1887.5], health=20, in_lava=False,
                                      ready=False, last_death={"t": 2})
                    raise httpx.ReadTimeout("bridge block probe stalled")
                y = body["pos"][1]
                return {"name": "lava" if y == 64 else "air"}
            if action == "find_blocks":
                clock[0] += kwargs.get("timeout_s", 2.0)
                return {"blocks": []}
            if action == "control" and body.get("forward"):
                actions.append(("swim", clock[0]))
                clock[0] += body["ms"] / 1000
            if action in {"control", "place"} and status["ready"] is False:
                pytest.fail(f"Unexpected {action} after respawn during the bridge probe")
            if action == "place":
                assert kwargs["allow_reauth"] is False and 0 < kwargs["timeout_s"] <= 0.7
                actions.append(("bridge", clock[0]))
                status.update(in_lava=False, on_ground=True, pos=[3183.5, 65.0, -2045.3])
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()

    assert ("lava_bridge_probe_incomplete" in result["actions"]) is (bridge_probe != "normal")
    if bridge_probe == "respawned":
        assert result["ok"] is False and result["respawned"] is True
        assert result["pos_after"] == status["pos"]
        assert [action for action, _ in actions] == ["swim"]
        return

    assert result["ok"] is True and result["in_lava"] is False
    assert [action for action, _ in actions] == ["swim", "bridge"]
    assert actions[-1][1] < 1005.0


@pytest.mark.parametrize("placement_works", [False, True])
def test_critical_lava_escape_attempts_bridge_before_swimming_in_an_unverified_direction(monkeypatch, placement_works):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    status = {"pos": [3183.5, 66.2, -2048.5], "dimension": "the_nether", "health": 8,
              "in_lava": True, "on_ground": False, "alive": True, "ready": True}
    actions = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "place":
                actions.append("bridge")
                if not placement_works:
                    raise ApiError("no_support", "no adjacent support")
                status.update(in_lava=False, on_ground=True, pos=[3183.5, 67, -2048.5])
            if action == "control" and body.get("forward"):
                actions.append("swim")
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()

    assert actions[0] == "bridge"
    assert result["ok"] is placement_works
    if placement_works:
        assert actions == ["bridge"]
    else:
        assert actions[-1] == "swim"


@pytest.mark.parametrize("west_blocked", [False, True])
@pytest.mark.parametrize("height_change", [0, 1.6])
def test_lava_escape_changes_unverified_swim_heading_after_stalling(monkeypatch, west_blocked, height_change):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3184.7, 67.2, -2048.3], "yaw": 0.0, "health": 20,
        "dimension": "the_nether", "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    swim_headings = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": "lava" if body["pos"][1] <= math.floor(status["pos"][1]) else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                swim_headings.append(status["yaw"])
                if status["yaw"] != 0:
                    x = 3182.5 if status["yaw"] > 0 else 3186.9
                    status.update(in_lava=False, on_ground=True, pos=[x, 67, -2048.3])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    if west_blocked:
        monkeypatch.setattr(env, "_point_allowed", lambda x, _z, **_kwargs: x >= 3184.7)
    assert env.escape_lava()["in_lava"] is True
    status["pos"] = [3184.7, 67.2 + height_change, -2048.3]
    clock[0] += 3.0
    escaped = env.escape_lava()

    assert escaped["ok"] is True
    assert swim_headings == pytest.approx([0.0, (-1 if west_blocked else 1) * math.pi / 2])


@pytest.mark.parametrize("shore_exists", [True, False])
def test_lava_retreat_checks_for_a_shore_even_when_local_ray_reads_are_slow(monkeypatch, shore_exists):
    from mc_agent.skills.combat_env import CombatEnv, aim_yaw_pitch

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    start = [1862.4, 28.0, 1809.8]
    surface = [1862.4, 29.1, 1809.8]
    bank = [1864.5, 30.0, 1810.5]
    shore = [1864, 29, 1810]
    bank_yaw = aim_yaw_pitch(surface, bank)[0]
    status = {
        "pos": start, "health": 14.9, "yaw": 0.0, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    shore_queries = []
    pulses = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                if "timeout_s" in kwargs:
                    allotted = kwargs["timeout_s"]
                    clock[0] += min(allotted, 0.28)
                    if allotted < 0.28:
                        raise httpx.ReadTimeout("slow lava terrain probe")
                if body["pos"] == shore and shore_exists:
                    return {"name": "netherrack"}
                return {"name": "lava" if body["pos"][1] <= 29 else "air"}
            if action == "find_blocks":
                shore_queries.append(kwargs["timeout_s"])
                clock[0] += 0.13
                return {"blocks": [{"name": "netherrack", "pos": shore}] if shore_exists else []}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                clock[0] += body["ms"] / 1000
                pulses.append((body["forward"], status["yaw"]))
                if not body["forward"]:
                    status["pos"] = surface
                elif shore_exists and math.isclose(status["yaw"], bank_yaw):
                    status.update(pos=bank, in_lava=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()
    assert shore_queries and 0.7 <= shore_queries[0] <= 1.5
    assert pulses[0] == (False, 0.0)
    assert result["ok"] is shore_exists
    if shore_exists:
        assert result["pos_after"] == bank
        assert pulses[1] == (True, pytest.approx(bank_yaw))
    else:
        assert result["in_lava"] is True
    assert clock[0] - 1000.0 <= 5.0


@pytest.mark.parametrize("scene", [
    "critical", "read_timeout", "status_timeout", "path_lands", "healthy", "overworld", "swim_lands", "respawn",
])
def test_lava_shore_navigation_reserves_rescue_time_when_health_drops_during_swim(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    origin = [3551.0, 15, -3765.5]
    bank = [3547.5, 17, -3765.5]
    critical = scene not in {"healthy", "overworld", "swim_lands"}
    status = {
        "pos": origin, "dimension": "overworld" if scene == "overworld" else "the_nether",
        "last_death": {"t": 1}, "health": 13, "on_fire": True,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    actions = []

    class LavaBot:
        def status(self):
            if critical and status["in_lava"] and clock[0] >= 1002.7:
                status.update(health=0, alive=False, ready=False)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                if scene == "status_timeout" and any(step[0] == "path" for step in actions):
                    assert kwargs["timeout_s"] == 0.25
                    raise httpx.ReadTimeout("shore landing result unavailable")
                return self.status()
            if action == "inventory":
                return {"items": [] if scene == "path_lands" else [{"name": "cobblestone", "count": 4}]}
            if action == "get_block":
                return {"name": "lava" if body["pos"][1] <= 15 else "air"}
            if action == "control" and body.get("forward"):
                actions.append(("swim", clock[0]))
                clock[0] += body["ms"] / 1000
                status.update(health=9 if scene == "healthy" else 5.2, pos=[3550.7, 15, -3765.4])
                if scene == "swim_lands":
                    status.update(pos=bank, in_lava=False, on_ground=True)
            if action == "move_to":
                actions.append(("path", body["timeout"], kwargs["timeout_s"]))
                if scene == "read_timeout":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("shore movement acknowledgement timed out")
                clock[0] += body["timeout"] / 1000
                if scene == "path_lands":
                    status.update(pos=bank, in_lava=False, on_ground=True)
                if scene == "respawn":
                    status.update(pos=[3520.5, 64.1, -3791.5], last_death={"t": 2}, in_lava=False, on_ground=True)
            if action == "stop":
                actions.append(("stop", kwargs["timeout_s"]))
            if action == "place":
                actions.append(("bridge", clock[0]))
                if self.status()["alive"] is False:
                    raise ApiError("dead", "the swim recovery stalled too long")
                status.update(pos=[3550.7, 16, -3765.4], in_lava=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {})
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *args, **kwargs: bank)
    result = env.escape_lava()

    paths = [action[1:] for action in actions if action[0] == "path"]
    assert paths == ([] if scene == "swim_lands" else [(500, 0.8) if critical else (1200, 2.0)])
    assert result["ok"] is (scene not in {"respawn", "status_timeout"})
    assert ("lava_shore_critical_budget" in result["actions"]) is critical
    if scene == "respawn":
        assert result["respawned"] and not any(action[0] == "bridge" for action in actions)
    if scene == "status_timeout":
        assert result["reason"] == "lava_status_unavailable" and not any(action[0] == "bridge" for action in actions)
    if scene in {"critical", "read_timeout"}:
        bridge, = [action[1] for action in actions if action[0] == "bridge"]
        assert bridge < 1002.7 and not result["in_lava"]
    if scene == "read_timeout":
        assert ("stop", 0.25) in actions


@pytest.mark.parametrize("lands_after_jump", [False, True])
def test_lava_shore_hop_checks_landing_before_requesting_pathfinder(monkeypatch, lands_after_jump):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    bank = [3568.5, 65, 1794.5]
    status = {
        "pos": [3567.3, 64.5, 1794.7], "health": 7, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    calls = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": "air"}
            if action == "control" and body.get("forward"):
                status.update(in_lava=False, pos=[3568.4, 65.3, 1794.5])
            if action == "status":
                calls.append("verify_landing")
                if lands_after_jump:
                    status.update(on_ground=True, pos=bank)
                return self.status()
            if action == "move_to":
                calls.append(action)
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {})
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *args, **kwargs: bank)
    result = env.escape_lava()
    assert result["ok"] is lands_after_jump
    assert calls == (["verify_landing"] if lands_after_jump else ["verify_landing", "move_to"])
    if not lands_after_jump:
        assert result["reason"] == "lava_landing_unconfirmed"


@pytest.mark.parametrize("fallback", ["bridge", "alternate_bank", "third_bank"])
@pytest.mark.parametrize("status_timeout", [False, True])
def test_lava_escape_skips_a_shore_path_that_left_the_bot_at_the_same_rim(monkeypatch, fallback, status_timeout):
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    shore = [3188.5, 66, -2046.5]
    status = {
        "pos": [3186.7, 64.6, -2046.8], "health": 20, "alive": True, "ready": True,
        "in_lava": True, "on_ground": False, "dimension": "the_nether",
    }
    calls = []
    can_bridge = False
    headings = []
    check_first_path = False

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal check_first_path
            if action == "status":
                if check_first_path and status_timeout:
                    check_first_path = False
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("shore verification exceeded the recovery deadline")
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}] if can_bridge else []}
            if action == "get_block":
                return {"name": "lava" if body["pos"][1] <= 64 else "air"}
            if action == "find_blocks":
                return {"blocks": [
                    {"name": "netherrack", "pos": [3188, 65, -2047]},
                    {"name": "basalt", "pos": [3183, 64, -2047]},
                    *([{"name": "netherrack", "pos": [3186, 66, -2051]}] if fallback == "third_bank" else []),
                ]}
            if action == "look":
                headings.append(body["yaw"])
                clock[0] += 0.6
            if action == "control" and body.get("forward"):
                clock[0] += body["ms"] / 1000
            if action == "move_to":
                calls.append(action)
                if body["x"] == shore[0]:
                    # The pathfinder replies successfully before the bot leaves the lava hitbox.
                    status["pos"] = [3186.7, 64.6, -2046.2]
                    clock[0] += 1.8
                    check_first_path = True
                elif fallback == "third_bank" and body["x"] == 3183.5:
                    # A vertical rise without movement across the pool does not make a shore path viable.
                    status["pos"] = [3186.7, 65.8, -2046.2]
                else:
                    assert body["x"] == (3186.5 if fallback == "third_bank" else 3183.5)
                    status.update(in_lava=False, on_ground=True, pos=[body["x"], body["y"], body["z"]])
            if action == "place":
                calls.append(action)
                status.update(in_lava=False, on_ground=True, pos=[3186.7, 65, -2046.2])
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    env = guard.combat_env

    def scan(*args, **kwargs):
        clock[0] += 1.3
        return {}

    monkeypatch.setattr(env, "scan_safe_yaws", scan)
    if fallback == "bridge":
        monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *args, **kwargs: shore)
    first = env.escape_lava()
    assert not first["ok"]
    if status_timeout:
        assert first["reason"] == "time_budget"
    else:
        assert "lava_move_to_shore" in first["actions"]

    can_bridge = fallback == "bridge"
    second = env.escape_lava()
    if fallback == "third_bank":
        assert not second["ok"]
        assert env.escape_lava()["ok"]
        assert calls == ["move_to"] * 3
        return
    assert second["ok"]
    if fallback == "bridge":
        assert "lava_shore_path_stalled" in second["actions"]
        assert calls == ["move_to", "place"]
    else:
        assert calls == ["move_to", "move_to"]
        assert headings[0] < 0 < headings[1]


@pytest.mark.parametrize("water_floor", ["stone", "lava", "air"])
@pytest.mark.parametrize("entry", ["poll", "boundary", "tool"])
def test_critical_post_lava_burning_can_douse_in_verified_adjacent_water(monkeypatch, water_floor, entry):
    status = {
        "pos": [0.5, 63.6, 0.5], "health": 5, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "in_water": False, "in_lava": True, "on_ground": False, "on_fire": True,
    }
    targets = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 2}]}
            if action == "get_block":
                x, y, z = body["pos"]
                blocks = {(0, 63, 0): "stone", (1, 63, 0): "water", (1, 62, 0): water_floor}
                return {"name": blocks.get((x, y, z), "air")}
            if action == "move_to":
                targets.append([body[k] for k in ("x", "y", "z")])
                status.update(pos=targets[-1], in_water=True, on_fire=False)
                return {"ok": True}
            raise AssertionError(action)

    events = []
    guard = GenerationGuard(CaveBot(), EventSink(events.append), checkpoint=lambda: None)

    def escape(**_kwargs):
        status.update(pos=[0.5, 64, 0.5], health=9.8, in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False, "pos_after": status["pos"]}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    if entry == "poll":
        guard.poll()
    elif entry == "boundary":
        guard.stabilize_after_tools()
    else:
        receipt = guard.handle_preemption(
            {"source": "boundary_threat", "primary_type": "lava"}, interrupted_name="mc_skill_mine_dfs",
        )
        assert receipt["status_after"]["in_water"] is (water_floor == "stone")
    escape_event, douse = (event for event in events if event["type"] == "guardian.action.completed")
    assert escape_event["action"] == ("boundary_escape_lava" if entry == "boundary" else "escape_lava")
    assert douse["action"] == "douse_in_water"
    assert douse["ok"] is (water_floor == "stone")
    assert targets == ([[1.5, 63, 0.5]] if water_floor == "stone" else [])

