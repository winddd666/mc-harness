from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("lands_in_water", [False, True])
@pytest.mark.parametrize(
    "mob, trigger, retreat_action",
    [
        ("creeper", "pre_hurt_creeper", "retreat_from_creeper"),
        ("zombified_piglin", "tool_interrupted_hurt", "flee"),
    ],
)
def test_retreat_rechecks_airborne_landing(monkeypatch, lands_in_water, mob, trigger, retreat_action):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    hostile = {"id": 66, "name": mob, "pos": [0.5, 64.0, 4.5], "dist": 4.0}
    pos = [0.5, 64.4, 0.5]
    landing_checked = False
    blocking = False
    calls = []

    class ShoreBot:
        def status(self):
            return {
                "pos": list(pos), "health": 20, "food": 20, "ready": True, "alive": True,
                "on_ground": landing_checked and not lands_in_water,
                "in_water": landing_checked and lands_in_water,
                "blocking": blocking,
                "nearest_hostile": {**hostile, "dist": math.dist(pos, hostile["pos"])},
            }

        def call(self, action, body=None, **kwargs):
            nonlocal landing_checked, blocking
            if action != "control" or (body or {}).get("forward"):
                calls.append(action)
            if action == "status":
                assert 0 < kwargs["timeout_s"] <= 0.45
                landing_checked = True
                pos[1] = 64.0
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [self.status()["nearest_hostile"]]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "posture":
                blocking = bool(body.get("shield"))
            if action == "control" and body.get("forward"):
                assert landing_checked and not lands_in_water
                pos[2] -= 2.3
            if action == "move_to":
                pos[:] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    def water_retreat(*_args, **_kwargs):
        calls.append("water_retreat")
        return {"ok": False, "in_water": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "escape_deep_water_to_shore", water_retreat)
    result = CombatControllerSlot().handle(env, {
        "source": trigger, "primary_entity_id": 66, "primary_type": mob,
    })
    retreat = next(action for action in result["actions"] if action["action"] == retreat_action)
    assert landing_checked
    assert retreat["ok"] is not lands_in_water
    if lands_in_water:
        assert retreat["reason"] == "unsafe_landing"
        assert result["latched_guard"] is (mob == "creeper")
        assert "control" not in calls and "move_to" not in calls
        assert ("water_retreat" in calls) is (mob == "creeper")
    else:
        assert calls.index("status") < calls.index("control")


@pytest.mark.parametrize("landing", ["dry", "airborne"])
def test_unshielded_creeper_retreat_waits_for_a_later_landing(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    creeper = {"id": 4881399, "name": "creeper", "pos": [0.5, 60, 3.5], "dist": 3.9}
    status = {
        "pos": [0.5, 63.1, 0.5], "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": False, "nearest_hostile": creeper,
    }
    samples = []
    moves = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                samples.append(kwargs["timeout_s"])
                if len(samples) == 4 and landing == "dry":
                    status.update(on_ground=True, pos=[0.5, 60, 0.5])
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [creeper]}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                moves.append(action)
                status["pos"] = [0.5, 60, -2.8]
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "probe_retreat", lambda _pos, _yaw, distance: (60, False) if distance == 3.5 else None)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"]},
    )

    assert len(samples) == (4 if landing == "dry" else 5)
    assert all(0 < timeout <= 0.65 for timeout in samples)
    retreat, = result["actions"]
    assert retreat["ok"] is (landing == "dry")
    assert moves == (["control"] if landing == "dry" else [])


def test_missing_offhand_pickaxe_for_bunker_still_allows_boundary_retreat():
    pos = [5199.5, 84, 7615.5]
    shooter = {"id": 42, "name": "skeleton", "dist": 8.0, "pos": [5207.5, 84, 7615.5]}
    moves = []

    class RidgeBot:
        def status(self):
            return {
                "pos": list(pos), "health": 3.5, "food": 20, "ready": True, "alive": True,
                "on_ground": True, "nearest_hostile": shooter,
            }

        def inventory(self):
            return {
                "items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1},
                          {"name": "cobblestone", "count": 16}],
                "offhand": {"name": "iron_pickaxe", "count": 1}, "held": {"name": "stone_sword"},
            }

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return self.inventory()
            if action == "events":
                return {"events": []}
            if action == "get_entities":
                return {"entities": [shooter]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 84 else "air"}
            if action == "equip" and body["item"] == "iron_pickaxe":
                raise ApiError("missing_item", "no iron_pickaxe in inventory")
            if action == "control" and body.get("forward"):
                assert body["jump"] is False
                pos[0] -= 2.3
                moves.append(list(pos))
            assert action not in {"dig", "attack"}
            return {"ok": True}

    events = []
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))
    guard.stabilize_after_tools()

    reflex, = [event for event in events if event["type"] == "guardian.action.completed"]
    assert reflex["action"] == "boundary_subcortical_combat"
    bunker, retreat = reflex["result"]["actions"]
    assert bunker == {"action": "emergency_bunker", "ok": False, "code": "missing_item"}
    assert retreat["action"] == "flee" and retreat["ok"] is True
    assert moves == [[5197.2, 84, 7615.5]]


@pytest.mark.parametrize("scene", ["wall", "open", "unsupported", "creeper", "shooter_changed", "stale_exit"])
def test_staircase_surface_exit_checks_cover_after_point_blank_shooter_blocks_retreat(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatEnv

    origin = [356580.5, 64, 369945.5]
    shooter = {"id": 1027040, "name": "skeleton", "dist": 1.5, "pos": [356582, 64, 369945.5]}
    status = {
        "pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    moves = []
    retreated = False

    class ExitBot:
        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "iron_pickaxe"}}
            if action == "get_entities":
                if retreated and scene == "creeper":
                    return {"entities": [shooter, {"id": 4, "name": "creeper", "dist": 4.4}]}
                return {"entities": [{**shooter, "id": 43} if retreated and scene == "shooter_changed" else shooter]}
            if action == "get_block":
                x, y, z = body["pos"]
                if y == 63 and (scene != "unsupported" or z == 369945):
                    return {"name": "stone"}
                if scene != "open" and x == 356581 and 64 <= y <= 66 and z in {369946, 369947}:
                    return {"name": "stone"}
                return {"name": "air"}
            if action == "move_to":
                moves.append(body)
                status["pos"] = [body["x"], body["y"], body["z"]]
            elif action not in {"stop", "control", "posture"}:
                pytest.fail(f"Unexpected action near surface exit: {action}")
            return {"ok": True}

    def flee(*_args, **_kwargs):
        nonlocal retreated
        retreated = True
        return {"ok": False, "mode": "flee", "moved_dist": 0.0, "reason": "no_safe_retreat"}

    env_events = EventSink(lambda _: None)
    guard = GenerationGuard(ExitBot(), env_events, checkpoint=lambda: None)
    monkeypatch.setattr(CombatEnv, "arena_snapshot", lambda *_args: {"enclosure": "surface"})
    monkeypatch.setattr(CombatEnv, "flee", flee)
    exit_pos = [origin[0], origin[1], origin[2] - 2.0] if scene == "stale_exit" else origin
    guard.stabilize_after_tools([json.dumps({
        "ok": True, "vertical": "up", "steps_done": 10, "stop_reason": "reached_surface",
        "pos_after": exit_pos, "surface_evidence": {"floor": "dirt", "pos": exit_pos},
    })])

    outcome, = [
        event["result"] for event in env_events.events
        if event["type"] == "guardian.action.completed" and event["action"] == "boundary_subcortical_combat"
    ]
    assert retreated and outcome["actions"][0]["reason"] == "no_safe_retreat"
    assert (outcome["mode"] == "natural_cover") is (scene == "wall")
    if scene == "wall":
        move, = moves
        assert [move["x"], move["y"], move["z"]] == pytest.approx([356580.5, 64, 369947.7])
        assert outcome["actions"][-1]["ok"]
    else:
        assert moves == []


@pytest.mark.parametrize(("pressure", "reacts"), [
    ("close_melee", True), ("burst_without_sighting", True), ("above_threshold", False),
    ("distant_melee", False), ("occluded_melee", False), ("shield_raised", False), ("status_only", False),
])
def test_low_health_unshielded_melee_pressure_can_bypass_reaction_cooldown(monkeypatch, pressure, reacts):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    nearest = {"id": 42, "name": "zombie", "dist": 6.0 if pressure == "distant_melee" else 2.1,
               "blocked_by_wall": pressure == "occluded_melee"}
    status = {"pos": [34026.3, 69, 34004.2], "health": 8.1 if pressure == "above_threshold" else 8.0,
              "ready": True, "alive": True, "on_ground": True, "food": 20, "dimension": "overworld",
              "blocking": pressure == "shield_raised", "nearest_hostile": None if pressure == "burst_without_sighting" else nearest}
    hit_count = {"burst_without_sighting": 2, "status_only": 0}.get(pressure, 1)
    hits = [{"seq": i + 1, "type": "hurt", "source": "mob_attack"} for i in range(hit_count)]
    retreats = []

    class HillBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": []}
            raise AssertionError(action)

    def retreat(*_a, **_kw):
        retreats.append(True)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    guard = GenerationGuard(HillBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_reaction = 999.0
    guard.previous_health = 12.0
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda *_a, **_kw: None)
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)

    incident = guard.poll()
    assert (incident is not None) is reacts
    if reacts:
        assert incident["reasons"] == ["fresh_hurt"] and incident["requires_replan"]
    clock[0] += 0.3
    assert guard.poll() is None
    assert retreats == ([True] if reacts else [])


def test_ranged_retreat_without_shield_resolves_the_shooter_from_live_entities(monkeypatch):
    retreat_targets = []
    shooter = {"id": 44, "name": "pillager", "pos": [-10936.8, 63.0, -10992.5]}

    class RiverbankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-10934.7, 63.2, -10993.8], "health": 16.0, "food": 20, "ready": True, "alive": True,
                    "on_ground": False, "nearest_hostile": {"id": 44, "name": "pillager", "dist": 2.6},
                }
            if action == "events":
                event = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 44, "name": "pillager"}}
                return {"events": [event] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [{"id": 47, "name": "pillager", "pos": [-10931.0, 63.0, -10992.5]}, shooter]}
            return {"ok": True}

    def retreat(threat_pos, **kwargs):
        retreat_targets.append(threat_pos)
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RiverbankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    assert guard.poll() is not None
    assert retreat_targets == [shooter["pos"]]


@pytest.mark.parametrize("safe_step", [True, False])
@pytest.mark.parametrize("direction", ["side", "away"])
def test_bug069_retreat_can_take_a_verified_short_step_when_long_routes_are_unsafe(safe_step, direction):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    status = {"pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True}
    controls = []

    class NarrowBankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                far_edge = 2 if safe_step else 1
                on_bank = x == 0 and 0 <= z <= far_edge if direction == "side" else z == 0 and -far_edge <= x <= 0
                return {"name": "stone" if y == 63 and on_bank else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                controls.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [0.5 + 0.7 * dx, 64.0, 0.5 + 0.7 * dz]
            return {"ok": True}

    result = CombatEnv(NarrowBankBot()).flee(
        [4.5, 64, 0.5], allow_shaft_seal=False, wait_for_ground=direction == "away",
    )

    assert result["ok"] is safe_step
    if safe_step:
        control, = controls
        expected = [0.5, 64.0, 1.2] if direction == "side" else [-0.2, 64.0, 0.5]
        assert result["pos_after"] == pytest.approx(expected)
        assert control["forward"] and not control["sprint"] and not control["jump"]
        assert control["ms"] < 350
    else:
        assert controls == []


@pytest.mark.parametrize("room_for_short_exit", [True, False])
def test_flee_uses_verified_short_exit_when_full_followup_hits_drop(room_for_short_exit):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [0.5, 64, 0.5], "alive": True, "ready": True, "on_ground": True}
    followups = []
    floor_edge = 6 if room_for_short_exit else 4

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, _ = body["pos"]
                return {"name": "stone" if y == 63 and x <= floor_edge else "air"}
            if action == "control":
                status["pos"] = [2.6, 64, 0.5]
            if action == "move_to":
                followups.append(body)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    result = CombatEnv(RetreatBot()).flee([-1.8, 64, 0.5], distance=13.0, allow_shaft_seal=False)

    if room_for_short_exit:
        move, = followups
        assert (move["x"], move["y"], move["z"]) == pytest.approx((6.1, 64, 0.5))
        assert result["ok"] is True and result["moved_dist"] == pytest.approx(5.6)
    else:
        assert followups == []
        assert result["moved_dist"] == pytest.approx(2.1)


@pytest.mark.parametrize(
    "dimension, health",
    [
        ("the_nether", 16.5),
        ("overworld", 8.0),
    ],
)
def test_guardian_damage_retreat_checks_terrain(dimension, health):
    mutations = []

    class RetreatBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [724.3, 68, 636.7], "health": health, "food": 20, "ready": True, "alive": True,
                    "on_ground": True, "blocking": True, "dimension": dimension,
                    "nearest_hostile": {"id": 42, "name": "zombie", "dist": 1.1},
                }
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}] if since < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_block":
                x, y, z = body["pos"]
                if y == 67:
                    if dimension == "the_nether":
                        return {"name": "magma_block" if (x, z) == (724, 636) else "lava"}
                    return {"name": "stone" if (x, z) == (724, 636) else "air"}
                if dimension == "overworld":
                    return {"name": "air"}
                return {"name": "air" if y >= 68 else "netherrack"}
            if action == "find_blocks":
                return {"blocks": []}
            mutations.append(action)
            return {"ok": True}

    guard = GenerationGuard(RetreatBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["result"]["reason"] == "no_safe_retreat"
    assert mutations == []


@pytest.mark.parametrize("episode_timeout", [False, True])
def test_bug072_flee_bounds_nested_shaft_moves_and_stops_on_timeout(monkeypatch, episode_timeout):
    from mc_agent.harness.control import RunTimedOut

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    moves = []
    calls = []
    events = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            timeout_s = kwargs.get("timeout_s", 30.0)
            calls.append((action, timeout_s))
            if action == "move_to":
                moves.append((clock[0], timeout_s, body["timeout"]))
                clock[0] += min(7.0, timeout_s)
                if timeout_s < 7.0:
                    raise httpx.ReadTimeout("nested shaft move reached the retreat deadline")
            if action == "stop":
                clock[0] += timeout_s
            return {"ok": True}

    def checkpoint():
        if episode_timeout and clock[0] >= 1014.0:
            raise RunTimedOut("episode deadline")

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=checkpoint)
    orig_call = bot.call

    def retreat_recorded_shaft():
        for y in range(-17, -3):
            bot.call("move_to", {"x": -2712.5, "y": y, "z": -10275.4, "timeout": 20000})
        return json.dumps({"ok": True, "mode": "retreat_shaft"})

    if episode_timeout:
        with pytest.raises(RunTimedOut):
            guard.run_tool_with_reflex("mc_skill_flee", retreat_recorded_shaft)
        assert len(moves) == 2
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", retreat_recorded_shaft))
        assert result["ok"] is False and result["error"]["code"] == "time_budget"
        assert len(moves) == 5
        assert any(event["type"] == "guardian.recovery_budget_exceeded" and event["budget_s"] == 35.0 for event in events)

    assert all(start + timeout <= 1033.0 and start + server_ms / 1000 <= 1033.0 for start, timeout, server_ms in moves)
    assert calls[-1] == ("stop", 1.5)
    assert clock[0] - 1000.0 < 35.0
    assert bot.call == orig_call


def test_bug072_real_flee_entry_accepts_the_mining_sessions_httpx_timeout(monkeypatch):
    from mc_agent.harness.mc_tools import McToolset

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    request_timeouts = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            request_timeouts.append(kwargs["timeout_s"])
            return {"pos": [0.5, 64, 0.5], "on_ground": True}

    def retreat(scoped, **kwargs):
        scoped.bot.call("status", method="GET")
        clock[0] += 32.6
        scoped.bot.call("status", method="GET")
        return {"ok": True, "mode": "retreat_shaft", "pos_after": [1.5, 64, 0.5]}

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", retreat)
    bot = ShaftBot()
    toolset = McToolset(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", toolset.mc_skill_flee))

    assert result["ok"] is True and result["mode"] == "retreat_shaft"
    initial, final = request_timeouts
    assert isinstance(initial, httpx.Timeout) and isinstance(final, httpx.Timeout)
    assert initial.read == 5.0
    assert initial.connect == initial.write == initial.pool == 0.5
    assert 2 * sum(final.as_dict().values()) + 0.15 <= 1033.0 - clock[0]
    assert final.connect <= 0.5 and final.write <= 0.5 and final.pool <= 0.5


@pytest.mark.parametrize("placement", [
    "complete", "existing_wall", "partial", "receipt_only", "threat_inside",
    "already_sealed", "sealed_with_exposed_mob", "sealed_unknown_threat",
])
def test_flee_only_confirms_a_shaft_seal_after_both_cells_are_solid(monkeypatch, placement):
    from mc_agent.skills.combat_env import CombatEnv

    blocks = {(1, 95, 0): "stone"} if placement == "existing_wall" else {}
    sealed = placement in {"already_sealed", "sealed_with_exposed_mob", "sealed_unknown_threat"}
    if sealed:
        blocks.update({(1, 95, 0): "tuff", (1, 96, 0): "tuff"})
    attempts = []
    status = {"pos": [0.5, 95, 0.5], "alive": True, "ready": True, "on_ground": True}
    threat = [0.9, 95, 0.5] if placement == "threat_inside" else [3.0, 95, 0.5]

    class ShaftBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "get_entities":
                if placement == "sealed_unknown_threat":
                    raise httpx.ReadTimeout("entity scan timed out")
                entities = [{"id": 42, "name": "zombie", "pos": threat}]
                if placement == "sealed_with_exposed_mob":
                    entities.append({"id": 43, "name": "spider", "pos": [0.5, 95, 2.5]})
                return {"entities": entities}
            if action == "place":
                cell = tuple(body["pos"])
                attempts.append(cell)
                if placement == "partial" and cell[1] == 96:
                    raise ApiError("blocked_by_entity", "placement failed")
                if placement != "receipt_only":
                    blocks[cell] = "cobblestone"
                return {"ok": True}
            pytest.fail(f"unexpected action {action}")

    env = CombatEnv(ShaftBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
    monkeypatch.setattr(env, "probe_retreat", lambda *_a: None)
    result = env.flee(threat)

    complete = placement in {"complete", "existing_wall", "already_sealed"}
    assert result["ok"] is complete
    assert result["mode"] == ("sealed" if complete else "flee")
    if sealed or placement == "threat_inside":
        assert attempts == []
        if not complete:
            assert result["reason"] == "no_safe_retreat"
    else:
        assert attempts[-1] == (1, 96, 0)


@pytest.mark.parametrize("terrain", [
    "wall", "fire", "unknown", "drifted_during_probe", "missed_landing", "respawned",
    "live_shooter_covered", "shooter_flanked", "scan_timeout", "wall_removed_during_scan",
])
def test_bug076_natural_cover_requires_solid_shot_blocking_and_confirmed_landing(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0, "food": 20,
        "nearest_hostile": {"id": 44, "name": "pillager", "dist": 8.0},
    }
    shooter = [8.5, 64.0, 0.5]
    moves = []
    status_reads = []
    wall_removed = False

    class CoverBot:
        def status(self):
            status_reads.append(True)
            if terrain == "drifted_during_probe" and len(status_reads) >= 2:
                status.update(pos=[1.0, 63.4, 0.5], on_ground=False)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal wall_removed
            if action == "status":
                return self.status()
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                if terrain == "scan_timeout":
                    raise httpx.ReadTimeout("shooter scan was unavailable")
                if terrain == "wall_removed_during_scan":
                    assert len(moves) == 1
                    wall_removed = True
                current_shooter = [0.5, 64.0, -5.0] if terrain == "shooter_flanked" else shooter
                return {"entities": [{"id": 44, "name": "pillager", "pos": current_shooter}]}
            if action == "get_block":
                x, y, z = body["pos"]
                if not wall_removed and y in (64, 65) and 3 <= x <= 5 and 1 <= z <= 2:
                    if terrain == "unknown":
                        raise httpx.ReadTimeout("cover voxel was not read")
                    return {"name": "fire" if terrain == "fire" else "stone"}
                return {"name": "stone" if y == 63 else "air"}
            if action == "move_to":
                moves.append(body)
                if terrain == "respawned":
                    status.update(pos=[1.5, 64.0, 0.5], last_death={"t": 1})
                elif terrain != "missed_landing":
                    status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = CoverBot()
    if terrain == "wall":
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
        result = next(a["result"] for a in incident["actions"] if a["action"] == "combat_or_flee")
    else:
        result = CombatEnv(bot).seek_natural_cover(
            shooter, verify_live_shooters=terrain in {
                "live_shooter_covered", "shooter_flanked", "scan_timeout", "wall_removed_during_scan",
            },
        )

    if terrain in {"fire", "unknown", "drifted_during_probe"}:
        assert result is None
        assert moves == []
    else:
        goal, = moves
        assert (goal["x"], goal["y"], goal["z"]) == pytest.approx((0.5, 64.0, 2.7))
        assert result["ok"] is (terrain in {"wall", "live_shooter_covered"})
        if result["ok"]:
            assert result["mode"] == "natural_cover"
        else:
            failed_rescan = terrain in {"shooter_flanked", "scan_timeout", "wall_removed_during_scan"}
            assert result["reason"] == (
                "shooter_lanes_unconfirmed" if failed_rescan else "cover_unconfirmed"
            )
            assert bool(result.get("respawned")) is (terrain == "respawned")


def test_stationary_natural_cover_rejects_an_exposed_hitbox_edge(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv

    pos = [0.5, 64, 0.08]
    shooter = [8.5, 64, 0.08]

    class CornerBot:
        def status(self):
            return {"pos": pos, "ready": True, "alive": True, "on_ground": True}

        def call(self, action, body=None, **_kwargs):
            assert action == "get_block"
            x, y, z = body["pos"]
            return {"name": "stone" if x == 2 and z == 0 and y in (64, 65) else "air"}

    env = CombatEnv(CornerBot())
    monkeypatch.setattr(env, "probe_retreat", lambda *_args: None)

    assert all(not env.has_los(shooter, pos, dst_height=height)["clear"] for height in (0.75, 1.6))
    assert env.seek_natural_cover(shooter, verify_live_shooters=True) is None

