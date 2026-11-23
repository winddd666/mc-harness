from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("followup", [
    "stable", "closer", "shifted_target", "other_hostile", "new_life", "late_hurt", "missing_scan", "nightfall",
])
def test_w21_stationary_spider_does_not_repeatedly_cancel_generation(monkeypatch, followup):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [282881.5, 34, 223061.5]
    spider = {"id": 2467934, "name": "spider", "dist": 2.2, "pos": [282883.7, 34, 223061.5]}
    status = {
        "pos": origin, "dimension": "overworld", "health": 20, "food": 20, "is_day": True,
        "ready": True, "alive": True, "on_ground": True, "in_water": False, "in_lava": False, "on_fire": False,
        "submerged": False, "blocking": False, "sneaking": False, "held": {"name": "cobblestone", "count": 61},
        "offhand": None, "armor_points": 0, "effects": [], "busy": None, "held_controls": None, "yaw": -3.14,
        "nearest_hostile": spider,
    }
    hurts = []
    attempts = []
    events = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hurts if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [status["held"], {"name": "stone_pickaxe", "count": 1}], "offhand": None}
            if action == "get_entities":
                if len(attempts) == 3 and followup == "late_hurt" and not hurts:
                    hurts.append({"seq": 1, "type": "hurt", "source": "mob_attack", "health": 18})
                if len(attempts) == 3 and followup == "missing_scan":
                    return {"entities": []}
                if len(attempts) == 3 and followup == "other_hostile":
                    return {"entities": [spider, {"id": 2467935, "name": "skeleton", "dist": 4.0}]}
                return {"entities": [spider]}
            pytest.fail(f"Unexpected {action} during stationary spider warning")

    guard = GenerationGuard(CaveBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def blocked_retreat(trigger):
        assert trigger["source"] == "pre_hurt_melee"
        attempts.append(clock[0])
        return {
            "mode": "cover_or_flee", "status_after": dict(status),
            "actions": [{"action": "flee", "ok": False, "reason": "no_safe_retreat", "moved": 0.0}],
        }

    monkeypatch.setattr(guard, "_run_subcortical_combat", blocked_retreat)
    assert guard.poll()["requires_replan"] is True
    clock[0] += 15.1
    spider.update(dist=1.6, pos=[282883.1, 34, 223061.5])
    assert guard.poll()["requires_replan"] is True

    clock[0] += 15.1
    if followup == "closer":
        spider.update(dist=1.4, pos=[282882.9, 34, 223061.5])
    elif followup == "shifted_target":
        spider.update(pos=[282881.5, 34, 223063.1])
    elif followup == "new_life":
        status["last_death"] = {"t": 1}
    elif followup == "nightfall":
        status["is_day"] = False
    incident = guard.poll()
    assert len(attempts) == 3
    assert incident["requires_replan"] is (followup != "stable")
    if followup == "stable":
        for _ in range(7):
            clock[0] += 15.1
            assert guard.poll()["requires_replan"] is False
        verified = [event for event in events if event["type"] == "guardian.stationary_melee_verified"]
        assert len(verified) == 8 and all(event["entity_id"] == spider["id"] for event in verified)


@pytest.mark.parametrize("followup", [
    "stable", "shifted_target", "other_hostile", "closer", "gear_changed", "dropped_health",
    "first_scan_missing", "status_missing", "hurt_after_retreat", "target_shift_during_verification",
    "first_life", "first_life_with_null", "first_life_new_death", "first_life_death_during_verification",
    "previous_death_omitted", "verification_read_failed",
])
def test_stationary_zombie_warning_at_rounded_boundary_needs_two_matching_live_samples(monkeypatch, followup):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 899686, "name": "zombie", "dist": 2.5, "pos": [328073, 71, 333228.5]}
    status = {
        "pos": [328070.5, 71, 333228.5], "dimension": "overworld", "last_death": {"t": 1}, "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "in_water": False, "in_lava": False, "on_fire": False,
        "submerged": False, "blocking": False, "sneaking": False, "held": None, "offhand": None,
        "armor_points": 0, "effects": [], "busy": None, "held_controls": None, "yaw": -2.36,
        "nearest_hostile": zombie,
    }
    if followup.startswith("first_life"):
        status.pop("last_death")
    hits = []
    nearby = [zombie]
    events = []
    attempts = []
    verification_started = [False]

    class AnchorBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if followup == "verification_read_failed" and verification_started[0]:
                    verification_started[0] = False
                    raise httpx.ReadTimeout("no full status after the scan")
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if len(attempts) == 3 and followup == "target_shift_during_verification":
                    status["nearest_hostile"] = {**zombie, "pos": [328070.5, 71, 333230.8]}
                if len(attempts) == 3 and followup == "first_life_death_during_verification":
                    status["last_death"] = {"t": 2}
                if len(attempts) == 3 and followup == "verification_read_failed":
                    verification_started[0] = True
                return {"entities": nearby}
            return {"ok": True}

    guard = GenerationGuard(AnchorBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def combat(_trigger):
        attempts.append(clock[0])
        if len(attempts) == 3 and followup == "hurt_after_retreat":
            status["health"] = 19
            hits.append({"seq": 1, "type": "hurt", "source": "mob_attack"})
        return {
            "mode": "cover_or_flee", "status_after": dict(status),
            "actions": [{"action": "flee", "ok": False, "reason": "no_safe_retreat", "moved": 0.0}],
        }

    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    assert guard.poll()["requires_replan"] is True
    clock[0] += 15.1
    zombie.update(dist=2.3, pos=[328072.8, 71, 333228.5])
    if followup == "first_scan_missing":
        nearby.clear()
    assert guard.poll()["requires_replan"] is True
    nearby[:] = [zombie]

    clock[0] += 15.1
    if followup == "shifted_target":
        zombie["pos"] = [328070.5, 71, 333230.8]
    elif followup == "other_hostile":
        nearby.append({"id": 11, "name": "skeleton", "dist": 4, "pos": [328066.5, 71, 333228.5]})
    elif followup == "closer":
        zombie.update(dist=2.2, pos=[328072.7, 71, 333228.5])
    elif followup == "gear_changed":
        status["held"] = {"name": "wooden_axe"}
    elif followup == "dropped_health":
        status["health"] = 19
        guard.previous_health = 19  # The last observation already saw the lower health.
    elif followup == "status_missing":
        status.pop("effects")
    elif followup == "first_life_new_death":
        status["last_death"] = {"t": 2}
    elif followup == "first_life_with_null":
        status["last_death"] = None
    elif followup == "previous_death_omitted":
        status.pop("last_death")

    incident = guard.poll()
    assert len(attempts) == 3
    unchanged = followup in {"stable", "first_life", "first_life_with_null"}
    assert incident["requires_replan"] is (not unchanged)
    if unchanged:
        assert "同一近敌仍在" in incident["note"]
        matches = [event for event in events if event["type"] == "guardian.stationary_melee_verified"]
        assert len(matches) == 1 and matches[0]["entity_id"] == zombie["id"]


@pytest.mark.parametrize("followup", [
    "unchanged", "closer", "new_target", "other_hostile", "scan_closer", "moved", "hurt", "status_timeout", "read_gap", "scan_gap",
    "equal_range_shift", "shift_during_hold", "shift_during_timeout", "coordinate_missing", "gear_missing",
    "dry_flag_missing", "life_sample_missing",
    "first_life", "first_life_new_death",
])
def test_bug009_stationary_melee_dig_timeout_defers_one_verified_repeat(monkeypatch, followup):
    import httpx

    from mc_agent.harness.guardian import _RecoveryBudgetExceeded

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    spider = {"id": 637768, "name": "spider", "dist": 2.7, "pos": [34023.1, 62, 34004.7]}
    status = {
        "pos": [34020.4, 62, 34004.7], "dimension": "overworld", "health": 9.33, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": False,
        "in_water": False, "submerged": False, "in_lava": False, "on_fire": False, "busy": None,
        "held": None, "offhand": None, "held_controls": None, "last_death": None, "armor_points": 0,
    }
    if followup == "gear_missing":
        status.pop("held")
    elif followup == "dry_flag_missing":
        status.pop("in_lava")
    elif followup == "life_sample_missing":
        status["last_death"] = {"t": 1}
    elif followup.startswith("first_life"):
        status.pop("last_death")
    damage = []
    attempts = []
    hostiles = [spider]
    read_state = {"fail_next": False}

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if read_state.pop("fail_next", False):
                    raise httpx.ReadTimeout("missing status sample")
                # The live /status schema only includes ID, name and distance for its nearest hostile.
                return {**status, "nearest_hostile": {key: spider[key] for key in ("id", "name", "dist")}}
            if action == "events":
                return {"events": [hit for hit in damage if hit["seq"] > kwargs["params"]["since"]]}
            if action == "get_entities":
                if followup == "scan_closer" and body["radius"] == 8:
                    return {"entities": [{**spider, "dist": 2.0}]}
                if followup == "scan_gap" and body["radius"] == 8:
                    return {}
                if followup == "coordinate_missing":
                    return {"entities": [{key: spider[key] for key in ("id", "name", "dist")}]}
                return {"entities": list(hostiles)}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def combat(_env, trigger):
        attempts.append(trigger["primary_entity_id"])
        if followup == "shift_during_timeout" and len(attempts) == 1:
            spider["pos"] = [34020.4, 62, 34007.4]
        clock[0] += 7.5
        raise _RecoveryBudgetExceeded("status" if followup == "status_timeout" else "dig")

    monkeypatch.setattr(guard.combat_slot, "handle", combat)
    first = guard.poll()
    assert first["requires_replan"] and first["actions"][0]["result"]["mode"] == "combat_timeout"
    assert attempts == [spider["id"]]

    clock[0] += 13
    if followup == "closer":
        spider.update(dist=2.0, pos=[34022.4, 62, 34004.7])
    elif followup == "new_target":
        spider["id"] += 1
    elif followup == "equal_range_shift":
        spider["pos"] = [34020.4, 62, 34007.4]
    elif followup == "shift_during_timeout":
        spider["pos"] = [34023.1, 62, 34004.7]
    elif followup == "life_sample_missing":
        status.pop("last_death")
    elif followup == "first_life_new_death":
        status["last_death"] = {"t": 2}
    elif followup == "moved":
        status["pos"] = [34020.8, 62, 34004.7]
    elif followup == "hurt":
        status["health"] = 7.33
        damage.append({"seq": 1, "type": "hurt", "source": "mob_attack"})
    elif followup == "read_gap":
        read_state["fail_next"] = True
        assert guard.poll() is None
        clock[0] += 0.3

    second = guard.poll()
    if followup in {"unchanged", "other_hostile", "shift_during_hold", "first_life"}:
        assert second is None and attempts == [spider["id"]]
        deferred, = [event for event in guard.events.events if event["type"] == "guardian.melee_timeout_deferred"]
        assert deferred["target_id"] == spider["id"]
        if followup == "other_hostile":
            hostiles.append({"id": 41, "name": "zombie", "dist": 3.0})
            clock[0] += 2.5
        elif followup == "shift_during_hold":
            spider["pos"] = [34020.4, 62, 34007.4]
            clock[0] += 0.3
        else:
            clock[0] += 12
        assert guard.poll()["requires_replan"] and attempts == [spider["id"], spider["id"]]
    else:
        assert second["requires_replan"]
        if followup == "hurt":
            assert "fresh_hurt" in second["reasons"]
        else:
            assert attempts[-1] == spider["id"] and len(attempts) == 2


@pytest.mark.parametrize("after_kill", [
    "stable", "hurt", "moved", "shooter", "death", "unverified",
    "shield_released", "shield_changed", "shield_latched", "shield_still_blocking",
])
def test_bug_009_resolved_melee_warning_keeps_generation_only_when_stable(monkeypatch, after_kill):
    spider = {"id": 4614512, "name": "spider", "dist": 2.7, "pos": [3.2, 64, 0.5]}
    status = {
        "pos": [0.5, 64, 0.5], "health": 20, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": spider,
        "held": {"name": "diamond_sword", "slot": 37, "durability_used": 5},
    }
    events = []
    attempts = []
    raised_shield = after_kill.startswith("shield_")
    if raised_shield:
        status["offhand"] = {"name": "shield", "slot": 45, "durability_used": 7}

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if after_kill == "unverified" and attempts:
                    raise httpx.ReadTimeout("post-combat event read failed")
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [status["held"]]}
            raise AssertionError(action)

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda _: {
        "source": "pre_hurt_melee", "primary_type": "spider", "primary_entity_id": spider["id"], "distance": 2.7,
    })

    def kill(_env, trigger):
        attempts.append(trigger["primary_entity_id"])
        status.update(nearest_hostile=None, held={**status["held"], "durability_used": 6})
        if after_kill == "hurt":
            events.append({"seq": 1, "type": "hurt", "source": "arrow"})
        elif after_kill == "moved":
            status["pos"] = [1.5, 64, 0.5]
        elif after_kill == "shooter":
            status["nearest_hostile"] = {"id": 14, "name": "skeleton", "dist": 9}
        elif after_kill == "death":
            events.append({"seq": 1, "type": "death"})
        steps = []
        if raised_shield:
            steps.append({"action": "raise_shield_entry", "ok": True})
            status["offhand"] = {**status["offhand"], "durability_used": 8}
            if after_kill == "shield_changed":
                status["offhand"] = None
            elif after_kill == "shield_still_blocking":
                status["blocking"] = True
        steps.append({"action": "melee_attack", "entity_id": spider["id"], "ok": True, "killed": True})
        return {
            "handled": True, "mode": "counter_killed", "actions": steps,
            "latched_guard": after_kill == "shield_latched",
        }

    monkeypatch.setattr(guard.combat_slot, "handle", kill)
    incident = guard.poll()
    assert attempts == [spider["id"]]
    assert incident["requires_replan"] is (after_kill not in {"stable", "shield_released"})


@pytest.mark.parametrize("transition", ["same_life", "other_life", "stale", "healed", "fell_only"])
def test_guardian_resumes_fresh_spawn_combat_hurt_once_when_world_becomes_ready(monkeypatch, transition):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 44, "name": "zombie", "pos": [1.8, 61, 0.5], "dist": 1.3}
    status = {"pos": [0.5, 61, 0.5], "health": 7, "food": 20, "dimension": "overworld", "last_death": {"t": 1},
              "ready": False, "alive": True, "on_ground": True}
    events = [
        {"seq": 1, "type": "hurt", "source": "arrow"},
        {"seq": 2, "type": "death"},
        {"seq": 3, "type": "spawn"},
        {"seq": 4, "type": "hurt", "source": "fall" if transition == "fell_only" else "mob_attack", "source_entity": zombie},
    ]
    retreats = []
    emitted = []

    class RespawnBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory" and status["ready"]:
                return {"items": []}
            pytest.fail(f"Unexpected respawn command: {action}")

    def retreat(_state, attacker, _counts):
        retreats.append(attacker)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    guard = GenerationGuard(RespawnBot(), EventSink(emitted.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda *_a, **_kw: None)
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    first = guard.poll()
    assert "death_or_connection_changed" in first["reasons"] and first["actions"] == []

    status["ready"] = True
    if transition == "other_life":
        status["last_death"] = {"t": 2}
    if transition == "healed":
        status["health"] = 20
    clock[0] += 4.1 if transition == "stale" else 0.5
    resumed = guard.poll()
    assert (resumed is not None) is (transition == "same_life")
    if resumed is not None:
        assert resumed["reasons"] == ["fresh_hurt"] and resumed["requires_replan"]
        assert resumed["event_seqs"] == []
    clock[0] += 0.5
    assert guard.poll() is None
    assert retreats == ([zombie] if transition == "same_life" else [])
    recorded = [event["event_seqs"] for event in emitted if event["type"] == "guardian.spawn_hurt_resumed"]
    assert recorded == ([[4]] if transition == "same_life" else [])


@pytest.mark.parametrize("recheck", ["grounded", "airborne", "changed_life", "loading", "occluded", "unknown"])
def test_low_health_spawn_reacts_to_visible_melee_before_first_new_hit(monkeypatch, recheck):
    origin = [6152.5, 68, 8.5]
    zombie = {"id": 44, "name": "zombie", "pos": [6153.4, 68, 8.5], "dist": 0.9}
    status = {
        "pos": origin, "health": 11, "food": 16, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    history = [
        {"seq": 1, "type": "hurt", "source": "mob_attack", "health": 0},
        {"seq": 2, "type": "death"},
        {"seq": 3, "type": "hurt", "source": "fall", "health": 11},
        {"seq": 4, "type": "spawn"},
    ]
    reads = 0
    triggers = []

    class RespawnBot:
        def call(self, action, body=None, **kwargs):
            nonlocal reads
            if action == "status":
                reads += 1
                if reads > 1:
                    if recheck == "airborne":
                        return {**status, "on_ground": False}
                    if recheck == "changed_life":
                        return {**status, "last_death": {"t": 2}}
                    if recheck == "loading":
                        return {**status, "ready": False}
                    if recheck == "unknown":
                        return {**status, "nearest_hostile": {"id": 44, "name": "zombie", "dist": 0.9}}
                return dict(status)
            if action == "events":
                return {"events": [row for row in history if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}]}
            if action == "get_entities":
                return {"entities": []}
            raise AssertionError(action)

    guard = GenerationGuard(RespawnBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.previous_health = 20.0
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": recheck != "occluded"})

    def react(trigger):
        triggers.append(trigger["primary_entity_id"])
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    monkeypatch.setattr(guard, "_run_subcortical_combat", react)
    incident = guard.poll()

    assert triggers == ([44] if recheck == "grounded" else [])
    assert ("pre_hurt_melee" in incident["reasons"]) is (recheck == "grounded")
    assert "death_or_connection_changed" in incident["reasons"]
    assert incident["event_seqs"] == [1, 2, 3, 4]
    assert incident["requires_replan"] and guard.retry_after_ready
    assert all(a["action"] != "eat" for a in incident["actions"])


@pytest.mark.parametrize("path, endpoint", [("/api/attack", "attack"), ("/status", "status"), ("/unrecognized", None), (None, None)])
def test_combat_timeout_records_only_a_known_endpoint(path, endpoint):
    events = []
    stops = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            assert action == "stop" and kwargs["allow_reauth"] is False
            stops.append(action)
            return {"ok": True}

    def stalled_call():
        if path is None:
            raise httpx.ReadTimeout("request unavailable")
        request = httpx.Request(
            "POST", f"https://example.invalid{path}?marker=omitted",
            json={"entity_id": 6079222, "extra": "omit-payload"},
        )
        raise httpx.ReadTimeout("request stalled", request=request)

    guard = GenerationGuard(RidgeBot(), EventSink(events.append), checkpoint=lambda: None)
    result = guard._action("subcortical_combat", stalled_call)
    completed, = [event for event in events if event["type"] == "guardian.action.completed"]
    assert completed.get("endpoint") == endpoint
    assert completed.get("target_id") == (6079222 if endpoint == "attack" else None)
    assert "omitted" not in str(completed)
    assert "omit-payload" not in str(completed)
    assert result["error"] == {"code": "network_uncertain", "message": "ReadTimeout", "timeout": True}
    assert stops == ["stop"]


@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("slow_status", [False, True])
def test_combat_attacks_reserve_time_for_final_status_and_release(monkeypatch, ranged, slow_status):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    mob = "skeleton" if ranged else "zombie"
    threat = {"id": 42, "name": mob, "pos": [2.5, 64, 0.5], "dist": 2.0}
    attacks = []
    reads = []
    commands = []

    class ArenaBot:
        def status(self):
            if slow_status and len(attacks) == 2 and not reads:
                reads.append(clock[0])
                clock[0] += 0.9
            return {"pos": [0.5, 64, 0.5], "health": 20, "food": 20, "on_ground": True,
                    "ready": True, "alive": True, "nearest_hostile": threat}

        def call(self, action, body=None, **kwargs):
            commands.append((action, body))
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                return {"entities": [threat]}
            if action == "attack":
                read_s = kwargs["timeout_s"]
                requested_ms = round((read_s - 0.35) * 1000)
                assert body["timeout"] == (requested_ms if ranged else max(300, requested_ms - 1250))
                assert kwargs["allow_reauth"] is False
                attacks.append(requested_ms)
                clock[0] += 2.7 if len(attacks) <= 2 else read_s
                return {"hits": 1, "killed": len(attacks) == 3}
            return {"ok": True}

    env = CombatEnv(ArenaBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "chase_to_range", lambda *_a, **_kw: {"ok": True})
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged" if ranged else "pre_hurt_melee", "primary_type": mob,
        "primary_entity_id": 42, "distance": 2.0,
    })

    assert attacks[:2] == [2400 if ranged else 2600] * 2
    assert attacks[2:] == ([] if slow_status else [pytest.approx(1200, abs=1)])
    assert clock[0] < 1007.0
    assert commands[-1] == ("posture", {})
    assert outcome["status_after"]["health"] == 20


@pytest.mark.parametrize("scene", ["melee", "shield_rush", "respawn"])
def test_bug145_empty_attack_receipt_stops_a_reactive_attack_sequence(monkeypatch, scene):
    ranged = scene != "melee"
    mob = "skeleton" if ranged else "zombie"
    enemy = {"id": 42, "name": mob, "pos": [2.5, 64, 0.5], "dist": 2.0}
    shield = {"name": "shield", "count": 1} if ranged else None
    sword = {"name": "iron_sword", "count": 1}
    status = {
        "pos": [0.5, 64, 0.5], "dimension": "overworld", "health": 20, "food": 20,
        "on_ground": True, "ready": True, "alive": True, "nearest_hostile": enemy,
        "held": sword, "offhand": shield, "blocking": False,
    }
    attacks = []
    retreats = []

    def mock_api(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "attack":
            attacks.append(json.loads(request.content))
            if scene == "respawn":
                status.update(last_death={"t": 1}, pos=[100.5, 64, 0.5])
            return httpx.Response(200, json={"ok": True})
        if endpoint == "status":
            result = status
        elif endpoint == "inventory":
            result = {"items": [sword], "held": sword, "offhand": shield}
        elif endpoint == "get_entities":
            result = {"entities": [enemy]}
        elif endpoint == "posture":
            status["blocking"] = bool(json.loads(request.content).get("shield"))
            result = {"blocking": status["blocking"]}
        else:
            assert endpoint in {"stop", "control", "look"}
            result = {}
        return httpx.Response(200, json={"ok": True, "result": result})

    def flee(threat, **_kwargs):
        retreats.append(threat)
        return {"ok": True, "mode": "flee", "moved_dist": 5.0}

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(mock_api)) as bot:
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
        monkeypatch.setattr(guard.combat_env, "chase_to_range", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(guard.combat_env, "flee", flee)
        outcome = guard._run_subcortical_combat({
            "source": "pre_hurt_ranged" if ranged else "pre_hurt_melee", "primary_type": mob,
            "primary_entity_id": enemy["id"], "distance": 2.0,
        })

    attack, = attacks
    assert attack["entity_id"] == enemy["id"] and attack["feedback"] is True
    name = "shield_rush_attack" if ranged else "melee_attack"
    attempt, = [action for action in outcome["actions"] if action["action"] == name]
    assert attempt["ok"] is False and attempt["killed"] is False
    assert attempt["code"] == "attack_hit_unconfirmed"
    if scene == "respawn":
        assert not outcome["handled"] and not outcome["latched_guard"] and not retreats
    elif ranged:
        assert outcome["latched_guard"] and outcome["status_after"]["blocking"] and not retreats
    else:
        assert retreats == [enemy["pos"]]


@pytest.mark.parametrize("latest_scan", ["blocked", "empty", "different_target", "timeout"])
def test_counterattack_uses_latest_confirmed_target_sighting(latest_scan):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    target = {"id": 42, "name": "zombie", "pos": [3.5, 64, 0.5], "dist": 3.0}
    scans = 0

    class CaveBot:
        def status(self):
            return {"pos": [0.5, 64, 0.5], "health": 20, "food": 20, "on_ground": True,
                    "ready": True, "alive": True, "nearest_hostile": target}

        def call(self, action, body=None, **kwargs):
            nonlocal scans
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                scans += 1
                if scans == 1:
                    return {"entities": [target]}
                if latest_scan == "blocked":
                    return {"entities": [{**target, "blocked_by_wall": True}]}
                if latest_scan == "different_target":
                    return {"entities": [{**target, "id": 43}]}
                if latest_scan == "timeout":
                    raise httpx.ReadTimeout("entity scan timed out")
                return {"entities": []}
            if action == "attack":
                assert latest_scan == "timeout" and body["entity_id"] == target["id"]
                raise httpx.ReadTimeout("attack reply timed out")
            return {"ok": True}

    trigger = {
        "source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie", "distance": 3.0,
    }
    env = CombatEnv(CaveBot())
    slot = CombatControllerSlot()
    if latest_scan == "timeout":
        with pytest.raises(httpx.ReadTimeout, match="attack reply timed out"):
            slot.handle(env, trigger)
    else:
        outcome = slot.handle(env, trigger)
        code = "blocked_by_wall" if latest_scan == "blocked" else "target_not_visible"
        assert {"action": "melee_attack", "entity_id": 42, "ok": False, "code": code} in outcome["actions"]

    assert scans == 2


@pytest.mark.parametrize(("remaining_hostile", "retreat_works"), [(True, True), (True, False), (False, True)])
def test_counter_kill_disengages_from_second_hostile_at_critical_health(monkeypatch, remaining_hostile, retreat_works):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    target = {"id": 42, "name": "zombie", "dist": 2.8, "pos": [3.3, 64.0, 0.5]}
    survivor = {"id": 43, "name": "zombie", "dist": 2.8, "pos": [0.5, 64.0, 3.3]}
    retreats = []
    attacks = []
    looks = []

    class CaveBot:
        def status(self):
            nearest = survivor if attacks and remaining_hostile else target
            return {
                "pos": [0.5, 64.0, 0.5], "health": 3.5 if attacks else 20.0, "food": 20,
                "ready": True, "alive": True, "on_ground": True, "blocking": False,
                "nearest_hostile": {key: nearest[key] for key in ("id", "name", "dist")},
            }

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                items = [{"name": "stone_sword", "count": 1}]
                if not retreat_works:
                    items.append({"name": "shield", "count": 1})
                return {"items": items}
            if action == "get_entities":
                return {"entities": [survivor] if attacks else [target, survivor]}
            if action == "look":
                looks.append(body)
            if action == "attack":
                attacks.append(body["entity_id"])
                return {"hits": 1, "killed": True}
            return {"ok": True}

    def retreat(_env, threat_pos, **kwargs):
        retreats.append(threat_pos)
        return {"ok": retreat_works}

    monkeypatch.setattr(CombatEnv, "flee", retreat)
    outcome = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie"},
    )

    assert attacks == [42]
    assert retreats == ([survivor["pos"]] if remaining_hostile else [])
    assert outcome["mode"] == ("low_hp_disengage" if remaining_hostile else "counter_killed")
    if not retreat_works:
        assert outcome["latched_guard"] is True
        assert looks[-1] == {"entity_id": 43}

