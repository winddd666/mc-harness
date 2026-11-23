from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(
    "health, shooter_dist, fireball_age, shield_after_fireball, defer",
    [
        (5.3, 6.2, None, False, True),
        (17.6, None, 5.0, False, True),
        (14.3, None, 9.0, False, True),
        (2.53, 18.7, None, False, True),
        (12.933, None, 8.214, True, True),
        (12.933, None, 12.01, True, False),
    ],
)
def test_poll_and_boundary_skip_eat_in_ranged_mob_fire(
    monkeypatch, health, shooter_dist, fireball_age, shield_after_fireball, defer,
):
    recent_nether_fireball = fireball_age is not None
    clock = [7000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_items: list[dict] = []
    posture_calls: list[dict] = []
    state = {"inside_shaft": False}

    class _EnclosedSkills:
        class _Eng:
            nodes = {(0, 64, 0): True}

            def is_inside_shaft(self):
                return state["inside_shaft"]

        mining_dfs = _Eng()

    class _SkeletonRangeBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": health,
                    "food": 19,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": not recent_nether_fireball or shield_after_fireball,
                    "dimension": "the_nether" if recent_nether_fireball else "overworld",
                    "on_fire": recent_nether_fireball and not shield_after_fireball,
                    "nearest_hostile": None if shooter_dist is None else {
                        "id": 612494, "name": "skeleton", "pos": [0.5 + shooter_dist, 64.0, 0.5], "dist": shooter_dist,
                    },
                }
            if action == "events":
                if recent_nether_fireball and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "fireball"}], "last_seq": 1}
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 10}, {"name": "shield", "count": 1}]}
            if action == "posture":
                posture_calls.append(pl)
            if action == "use_item":
                use_items.append(pl)
            return {"ok": True}

    guard = GenerationGuard(_SkeletonRangeBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_EnclosedSkills())
    if recent_nether_fireball:
        guard._drain_events_pages()
        clock[0] += fireball_age
    else:
        guard._mark_ranged_engagement(clock[0], attacker={"id": 612494, "name": "skeleton"})
    if not defer:
        incident = guard.poll()
        assert [a["action"] for a in incident["actions"]] == ["eat"]
        assert posture_calls == [{"shield": False}]
        assert use_items == [{"item": "cooked_beef"}]
        return
    assert guard.poll() is None
    assert guard.stabilize_after_tools(["ok"]) is None
    assert posture_calls == []
    assert use_items == []

    # Safe inside enclosed shaft (or once skeleton moves >16m), eating succeeds
    state["inside_shaft"] = True
    clock[0] += 0.5
    inc_shaft = guard.poll()
    assert inc_shaft is not None
    assert [a["action"] for a in inc_shaft["actions"]] == ["eat"]
    assert use_items == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("sources", [("mob_attack", "fireball"), ("fireball", "mob_attack")])
def test_mixed_nether_damage_keeps_melee_eat_cooldown_regardless_of_event_order(monkeypatch, sources):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-406.3, 58, -1903.3], "dimension": "the_nether", "health": 17, "food": 19,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "on_fire": False,
    }
    eats = []
    hits = [{"seq": seq, "type": "hurt", "source": source} for seq, source in enumerate(sources, 1)]

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                eats.append(body)
            return {"ok": True}

    guard = GenerationGuard(FoodBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    clock[0] += 9.0

    assert guard.poll() is None
    assert eats == []
    clock[0] += 1.1
    eat, = guard.poll()["actions"]
    assert eat["action"] == "eat" and eat["ok"]
    assert eats == [{"item": "cooked_beef"}]


def test_lava_contact_without_damage_preserves_later_melee_reflex(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3566.3, 64.4, 1791.1], "health": 20, "food": 20, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "on_fire": True, "alive": True, "ready": True,
        "nearest_hostile": {"name": "wither_skeleton", "id": 44, "dist": 6.0},
    }
    hurts = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hurt for hurt in hurts if hurt["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape_lava(*, force_rim_escape):
        assert force_rim_escape is False
        status.update(in_lava=False, on_ground=True, pos=[3566.3, 65, 1791.1])
        return {"ok": True, "in_lava": False}

    retreats = []
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(guard, "_defend_or_flee", lambda *args, **kwargs: retreats.append(True) or {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat",
    })
    assert guard.poll()["actions"][0]["action"] == "escape_lava"

    clock[0] += 10.0
    status["health"] = 17.5
    hurts.append({"seq": 1, "type": "hurt", "source": "on_fire"})
    assert guard.poll() is None
    assert retreats == []

    clock[0] += 4.0
    status["health"] = 12.0
    hurts.extend([{"seq": 2, "type": "hurt", "source": "mob_attack"}, {"seq": 3, "type": "hurt", "source": "on_fire"}])
    incident = guard.poll()
    assert incident["requires_replan"] and incident["actions"][0]["action"] == "combat_or_flee"
    assert retreats == [True]


@pytest.mark.parametrize("recent_reaction", [False, True])
@pytest.mark.parametrize(
    "block, block_y, clears",
    [("fire", 52, True), ("fire", 53, True), ("fire", 54, False), ("soul_fire", 52, False), ("netherrack", 52, False)],
)
def test_contact_fire_only_digs_live_flames_inside_the_hitbox(monkeypatch, block, block_y, clears, recent_reaction):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    blocks = {(-419, block_y, -1881): block}
    digs = []

    class FireBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-418.3, 52, -1880.5], "health": 10, "food": 20, "dimension": "the_nether",
                    "on_ground": True, "on_fire": True, "alive": True, "ready": True, "blocking": True,
                    "nearest_hostile": {"id": 44, "name": "blaze", "dist": 5.5},
                }
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "in_fire"}]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "dig":
                assert kwargs["timeout_s"] <= 0.6 and kwargs["allow_reauth"] is False
                digs.append(body["pos"])
                if clears:
                    blocks[tuple(body["pos"])] = "air"
            return {"ok": True}

    guard = GenerationGuard(FireBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if recent_reaction:
        guard.last_reaction = 999.0
    incident = guard.poll()
    assert incident is not None
    clear, = incident["actions"]
    assert clear["action"] == "clear_contact_fire" and clear["ok"] is clears
    expected_digs = [[-419, block_y, -1881]] if block != "netherrack" and block_y < 54 else []
    assert digs == expected_digs
    assert not incident["requires_replan"]


def test_boundary_does_not_eat_while_still_taking_contact_fire_damage(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    uses = []
    status = {
        "pos": [-393.2, 55, -1890.5], "health": 10.08, "food": 19, "dimension": "the_nether",
        "on_ground": True, "on_fire": True, "alive": True, "ready": True,
    }

    class FireBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"seq": 1, "type": "hurt", "source": "in_fire"},
                ]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                uses.append(body)
                status["food"] = 20
            return {"ok": True}

    guard = GenerationGuard(FireBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.stabilize_after_tools() is None
    assert uses == []
    clock[0] += 2.0
    assert "进食" in guard.stabilize_after_tools()
    assert uses == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("landing", ["grounded", "airborne", "back_in_lava", "respawned", "stale_success"])
def test_lava_tool_preemption_uses_latest_landing_status(monkeypatch, landing):
    status = {
        "pos": [6571.9, 64.8, 6592.6], "in_lava": True, "on_ground": False, "on_fire": True,
        "health": 18.1, "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    events = []
    douse_checks = []

    class LavaBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                if kwargs.get("timeout_s") == 0.25:
                    if landing == "grounded":
                        status.update(on_ground=True)
                    elif landing == "back_in_lava":
                        status.update(in_lava=True)
                    elif landing == "respawned":
                        status.update(last_death={"t": 2}, health=20, on_ground=True)
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            raise AssertionError(endpoint)

    def escape():
        status.update(pos=[6572.7, 65, 6592.6], in_lava=landing == "stale_success")
        if landing == "stale_success":
            return {"ok": True, "in_lava": False}
        return {"ok": False, "in_lava": False, "reason": "lava_landing_unconfirmed"}

    guard = GenerationGuard(LavaBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    monkeypatch.setattr(guard, "_douse_after_lava", lambda _before, *, after: douse_checks.append(dict(after)))

    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="pre_ensure",
    )

    assert receipt["requires_replan"]
    assert ("并脱困" in receipt["message"]) is (landing == "grounded")
    assert ("死亡并重生" in receipt["message"]) is (landing == "respawned")
    assert bool(douse_checks) is (landing == "grounded")
    latest = receipt["status_after"]
    assert latest["in_lava"] is (landing in {"back_in_lava", "stale_success"})
    assert latest["on_ground"] is (landing in {"grounded", "respawned"})
    event, = [event for event in events if event["type"] == "guardian.preempted_tool"]
    assert event["status_after"] == latest


@pytest.mark.parametrize("death_recorded", [False, True])
def test_lava_landing_at_full_health_uses_death_record_instead_of_healing(monkeypatch, death_recorded):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [17087.0, 71.0, 29993.3], "health": 17.184, "ready": True, "alive": True,
        "dimension": "minecraft:overworld", "in_lava": True, "on_ground": False,
    }

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "get_block":
                return {"name": "air"}
            if endpoint == "inventory":
                return {"items": []}
            if endpoint == "control" and body.get("forward"):
                status.update(health=20, in_lava=False, on_ground=True, pos=[17086.1, 66.2, 30000.7])
                if death_recorded:
                    status["last_death"] = {"t": 1}
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})

    result = env.escape_lava()

    assert result["ok"] is (not death_recorded)
    assert result.get("respawned", False) is death_recorded


def test_r21_preempted_lava_death_reports_respawned_not_escaped():
    """Reproduces r21_short hy4_hy_8_nether: lava preemption ending in death/respawn must NOT claim '已中断原工具并脱困'."""
    dead_lava_state = {"calls": 0}

    class _LavaDiePreemptBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                dead_lava_state["calls"] += 1
                if dead_lava_state["calls"] <= 2:
                    return {
                        "pos": [778.9, 29.8, -824.5],
                        "health": 2.0,
                        "alive": True,
                        "ready": True,
                        "in_lava": True,
                        "on_ground": False,
                        "dimension": "minecraft:the_nether",
                    }
                return {
                    "pos": [7828.5, 64.1, 2818.5],
                    "health": 20.0,
                    "alive": True,
                    "ready": True,
                    "in_lava": False,
                    "on_ground": True,
                    "dimension": "minecraft:overworld",
                }
            if action == "inventory":
                return {"items": []}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "lava"}
            return {"ok": True}

    guard = GenerationGuard(_LavaDiePreemptBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="mc_skill_mine_dfs",
    )
    assert receipt["reflex_mode"] == "lava_escape"
    assert "并脱困" not in receipt["message"]
    assert "死亡并重生" in receipt["message"]

    # Also catch close-to-spawn Nether lava death (<64m displacement) where ready=True & health=20 on respawn
    close_state = {"calls": 0}

    class _CloseSpawnLavaDieBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                close_state["calls"] += 1
                if close_state["calls"] <= 2:
                    return {
                        "pos": [712.0, 58.0, 635.0],
                        "health": 11.0,
                        "alive": True,
                        "ready": True,
                        "in_lava": True,
                        "on_ground": False,
                        "dimension": "minecraft:the_nether",
                        "last_death": {"pos": [704.5, 64.0, 640.5], "t": 1000},
                    }
                return {
                    "pos": [704.5, 64.0, 640.5],
                    "health": 20.0,
                    "alive": True,
                    "ready": True,
                    "in_lava": False,
                    "on_ground": True,
                    "dimension": "minecraft:the_nether",
                    "last_death": {"pos": [712.0, 58.0, 635.0], "t": 2000},
                }
            if action == "inventory":
                return {"items": []}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "lava"}
            return {"ok": True}

    from mc_agent.skills.combat_env import CombatEnv

    close_state["calls"] = 0
    out_close = CombatEnv(_CloseSpawnLavaDieBot()).escape_lava()
    assert out_close["ok"] is False
    assert out_close.get("respawned") is True

    close_state["calls"] = 0
    g_close = GenerationGuard(_CloseSpawnLavaDieBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    rec_close = g_close.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="mc_skill_unstick",
    )
    assert rec_close["reflex_mode"] == "lava_escape"
    assert "并脱困" not in rec_close["message"]
    assert "死亡并重生" in rec_close["message"]


def test_r22_fireball_skips_dig_cover_and_same_spot_arrow_escalates_to_flee(monkeypatch):
    """Reproduces r22_short glm_1 (20s dig_cover in fireball) & hy4_10 (3x repeated dig_cover at same exposed spot)."""
    clock = [9000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    dig_cover_calls: list[str] = []
    flee_calls: list[dict] = []
    hurt_state = {"seq": 1, "src": "fireball", "hp": 16.0, "mob": "blaze"}

    class _RangedFallbackSkills:
        mining_dfs = None

        def dig_cover_pocket(self):
            dig_cover_calls.append(hurt_state["src"])
            return {"ok": True, "mode": "cover_pocket", "pos_after": [6346.3, 54.0, -9022.3]}

        def defend_or_flee(self, **kwargs):
            flee_calls.append(kwargs)
            return {"ok": True, "mode": "flee", "moved_dist": 6.0}

    class _RangedFallbackBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [6346.3, 54.0, -9022.3],
                    "health": hurt_state["hp"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                    "nearest_hostile": {"id": 91, "name": hurt_state["mob"], "pos": [6350.0, 54.0, -9022.3], "dist": 3.7},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = hurt_state["seq"]
                rows = [{"seq": seq, "type": "hurt", "health": hurt_state["hp"], "source": hurt_state["src"]}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}], "offhand": {"name": "shield"}}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 54, 0])[1] < 54 else "air"}
            return {"ok": True}

    guard = GenerationGuard(_RangedFallbackBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_RangedFallbackSkills())
    # 1) Blaze fireball must NEVER spend 20s digging cover pocket — directly execute combat_or_flee
    inc_fb = guard.poll()
    assert inc_fb is not None
    assert [a["action"] for a in inc_fb["actions"] if a["action"] in {"dig_cover", "combat_or_flee"}] == ["combat_or_flee"]
    assert dig_cover_calls == []

    # 2) Arrow hit at exposed spot digs cover once; a subsequent arrow hit 6.5s later at the SAME block escalates to combat_or_flee!
    hurt_state.update({"seq": 2, "src": "arrow", "hp": 14.0, "mob": "skeleton"})
    clock[0] += 4.0
    inc_a1 = guard.poll()
    assert inc_a1 is not None
    assert "dig_cover" in [a["action"] for a in inc_a1["actions"]]
    assert len(dig_cover_calls) == 1

    hurt_state.update({"seq": 3, "src": "arrow", "hp": 6.0, "mob": "skeleton"})
    clock[0] += 6.5
    inc_a2 = guard.poll()
    assert inc_a2 is not None
    assert len(dig_cover_calls) == 1  # NOT re-dug on the exact same broken block!
    assert any(a["action"] == "combat_or_flee" for a in inc_a2["actions"])


@pytest.mark.parametrize("short_bank", [False, True])
def test_poll_nether_retreat_checks_footing_at_high_health(monkeypatch, short_bank):
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    status = {
        "pos": [-407.5, 59.0 if short_bank else 59.8, -1887.3],
        "health": 14.83, "food": 20, "dimension": "the_nether",
        "on_ground": short_bank, "alive": True, "ready": True,
        "nearest_hostile": {"id": 77, "name": "wither_skeleton", "pos": [-407.5, 59, -1883.3], "dist": 4},
    }
    landings = []
    movements = []

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if kwargs["timeout_s"] <= 0.45:
                    landings.append(action)
                    status.update(on_ground=True, pos=[-407.5, 59.0, -1887.3])
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "mob_attack"}]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if short_bank and (x != -408 or not -1890 <= z <= -1888):
                    return {"name": "lava" if y == 58 else "air"}
                return {"name": "netherrack" if y < 59 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"]
                movements.append(body)
                status["pos"] = [-407.5, 59.0, -1888.0 if short_bank else -1889.6]
            if action == "move_to":
                assert not short_bank
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    assert landings == ([] if short_bank else ["status"])
    control, = movements
    assert control["sprint"] is not short_bank and not control["jump"]
    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee" and retreat["ok"]


@pytest.mark.parametrize("terrain", ["exposed_lava", "solid_cover", "already_landed"])
@pytest.mark.parametrize(("source", "health"), [
    ("mob_attack", 2.86), ("mob_attack", 14.0), ("player_explosion", 15.87), ("fireball", 17.3),
])
def test_nether_knockback_checks_for_lava_before_waiting_to_retreat(monkeypatch, terrain, source, health):
    melee_above_retreat_threshold = source == "mob_attack" and health >= 11.0
    status = {
        "pos": [-1873.7, 36.6, -3688.7], "health": health, "food": 20,
        "in_lava": False, "on_ground": False, "alive": True, "ready": True, "blocking": not melee_above_retreat_threshold,
        "dimension": "the_nether", "nearest_hostile": {"id": 44, "name": "wither_skeleton", "dist": 3.0},
    }
    moves = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": source}]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_block":
                assert kwargs["timeout_s"] <= 0.5 and kwargs["allow_reauth"] is False
                y = body["pos"][1]
                if y == 35 and terrain == "solid_cover":
                    return {"name": "netherrack"}
                return {"name": "lava" if y == 34 else "air"}
            return {"ok": True}

    def escape_lava(*, force_rim_escape=False):
        assert not force_rim_escape
        moves.append("escape_lava")
        status.update(on_ground=True)
        return {"ok": True, "in_lava": False, "already_dry": terrain == "already_landed"}

    def flee(_pos, *, wait_for_ground):
        moves.append("flee")
        assert wait_for_ground is True
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(guard.combat_env, "flee", flee)

    incident = guard.poll()

    if health >= 11.0 and terrain != "solid_cover":
        assert moves == ["escape_lava"]
        assert incident["actions"][0]["action"] == "escape_lava"
    elif melee_above_retreat_threshold:
        assert moves == []
        assert [action["action"] for action in incident["actions"]] == ["raise_shield"]
    elif terrain == "exposed_lava":
        assert moves == ["escape_lava"]
        assert incident["actions"][0]["result"]["mode"] == "lava_escape"
    else:
        assert moves == (["flee"] if terrain == "solid_cover" else ["escape_lava", "flee"])
        assert incident["actions"][0]["result"]["mode"] == "flee"


@pytest.mark.parametrize(("adjacent", "error_code"), [
    ("lava", "unsafe_lava_dig"), ("flowing_lava", "unsafe_lava_dig"),
    ("unknown", "unstick_dig_unverified"), ("dry", None), ("diagonal_lava", None),
])
def test_nether_unstick_preserves_blocks_holding_back_lava_from_the_side(adjacent, error_code):
    target = (1493, 23, 1456)
    hazard = (1493, 23, 1457)
    blocks = {target: "netherrack"}
    if adjacent in {"lava", "flowing_lava"}:
        blocks[hazard] = adjacent
    elif adjacent == "diagonal_lava":
        blocks[(1494, 23, 1457)] = "lava"
    digs = []
    moves = []
    events = []

    class BastionBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [1493.5, 21, 1456.5], "on_ground": True, "dimension": "the_nether",
                    "in_lava": False, "ready": True, "alive": True,
                }
            if action == "events":
                return {"events": []}
            if action == "get_block":
                pos = tuple(body["pos"])
                if adjacent == "unknown" and pos == hazard:
                    raise httpx.ReadTimeout("could not read the side of the block")
                name = blocks.get(pos, "netherrack" if pos[1] <= 20 else "air")
                return {"name": name, "diggable": name == "netherrack"}
            if action == "dig":
                cell = tuple(body["pos"])
                digs.append(cell)
                blocks.pop(cell, None)
                return {"ok": True, "dug": "netherrack"}
            if action == "control":
                moves.append(body)
            return {"ok": True}

    bot = BastionBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", lambda: json.dumps(skills.unstick())))

    if error_code is None:
        assert result["ok"] and digs == [target] and moves
    else:
        assert result["error"]["code"] == error_code and result["requires_replan"]
        assert result["error"]["target"] == list(target)
        assert digs == [] and moves == []
        assert blocks[target] == "netherrack"
    if adjacent in {"lava", "flowing_lava"}:
        blocked, = [event for event in events if event["type"] == "guardian.lava_dig_blocked"]
        assert blocked["target"] == list(target) and blocked["lava_pos"] == list(hazard)

