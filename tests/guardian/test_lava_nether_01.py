from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("api_error", [False, True])
def test_hurt_interrupted_move_enters_lava_reflex_with_distant_hostile(monkeypatch, api_error):
    moves = []
    status = {
        "pos": [-395.9, 53, -1889.5], "health": 10.3, "food": 20, "dimension": "the_nether",
        "in_lava": True, "alive": True, "ready": True, "on_ground": False,
        "nearest_hostile": {"id": 44, "name": "blaze", "dist": 16.7},
    }

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "move_to":
                moves.append(body)
                if api_error:
                    raise ApiError("interrupted", "stopped because of hurt")
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    def escape():
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    bot = LedgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    move = {"entity_id": 44, "range": 3}
    receipt = json.loads(guard.run_tool_with_reflex("mc_move_to", lambda: json.dumps(bot.call("move_to", move))))

    assert moves == [move]
    assert receipt["preempted_by_reflex"] and receipt["reflex_mode"] == "lava_escape"
    assert status["in_lava"] is False


@pytest.mark.parametrize("change", ["stationary", "drifted", "hurt", "landed"])
def test_bug_009_stationary_lava_escape_keeps_retrying_without_duplicate_cancel(monkeypatch, change):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3567.0, 64.6, 1790.3], "health": 20, "food": 20, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "ready": True, "alive": True, "last_death": {"t": 10},
    }
    hurts = []
    escapes = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in hurts if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape(**_kwargs):
        escapes.append(True)
        if len(escapes) == 2 and change == "drifted":
            status["pos"] = [3568, 64.6, 1790.3]
        if len(escapes) == 2 and change == "hurt":
            status["health"] = 18
            hurts.append({"seq": 1, "type": "hurt", "source": "lava"})
        if len(escapes) == 2 and change == "landed":
            status.update(in_lava=False, on_ground=True)
        return {"ok": not status["in_lava"], "in_lava": status["in_lava"], "pos_after": status["pos"]}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    assert guard.poll()["requires_replan"] is True
    clock[0] += 2.1
    result = guard.poll()
    assert result["requires_replan"] is (change != "stationary")
    assert len(escapes) == 2
    if change == "stationary":
        clock[0] += 8.0
        assert guard.poll()["requires_replan"] is True


def test_nether_deep_lava_preemption_prioritizes_escape_over_nearby_mob(monkeypatch):
    """Reproduces glm_glm_2_nether_bastion: in_lava overrides nearby zombified_piglin during preemption & deep lava swims up first."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    state = {"in_lava": True, "pos": [6.4, 27.6, -815.0], "attacked": False}
    action_log: list[tuple[str, dict]] = []

    class _DeepLavaNetherBot:
        _last_event_seq = 0

        def status(self):
            return {
                "pos": list(state["pos"]),
                "health": 16.8,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": False,
                "in_lava": state["in_lava"],
                "dimension": "minecraft:the_nether",
                "yaw": 0.0,
                "nearest_hostile": {"id": 88302, "name": "zombified_piglin", "dist": 18.9, "pos": [25.0, 31.0, -815.0]},
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            action_log.append((action, payload))
            if action == "status":
                return self.status()
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "netherrack", "count": 32}]}
            if action == "get_block":
                p = payload["pos"]
                # Deep lava below y=30; solid bank at z=-813, y=30
                if p == [6, 30, -813]:
                    return {"name": "netherrack"}
                if p[1] in (27, 28, 29, 30) and p[2] <= -814:
                    return {"name": "lava"}
                if p[1] < 27:
                    return {"name": "netherrack"}
                return {"name": "air"}
            if action == "control" and payload.get("jump"):
                if state["pos"][1] < 30.2:
                    state["pos"] = [6.4, 30.5, -815.0]
                elif payload.get("forward"):
                    state["in_lava"] = False
                    state["pos"] = [6.5, 31.0, -812.5]
            if action in ("attack", "move_to"):
                state["attacked"] = True
            return {"ok": True}

    guard = GenerationGuard(_DeepLavaNetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    # 1) Even when preemption trigger names a nearby mob (`zombified_piglin`), handle_preemption escapes lava FIRST!
    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_entity_id": 88302, "primary_type": "zombified_piglin", "distance": 18.9},
        interrupted_name="mc_skill_mine_dfs",
    )
    assert receipt["reflex_mode"] == "lava_escape"
    assert state["in_lava"] is False
    assert state["attacked"] is False
    assert any(act == "control" and pl.get("jump") for act, pl in action_log)

    # 2) Direct SubcorticalInstinctController call while in_lava also prioritizes lava_escape
    state["in_lava"] = True
    state["pos"] = [6.4, 27.6, -815.0]
    out = SubcorticalInstinctController().on_encounter(
        CombatEnv(_DeepLavaNetherBot()),
        {"source": "boundary_threat", "primary_entity_id": 88302, "primary_type": "zombified_piglin", "distance": 18.9},
    )
    assert out["mode"] == "lava_escape"
    assert out["handled"] is True
    assert state["in_lava"] is False


@pytest.mark.parametrize("situation", [
    "identified", "unattributed", "other_attacker", "second_hostile", "wall",
    "out_of_reach", "escaped", "respawned", "different_life_after_scan", "weapon_changed",
])
def test_armed_nether_bot_strikes_once_after_a_lone_piglin_blocks_retreat(monkeypatch, situation):
    pos = [38023.3, 66.9, 39359.4]
    distance = 3.0 if situation == "out_of_reach" else 2.2
    piglin = {
        "id": 839861, "name": "piglin", "pos": [pos[0] + distance, pos[1], pos[2]],
        "dist": distance, "blocked_by_wall": situation == "wall",
    }
    sword = {"name": "iron_sword", "count": 1}
    status = {
        "pos": pos, "health": 7.5, "food": 20, "ready": True, "alive": True, "on_ground": True,
        "dimension": "the_nether", "last_death": {"t": 1}, "nearest_hostile": piglin, "held": sword,
    }
    hit = {"seq": 1, "type": "hurt", "source": "mob_attack"}
    if situation != "unattributed":
        hit["source_entity"] = {**piglin, "id": piglin["id"] + int(situation == "other_attacker")}
    attacks = []
    stop_calls = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [sword], "held": status["held"], "offhand": None}
            if action == "get_entities":
                assert body == {"types": ["hostile"], "radius": 6} and kwargs["timeout_s"] == 0.35
                if situation == "different_life_after_scan":
                    status["last_death"] = {"t": 2}
                if situation == "weapon_changed":
                    status["held"] = {"name": "iron_pickaxe"}
                return {"entities": [piglin, {"id": 839862, "name": "wither_skeleton"}] if situation == "second_hostile" else [piglin]}
            if action == "attack":
                attacks.append(body)
                return {"hits": 1, "killed": False, "hit_log": [{"hit": 1, "entity_id": piglin["id"]}]}
            if action == "stop":
                stop_calls.append(action)
                return {"ok": True}
            pytest.fail(f"Unexpected mutation while cornered: {action}")

    def retreat(*_args, **_kwargs):
        if situation == "escaped":
            return {"mode": "flee", "ok": True, "moved_dist": 4.0}
        return {"mode": "flee", "ok": False, "reason": "no_safe_retreat", "respawned": situation == "respawned"}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda _status: None)
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    if situation in {"identified", "unattributed"}:
        attack, = attacks
        assert attack["entity_id"] == piglin["id"] and attack["max_hits"] == 1 and attack["chase"] is False
        assert attack["feedback"] is True and attack["timeout"] <= 350
        strike = incident["actions"][-1]
        assert strike["action"] == "cornered_melee_strike"
        assert strike["result"]["ok"] is True and strike["result"]["killed"] is False
    else:
        assert attacks == []
    assert stop_calls == []


def test_r15_nether_bastion_wide_lava_steers_via_find_blocks_onto_elevated_ledge():
    """Reproduces r15 hy4_hy_6_nether_bastion: wide Nether lava lake steers toward elevated basalt ledge via find_blocks+move_to."""
    from mc_agent.skills.combat_env import CombatEnv

    st = {
        "pos": [227.4, 31.5, 240.0],
        "in_lava": True,
        "yaw": 3.14159,  # facing South deeper into lava lake!
    }
    calls: list[tuple[str, dict]] = []

    class _NetherLavaLakeBot:
        def status(self):
            return {
                "pos": list(st["pos"]),
                "health": 18.0,
                "alive": True,
                "ready": True,
                "in_lava": st["in_lava"],
                "yaw": st["yaw"],
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 61}]}
            if action == "get_block":
                p = pl.get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 240)}:
                    return {"name": "air"}
                if tuple(p) in {(229, 32, 234), (235, 31, 240), (235, 32, 240)}:
                    return {"name": "basalt"}  # (235,31,240) has solid at y+1 -> sheer wall, skip!
                return {"name": "lava"}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "basalt", "pos": [235, 31, 240], "dist": 7.6},
                        {"name": "netherrack", "pos": [229, 32, 234], "dist": 6.2},
                    ]
                }
            if action == "move_to":
                # A pathfinder can return successfully from the liquid at the edge of its tolerance.
                st["pos"] = [pl["x"] - pl["range"], pl["y"], pl["z"]]
                st["in_lava"] = st["pos"][0] - 0.3 < 229
                assert kwargs["timeout_s"] == 2.0 and kwargs["allow_reauth"] is False
                return {"ok": True}
            return {"ok": True}

    out = CombatEnv(_NetherLavaLakeBot()).escape_lava()
    assert out["ok"] is True and out["in_lava"] is False
    assert "lava_move_to_shore" in out["actions"]
    ctrl = next(pl for act, pl in calls if act == "control" and pl.get("forward"))
    assert ctrl["ms"] >= 1350
    mv = next(pl for act, pl in calls if act == "move_to")
    assert (mv["x"], mv["y"], mv["z"]) == (229.5, 33.0, 234.5)

    # Pathfinding ReadTimeout inside lava move_to must /stop and continue into place fallback rather than throwing!
    import httpx
    st_rt = {"pos": [227.4, 31.5, 240.0], "in_lava": True}
    rt_calls: list[str] = []

    class _LavaMoveTimeoutBot:
        def status(self):
            return {**st_rt, "health": 12.0, "alive": True, "ready": True, "yaw": 3.14, "dimension": "minecraft:the_nether"}

        def call(self, action, body=None, **kwargs):
            rt_calls.append(action)
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": [{"name": "netherrack", "pos": [229, 32, 234], "dist": 5.9}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 240)}:
                    return {"name": "air"}
                return {"name": "lava"}
            if action == "move_to":
                raise httpx.ReadTimeout("simulated pathfinder timeout in lava")
            if action == "place":
                st_rt["in_lava"] = False
            return {"ok": True}

    out_rt = CombatEnv(_LavaMoveTimeoutBot()).escape_lava()
    assert out_rt["ok"] is True and out_rt["in_lava"] is False
    assert "lava_move_to_shore_timeout" in out_rt["actions"]
    assert "stop" in rt_calls
    assert "lava_place_cobblestone" in out_rt["actions"]


def test_bug026_ranged_arrow_cover_or_flee_lava_sneak_armor_and_cooldown(monkeypatch):
    """Covers BUG-026: arrow hurts cover/flee instead of static shield-block, auto-armor/torches, lava-edge sneak+seal, and 10s ranged cooldown."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    clock = [2000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    # 1) Arrow hit WITH unblocking shield in inventory -> NEVER stops at static raise_shield!
    class _ArrowCoverSkills:
        class _Eng:
            nodes = {}
            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def __init__(self, *, cover_ok: bool):
            self.cover_ok = cover_ok

        def dig_cover_pocket(self):
            return {"ok": True, "mode": "cover_pocket"} if self.cover_ok else {"ok": False, "reason": "no_solid_wall"}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 12.0}

    def _make_arrow_bot():
        class _ArrowShieldBot:
            _last_event_seq = 0
            def call(self, action, body=None, **kwargs):
                if action == "status":
                    return {
                        "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                        "in_water": False, "in_lava": False, "on_ground": True, "blocking": False,
                        "nearest_hostile": {"id": 61, "name": "skeleton", "dist": 10.0},
                    }
                if action == "events":
                    since = int((kwargs.get("params") or {}).get("since", 0))
                    rows = [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow", "source_entity": {"id": 61}}] if since < 1 else []
                    return {"events": rows, "last_seq": 1}
                if action == "inventory":
                    return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}], "offhand": None}
                return {"ok": True}
        return _ArrowShieldBot()

    g_cov = GenerationGuard(_make_arrow_bot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=True))
    inc_cov = g_cov.poll()
    assert inc_cov is not None
    assert [a["action"] for a in inc_cov["actions"]] == ["dig_cover"]  # not standing still raising shield!

    g_flee = GenerationGuard(_make_arrow_bot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=False))
    inc_flee = g_flee.poll()
    assert inc_flee is not None
    flee_names = [a["action"] for a in inc_flee["actions"]]
    assert "dig_cover" in flee_names and "combat_or_flee" in flee_names

    # Skeleton at 8.5m (>5m) with shield+sword prioritizes cover_or_flee, and blaze skips fire-trap cover_pocket!
    class _RangedCaveEnvBot:
        def __init__(self, mob_name: str, dist: float):
            self.mob_name = mob_name
            self.dist = dist
            self.fled = False

        def status(self):
            return self.call("status")

        def inventory(self):
            return self.call("inventory")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 40.0, 0.5], "health": 20.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False,
                }
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}],
                    "offhand": {"name": "shield", "count": 1},
                    "held": {"name": "iron_sword", "count": 1},
                    "armor": {"head": None, "chest": None, "legs": None, "feet": None},
                }
            if action == "get_entities":
                return {"entities": [{"id": 70, "name": self.mob_name, "pos": [0.5 + self.dist, 40.0, 0.5], "dist": self.dist}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 40, 0])
                return {"name": "stone" if p[1] < 40 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "move_to":
                self.fled = True
                return {"ok": True}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    out_skel = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("skeleton", 8.5)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 70, "primary_type": "skeleton", "pos": [9.0, 40.0, 0.5], "distance": 8.5},
    )
    assert out_skel["mode"] == "cover_or_flee"

    out_blaze = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("blaze", 6.0)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 70, "primary_type": "blaze", "pos": [6.5, 40.0, 0.5], "distance": 6.0},
    )
    assert out_blaze["mode"] == "cover_or_flee"
    assert not any(a["action"] == "cover_pocket" for a in out_blaze["actions"])

    # 2) Cave gear (armor slots -> head/torso/legs/feet + offhand shield + cave torch) & lava-edge sneak+seal
    gear_calls: list[tuple[str, dict]] = []

    class _CaveAndLavaBot:
        _last_event_seq = 0
        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            gear_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [10.5, 40.0, 10.5], "health": 18.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False, "sneaking": False, "blocking": False,
                    "dimension": "minecraft:overworld",
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {
                    "items": [
                        {"name": "iron_helmet", "count": 1},
                        {"name": "iron_chestplate", "count": 1},
                        {"name": "iron_leggings", "count": 1},
                        {"name": "iron_boots", "count": 1},
                        {"name": "shield", "count": 1},
                        {"name": "torch", "count": 8},
                        {"name": "cobblestone", "count": 16},
                    ],
                    "armor": {"head": None, "chest": None, "legs": None, "feet": None},
                    "offhand": None,
                }
            if action == "get_block":
                p = pl.get("pos", [0, 40, 0])
                if tuple(p) == (11, 39, 10):
                    return {"name": "lava"}
                if p[1] < 40:
                    return {"name": "stone"}
                return {"name": "air"}
            return {"ok": True}

    g_prep = GenerationGuard(_CaveAndLavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    g_prep.prime()
    equips = [(pl.get("item"), pl.get("slot")) for act, pl in gear_calls if act == "equip"]
    assert ("iron_helmet", "head") in equips
    assert ("iron_chestplate", "torso") in equips
    assert ("iron_leggings", "legs") in equips
    assert ("iron_boots", "feet") in equips
    assert ("shield", "off-hand") in equips

    gear_calls.clear()
    note_edge = g_prep.stabilize_after_tools(['{"cave": true, "lava": true}'])
    assert note_edge is not None
    assert "岩浆边潜行" in note_edge
    assert "补放洞穴火把" in note_edge
    assert any(act == "posture" and pl.get("sneak") is True for act, pl in gear_calls)
    assert any(act == "place" and pl == {"item": "cobblestone", "pos": [11, 39, 10]} for act, pl in gear_calls)
    assert any(act == "place" and pl == {"item": "torch", "pos": [10, 40, 10]} for act, pl in gear_calls)

    # 3) pre_hurt_ranged suppresses repeats for 10s AND on_fire ticks don't clear suppress_until!
    class _BlazeOnFireDebounceBot:
        _last_event_seq = 0
        fire_seq = 0
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False,
                    "nearest_hostile": {"id": 91, "name": "skeleton", "pos": [11.0, 64.0, 0.5], "dist": 10.5},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": self.fire_seq, "type": "hurt", "health": 16.0, "source": "on_fire"}] if self.fire_seq > since else []
                return {"events": rows, "last_seq": self.fire_seq}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "netherrack" if p[1] < 64 else "air"}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    blaze_bot = _BlazeOnFireDebounceBot()
    g_cd = GenerationGuard(blaze_bot, EventSink(lambda _: None), checkpoint=lambda: None)
    inc_r1 = g_cd.poll()
    assert inc_r1 is not None and "pre_hurt_ranged" in inc_r1["reasons"]
    clock[0] += 5.0
    blaze_bot.fire_seq = 1  # ambient on_fire tick in between
    assert g_cd.poll() is None  # no fake cancel on non-combat fire tick AND pre_hurt_ranged 10s cooldown stays intact!
    clock[0] += 1.0
    assert g_cd.poll() is None  # still inside 10s pre_hurt_ranged cooldown!

    # 4) Known shooter pos on arrow hurt sidesteps ~2m perpendicularly BEFORE equip/raise_shield + cover/flee,
    #    and piglin_brute / pre_hurt_ranged immediately equip+raise shield on entry!
    side_calls: list[tuple[str, dict]] = []

    class _KnownShooterArrowBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            side_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False, "blocking": False,
                    "nearest_hostile": {"id": 88, "name": "skeleton", "pos": [10.5, 64.0, 0.5], "dist": 10.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow", "source_entity": {"id": 88, "pos": [10.5, 64.0, 0.5]}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}], "offhand": None}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            return {"ok": True}

    g_side = GenerationGuard(_KnownShooterArrowBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=False))
    inc_side = g_side.poll()
    assert inc_side is not None
    side_acts = [a["action"] for a in inc_side["actions"]]
    assert side_acts[:4] == ["lateral_sidestep", "equip_shield", "face_attacker", "raise_shield"]
    assert side_acts[-1] == "combat_or_flee"

    out_brute = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("piglin_brute", 2.9)),
        {"source": "pre_hurt_melee", "primary_entity_id": 70, "primary_type": "piglin_brute", "pos": [3.4, 40.0, 0.5], "distance": 2.9},
    )
    assert any(a["action"] == "raise_shield_entry" for a in out_brute["actions"])


@pytest.mark.parametrize("covered", [False, True])
def test_tool_boundary_guards_an_exposed_two_block_drop_into_lava(covered):
    calls = []
    lava = [-1869, 34, -3689]

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [-1868.5, 36, -3687.1], "dimension": "the_nether", "health": 12.76,
                        "food": 20, "on_ground": True, "alive": True, "ready": True, "blocking": True}
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "basalt", "count": 4}]}
            if action == "get_block":
                x, y, z = body["pos"]
                if body["pos"] == lava:
                    return {"name": "lava"}
                if x == lava[0] and z == lava[2] and y == 35 and not covered:
                    return {"name": "air"}
                return {"name": "netherrack" if y < 36 else "air"}
            calls.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    note = guard.stabilize_after_tools()

    if covered:
        assert calls == [] and note is None
    else:
        assert calls == [
            ("posture", {"sneak": True, "shield": True, "jump": False}),
            ("place", {"item": "basalt", "pos": lava}),
        ]
        assert note is not None and "岩浆边潜行封边" in note


@pytest.mark.parametrize("outcome", ["dry", "fell", "respawned", "stale_target", "distant_target"])
def test_tool_boundary_checks_the_reported_lava_flow_above_the_ledge(monkeypatch, outcome):
    status = {
        "pos": [17086.5, 69, 29993.5], "dimension": "overworld", "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "in_lava": False,
    }
    flow = [17087, 71, 29992] if outcome != "distant_target" else [17090, 71, 29992]
    lower_lava = [17087, 68, 29993]
    actions = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                pos = body["pos"]
                lava = pos == lower_lava or (pos == flow and outcome != "stale_target")
                return {"name": "lava" if lava else "air"}
            if action == "place":
                actions.append(body["pos"])
                if body["pos"] == flow and outcome in {"fell", "respawned"}:
                    status.update(in_lava=True, on_ground=False)
                    if outcome == "respawned":
                        status["last_death"] = {"t": 1}
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape():
        actions.append("escape")
        status.update(in_lava=False, on_ground=True)
        return {"ok": True}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    receipt = json.dumps({
        "ok": False, "error": {"code": "interact_flowing_fluid", "block": "lava", "target": flow, "level": 4},
    })

    note = guard.stabilize_after_tools([receipt])

    assert actions == {
        "dry": [flow, lower_lava], "fell": [flow, "escape"], "respawned": [flow],
        "stale_target": [lower_lava], "distant_target": [lower_lava],
    }[outcome]
    assert ("封边后脱离岩浆" in note) is (outcome == "fell")


@pytest.mark.parametrize("case", ["diagonal", "above", "duplicate", "stale_position", "dry", "refused"])
def test_tool_boundary_checks_the_staircase_lava_blocker(case):
    pos = [1503.4, 66, 1417.5]
    lava_below = [1503, 65, 1416]
    blocker = {
        "diagonal": [1502, 66, 1418], "above": [1502, 68, 1417], "duplicate": lava_below,
    }.get(case, [1502, 66, 1418])
    status = {
        "pos": pos, "dimension": "the_nether", "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "in_lava": False,
    }
    placements = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "basalt", "count": 4}]}
            if action == "get_block":
                cell = body["pos"]
                if cell in placements and not (cell == blocker and case == "refused"):
                    return {"name": "basalt"}
                lava = cell == lava_below or (cell == blocker and case != "dry")
                return {"name": "lava" if lava else "air"}
            if action == "place":
                placements.append(body["pos"])
                return {"ok": not (body["pos"] == blocker and case == "refused")}
            assert action == "posture"
            return {"ok": True}

    events = EventSink(lambda _: None)
    guard = GenerationGuard(LavaBot(), events, checkpoint=lambda: None)
    receipt = json.dumps({
        "ok": False, "stop_reason": "fluid_or_bedrock", "direction": "south", "vertical": "down",
        "pos_after": [1506.4, 66, 1417.5] if case == "stale_position" else pos,
        "blocker": {"block": "lava", "pos": blocker, "relation": "in_path" if case == "above" else "adjacent_fluid"},
    })

    guard.stabilize_after_tools([receipt])

    expected_placements = [blocker, lava_below] if case in {"diagonal", "above", "refused"} else [lava_below]
    assert placements == expected_placements
    completed = next(event for event in events.events if event["type"] == "guardian.action.completed")
    assert completed["result"]["sealed_positions"] == (expected_placements[1:] if case == "refused" else expected_placements)

