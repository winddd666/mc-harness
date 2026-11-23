from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_creeper_l_corner_not_carved_shaft_and_close_creeper_latches_shield(monkeypatch):
    """Reproduces hy4_hy_8_nether & glm_glm_3_village: L-corner is open_cave; close creeper post-flee latches shield."""
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, SubcorticalInstinctController

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    postures: list[dict] = []

    class _CornerAndCloseCreeperBot:
        def __init__(self) -> None:
            self.pos = [0.5, 64.0, 0.5]
            self.blocks = {
                (0, 64, -1): "stone", (0, 65, -1): "stone",  # North wall
                (1, 64, 0): "stone", (1, 65, 0): "stone",    # East wall (South & West open = L-corner!)
                (0, 66, 0): "oak_leaves",                     # Overhead leaf
            }

        def status(self):
            return {
                "pos": list(self.pos), "health": 20.0, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "in_water": False, "yaw": 0.0,
                "nearest_hostile": {"id": 55, "name": "creeper", "pos": [self.pos[0], 64.0, self.pos[2] + 2.2], "dist": 2.2},
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                p = tuple(payload["pos"])
                return {"name": self.blocks.get(p, "stone" if p[1] < 64 else "air")}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_entities":
                # Creeper chased alongside us and is STILL 2.2m away!
                return {"entities": [{"id": 55, "name": "creeper", "pos": [self.pos[0], 64.0, self.pos[2] + 2.2], "dist": 2.2}]}
            if action == "move_to":
                self.pos[2] -= 6.0  # Moved >= 1.5m so flee() ok=True, yet creeper still at 2.2m!
                return {"pos": list(self.pos)}
            if action == "posture":
                postures.append(payload)
            return {"ok": True}

    bot = _CornerAndCloseCreeperBot()
    env = CombatEnv(bot)
    snap = env.arena_snapshot([0.5, 64.0, 0.5])
    assert snap["enclosure"] != "carved_shaft"

    out = CombatControllerSlot(SubcorticalInstinctController()).handle(
        env, {"source": "pre_hurt_creeper", "primary_entity_id": 55, "primary_type": "creeper", "distance": 2.6}
    )
    assert out["mode"] == "creeper_evade"
    assert out["latched_guard"] is True
    assert postures and postures[-1].get("shield") is True

    # CombatEnv.flee detects respawn teleport (>64m) as ok=False, mode='respawned'
    class _DeadFleeBot:
        def __init__(self):
            self.dead = False

        def status(self):
            return {"pos": [5000.0, 70.0, 5000.0] if self.dead else [0.5, 64.0, 0.5],
                    "health": 20.0, "alive": True, "ready": True, "yaw": 0.0}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                self.dead = True
            return {"ok": True}

    flee_resp = CombatEnv(_DeadFleeBot()).flee([0.5, 64.0, 3.0])
    assert flee_resp["ok"] is False and flee_resp["mode"] == "respawned"


def test_night_surface_bunker_after_failed_flee_boundary_eat_and_no_dead_eat(monkeypatch):
    """Covers hy4_hy_2_bare night flee->bunker, boundary_eat NameError fix, and post-death not_ready eat guard."""
    clock = [300.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    bunker_calls = []
    eat_calls = []
    state = {"hp": 13.0, "food": 20, "blocking": False, "ready": True, "alive": True, "hurt_seq": 1}

    class _NightSkills:
        mining_dfs = None

        def dig_safety_bunker(self, urgent=False):
            bunker_calls.append(urgent)
            return {"ok": True, "inside_shaft": True, "roof_placed": True}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 12.0}

        def dig_cover_pocket(self):
            return {"ok": True, "mode": "cover_pocket", "sealed": [True, True]}

    class _NightBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 70.0, 0.5], "health": state["hp"], "food": state["food"],
                    "alive": state["alive"], "ready": state["ready"], "on_ground": True,
                    "in_water": False, "in_lava": False, "is_day": False, "blocking": state["blocking"],
                    "nearest_hostile": {"id": 11, "name": "zombie", "dist": 2.0} if state["hp"] <= 13 else None,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = state["hurt_seq"]
                rows = [{"seq": seq, "type": "hurt", "health": state["hp"], "source": "mob_attack"}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 16}, {"name": "bread", "count": 2}]}
            if action == "use_item":
                eat_calls.append(dict(body or {}))
                return {"ok": True}
            return {"ok": True}

    guard = GenerationGuard(_NightBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_NightSkills())
    # 1) First melee hurt on surface at night -> flees
    inc1 = guard.poll()
    assert inc1 is not None and [a["action"] for a in inc1["actions"]] == ["combat_or_flee"]

    # 2) Second melee hurt shortly after with no sword -> bunkers instead of infinite night surface flee
    clock[0] += 5.0
    state["hurt_seq"] = 2
    inc2 = guard.poll()
    assert inc2 is not None and "emergency_bunker" in [a["action"] for a in inc2["actions"]]
    assert bunker_calls == [True]

    # 3) stabilize_after_tools with blocking=True and food=10 eats cleanly without NameError('st')
    state["hp"] = 18.0
    state["food"] = 10
    state["blocking"] = True
    eat_calls.clear()
    note = guard.stabilize_after_tools(["ok"])
    assert note is not None and "顺手进食 bread" in note
    assert eat_calls == [{"item": "bread"}]

    # 4) If bot died / ready=False after combat, poll() must NOT call use_item(food)
    state["ready"] = False
    state["alive"] = False
    eat_calls.clear()
    clock[0] += 5.0
    inc_dead = guard.poll()
    assert inc_dead is not None
    assert eat_calls == []

    # 5) Already blocking arrow hurt escalates to dig_cover instead of standing still calling raise_shield
    state["ready"] = True
    state["alive"] = True
    state["hp"] = 15.0
    state["food"] = 20
    state["blocking"] = True

    class _ShieldAlreadyUpBot(_NightBot):
        def call(self, action, body=None, **kwargs):
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 10, "type": "hurt", "health": 15.0, "source": "arrow"}] if since < 10 else []
                return {"events": rows, "last_seq": 10}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}]}
            return super().call(action, body, **kwargs)

    guard2 = GenerationGuard(_ShieldAlreadyUpBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_NightSkills())
    inc_shield = guard2.poll()
    assert inc_shield is not None
    assert [a["action"] for a in inc_shield["actions"]] == ["dig_cover"]


@pytest.mark.parametrize("new_risk", ["closer", "different_target", "new_hurt"])
@pytest.mark.parametrize("first_warning", ["poll", "tool"])
def test_bug_009_stationary_failed_creeper_retreat_only_cancels_again_on_change(monkeypatch, new_risk, first_warning):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    creeper = {"id": 4547388, "name": "creeper", "dist": 2.9, "pos": [2975.5, 53, -10040.3]}
    status = {
        "pos": [2975.5, 53, -10037.4], "health": 20, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": creeper,
    }
    hits = []
    attempts = []

    class CreeperBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(CreeperBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def cornered(_env, trigger):
        attempts.append(trigger["primary_entity_id"])
        if len(attempts) == 3 and new_risk == "new_hurt":
            hits.append({"type": "hurt", "seq": 1, "source": "arrow"})
        retreat = {"action": "retreat_from_creeper", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"}
        if first_warning == "tool" and len(attempts) == 2:
            creeper["dist"] = 4.0
        return {"handled": True, "mode": "creeper_evade", "actions": [retreat]}

    monkeypatch.setattr(guard.combat_slot, "handle", cornered)
    if first_warning == "tool":
        creeper.update(dist=4.2, pos=[2975.5, 53, -10041.6])
        receipt = guard.handle_preemption({
            "source": "pre_hurt_creeper", "primary_entity_id": creeper["id"], "primary_type": "creeper", "distance": 4.4,
        }, interrupted_name="mc_skill_staircase")
        assert receipt["preempted_by_reflex"] and receipt["requires_replan"]
        clock[0] += 3.42
        creeper.update(dist=3.9, pos=[2975.5, 53, -10041.3])
    else:
        assert guard.poll()["requires_replan"] is True
        clock[0] += 15.0

    second = guard.poll()
    assert second["reasons"] == ["pre_hurt_creeper"] and second["requires_replan"] is False

    clock[0] += 6.0 if new_risk == "closer" else 15.5
    if new_risk == "closer":
        creeper["dist"] = 2.25
    elif new_risk == "different_target":
        creeper["id"] += 1
    assert guard.poll()["requires_replan"] is True
    assert len(attempts) == 3


@pytest.mark.parametrize("change", ["stationary", "regenerated", "dropped_health", "hurt", "moved", "shooter_closed_in"])
def test_bug_009_failed_distant_ranged_retreat_keeps_generation_if_unchanged(monkeypatch, change):
    shooter = {"id": 4920712, "name": "skeleton", "dist": 11.2, "pos": [5267.6, 23, -7421.5]}
    status = {
        "pos": [5256.4, 23, -7421.5], "health": 19, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": shooter,
        "held": {"name": "stone_pickaxe", "slot": 41},
    }
    hurt = []

    class ShotBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {**status, "nearest_hostile": dict(shooter)}
            if action == "events":
                return {"events": [e for e in hurt if e["seq"] > kwargs["params"]["since"]]}
            assert action == "inventory"
            return {"items": [], "offhand": None}

    guard = GenerationGuard(ShotBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def blocked_retreat(_env, trigger):
        assert trigger["source"] == "pre_hurt_ranged"
        if change == "hurt":
            hurt.append({"seq": 1, "type": "hurt", "source": "arrow"})
        elif change == "regenerated":
            status["health"] = 20
        elif change == "dropped_health":
            status["health"] = 18
        elif change == "moved":
            status["pos"] = [5254.4, 23, -7421.5]
        elif change == "shooter_closed_in":
            shooter["dist"] = 5.0
        return {"handled": True, "mode": "cover_or_flee", "actions": [
            {"action": "flee", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"},
        ]}

    monkeypatch.setattr(guard.combat_slot, "handle", blocked_retreat)
    incident = guard.poll()

    assert incident["reasons"] == ["pre_hurt_ranged"]
    assert incident["requires_replan"] is (change not in {"stationary", "regenerated"})


@pytest.mark.parametrize("scene", ["verified", "shielded", "new_life", "second_shooter", "unsupported"])
def test_w21_close_shooter_stalled_retreat_can_place_cover_before_the_first_arrow(monkeypatch, scene):
    from time import monotonic

    origin = [261034.4, 63, 253596.5]
    shooter = {"id": 2461801, "name": "skeleton", "pos": [261038.3, 63, 253596.5], "dist": 3.9}
    status = {
        "pos": origin, "health": 20, "food": 20, "last_death": {"t": 1}, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
        "held": {"name": "iron_sword", "slot": 38},
    }
    placed = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {
                    "items": [{"name": "cobblestone", "count": 27}, status["held"]],
                    "offhand": {"name": "shield"} if scene == "shielded" else None,
                }
            if action == "get_entities":
                second = {"id": 2461802, "name": "skeleton", "pos": [261034.4, 63, 253592.5], "dist": 4.0}
                return {"entities": [shooter, second] if scene == "second_shooter" else [shooter]}
            if action == "get_block":
                x, y, z = body["pos"]
                ground = y == 62 and (scene != "unsupported" or x == 261034)
                return {"name": "cobblestone" if (x, y, z) in placed else "grass_block" if ground else "air"}
            if action == "place":
                assert body["item"] == "cobblestone"
                placed.append(tuple(body["pos"]))
                return {"ok": True}
            pytest.fail(f"Unexpected {action} during shore cover")

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def blocked_retreat(_env, trigger):
        assert trigger["source"] == "pre_hurt_ranged"
        if scene == "new_life":
            status["last_death"] = {"t": 2}
        return {
            "handled": True, "mode": "cover_or_flee", "status_after": dict(status),
            "actions": [{"action": "flee", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"}],
        }

    monkeypatch.setattr(guard.combat_slot, "handle", blocked_retreat)
    incident = guard.poll()

    assert incident["reasons"] == ["pre_hurt_ranged"]
    if scene == "verified":
        assert placed == [(261035, 63, 253596), (261035, 64, 253596)]
        assert incident["actions"][-1]["action"] == "place_arrow_cover" and incident["actions"][-1]["ok"] is True
        assert guard._ranged_cover_active_until > monotonic()
    else:
        assert placed == []
        assert not any(action["action"] == "place_arrow_cover" and action["ok"] for action in incident["actions"])


@pytest.mark.parametrize("followup", ["same_threat", "attempted_place", "closer", "changed_yaw", "new_hurt", "other_hostile"])
def test_bug152_failed_melee_cover_rechecks_a_stationary_warning_before_cancel(monkeypatch, followup):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [209260.8, 75, 209612.5]
    zombie = {"id": 2198769, "name": "zombie", "dist": 2.4, "pos": [209263.2, 75, 209612.5]}
    status = {
        "pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "health": 3.909, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "in_water": False, "in_lava": False, "on_fire": False,
        "submerged": False, "blocking": False, "sneaking": False, "held": None, "offhand": None,
        "armor_points": 0, "effects": [], "busy": None, "held_controls": None, "yaw": -1.79,
        "nearest_hostile": zombie,
    }
    events = []
    hurts = []
    attempts = []

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "oak_log", "count": 1}]}
            if action == "events":
                return {"events": [hurt for hurt in hurts if hurt["seq"] > kwargs["params"]["since"]]}
            if action == "get_entities":
                if followup == "new_hurt" and len(attempts) == 2:
                    hurts.append({"seq": 3, "type": "hurt", "source": "mob_attack"})
                if followup == "other_hostile" and len(attempts) == 2:
                    return {"entities": [zombie, {"id": 12, "name": "skeleton", "dist": 4.0}]}
                return {"entities": [zombie]}
            return {"ok": True}

    guard = GenerationGuard(LedgeBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def blocked_retreat(_trigger):
        attempts.append(clock[0])
        if followup == "changed_yaw" and len(attempts) == 2:
            status["yaw"] = 0.4
        return {
            "handled": True, "mode": "cover_or_flee", "status_after": dict(status),
            "actions": [
                {"action": "flee", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"},
                {"action": "place_melee_cover", "ok": False, "placed": 0,
                 "placement_attempts": int(followup == "attempted_place" and len(attempts) == 2)},
            ],
        }

    monkeypatch.setattr(guard, "_run_subcortical_combat", blocked_retreat)
    assert guard.poll()["requires_replan"] is True
    clock[0] += 15.1
    if followup == "closer":
        zombie.update(dist=2.2, pos=[209263.0, 75, 209612.5])
    else:
        zombie.update(dist=2.5, pos=[209263.3, 75, 209612.5])

    assert guard.poll()["requires_replan"] is (followup != "same_threat")
    matches = [event for event in events if event["type"] == "guardian.stationary_melee_cover_verified"]
    assert len(matches) == int(followup == "same_threat")
    if followup == "same_threat":
        clock[0] += 15.1
        zombie.update(dist=2.4, pos=[209263.2, 75, 209612.5])
        assert guard.poll()["requires_replan"] is True


@pytest.mark.parametrize("landing", ["magma", "stone", "respawned", "death_event"])
def test_hot_floor_damage_during_combat_preserves_the_shield_and_crouches(monkeypatch, landing):
    spider = {"id": 42, "name": "spider", "dist": 1.4}
    status = {"pos": [-2900.6, 70, -10098.9], "health": 4.0, "food": 17, "ready": True, "alive": True,
              "on_ground": True, "blocking": True, "nearest_hostile": spider, "dimension": "overworld"}
    events = []
    posture = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                assert 0 < kwargs["timeout_s"] <= 0.5
                return {"name": "stone" if landing == "stone" else "magma_block"}
            if action == "posture":
                posture.append(body)
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            pytest.fail(f"Unexpected call after combat: {action}")

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda _: {
        "source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "spider", "distance": 1.4,
    })

    def retreat(_env, _trigger):
        events.append({"seq": 1, "type": "hurt", "source": "hot_floor"})
        status["health"] = 3.33
        if landing == "respawned":
            status["last_death"] = {"t": 321}
        elif landing == "death_event":
            events.append({"seq": 2, "type": "death"})
        return {"handled": True, "mode": "cover_or_flee", "actions": [
            {"action": "raise_shield_entry", "ok": True},
            {"action": "flee", "ok": False, "reason": "unsafe_landing", "moved": 0.0},
        ]}

    monkeypatch.setattr(guard.combat_slot, "handle", retreat)
    incident = guard.poll()

    assert posture == ([{"sneak": True, "shield": True, "jump": False}] if landing == "magma" else [])
    assert ("hot_floor" in incident["reasons"]) is (landing == "magma")
    assert incident["requires_replan"]


@pytest.mark.parametrize("landing", ["shooter", "clear", "respawned"])
def test_poll_restores_shield_toward_remaining_shooter_after_attack_timeout(monkeypatch, landing):
    zombie = {"id": 42, "name": "zombie", "dist": 1.6, "pos": [4025.6, 69, 26022.5]}
    skeleton = {"id": 178789, "name": "skeleton", "dist": 7.1, "pos": [4027.3, 69, 26029.7]}
    shield = {"name": "shield", "count": 1}
    status = {
        "pos": [4024, 69, 26022.5], "health": 20, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "offhand": shield,
        "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    commands = []

    class ArenaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": shield}
            commands.append((action, body))
            if action == "stop":
                status["nearest_hostile"] = None if landing == "clear" else skeleton
                if landing == "respawned":
                    status["last_death"] = {"t": 2}
            if action == "posture":
                status["blocking"] = body.get("shield") is True
                return {"blocking": status["blocking"]}
            return {"ok": True}

    guard = GenerationGuard(ArenaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def attack(_trigger):
        request = httpx.Request("POST", "https://example.invalid/attack", json={"entity_id": 42})
        raise httpx.ReadTimeout("attack reply timed out", request=request)

    monkeypatch.setattr(guard, "_run_subcortical_combat", attack)
    incident = guard.poll()

    assert incident["actions"][0]["error"]["code"] == "network_uncertain"
    expected = [("stop", {})]
    if landing == "shooter":
        expected += [("look", {"entity_id": 178789}), ("posture", {"shield": True, "jump": False, "sneak": False})]
        assert incident["actions"][-1] == {"action": "raise_shield_after_combat_timeout", "ok": True}
    assert commands == expected
    assert incident["status"]["blocking"] is (landing == "shooter")


@pytest.mark.parametrize("shooter_disappears", [False, True])
def test_cave_combat_ignores_distant_wall_hostiles_and_finishes_shield_rush(shooter_disappears):
    """Reproduces r15 cave_combat: distant mobs >8.5m outside walls must not disable sword counter_kill or multi-hit shield_rush_ranged."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    strikes = {"n": 0}

    class _CaveArenaBot:
        def __init__(self, *, solo_skeleton: bool = False):
            self.solo_skeleton = solo_skeleton

        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 18.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                if self.solo_skeleton:
                    return {
                        "entities": [
                            {"id": 20, "name": "skeleton", "pos": [0.5, 64.0, 5.0], "dist": 4.5},
                            # Ambient outdoor mobs > 8.5m behind cobblestone walls
                            {"id": 31, "name": "zombie", "pos": [12.5, 64.0, 0.5], "dist": 12.0},
                            {"id": 32, "name": "zombie", "pos": [-13.5, 64.0, 0.5], "dist": 14.0},
                        ]
                    }
                return {
                    "entities": [
                        {"id": 10, "name": "zombie", "pos": [2.5, 64.0, 0.5], "dist": 2.0},
                        {"id": 20, "name": "skeleton", "pos": [0.5, 64.0, 5.0], "dist": 4.5},
                        {"id": 31, "name": "zombie", "pos": [12.5, 64.0, 0.5], "dist": 12.0},
                        {"id": 32, "name": "spider", "pos": [-13.5, 64.0, 0.5], "dist": 14.0},
                    ]
                }
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 or abs(p[0]) >= 5 or abs(p[2]) >= 5 else "air"}
            if action == "attack":
                strikes["n"] += 1
                if self.solo_skeleton and shooter_disappears and strikes["n"] == 2:
                    raise ApiError("no_such_entity", "entity is no longer visible")
                return {"hits": 1, "killed": strikes["n"] >= 2}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    # 1) Zombie + skeleton inside arena + 2 mobs outside at 12-14m -> counter-kills zombie instead of circle-fleeing
    out_z = ctrl.on_encounter(
        CombatEnv(_CaveArenaBot(solo_skeleton=False)),
        {"source": "pre_hurt_melee", "primary_entity_id": 10, "primary_type": "zombie", "distance": 2.0},
    )
    assert out_z["mode"] == "counter_killed"

    # 2) Solo skeleton inside + 2 outdoor mobs -> shield_rush_ranged finishes skeleton in 2 strikes
    strikes["n"] = 0
    out_s = ctrl.on_encounter(
        CombatEnv(_CaveArenaBot(solo_skeleton=True)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 20, "primary_type": "skeleton", "distance": 4.5},
    )
    assert out_s["mode"] == "shield_rush_ranged"
    assert strikes["n"] == 2
    if shooter_disappears:
        assert {"action": "shield_rush_attack", "ok": False, "code": "no_such_entity"} in out_s["actions"]
        assert not any(a["action"] == "fallback_flee" for a in out_s["actions"])
    else:
        assert any(a["action"] == "shield_rush_attack" and a.get("killed") is True for a in out_s["actions"])


@pytest.mark.parametrize("scenario", ["cornered", "escaped", "second_hostile", "out_of_reach"])
def test_cornered_unshielded_bot_strikes_once_only_against_a_close_lone_melee_mob(monkeypatch, scenario):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    distance = 2.5 if scenario == "out_of_reach" else 0.6
    zombie = {"id": 11, "name": "zombie", "dist": distance, "pos": [0.5 + distance, 64.0, 0.5]}
    calls = []

    class CaveBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5], "health": 2.7, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "nearest_hostile": zombie,
            }

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}], "held": {"name": "dirt"}}
            if action == "get_entities":
                mobs = [zombie]
                if scenario == "second_hostile":
                    mobs.append({"id": 12, "name": "zombie", "dist": 1.7, "pos": [2.2, 64, 0.5]})
                return {"entities": mobs}
            if action == "get_block":
                return {"name": "air"}
            if action in {"equip", "attack"}:
                calls.append((action, body))
            return {"hits": 1} if action == "attack" else {"ok": True}

    def flee(*args, **kwargs):
        if scenario == "escaped":
            return {"ok": True, "mode": "flee", "moved_dist": 5.0}
        return {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    result = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": 11},
    )

    attacks = [body for action, body in calls if action == "attack"]
    if scenario == "cornered":
        attack, = attacks
        assert calls[0] == ("equip", {"item": "stone_sword", "slot": "hand"})
        assert attack["entity_id"] == 11 and attack["max_hits"] == 1 and attack["timeout"] <= 350
        assert result["actions"][-1]["ok"] is True
    else:
        assert attacks == []


@pytest.mark.parametrize("terrain", [
    "verified", "no_support", "incomplete", "second_hostile", "enemy_moved", "knockback", "retreat_ok",
    "missing_wall_flag_clear", "missing_wall_flag_blocked", "missing_wall_flag_unloaded", "reported_wall",
])
def test_unarmed_bot_builds_supported_cover_when_a_close_zombie_blocks_retreat(monkeypatch, terrain):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    origin = [309346.5, 19, 301950.5]
    zombie = {
        "id": 854150, "name": "zombie", "dist": 2.4, "pos": [309348.9, 19, 301950.5], "blocked_by_wall": False,
    }
    status = {
        "pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "is_day": False, "nearest_hostile": zombie,
    }
    blocks = {}
    attempts = []
    scans = 0

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal scans
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 22}]}
            if action == "get_entities":
                scans += 1
                if terrain == "second_hostile":
                    return {"entities": [zombie, {"id": 854151, "name": "skeleton", "dist": 3.1}]}
                if terrain == "enemy_moved" and scans >= 3:
                    return {"entities": [{**zombie, "pos": [309346.5, 19, 301948.1]}]}
                if scans == 2 and terrain.startswith("missing_wall_flag"):
                    observed = dict(zombie)
                    del observed["blocked_by_wall"]
                    return {"entities": [observed]}
                if scans == 2 and terrain == "reported_wall":
                    return {"entities": [{**zombie, "blocked_by_wall": True}]}
                return {"entities": [zombie]}
            if action == "get_block":
                cell = tuple(body["pos"])
                if cell == (309347, 20, 301950):
                    if terrain == "missing_wall_flag_unloaded":
                        return {}
                    if terrain == "missing_wall_flag_blocked":
                        return {"name": "stone"}
                floor = cell[1] == 18 and (terrain != "no_support" or cell[0] == 309346)
                return {"name": blocks.get(cell, "stone" if floor else "air")}
            if action == "place":
                target = tuple(body["pos"])
                attempts.append(target)
                if terrain != "incomplete" or len(attempts) == 1:
                    blocks[target] = body["item"]
                if terrain == "knockback":
                    status.update(on_ground=False)
            if action in {"move_to", "attack", "dig"} or action == "control" and body.get("forward"):
                pytest.fail(f"Unexpected operation while cornered: {action}")
            if action == "status":
                return dict(status)
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "flee", lambda *_args, **_kwargs: (
        {"ok": True, "mode": "flee", "moved_dist": 4.0} if terrain == "retreat_ok"
        else {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}
    ))
    result = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"],
    })

    expected_placements = [(309347, 19, 301950), (309347, 20, 301950)]
    if terrain in {"verified", "incomplete", "enemy_moved", "missing_wall_flag_clear"}:
        assert attempts == expected_placements
    elif terrain == "knockback":
        assert attempts == expected_placements[:1]
    else:
        assert attempts == []
    assert (result["mode"] == "placed_cover") is (terrain in {"verified", "missing_wall_flag_clear"})
    if terrain in {"no_support", "incomplete"}:
        assert result["actions"][-1]["placement_attempts"] == len(attempts)
    if terrain in {"verified", "missing_wall_flag_clear"}:
        assert result["actions"][-1]["action"] == "place_melee_cover"
        assert result["actions"][-1]["placed"] == 2

