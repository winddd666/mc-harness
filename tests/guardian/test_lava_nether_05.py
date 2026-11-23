from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(
    "cover", ["safe", "exposed", "fresh_hit", "burning_after_hit", "burning_old_hit"],
)
def test_unsafe_nether_flee_rechecks_recent_cover_before_leaving(monkeypatch, cover):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    blaze = {"id": 44, "name": "blaze", "dist": 6.0, "pos": [0.5, 64, 6.5]}
    second = {"id": 45, "name": "blaze", "dist": 8.0, "pos": [-7.5, 64, 0.5]}
    status = {
        "pos": [0.5, 64, 0.5], "dimension": "the_nether", "health": 8.3,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": blaze,
    }
    if cover.startswith("burning_"):
        status.update(health=2.3, on_fire=True)
    checks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                return {"entities": [blaze, second]}
            raise AssertionError(action)

    def seek_cover(_env, pos, *, other_shooters, allow_stationary=True):
        checks.append("cover")
        assert pos == blaze["pos"] and other_shooters == [second["pos"]]
        if cover != "exposed" and allow_stationary:
            return {"ok": True, "mode": "natural_cover", "moved_dist": 0.0, "pos_after": status["pos"]}
        return None

    def flee(_env, pos, **_kwargs):
        checks.append("flee")
        assert pos == blaze["pos"]
        return {"ok": True, "mode": "flee", "moved_dist": 2.2}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = FortressBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._mark_ranged_engagement(987.0, attacker=blaze, in_cover=True)
    if cover == "fresh_hit":
        guard._last_ranged_hurt_at = 999.0
    if cover == "burning_after_hit":
        guard._last_ranged_hurt_at = 991.0
    if cover == "burning_old_hit":
        guard._last_ranged_hurt_at = 987.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert result["requires_replan"]
    assert result["mode"] == ("natural_cover" if cover in {"safe", "burning_old_hit"} else "flee")
    assert checks == {
        "safe": ["cover"],
        "exposed": ["cover", "flee"],
        "fresh_hit": ["flee"],
        "burning_after_hit": ["cover", "flee"],
        "burning_old_hit": ["cover"],
    }[cover]


@pytest.mark.parametrize("safe_bank", [True, False])
def test_repeated_magma_damage_after_successful_sneak_seeks_safe_ground(monkeypatch, safe_bank):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    hurt = {"seq": 1, "type": "hurt", "source": "hot_floor"}
    status = {"pos": [0.5, 64.0, 0.5], "health": 19.54, "food": 20, "ready": True,
              "alive": True, "on_ground": True, "blocking": True, "sneaking": False, "dimension": "the_nether"}
    calls = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [dict(hurt)] if kwargs["params"]["since"] < hurt["seq"] else []}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if y < 64:
                    return {"name": "netherrack" if safe_bank and (x, z) != (0, 0) else "magma_block"}
                return {"name": "air"}
            calls.append((action, body))
            if action == "posture":
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    initial = guard.poll()
    assert initial["requires_replan"] is False
    assert calls == [("posture", {"sneak": True, "shield": True, "jump": False})]
    clock[0] += 4.0
    hurt["seq"] += 1
    status["health"] = 19.08

    repeat = guard.poll()

    assert repeat["requires_replan"] is True
    action, = repeat["actions"]
    assert action["ok"] is safe_bank
    if safe_bank:
        assert action["result"]["mode"] == "stepped_off"
        assert calls[-1][0] == "move_to"
        assert status["pos"] != [0.5, 64.0, 0.5]
    else:
        assert action["result"]["reason"] == "no_safe_hot_floor_exit"
        assert all(action == "posture" for action, _ in calls)


@pytest.mark.parametrize("still_magma", [True, False])
def test_attack_replans_only_on_confirmed_magma(still_magma):
    status = {"pos": [0.5, 64, 0.5], "health": 17, "food": 20, "ready": True, "alive": True, "on_ground": True}
    moves = []
    attacks = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [] if since else [{"seq": 1, "type": "hurt", "source": "hot_floor"}]}
            if action == "posture":
                return {"sneaking": True}
            if action == "get_block":
                x, y, z = body["pos"]
                if y >= 64:
                    return {"name": "air"}
                return {"name": "magma_block" if still_magma and (x, z) == (0, 0) else "stone"}
            if action == "move_to":
                moves.append(body)
                status["pos"] = [body[k] for k in ("x", "y", "z")]
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll()["actions"][0]["ok"] is True

    def attack():
        attacks.append(True)
        raise httpx.ReadTimeout("attack timed out")

    if still_magma:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert result["requires_replan"] is True and result["error"]["code"] == "hot_floor"
        assert result["recovery"]["result"]["mode"] == "stepped_off"
        assert len(moves) == 1 and attacks == []
    else:
        with pytest.raises(httpx.ReadTimeout, match="attack timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert attacks == [True] and moves == []


@pytest.mark.parametrize("terrain", ["solid", "drop", "lava", "unknown"])
def test_nether_attack_checks_footing_toward_nearby_blaze(terrain):
    calls = []
    blaze = {"id": 4645315, "name": "blaze", "dist": 1.3, "pos": [1.8, 65, 0.5]}

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 65, 0.5], "health": 17.64, "ready": True, "alive": True,
                    "on_ground": True, "dimension": "the_nether", "nearest_hostile": blaze,
                }
            if action == "get_block":
                x, y, _ = body["pos"]
                if y == 64:
                    if x == 0 or terrain == "solid":
                        return {"name": "netherrack"}
                    if terrain == "unknown":
                        raise httpx.ReadTimeout("unverified footing")
                    return {"name": "lava" if terrain == "lava" else "air"}
                return {"name": "air"}
            if action == "attack":
                calls.append(action)
                raise httpx.ReadTimeout("attack response timed out")
            raise AssertionError(action)

    bot = FortressBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": blaze["id"], "timeout": 5000}))

    if terrain in {"solid", "unknown"}:
        with pytest.raises(httpx.ReadTimeout, match="attack response timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert calls == ["attack"]
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert calls == []
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("contact", ["in_lava", "lava_hurt", "lava_hurt_then_heal"])
def test_lava_watcher_preempts_death_only_mining_and_waits_for_stop(monkeypatch, interrupted, contact):
    import threading
    import time

    stop_started = threading.Event()
    dig_returning = threading.Event()
    stop_finished = threading.Event()
    watcher_sampled = threading.Event()
    status = {
        "pos": [6391.6, 64, -6359.7], "health": 20, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": True, "in_lava": False,
    }
    escapes = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                snapshot = dict(status)
                if threading.current_thread().name == "mc-creeper-watch":
                    watcher_sampled.set()
                return snapshot
            if action == "get_block":
                return {"name": "stone"}
            if action == "events":
                if contact != "in_lava" and status.get("on_fire"):
                    return {"events": [{"seq": 1, "type": "hurt", "source": "lava"}]}
                return {"events": []}
            if action == "dig":
                assert body["stop_on"] == ["death"]
                assert watcher_sampled.wait(1.5)
                status.update(
                    pos=[6391.6, 63, -6359.7], health=4.3 if contact == "in_lava" else 16.528, on_ground=contact != "in_lava",
                    in_lava=contact == "in_lava", on_fire=True,
                    nearest_hostile={"id": 255310, "name": "witch", "dist": 12.1},
                )
                assert stop_started.wait(1.5)
                dig_returning.set()
                if interrupted:
                    raise ApiError("interrupted", "stopped")
            if action == "stop" and threading.current_thread().name == "mc-creeper-watch":
                stop_started.set()
                assert dig_returning.wait(1.5)
                time.sleep(0.12)
                stop_finished.set()
            return {"ok": True}

    bot = LavaBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def escape(*, force_rim_escape=False):
        assert stop_finished.is_set()
        assert force_rim_escape is (contact != "in_lava")
        escapes.append(status["pos"])
        status.update(pos=[6390.5, 64, -6359.7], on_ground=True, in_lava=False)
        if contact == "lava_hurt_then_heal":
            status["health"] = 20
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_blocks", lambda: json.dumps(bot.call(
        "dig", {"pos": [6393, 64, -6360], "stop_on": ["death"]},
    ))))

    assert escapes == [[6391.6, 63, -6359.7]]
    assert result["reflex_mode"] == "lava_escape" and result["status_after"]["on_ground"] is True
    assert "并脱困" in result["message"]


@pytest.mark.parametrize("contact", ["brief_lava", "lava_timeout", "burn_only", "stale_lava", "new_life", "ordinary_timeout"])
def test_w19_death_only_move_hands_off_confirmed_lava_contact(monkeypatch, contact):
    import threading
    import time
    from mc_agent.harness.mc_tools import McToolset

    status = {
        "pos": [214216.5, 56, 164524.0], "health": 20, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "in_lava": False, "on_fire": False,
    }
    events = []
    actions = []
    stopped = threading.Event()
    observed = threading.Event()

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "status":
            if threading.current_thread().name == "mc-creeper-watch" and actions:
                observed.set()
            result = dict(status)
        elif endpoint == "move_to":
            assert json.loads(request.content)["stop_on"] == ["death"]
            actions.append("move_to")
            status.update(pos=[214186.4, 15, 164525.6], health=7.89, on_ground=False, on_fire=True)
            stamp = time.time() * 1000
            if contact not in {"burn_only", "ordinary_timeout"}:
                events.append({"seq": 1, "type": "hurt", "source": "lava", "t": stamp - (4000 if contact == "stale_lava" else 0)})
            events.append({"seq": 2, "type": "hurt", "source": "on_fire", "t": stamp})
            if contact == "new_life":
                status["last_death"] = {"t": 2}
            if contact in {"lava_timeout", "ordinary_timeout"}:
                raise httpx.ReadTimeout("move response timed out", request=request)
            if contact == "brief_lava":
                assert stopped.wait(2.0), "movement continued after a confirmed lava hit"
            else:
                assert observed.wait(2.0)
            result = {"pos": status["pos"]}
        elif endpoint == "stop":
            actions.append("stop")
            stopped.set()
            result = {"released": True}
        elif endpoint == "events":
            result = {"events": list(events)}
        elif endpoint == "inventory":
            result = {"items": []}
        else:
            pytest.fail(f"Unexpected movement endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": result})

    def escape(*, force_rim_escape):
        assert stopped.is_set() and force_rim_escape
        actions.append("escape")
        status.update(pos=[214186.4, 13, 164525.6], on_ground=True, on_fire=False)
        return {"ok": True, "in_lava": False}

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot)
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
        result = json.loads(guard.run_tool_with_reflex(
            "mc_move_to", lambda: toolset.mc_move_to(x=214163, z=164522, range=2, ignore_hurt=True),
        ))

    if contact in {"brief_lava", "lava_timeout"}:
        assert actions == ["move_to", "stop", "escape"]
        assert result["reflex_mode"] == "lava_escape" and result["action_released"] is True
        assert result["status_after"]["pos"] == [214186.4, 13, 164525.6]
    elif contact == "ordinary_timeout":
        assert actions == ["move_to", "stop"] and result["error"]["code"] == "move_timeout"
    else:
        assert actions == ["move_to"] and result["ok"] is True


@pytest.mark.parametrize("terrain", ["netherrack", "drop", "lava"])
def test_nether_combat_checks_footing_before_issuing_a_chase_pulse(terrain):
    moves = []
    status = {
        "pos": [0.5, 64, 0.5], "yaw": 0.0, "health": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "the_nether",
    }

    class CombatBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                _, y, z = body["pos"]
                if y == 63:
                    return {"name": "netherrack" if terrain == "netherrack" or z >= 0 else "lava" if terrain == "lava" else "air"}
                return {"name": "air"}
            if action in {"control", "stop"}:
                moves.append(action)
                return {"ok": True}
            raise AssertionError(action)

    bot = CombatBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_combat", lambda: json.dumps(
        bot.call("control", {"forward": True, "sprint": True, "ms": 250, "wait": False, "stop_on": []}),
    )))

    if terrain == "netherrack":
        assert moves == ["control"] and result["ok"]
    else:
        assert moves == ["stop"]
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize(("mode", "gear", "scene", "expected"), [
    ("auto", "bow", "airborne", "shoot"),
    ("rush", "crossbow", "airborne", "shoot"),
    ("auto", "empty_bow", "airborne", "blaze_requires_ranged"),
    ("melee", "none", "high_nearby", "blaze_requires_ranged"),
    ("melee", "none", "supported", "attack"),
    ("melee", "none", "stationary_connection_miss", "attack_zero_hits"),
    ("melee", "none", "stationary_unchanged_hp", "attack_zero_hits"),
    ("melee", "none", "stationary_damage", "attack"),
    ("rush", "none", "missing_target_footing", "blaze_requires_ranged"),
    ("auto", "bow", "missing_bot_footing", "unsafe_landing"),
    ("auto", "bow", "wall", "blocked_by_wall"),
    ("auto", "bow", "blocked_then_visible", "shoot"),
    ("auto", "bow", "unknown_clear", "shoot"),
    ("auto", "bow", "unknown_blocked", "blocked_by_wall"),
    ("auto", "bow", "unknown_alternate", "shoot"),
    ("auto", "bow", "unknown_unloaded", "combat_visibility_unconfirmed"),
    ("auto", "bow", "target_lost", "shoot"),
    ("auto", "bow", "wall_after_shot", "shoot"),
    ("auto", "bow", "respawn", "combat_position_changed"),
    ("auto", "bow", "shot_unconfirmed", "shoot_unconfirmed"),
    ("auto", "bow", "empty_shot_receipt", "shoot_unconfirmed"),
])
def test_nether_blaze_combat_engages_without_chasing(mode, gear, scene, expected):
    from mc_agent.harness.mc_tools import McToolset

    # Position and enemy ID from the Long3 fortress run. The close cases model
    # the same blaze entering reach while the bot waits on a supported floor.
    origin = [3524.5, 69, -3802.4]
    target = {
        "id": 1133882, "name": "blaze", "blocked_by_wall": scene == "wall",
        "pos": [3526.5, 69, -3802.4] if scene in {"supported", "missing_target_footing"} or scene.startswith("stationary_")
        else [3525.5, 70.5, -3802.4] if scene == "high_nearby" else [3543.5, 62.7, -3790.5],
    }
    if scene.startswith("unknown_") or scene == "wall_after_shot":
        target.pop("blocked_by_wall")
    items = [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]
    if gear in {"bow", "crossbow", "empty_bow"}:
        items.append({"name": "crossbow" if gear == "crossbow" else "bow", "count": 1})
    if gear in {"bow", "crossbow"}:
        items.append({"name": "minecraft:spectral_arrow", "count": 2})
    writes = []

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if endpoint == "status":
            data = {
                "dimension": "the_nether", "pos": origin, "alive": True, "ready": True, "on_ground": True,
                "last_death": {"t": 100} if scene == "respawn" and writes else None,
            }
        elif endpoint == "get_entities":
            entities = [target]
            if scene == "blocked_then_visible":
                # Long3 H2 EP04, where the nearest blaze was behind obsidian.
                entities.insert(0, {
                    "id": 1136219, "name": "blaze", "pos": [3531.8, 55, -3801.8], "blocked_by_wall": True,
                })
            if scene == "unknown_alternate":
                entities.insert(0, {"id": 1136219, "name": "blaze", "pos": [3531.5, 69, -3802.4]})
            if scene == "target_lost" and writes:
                entities = [{**target, "id": 1133883}]
            data = {"entities": entities}
        elif endpoint == "inventory":
            data = {"items": items}
        elif endpoint == "get_block":
            x, y, z = body["pos"]
            unsupported = (scene == "missing_bot_footing" and x == 3524) or (
                scene == "missing_target_footing" and x == 3526
            )
            data = {"name": "air" if unsupported else "nether_bricks"}
            unknown_ray = scene.startswith("unknown_") or (scene == "wall_after_shot" and not writes)
            if unknown_ray and (x, y, z) != (3524, 68, -3803):
                if scene == "unknown_unloaded":
                    data = {}
                elif scene != "unknown_blocked" and not (scene == "unknown_alternate" and x == 3528 and z == -3803):
                    data = {"name": "air"}
        elif endpoint in {"shoot", "attack"}:
            writes.append((endpoint, body))
            if endpoint == "shoot":
                data = {"fired": scene != "shot_unconfirmed"}
                if scene == "empty_shot_receipt":
                    data["projectiles"] = []
                if len(writes) == 2:
                    data.update(target_health_before=4, target_health_after=0)
            else:
                data = {"hits": 1, "killed": True}
                if scene == "stationary_connection_miss":
                    data = {"hits": 1, "connected_hits": 0, "target_hp_delta": 0, "killed": False}
                elif scene == "stationary_unchanged_hp":
                    data = {"hits": 1, "target_health_before": 12, "target_health_after": 12, "killed": False}
                elif scene == "stationary_damage":
                    data = {"hits": 0, "target_health_before": 12, "target_health_after": 10, "killed": False}
        else:
            pytest.fail(f"Unexpected blaze combat endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": data})

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot)
        reply = json.loads(toolset.mc_skill_combat(mode=mode, target_name=" Minecraft:Blaze ", max_time_s=6))

    expected_calls = [expected] if expected in {"shoot", "attack"} else []
    if expected == "attack_zero_hits":
        expected_calls = ["attack"]
    if scene in {"respawn", "shot_unconfirmed", "empty_shot_receipt"}:
        expected_calls = ["shoot"]
    stopped_after_one = scene in {"target_lost", "wall_after_shot"}
    if expected == "shoot" and not stopped_after_one:
        expected_calls.append("shoot")
    assert [endpoint for endpoint, _ in writes] == expected_calls
    assert all(body["entity_id"] == target["id"] for _, body in writes)
    if expected == "attack":
        assert writes[0][1]["chase"] is False and writes[0][1]["max_hits"] == 1
        assert reply["ok"] and reply["result"]["killed"] is (scene != "stationary_damage")
    elif expected == "shoot":
        assert all(body["weapon"] == gear for _, body in writes)
        assert reply["ok"] and reply["result"]["shots"] == len(writes)
        assert reply["result"]["killed"] is (not stopped_after_one)
        assert reply["result"]["hits_landed"] == int(not stopped_after_one)
        if scene == "wall_after_shot":
            assert reply["result"]["end_reason"] == "blocked_by_wall"
    else:
        assert not reply["ok"] and reply["requires_replan"] and reply["error"]["code"] == expected


@pytest.mark.parametrize(("scene", "expected"), [
    ("clear", "shoot"), ("wall", "blocked_by_wall"), ("close_piglin", "rush"),
    ("closing_piglin", "combat_melee_pressure"), ("shield", "shield_blocking_shot"),
    ("no_blaze", "rush"), ("named_piglin", "rush"), ("no_ammo", "blaze_requires_ranged"),
])
def test_nether_auto_combat_reaches_stationary_blaze_from_a_mixed_sighting(monkeypatch, scene, expected):
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.skills import combat_v1

    # W17b HY11 EP02 actor sighting. The terrain and later snapshots are controlled here.
    origin = [0.5, 70, 3607.5]
    piglin = {"id": 2080288, "name": "piglin", "pos": [-3.9, 68, 3609.4], "dist": 4.6, "blocked_by_wall": True}
    other_piglin = {"id": 2080714, "name": "piglin", "pos": [-4.6, 68, 3610], "dist": 5.4, "blocked_by_wall": False}
    blaze = {"id": 2095792, "name": "blaze", "pos": [5.8, 74, 3586.5], "dist": 22.6}
    close_piglin = {**other_piglin, "pos": [2.5, 70, 3607.5], "dist": 2.0}
    writes = []
    rushes = []
    status_reads = 0

    def rush(_adapter, *, target_name, max_time_s):
        rushes.append(target_name)
        return {"killed": True}

    monkeypatch.setattr(combat_v1, "action_rush", rush)

    def handle(request):
        nonlocal status_reads
        endpoint = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if endpoint == "status":
            status_reads += 1
            closes = scene == "close_piglin" or (scene == "closing_piglin" and status_reads > 1)
            result = {
                "dimension": "the_nether", "pos": origin, "alive": True, "ready": True, "on_ground": True,
                "nearest_hostile": close_piglin if closes else piglin, "blocking": scene == "shield",
            }
        elif endpoint == "get_entities":
            entities = [piglin, close_piglin if scene == "close_piglin" else other_piglin]
            if scene != "no_blaze":
                entities.append(blaze)
            result = {"entities": entities}
        elif endpoint == "inventory":
            items = [{"name": "shield", "count": 1}, {"name": "bow", "count": 1}]
            if scene != "no_ammo":
                items.append({"name": "arrow", "count": 2})
            result = {"items": items}
        elif endpoint == "get_block":
            x, y, z = body["pos"]
            wall = scene == "wall" and z == 3604
            result = {"name": "nether_bricks" if wall or (x, y, z) == (0, 69, 3607) else "air"}
        elif endpoint == "shoot":
            writes.append((endpoint, body))
            result = {"fired": True, "used": {"arrow": 1}, "killed": True}
        elif endpoint in {"stop", "posture"}:
            writes.append((endpoint, body))
            result = {}
        else:
            pytest.fail(f"Unexpected tactical combat endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        reply = json.loads(McToolset(bot).mc_skill_combat(
            mode="auto", target_name="piglin" if scene == "named_piglin" else None, max_time_s=15,
        ))

    if expected == "rush":
        assert reply["ok"] and rushes == ["piglin"]
        assert [endpoint for endpoint, _ in writes] == ["stop", "posture"]
    elif expected == "shoot":
        assert not rushes and reply["ok"] and reply["result"]["killed"]
        assert reply["target_name"] == "blaze"
        assert writes == [("shoot", {
            "weapon": "bow", "entity_id": blaze["id"], "charge_ms": 1000, "stop_on": ["hurt", "death"],
        })]
    else:
        assert not rushes and not writes
        assert not reply["ok"] and reply["requires_replan"] and reply["error"]["code"] == expected


@pytest.mark.parametrize("case", [
    "distant_blaze", "unlisted_blaze", "read_timeout", "minor_hit", "ambient_fire", "new_life", "stale_hit",
])
def test_nether_combat_unwinds_read_polls_under_sustained_fireball_damage(monkeypatch, case):
    import threading
    import time

    from mc_agent.skills.combat_adapter import BotSessionCombatAdapter

    actor = threading.get_ident()
    observed = threading.Event()
    stopped = threading.Event()
    in_flight_read = case in {"unlisted_blaze", "read_timeout"}
    should_preempt = case == "distant_blaze" or in_flight_read
    fireball_source = "small_fireball" if case == "unlisted_blaze" else "fireball"
    blaze = {"id": 1133882, "name": "blaze", "dist": 20.1, "pos": [20.6, 64, 0.5]}
    status = {
        "ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64, 0.5],
        "on_ground": True, "dimension": "the_nether", "last_death": None,
        "nearest_hostile": None if case == "unlisted_blaze" else blaze,
    }
    hurt_events = []
    actions = []

    class CombatBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if threading.get_ident() != actor:
                    snapshot = dict(status)
                    observed.set()
                    return snapshot
                if not guard._in_reflex:
                    actions.append("actor_read")
                    if in_flight_read:
                        stopped.wait(1.5)
                        if case == "read_timeout":
                            raise httpx.ReadTimeout("battle status response timed out")
                return dict(status)
            if action == "events":
                return {"events": list(hurt_events)}
            if action == "stop":
                actions.append("stop")
                stopped.set()
                return {"ok": True, "released": True}
            raise AssertionError(action)

    bot = CombatBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    adapter = BotSessionCombatAdapter(bot)

    def retreat(_env, trigger):
        assert trigger["source"] == "tool_interrupted_hurt"
        assert trigger["hurt_source"] == fireball_source
        assert trigger["primary_entity_id"] == blaze["id"]
        actions.append("retreat")
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", retreat)

    def invoke():
        assert observed.wait(2.0)
        timestamp = (time.time() - (10.0 if case == "stale_hit" else 0.0)) * 1000
        if case != "ambient_fire":
            hurt = {"type": "hurt", "seq": 1, "source": fireball_source, "t": timestamp}
            if case == "unlisted_blaze":
                hurt["source_entity"] = blaze
            hurt_events.append(hurt)
        hurt_events.append({"type": "hurt", "seq": 2, "source": "on_fire", "t": time.time() * 1000})
        status.update(health=18 if case == "minor_hit" else 7.5, on_fire=True)
        if case == "new_life":
            status["last_death"] = {"seq": 3}
        if not in_flight_read:
            stopped.wait(1.5 if should_preempt else 0.75)
        result = adapter.call("status", method="GET")
        actions.append("continued")
        return json.dumps(result)

    result = json.loads(guard.run_tool_with_reflex("mc_skill_combat", invoke))
    if should_preempt:
        assert result["preempted_by_reflex"] is True
        assert result["action_released"] is True
        assert result["hurt_source"] == fireball_source
        assert actions == (["actor_read"] if in_flight_read else []) + ["stop", "retreat"]
    else:
        assert actions == ["actor_read", "continued"]


@pytest.mark.parametrize(("tool", "endpoint", "interrupted_error"), [
    ("mc_skill_mine_dfs", "dig", False), ("mc_skill_combat", "move_to", False),
    ("mc_move_to", "move_to", False), ("mc_move_to", "move_to", True),
    ("mc_skill_combat", "control", False), ("mc_skill_combat", "control", True),
])
def test_magma_interrupts_active_tool_before_the_boundary(tool, endpoint, interrupted_error):
    import threading

    stopped = threading.Event()
    status = {
        "pos": [0.5, 38.0, 0.5], "health": 19.5, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "the_nether", "sneaking": False,
    }
    actions = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                assert body["pos"] == [0, 37, 0]
                return {"name": "magma_block"}
            if action == endpoint:
                assert stopped.wait(3.0)
                if interrupted_error:
                    raise ApiError("interrupted", "stopped because of stop")
                return {"interrupted_by": "hurt"}
            if action == "stop":
                actions.append("stop")
                stopped.set()
            if action == "posture":
                actions.append("sneak")
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            return {"ok": True}

    bot = MagmaBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    body = {"stop_on": ["death"]} if tool == "mc_move_to" else {}
    if endpoint == "control":
        body = {"forward": True, "stop_on": ["death"]}
    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call(endpoint, body))))
    assert result["reflex_mode"] == "hot_floor"
    assert result["requires_replan"] is True
    assert status["sneaking"] is True
    assert actions == ["stop", "sneak"]

