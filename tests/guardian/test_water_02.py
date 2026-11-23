from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_non_combat_fire_fall_hurt_skips_shield_spam_and_water_trident_equips_shield(monkeypatch):
    """Covers r12 ds_ds_3_village on_fire/fall shield-spam & hy4_hy_9_deep_water trident offhand shield block."""
    clock = [800.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    # 1) Non-combat on_fire / fall hurt without close hostile must NOT raise shield or repeatedly cancel LLM
    calls: list[tuple[str, dict]] = []
    ev_state = {"seq": 1, "source": "on_fire", "hp": 17.0, "food": 20}

    class _EnvHurtBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            calls.append((action, payload))
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": ev_state["hp"],
                    "food": ev_state["food"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = (
                    [{"seq": ev_state["seq"], "type": "hurt", "health": ev_state["hp"], "source": ev_state["source"]}]
                    if ev_state["seq"] > since
                    else []
                )
                return {"events": rows, "last_seq": ev_state["seq"]}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}]}
            return {"ok": True}

    guard = GenerationGuard(_EnvHurtBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll() is None  # on_fire tick with hp=17, no water_bucket -> zero fake shield/cancel!
    clock[0] += 5.0
    ev_state["seq"] = 2
    ev_state["source"] = "fall"
    ev_state["hp"] = 11.0
    assert guard.poll() is None  # mild fall with hp=11, no hostile -> zero fake raise_shield!
    assert not any(a in ("posture", "look") for a, _ in calls)

    # Critical fall (hp <= 6.0) still notifies once for replan, without raising shield
    clock[0] += 5.0
    ev_state["seq"] = 3
    ev_state["hp"] = 4.0
    inc_crit = guard.poll()
    assert inc_crit is not None and "fresh_hurt" in inc_crit["reasons"]
    assert not any(a["action"] == "raise_shield" for a in inc_crit["actions"])

    # 2) Open-ocean trident hurt when escape_water fails equips shield from bag & blocks while jumping!
    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: {"ok": False, "in_water": True, "pos_after": [0.5, 62.5, 0.5]},
    )
    water_calls: list[tuple[str, dict]] = []

    class _DeepWaterTridentBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            water_calls.append((action, payload))
            if action == "status":
                return {
                    "pos": [0.5, 62.5, 0.5],
                    "health": 10.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": False,
                    "on_ground": False,
                    "blocking": False,
                    "nearest_hostile": {"id": 44, "name": "drowned", "dist": 11.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 10.0, "source": "trident", "source_entity": {"id": 44}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": None}
            return {"ok": True}

    guard_w = GenerationGuard(_DeepWaterTridentBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_DeepWaterTridentBot()))
    inc_w = guard_w.poll()
    assert inc_w is not None
    act_names = [a["action"] for a in inc_w["actions"]]
    assert "escape_water" in act_names
    assert "equip_shield" in act_names
    assert "raise_shield" in act_names
    assert any(act == "equip" and pl == {"item": "shield", "slot": "off-hand"} for act, pl in water_calls)
    assert any(act == "posture" and pl.get("shield") is True and pl.get("jump") is True for act, pl in water_calls)

    # 3) If escape_water fails with not_connected/bot-dead while submerged, NEVER fire follow-up equip/look/posture
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: (_ for _ in ()).throw(ApiError("not_connected", "bot not connected")),
    )
    dead_water_calls: list[str] = []

    class _DisconnectedSubmergedBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            dead_water_calls.append(action)
            if action == "status":
                return {
                    "pos": [0.5, 58.0, 0.5],
                    "health": 10.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": True,
                    "on_ground": False,
                    "blocking": False,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 10.0, "source": "trident", "source_entity": {"id": 44}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": None}
            return {"ok": True}

    guard_dc = GenerationGuard(_DisconnectedSubmergedBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_DisconnectedSubmergedBot()))
    inc_dc = guard_dc.poll()
    assert inc_dc is not None
    # Shield is equipped & raised BEFORE escape_water attempts retreat; once escape_water fails with not_connected,
    # NO follow-up escape_to_shore or post-failure status/equip/posture may fire.
    assert [a["action"] for a in inc_dc["actions"]] == ["equip_shield", "face_attacker", "raise_shield", "escape_water"]
    assert dead_water_calls.count("equip") == 1 and dead_water_calls.count("posture") == 1
    assert "find_blocks" not in dead_water_calls and "move_to" not in dead_water_calls

    # If equip itself fails with not_connected, zero follow-up look/posture/escape_water/escape_to_shore may fire.
    class _EquipFailSubmergedBot(_DisconnectedSubmergedBot):
        def call(self, action, body=None, **kwargs):
            if action == "equip":
                dead_water_calls.append(action)
                raise ApiError("not_connected", "bot not connected")
            return super().call(action, body, **kwargs)

    dead_water_calls.clear()
    guard_eq_dc = GenerationGuard(_EquipFailSubmergedBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_EquipFailSubmergedBot()))
    inc_eq_dc = guard_eq_dc.poll()
    assert inc_eq_dc is not None
    assert [a["action"] for a in inc_eq_dc["actions"]] == ["equip_shield"]
    assert not any(a in dead_water_calls for a in ("look", "posture", "find_blocks", "move_to"))

    # 4) escape_lava wide shore detection (4.5m away) & death-respawn teleport (>64m, ready=False) -> ok=False, respawned=True
    look_yaws: list[float] = []

    class _WideLavaPoolBot:
        def __init__(self, *, respawn_teleport: bool = False):
            self.respawn = respawn_teleport
            self.pos = [-23.5, 30.0, 778.0]
            self.in_lava = True
            self.ready = True

        def status(self):
            return {
                "pos": list(self.pos),
                "health": 10.0,
                "alive": True,
                "ready": self.ready,
                "in_lava": self.in_lava,
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                p = pl["pos"]
                # Shore only exists 4.5m to the East (x=-19), NOT along initial yaw=0.0!
                if p[1] == 30 and abs(p[0] - (-19)) <= 1 and abs(p[2] - 778) <= 2:
                    return {"name": "netherrack"}
                if p[1] == 30:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "look":
                look_yaws.append(round(float(pl["yaw"]), 2))
            if action == "control" and self.respawn:
                self.pos = [0.5, 64.1, 800.5]
                self.in_lava = False
                self.ready = False
            return {"ok": True}

    out_wide = CombatEnv(_WideLavaPoolBot(respawn_teleport=False)).escape_lava()
    assert look_yaws and abs(look_yaws[0]) > 0.5  # Turned away from initial yaw=0.0 toward East bank!
    out_tp = CombatEnv(_WideLavaPoolBot(respawn_teleport=True)).escape_lava()
    assert out_tp["ok"] is False and out_tp.get("respawned") is True


def test_bug023_deep_water_trident_shield_first_moves_to_dry_shore_and_3hit_cooldown(monkeypatch):
    """Covers BUG-023: trident/drowned in deep water raises shield first, moves to dry shore, and blocks reentry 30s on 3rd hit."""
    from mc_agent.skills.combat_env import CombatEnv
    from mc_agent.skills.safety import SafetyEmergency, check_and_stabilize

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    escape_calls: list[str] = []
    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: escape_calls.append("escape_water") or {"ok": False, "in_water": True},
    )

    # 1) Trident hit in deep water: equip+raise shield BEFORE escape, ignore y=50 ocean floor, move_to dry shore at [12.5, 63.0, -7.5]
    state = {
        "in_water": True,
        "submerged": True,
        "pos": [0.5, 61.5, 0.5],
        "hp": 15.5,
        "blocking": False,
        "offhand": None,
        "hurt_seq": 1,
        "shore_succeeds": True,
    }
    calls_1: list[tuple[str, dict]] = []

    class _ShoreOceanBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def inventory(self):
            return self.call("inventory")

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            calls_1.append((action, payload))
            if action == "status":
                return {
                    "pos": list(state["pos"]),
                    "health": state["hp"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": state["in_water"],
                    "submerged": state["submerged"],
                    "on_ground": not state["in_water"],
                    "blocking": state["blocking"],
                    "oxygen": 240,
                    "nearest_hostile": {"id": 44, "name": "drowned", "pos": [7.5, 62.0, 0.5], "dist": 7.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = (
                    [{"seq": state["hurt_seq"], "type": "hurt", "health": state["hp"], "source": "trident", "source_entity": {"id": 44, "pos": [7.5, 62.0, 0.5]}}]
                    if state["hurt_seq"] > since
                    else []
                )
                return {"events": rows, "last_seq": state["hurt_seq"]}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": state["offhand"]}
            if action == "equip" and payload.get("item") == "shield":
                state["offhand"] = {"name": "shield", "count": 1}
                return {"ok": True}
            if action == "posture":
                state["blocking"] = bool(payload.get("shield"))
                return {"ok": True}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "sand", "pos": [1, 50, 1], "dist": 11.5},  # ocean floor below waterline -> must ignore
                        {"name": "grass_block", "pos": [12, 62, -8], "dist": 14.3},
                        {"name": "stone", "pos": [20, 62, 10], "dist": 21.7},
                    ]
                }
            if action == "block_at":
                p = payload.get("pos", [0, 64, 0])
                if p[0] == 12 and p[2] == -8 and p[1] in (63, 64):
                    return {"name": "air"}
                return {"name": "water"}
            if action == "move_to":
                if state["shore_succeeds"]:
                    state["pos"] = [payload["x"], payload["y"], payload["z"]]
                    state["in_water"] = False
                    state["submerged"] = False
                return {"ok": True}
            return {"ok": True}

    emitted: list[dict] = []
    guard = GenerationGuard(_ShoreOceanBot(), EventSink(emitted.append), checkpoint=lambda: None, skills=MacroSkills(_ShoreOceanBot()))
    inc = guard.poll()
    assert inc is not None
    act_names = [a["action"] for a in inc["actions"]]
    assert act_names == ["equip_shield", "face_attacker", "raise_shield", "escape_water", "escape_to_shore"]
    move_calls = [pl for act, pl in calls_1 if act == "move_to"]
    assert move_calls and (move_calls[0]["x"], move_calls[0]["y"], move_calls[0]["z"]) == (12.5, 63.0, -7.5)
    assert state["in_water"] is False

    # 2) Three consecutive hurts still in water (<3s apart) -> 3rd hit forces shore escape & sets 30s no-water cooldown
    emitted.clear()
    escape_calls.clear()
    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 14.0, "blocking": False, "shore_succeeds": False, "hurt_seq": 2})
    bot2 = _ShoreOceanBot()
    bot2._last_event_seq = 1
    guard2 = GenerationGuard(bot2, EventSink(emitted.append), checkpoint=lambda: None, skills=MacroSkills(bot2))

    # Hit 1
    inc1 = guard2.poll()
    assert inc1 is not None
    assert guard2._water_hurt_streak == 1
    assert guard2.no_water_until < 0

    # Hit 2 (+1.2s, inside normal 3s window -> must NOT be dropped!)
    clock[0] += 1.2
    state.update({"hurt_seq": 3, "hp": 10.0, "blocking": False})
    inc2 = guard2.poll()
    assert inc2 is not None
    assert guard2._water_hurt_streak == 2

    # Hit 3 (+1.2s) -> climb succeeds on 3rd try, forces escape_to_shore (skipping slow escape_water) and activates 30s cooldown
    clock[0] += 1.2
    escape_calls.clear()
    state.update({"hurt_seq": 4, "hp": 6.5, "blocking": True, "shore_succeeds": True})
    inc3 = guard2.poll()
    assert inc3 is not None
    assert "water_avoid_cooldown" in inc3["reasons"]
    assert "禁止下水冷却中" in inc3["note"]
    assert guard2.no_water_until == pytest.approx(clock[0] + 30.0)
    assert any(ev.get("type") == "guardian.no_water_cooldown" and ev.get("duration_s") == 30.0 for ev in emitted)
    assert escape_calls == []
    assert [a["action"] for a in inc3["actions"]] == ["escape_to_shore"]
    assert state["in_water"] is False

    # Step back into water 6s later (within 30s cooldown, no new hurt) -> forced back ashore immediately!
    clock[0] += 6.0
    calls_1.clear()
    state.update({"in_water": True, "submerged": False, "pos": [10.5, 62.0, -7.5], "shore_succeeds": True})
    inc_reentry = guard2.poll()
    assert inc_reentry is not None
    assert "water_avoid_cooldown" in inc_reentry["reasons"]
    assert [a["action"] for a in inc_reentry["actions"]] == ["escape_to_shore"]
    assert state["in_water"] is False

    # 3) handle_preemption & stabilize_after_tools & safety.check_and_stabilize water-shield priority
    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 5.0, "blocking": False, "offhand": None, "shore_succeeds": True})
    guard_pre = GenerationGuard(_ShoreOceanBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_ShoreOceanBot()))
    rec = guard_pre.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_entity_id": 44, "primary_type": "drowned", "primary_pos": [7.5, 62.0, 0.5], "distance": 7.0},
        interrupted_name="mc_move_to",
    )
    assert rec["reflex_mode"] == "water_surface"
    assert state["blocking"] is True
    assert state["in_water"] is False

    state.update({"in_water": True, "submerged": False, "pos": [0.5, 62.2, 0.5], "hp": 11.0, "blocking": False, "offhand": None, "hurt_seq": 5, "shore_succeeds": True})
    calls_1.clear()
    bot_b = _ShoreOceanBot()
    bot_b._last_event_seq = 4
    guard_b = GenerationGuard(bot_b, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot_b))
    b_note = guard_b.stabilize_after_tools(['{"ok": false, "in_water": true}'])
    assert b_note is not None and "游向最近干燥陆地登岸" in b_note
    assert any(act == "equip" for act, _ in calls_1)

    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 4.0, "blocking": False, "offhand": None})
    calls_1.clear()
    with pytest.raises(SafetyEmergency):
        check_and_stabilize(_ShoreOceanBot())
    assert any(act == "equip" and pl == {"item": "shield", "slot": "off-hand"} for act, pl in calls_1)
    assert any(act == "posture" and pl.get("shield") is True and pl.get("jump") is True for act, pl in calls_1)

    # 4) Open ocean with zero shore blocks nearby lunges upward and retreats AWAY from threat_pos
    open_calls: list[tuple[str, dict]] = []

    class _OpenSeaNoLandBot:
        def status(self):
            return {
                "pos": [0.5, 61.2, 0.5],
                "health": 10.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": True,
                "submerged": True,
                "oxygen": 200,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            open_calls.append((action, payload))
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "block_at":
                return {"name": "water"}
            return {"ok": True}

    env_res = CombatEnv(_OpenSeaNoLandBot()).escape_deep_water_to_shore([7.5, 62.0, 0.5], keep_shield=True)
    assert env_res["mode"] == "surface_retreat"
    mv_open = [pl for act, pl in open_calls if act == "move_to"]
    assert mv_open and mv_open[0]["x"] < 0.0 and mv_open[0]["y"] >= 63.0
    assert any(act == "control" and pl.get("jump") is True and pl.get("forward") is True for act, pl in open_calls)


def test_bug028_real_void_geofence_across_escape_lava_water_and_lava_edge():
    """BUG-028 P0 regression: real VoidGeofence uses check_point(), NEVER contains(), in lava/water/edge paths."""
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv

    gf = VoidGeofence(center_x=225.0, center_z=235.0, radius=16.0, margin=4.0)  # safe_radius = 12.0

    # 1) escape_lava with find_blocks candidates (one outside safe_radius=12, one inside!) -> must NOT raise AttributeError!
    st_lava = {"pos": [227.4, 31.5, 235.0], "in_lava": True, "yaw": 3.14}
    moves: list[tuple[float, float, float]] = []

    class _GfLavaBot:
        def status(self):
            return {**st_lava, "health": 12.0, "alive": True, "ready": True, "dimension": "minecraft:the_nether"}

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "basalt", "pos": [239, 32, 235], "dist": 11.6},  # outside VoidGeofence safe_radius=12!
                        {"name": "netherrack", "pos": [229, 32, 234], "dist": 2.1},  # inside safe_radius -> pick this!
                    ]
                }
            if action == "get_block":
                p = pl.get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 235)}:
                    return {"name": "air"}
                return {"name": "lava"}
            if action == "move_to":
                moves.append((pl["x"], pl["y"], pl["z"]))
                st_lava["pos"] = [pl["x"], pl["y"], pl["z"]]
                st_lava["in_lava"] = False
                return {"ok": True}
            return {"ok": True}

    out_l = CombatEnv(_GfLavaBot(), geofence=gf).escape_lava()
    assert out_l["ok"] is True and out_l["in_lava"] is False
    assert (229.5, 33.0, 234.5) in moves

    # 2) escape_deep_water_to_shore (radial fallback + surface_retreat) with real VoidGeofence -> no AttributeError!
    class _GfWaterBot:
        def status(self):
            return {"pos": [225.5, 61.5, 235.5], "health": 14.0, "alive": True, "ready": True, "in_water": True, "submerged": True, "oxygen": 200}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    out_w = CombatEnv(_GfWaterBot(), geofence=gf).escape_deep_water_to_shore([232.0, 62.0, 235.5])
    assert out_w["mode"] == "surface_retreat"

    # 3) guard_lava_edge skips lava cell outside safe_radius and seals in-bounds adjacent lava!
    placed_edge: list[list[int]] = []

    class _GfEdgeBot:
        def status(self):
            return {"pos": [236.5, 40.0, 235.5], "on_ground": True, "in_lava": False, "ready": True, "alive": True, "sneaking": False}

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                p = tuple(pl.get("pos", [0, 0, 0]))
                if p in {(237, 39, 235), (235, 39, 235)}:  # 237.5 out of safe_radius(12 from 225); 235.5 in bounds!
                    return {"name": "lava"}
                return {"name": "stone" if p[1] < 40 else "air"}
            if action == "place":
                placed_edge.append(list(pl["pos"]))
            return {"ok": True}

    edge_res = CombatEnv(_GfEdgeBot(), geofence=gf).guard_lava_edge([236.5, 40.0, 235.5])
    assert edge_res["ok"] is True and edge_res["sneak"] is True
    assert placed_edge == [[235, 39, 235]]


@pytest.mark.parametrize("submerged", [True, False])
def test_water_retreat_turns_inward_at_geofence(submerged):
    import math

    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    geofence = VoidGeofence(radius=55.0, margin=5.0)
    status = {"pos": [49.9, 62.0, 0.0], "yaw": -math.pi / 2, "in_water": True, "submerged": submerged, "oxygen": 20}
    motions = []

    class WaterBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                assert body["x"] < status["pos"][0]
                assert geofence.check_point(body["x"], body["z"]) is None
                status["pos"] = [body["x"], body["y"], body["z"]]
            if action == "control":
                motions.append(body)
                if body.get("forward"):
                    x, _, z = status["pos"]
                    ux, uz = yaw_to_unit_xz(body["yaw"])
                    assert geofence.check_point(x + 5 * ux, z + 5 * uz) is None
            return {"ok": True}

    result = CombatEnv(WaterBot(), geofence=geofence).escape_deep_water_to_shore()
    surge = ["water_surface_surge"] if submerged else []
    assert result["actions"] == surge + ["surface_retreat_move", "surface_retreat_jump"]
    assert [motion["forward"] for motion in motions] == ([False, True] if submerged else [True])
    assert all(motion["jump"] for motion in motions)


@pytest.mark.parametrize("terrain", ["open", "roof", "lava", "drift", "new_life", "new_life_during_probe"])
def test_w20_stalled_drowning_can_use_a_nearby_water_column_to_reach_air(terrain):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    start = [241023.5, 16.2, 193556.3]
    column = [241023.5, 17.2, 193557.5]
    breathing = [241023.5, 18.9, 193557.5]
    state = {
        "pos": start, "dimension": "overworld", "last_death": {"t": 1}, "health": 3.17, "oxygen": 4,
        "ready": True, "alive": True, "in_water": True, "submerged": True, "on_ground": False, "yaw": 0.0,
    }
    steps = []

    class CaveWaterBot:
        def status(self):
            return dict(state)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if terrain == "new_life_during_probe" and state["pos"] == column:
                    state.update(last_death={"t": 2}, pos=[240965.5, 65, 193590.5])
                if x == 241023 and z in {193556, 193557} and y in {16, 17}:
                    return {"name": "water"}
                if x == 241023 and z == 193557:
                    if y == 19 and terrain in {"roof", "lava"}:
                        return {"name": "stone" if terrain == "roof" else "lava"}
                    if y in {18, 19}:
                        return {"name": "water"}
                    if y in {20, 21}:
                        return {"name": "air"}
                return {"name": "stone"}
            if action == "look":
                state["yaw"] = body["yaw"]
            if action == "control" and body.get("jump"):
                dx, dz = yaw_to_unit_xz(body.get("yaw", state["yaw"]))
                if body.get("forward") and dz > 0.9 and abs(dx) < 0.2:
                    steps.append("swim_to_column")
                    state["pos"] = column if terrain != "drift" else [241024.1, 17.2, 193557.5]
                    if terrain == "new_life":
                        state.update(last_death={"t": 2}, pos=[240965.5, 65, 193590.5])
                elif not body.get("forward") and state["pos"] == column:
                    steps.append("rise_to_air")
                    state.update(pos=breathing, oxygen=3, submerged=False)
            return {"ok": True}

    result = CombatEnv(CaveWaterBot()).escape_deep_water_to_shore(surface_first=True)

    if terrain == "open":
        assert steps == ["swim_to_column", "rise_to_air"]
        assert "water_pocket_ascent" in result["actions"]
        assert result["mode"] == "breathing" and result["pos_after"] == breathing and result["in_water"] is True
    else:
        assert "water_pocket_ascent" not in result["actions"]
        respawned = terrain in {"new_life", "new_life_during_probe"}
        assert steps == (["swim_to_column"] if respawned or terrain == "drift" else [])
        assert result["mode"] == ("respawned" if respawned else "surface_retreat")


def test_open_water_outside_geofence_can_swim_toward_the_safe_radius():
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    geofence = VoidGeofence(center_x=-11029.5, center_z=-11008.5, radius=220, margin=8)
    status = {"pos": [-11016.5, 62.2, -10782.8], "in_water": True, "on_ground": False,
              "alive": True, "ready": True, "yaw": math.pi}
    moves = []
    swims = []

    class WaterBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                assert geofence.check_point(body["x"], body["z"]) is None
                moves.append(body)
                raise ApiError("path_not_found", "pathfinder cannot leave the water")
            if action == "control" and body.get("forward"):
                x, y, z = status["pos"]
                dx, dz = yaw_to_unit_xz(body["yaw"])
                end = [x + 5 * dx, y, z + 5 * dz]
                assert geofence.distance_from_center(end[0], end[2]) < geofence.distance_from_center(x, z)
                swims.append(end)
                status["pos"] = end
            return {"ok": True}

    result = CombatEnv(WaterBot(), geofence=geofence).escape_deep_water_to_shore()

    assert len(moves) == 1 and len(swims) == 1
    assert result["pos_after"] == swims[0]
    assert result["in_water"] is True
    assert "surface_retreat_geofence_blocked" not in result["actions"]

