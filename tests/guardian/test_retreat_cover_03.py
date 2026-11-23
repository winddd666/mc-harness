from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("situation", ["lone", "second_hostile", "wall", "water", "new_life", "timeout", "escaped"])
def test_poll_tries_one_stationary_strike_when_an_armed_bot_cannot_retreat(monkeypatch, situation):
    zombie = {"id": 300235, "name": "zombie", "pos": [13570.3, 75, 13635.7], "dist": 0.7, "blocked_by_wall": False}
    sword = {"name": "iron_sword", "count": 1}
    status = {
        "pos": [13569.6, 75, 13635.7], "health": 5.66, "food": 20, "ready": True,
        "alive": True, "on_ground": True, "dimension": "overworld", "last_death": {"t": 1},
        "nearest_hostile": zombie, "held": sword,
    }
    attacks = []
    stops = []

    class CornerBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [hit] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [sword], "held": sword}
            if action == "get_entities":
                assert body == {"types": ["hostile"], "radius": 6} and kwargs["timeout_s"] == 0.35
                if situation == "new_life":
                    status["last_death"] = {"t": 2}
                hostiles = [{**zombie, "blocked_by_wall": situation == "wall"}]
                if situation == "second_hostile":
                    hostiles.append({"id": 300236, "name": "skeleton", "dist": 4.0})
                return {"entities": hostiles}
            if action == "attack":
                attacks.append(body)
                if situation == "timeout":
                    raise httpx.ReadTimeout("strike reply went missing")
                return {"hits": 1, "killed": True}
            if action == "stop":
                stops.append(action)
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(CornerBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda _status: None)

    def flee(*_args, **_kwargs):
        if situation == "water":
            status.update(in_water=True)
        if situation == "escaped":
            return {"ok": True, "mode": "flee", "moved_dist": 5.1}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    incident = guard.poll()

    if situation in {"lone", "timeout"}:
        attack, = attacks
        assert attack["entity_id"] == zombie["id"] and attack["max_hits"] == 1 and attack["chase"] is False
        assert incident["actions"][-1]["action"] == "cornered_melee_strike"
        assert incident["actions"][-1]["ok"] is (situation == "lone")
    else:
        assert attacks == []
    assert stops == (["stop"] if situation == "timeout" else [])


@pytest.mark.parametrize("scene", [
    "cornered", "killed", "withering_tick", "wall", "second_mob", "new_life", "no_wither",
    "stale_tick", "retreated", "equip_unconfirmed", "shield_dropped", "attack_timeout",
])
def test_withered_shielded_bot_readies_sword_after_a_cornered_retreat(monkeypatch, scene):
    import time

    origin = [-384.6, 55, 1746.3]
    target = {"id": 1060725, "name": "wither_skeleton", "dist": 1.3, "pos": [-383.3, 55, 1746.3],
              "blocked_by_wall": scene == "wall"}
    sword = {"name": "iron_sword", "count": 1}
    shield = {"name": "shield", "count": 1}
    status = {
        "pos": origin, "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "dimension": "the_nether", "last_death": {"t": 1}, "health": 5.0, "food": 20,
        "held": {"name": "cobblestone", "count": 44}, "offhand": shield, "nearest_hostile": target,
        "effects": [] if scene == "no_wither" else [{"name": "wither", "level": 1, "duration_s": 10}],
    }
    attack_source = None if scene in {"withering_tick", "stale_tick"} else "mob_attack"
    calls = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": attack_source}]
                        if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [sword, status["held"]], "held": status["held"], "offhand": shield}
            if action == "get_entities":
                return {"entities": [target, {"id": 42, "name": "blaze"}] if scene == "second_mob" else [target]}
            if action == "equip":
                calls.append((action, body))
                if scene != "equip_unconfirmed":
                    status["held"] = sword
                if scene == "shield_dropped":
                    status["blocking"] = False
                if scene == "new_life":
                    status["last_death"] = {"t": 2}
            if action == "attack":
                assert status["held"] == sword and status["blocking"]
                calls.append((action, body))
                status["blocking"] = False
                if scene == "attack_timeout":
                    request = httpx.Request("POST", "http://localhost/attack", json=body)
                    raise httpx.ReadTimeout("attack reply timed out", request=request)
                if scene == "killed":
                    status["nearest_hostile"] = None
                return {"hits": 1, "killed": scene == "killed"}
            if action == "stop":
                calls.append((action, body))
            if action == "posture":
                calls.append((action, body))
                status["blocking"] = body.get("shield") is True
                return {"blocking": status["blocking"]}
            return {"ok": True}

    def retreat(*_args, **_kwargs):
        if scene == "retreated":
            return {"mode": "flee", "ok": True, "moved_dist": 4.0}
        return {"mode": "flee", "ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda *_args: None)
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    if scene == "withering_tick":
        guard._last_melee_hurt_at = time.monotonic() - 0.3
    incident = guard.poll()
    attacks = [body for endpoint, body in calls if endpoint == "attack"]
    if scene in {"cornered", "killed", "withering_tick", "shield_dropped", "attack_timeout"}:
        attack, = attacks
        assert attack["entity_id"] == target["id"] and attack["max_hits"] == 1
        assert attack["chase"] is False and attack["feedback"] is True
        assert calls[0] == ("equip", {"item": "iron_sword", "slot": "hand"})
        if scene == "killed":
            assert calls[-1][0] == "attack"
        else:
            assert calls[-1][0] == "posture" and status["blocking"]
        strike = next(item for item in incident["actions"] if item["action"] == "cornered_melee_strike")
        if scene == "attack_timeout":
            assert strike["error"]["code"] == "network_uncertain" and strike["error"]["timeout"]
            assert [action for action, _ in calls].count("stop") == 1
        else:
            assert strike["ok"] and strike["result"]["killed"] is (scene == "killed")
    else:
        assert attacks == []


@pytest.mark.parametrize("scene", [
    "landed", "airborne", "respawn", "different_enemy", "outside_reach", "second_enemy", "no_wither",
    "lost_shield", "reface_failed", "lava_landing",
])
def test_cornered_wither_skeleton_gets_one_stationary_strike_after_knockback_settles(monkeypatch, scene):
    air_pos = [3531.7, 55.4, -3789.9]
    landed_pos = [3531.7, 55, -3790.1]
    enemy_pos = [3531.7, 55, -3791.6] if scene != "outside_reach" else [3531.7, 55, -3792.2]
    target = {"id": 2139054, "name": "wither_skeleton", "dist": math.dist(landed_pos, enemy_pos),
              "pos": enemy_pos, "blocked_by_wall": False}
    sword = {"name": "iron_sword", "slot": 37}
    shield = {"name": "shield", "slot": 45}
    status = {
        "pos": air_pos, "ready": True, "alive": True, "on_ground": False, "blocking": True,
        "dimension": "the_nether", "last_death": {"t": 1}, "health": 7, "food": 20,
        "held": sword, "offhand": shield, "nearest_hostile": target,
        "effects": [{"name": "wither", "level": 1, "duration_s": 10}],
    }
    writes = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": target}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{**sword, "count": 1}], "held": sword, "offhand": shield}
            if action == "get_entities":
                others = [{"id": 2139055, "name": "piglin"}] if scene == "second_enemy" else []
                return {"entities": [target, *others]}
            if action == "look":
                writes.append((action, body))
                if scene == "reface_failed":
                    raise ApiError("target_not_visible", "could not face the attacker")
            if action == "attack":
                assert status["pos"] == landed_pos and status["blocking"] is True
                writes.append((action, body))
                status["nearest_hostile"] = None
                return {"connected_hits": 1, "killed": True}
            return {"ok": True}

    def flee(*_args, **_kwargs):
        status.update(pos=landed_pos, health=4.133, on_ground=scene != "airborne")
        if scene == "respawn":
            status["last_death"] = {"t": 2}
        if scene == "different_enemy":
            status["nearest_hostile"] = {**target, "id": 2139055}
        if scene == "lost_shield":
            status["blocking"] = False
        if scene == "no_wither":
            status["effects"] = []
        return {"mode": "flee", "ok": False, "reason": "unsafe_landing", "on_ground": False,
                "in_lava": scene == "lava_landing", "in_water": False, "moved_dist": 0.0, "pos_after": air_pos}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_defend_or_flee", flee)
    incident = guard.poll()

    attacks = [body for action, body in writes if action == "attack"]
    if scene == "landed":
        attack, = attacks
        assert attack["entity_id"] == target["id"] and attack["chase"] is False and attack["max_hits"] == 1
        assert writes[0] == ("look", {"entity_id": target["id"]})
        assert incident["actions"][-1]["result"]["killed"] is True
    else:
        assert not attacks


@pytest.mark.parametrize(
    "survivor_name, survivor_dist, keep_shield",
    [("skeleton", 8.6, True), ("skeleton", 13.0, False), ("husk", 1.2, True), ("husk", 4.0, False)],
)
def test_counter_kill_keeps_raised_shield_facing_a_nearby_survivor(survivor_name, survivor_dist, keep_shield):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    melee = {"id": 42, "name": "zombie", "dist": 3.5, "pos": [4.0, 72.0, 0.5]}
    survivor = {"id": 43, "name": survivor_name, "dist": survivor_dist, "pos": [0.5, 72.0, 0.5 + survivor_dist]}
    calls = []
    killed = False
    blocking = True

    class ShieldBot:
        def status(self):
            nearest = survivor if killed else melee
            return {
                "pos": [0.5, 72.0, 0.5], "health": 13.32 if killed else 16.68, "food": 17,
                "alive": True, "ready": True, "on_ground": True, "blocking": blocking,
                "nearest_hostile": {key: nearest[key] for key in ("id", "name", "dist")},
            }

        def call(self, action, body=None, **kwargs):
            nonlocal killed, blocking
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [survivor] if killed else [melee, survivor]}
            if action in {"attack", "look", "posture"}:
                calls.append((action, body))
            if action == "attack":
                killed = True
                return {"hits": 1, "killed": True}
            if action == "posture":
                blocking = body.get("shield", False)
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(ShieldBot()), {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie"},
    )

    assert outcome["mode"] == "counter_killed"
    assert [body["entity_id"] for action, body in calls if action == "attack"] == [42]
    if keep_shield:
        assert calls[-2:] == [("look", {"entity_id": 43}), ("posture", {"shield": True, "sneak": False, "jump": False})]
        assert outcome["latched_guard"] is True and blocking
    else:
        assert calls[-1] == ("posture", {})
        assert outcome["latched_guard"] is False and not blocking


def test_bug034_ranged_combat_stance_blocking_and_cover_exempt_cancels(monkeypatch):
    """Covers BUG-034: 30s ranged cooldown + 35s combat stance + blocking/dig_cover exemption stop skeleton cancel storms."""
    clock = [3000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    state = {
        "skeleton_on": False,
        "blocking": False,
        "health": 18.0,
        "hurt_seq": 0,
        "close_creeper": False,
    }
    cover_calls = {"n": 0}

    class _CoverSkills:
        class _Eng:
            nodes = {}
            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def __init__(self, *, cover_ok: bool):
            self.cover_ok = cover_ok

        def dig_cover_pocket(self):
            cover_calls["n"] += 1
            return {"ok": self.cover_ok, "mode": "cover_pocket"} if self.cover_ok else {"ok": False, "reason": "no_filler_block"}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 4.0}

    # 1) Direct poll checks: initial pre_hurt_ranged cancels once, then 35s stance & blocking=True exempt sustained arrows!
    class _SustainedSkeletonBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                hostile = (
                    {"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}
                    if state["close_creeper"]
                    else ({"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0} if state["skeleton_on"] else None)
                )
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": state["health"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": state["blocking"],
                    "nearest_hostile": hostile,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": state["hurt_seq"], "type": "hurt", "health": state["health"], "source": "arrow"}] if state["hurt_seq"] > since else []
                return {"events": rows, "last_seq": state["hurt_seq"]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                if state["close_creeper"]:
                    return {"entities": [{"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}]}
                return {"entities": [{"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}] if state["skeleton_on"] else []}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            return {"ok": True}

    guard = GenerationGuard(_SustainedSkeletonBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_CoverSkills(cover_ok=False))
    state["skeleton_on"] = True
    inc1 = guard.poll()
    assert inc1 is not None and "pre_hurt_ranged" in inc1["reasons"]
    assert inc1["requires_replan"] is True

    # 12s & 24s later (inside 30s target cooldown & 35s stance): pre_hurt_ranged MUST NOT re-fire!
    clock[0] += 12.0
    assert guard.poll() is None
    clock[0] += 12.0
    assert guard.poll() is None

    # Sustained arrow hit while blocking=True and HP=18.0 -> exempted from cancelling generation (`requires_replan=False`),
    # and subsequent arrow hits within 15s of failed dig_cover stay completely silent (`None`)!
    state["blocking"] = True
    state["hurt_seq"] = 1
    clock[0] += 1.0
    inc_blk1 = guard.poll()
    assert inc_blk1 is None or inc_blk1["requires_replan"] is False
    clock[0] += 4.0
    state["hurt_seq"] = 2
    assert guard.poll() is None  # no repeat dig_cover churn or cancel storm!

    # Low HP (<=10.0) or close creeper STILL immediately overrides and requires replan!
    state["health"] = 9.0
    state["hurt_seq"] = 3
    clock[0] += 3.5
    inc_low = guard.poll()
    assert inc_low is not None and inc_low["requires_replan"] is True

    state["health"] = 18.0
    state["close_creeper"] = True
    clock[0] += 0.5
    inc_cr = guard.poll()
    assert inc_cr is not None and "pre_hurt_creeper" in inc_cr["reasons"] and inc_cr["requires_replan"] is True

    # 2) End-to-end run_mc_agent: sustained skeleton visibility + arrows over 26s cancels ONLY ONCE!
    clock[0] = 4000.0
    state.update({"skeleton_on": False, "blocking": False, "health": 18.0, "hurt_seq": 0, "close_creeper": False})
    req_n = {"n": 0}

    def handle_http(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            res = {
                "pos": [0.5, 64.0, 0.5],
                "health": state["health"],
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "blocking": state["blocking"],
                "nearest_hostile": {"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0} if state["skeleton_on"] else None,
            }
        elif action == "events":
            params_s = str(request.url.query or b"")
            since = int(params_s.split("since=")[-1].split("&")[0]) if "since=" in params_s else 0
            rows = [{"seq": state["hurt_seq"], "type": "hurt", "health": state["health"], "source": "arrow"}] if state["hurt_seq"] > since else []
            res = {"events": rows, "last_seq": state["hurt_seq"]}
        elif action == "inventory":
            res = {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}], "offhand": {"name": "shield"}}
        elif action == "get_entities":
            res = {"entities": [{"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}] if state["skeleton_on"] else []}
        elif action == "get_block":
            body = json.loads(request.content) if request.content else {}
            p = body.get("pos", [0, 64, 0])
            res = {"name": "stone" if p[1] < 64 else "air"}
        elif action == "find_blocks":
            res = {"blocks": []}
        else:
            res = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": res})

    def chat_ranged(messages, tools=None, *, control=None, **kwargs):
        req_n["n"] += 1
        if req_n["n"] == 1:
            state["skeleton_on"] = True
            clock[0] += 0.5
            check_control(control)
            pytest.fail("Expected first ranged trigger to cancel generation 1 once.")
        # Simulate 26s of sustained skeleton pressure (11s, 21s, + arrow hits while blocking=True)
        state["blocking"] = True
        for seq_i in range(1, 4):
            clock[0] += 9.0
            state["hurt_seq"] = seq_i
            check_control(control)
        return {"role": "assistant", "content": "处于远程交战姿态持续输出，未被反复打断。"}

    with BotSession("cached", transport=httpx.MockTransport(handle_http)) as bot:
        rep = run_mc_agent(
            "bug-034 sustained ranged stance",
            bot,
            chat_fn=chat_ranged,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
        )
    assert rep["status"] == "answered"
    assert req_n["n"] == 2
    assert rep["guardian"]["cancelled_generations"] == 1


@pytest.mark.parametrize("landing", ["cornered", "short", "moved", "clear", "respawned"])
@pytest.mark.parametrize("trailing_event", [None, "unattributed", "arrow"])
def test_poll_refaces_a_close_melee_attacker_with_shield_after_retreat(monkeypatch, landing, trailing_event):
    attacker = {"id": 77, "name": "wither_skeleton", "dist": 3.1, "pos": [3602.5, 64, 1817.6]}
    status = {"pos": [3599.4, 64, 1817.6], "dimension": "the_nether", "health": 14.4,
              "ready": True, "alive": True, "on_ground": True, "blocking": True, "food": 17,
              "last_death": {"t": 1}, "nearest_hostile": attacker}
    looks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurts = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}]
                if trailing_event == "unattributed":
                    hurts += [{"seq": 2, "type": "hurt", "source": "wither"}, {"seq": 3, "type": "hurt"}]
                elif trailing_event == "arrow":
                    hurts += [{"seq": 2, "type": "hurt", "source": "arrow"}, {"seq": 3, "type": "hurt"}]
                return {"events": [] if kwargs["params"]["since"] else hurts}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                looks.append(body)
            assert action != "posture"
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def retreat(_threat_pos, *, wait_for_ground):
        status.update(health=9.56, nearest_hostile={"id": 78, "name": "wither_skeleton", "dist": 5 if landing == "clear" else 2.2})
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        if landing in {"short", "moved"}:
            return {"ok": landing == "moved", "mode": "flee", "moved_dist": 1.2 if landing == "short" else 2.64}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    actions = [a["action"] for a in incident["actions"]]
    should_face = trailing_event != "arrow" and landing in {"cornered", "short", "moved"}
    assert actions == (["combat_or_flee", "face_attacker_after_retreat"] if should_face else ["combat_or_flee"])
    assert looks == ([{"entity_id": 78}] if should_face else [])


@pytest.mark.parametrize(
    "recent_arrow, hp_before, hp_after, blocking, shooter_dist, expected_action",
    [
        (False, 3.34, 2.55, True, None, None),
        (False, 3.03, 0.96, False, None, "raise_shield_after_retreat"),
        (False, 3.03, 5.3, False, None, None),
        (True, 3.34, 2.55, True, None, "face_attacker_after_retreat"),
        (False, 8.7, 1.63, True, None, "face_attacker_after_retreat"),
        (False, 8.7, 6.2, True, None, None),
        (True, 7.4, 1.3, False, 9.5, "raise_shield_after_retreat"),
        (True, 7.4, 1.3, False, 14.0, None),
        (False, 7.4, 1.3, False, 9.5, None),
        (True, 7.4, 5.3, False, 9.5, None),
    ],
)
def test_poll_checks_shield_after_surface_melee_retreat(
    monkeypatch, recent_arrow, hp_before, hp_after, blocking, shooter_dist, expected_action,
):
    front = {"id": 44, "name": "zombie", "dist": 2.5, "pos": [9250.0, 64, -11469.5]}
    flank = {"id": 55, "name": "zombie", "dist": 1.9, "pos": [9249.8, 64, -11469.6]}
    if shooter_dist is not None:
        flank = {"id": 55, "name": "skeleton", "dist": shooter_dist}
    status = {
        "pos": [9252.5, 64, -11469.5], "health": hp_before, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": blocking, "nearest_hostile": front,
        "offhand": {"name": "shield"},
    }
    looks = []
    postures = []

    class SurfaceBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if kwargs["params"]["since"]:
                    return {"events": []}
                hits = [{"seq": 2, "type": "hurt", "source": "mob_attack", "source_entity": front}]
                if recent_arrow:
                    hits.insert(0, {"seq": 1, "type": "hurt", "source": "arrow"})
                return {"events": hits}
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                looks.append(body)
            if action == "posture":
                postures.append(body)
                status["blocking"] = body.get("shield") is True
                return {"blocking": status["blocking"]}
            return {"ok": True}

    guard = GenerationGuard(SurfaceBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def retreat(*args, **kwargs):
        status.update(pos=[9250.1, 64, -11467.9], health=hp_after, nearest_hostile=flank)
        return {"ok": True, "mode": "flee", "moved_dist": 2.26}

    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    expected = ["combat_or_flee", *([expected_action] if expected_action else [])]
    assert [action["action"] for action in incident["actions"]] == expected
    assert looks == ([{"entity_id": 55}] if expected_action else [])
    assert postures == (
        [{"shield": True, "jump": False, "sneak": False}] if expected_action == "raise_shield_after_retreat" else []
    )


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("nearby", [False, True])
def test_poll_follows_unarmed_retreat_when_close_melee_threat_remains(monkeypatch, nearby, complete):
    attacker = {"id": 77, "name": "spider", "dist": 3.1, "pos": [5003.5, 39, -7590.8]}
    status = {"pos": [5000.4, 39, -7591.1], "dimension": "overworld", "health": 15.5, "food": 20,
              "ready": True, "alive": True, "on_ground": True, "nearest_hostile": attacker}
    followups = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [{**attacker, "pos": [5002, 39, -7591.4]}]}
            return {"ok": True}

    def flee(*args, **kwargs):
        status.update(pos=[5004.3, 39, -7591.4], health=6.7, nearest_hostile={
            "id": 77, "name": "spider", "dist": 2.3 if nearby else 5.0,
        })
        return {"ok": complete, "mode": "flee", "moved_dist": 5.94 if complete else 1.39}

    def step(landing, target, *, deadline, target_id):
        assert target_id == 77
        followups.append((landing["health"], target))
        return {"ok": True, "moved_dist": 0.7, "target_distance_after": 3.0}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(guard.combat_env, "step_away_from_melee", step)
    incident = guard.poll()

    actions = [a["action"] for a in incident["actions"]]
    assert actions == (["combat_or_flee", "followup_melee_retreat"] if nearby else ["combat_or_flee"])
    assert followups == ([(6.7, [5002, 39, -7591.4])] if nearby else [])
    if nearby:
        assert incident["actions"][-1]["result"]["target_distance_after"] == 3.0


@pytest.mark.parametrize("landing", ["grounded", "airborne", "new_life", "new_enemy", "unsupported", "moved"])
def test_unarmed_melee_retries_a_zero_movement_retreat_after_confirmed_landing(monkeypatch, landing):
    air_pos = [164991.2, 102.2, 157674.4]
    floor_pos = [164991.2, 102, 157674.5]
    spider = {"id": 2093585, "name": "spider", "dist": 2.7, "pos": [164993.9, 102, 157674.5]}
    status = {
        "pos": air_pos, "health": 8.8, "food": 20, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": False, "last_death": {"t": 1}, "nearest_hostile": spider,
    }
    pulses = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": spider}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                supported = landing != "unsupported" and body["pos"][1] == 101
                return {"name": "stone" if supported else "air"}
            if action == "look":
                assert body["yaw"] == pytest.approx(math.pi / 2)
                return {"ok": True}
            if action == "control":
                pulses.append(body)
                status.update(pos=[164990.5, 102, 157674.5], nearest_hostile={**spider, "dist": 3.4})
                return {"ok": True}
            pytest.fail(f"Unexpected retreat action: {action}")

    def blocked_flee(*args, **kwargs):
        assert kwargs["wait_for_ground"] is True
        status.update(pos=floor_pos, on_ground=landing != "airborne", health=7.333)
        if landing == "new_life":
            status["last_death"] = {"t": 2}
        if landing == "new_enemy":
            status["nearest_hostile"] = {**spider, "id": 2094000}
        return {
            "ok": False, "mode": "flee", "reason": "unsafe_landing", "pos_after": air_pos, "on_ground": False,
            "moved_dist": 1.0 if landing == "moved" else 0.0, "in_water": False, "in_lava": False,
        }

    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", blocked_flee)
    result = guard.poll()

    followups = [a for a in result["actions"] if a["action"] == "followup_melee_retreat"]
    assert pulses == ([{"forward": True, "ms": 220, "stop_on": ["death"]}] if landing == "grounded" else [])
    if landing in {"grounded", "unsupported"}:
        followup, = followups
        assert followup["ok"] is (landing == "grounded")
        assert followup["result"]["entity_id"] == spider["id"]
    else:
        assert not followups


@pytest.mark.parametrize(
    "health, distance_after, alternate_shooter, keep_shield",
    [(7.0, 11.6, False, True), (7.0, 16.0, False, False), (13.28, 1.9, True, True), (13.28, 4.0, True, False)],
)
def test_retreat_keeps_shield_against_a_nearby_shooter(health, distance_after, alternate_shooter, keep_shield):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    pos = [0.5, 64.0, 0.5]
    shooter = {"id": 77, "name": "skeleton", "pos": [9.6, 64.0, 0.5], "dist": 9.1}
    posture_calls = []

    class RetreatBot:
        def status(self):
            return {"pos": list(pos), "health": health, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "blocking": True, "nearest_hostile": dict(shooter)}

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [dict(shooter)]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "control" and body.get("forward"):
                pos[0] -= 2.5
                shooter["dist"] = distance_after
                if alternate_shooter:
                    shooter.update(id=88, name="blaze")
            if action in {"posture", "look"}:
                posture_calls.append((action, body))
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(RetreatBot()), {"source": "pre_hurt_ranged", "primary_entity_id": 77, "primary_type": "skeleton"},
    )
    assert {"action": "flee", "ok": True, "moved": 2.5} in outcome["actions"]
    assert outcome["latched_guard"] is keep_shield
    if keep_shield:
        assert posture_calls[-2:] == [
            ("look", {"entity_id": 88 if alternate_shooter else 77}),
            ("posture", {"shield": True, "sneak": False, "jump": False}),
        ]
    else:
        assert posture_calls[-1] == ("posture", {})

