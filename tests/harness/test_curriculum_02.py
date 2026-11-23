from tests.harness._common import *  # noqa: F401,F403

def test_curriculum_death_drop_recovery_and_dusk_surface_filter() -> None:
    """Daytime safe death drop triggers micro recover_death_drop; dusk/night skips daytime surface chores."""
    from mc_agent.harness.curriculum import select_next_target
    from mc_agent.harness.world_memory import WorldMemory

    state = {"tod": 2000, "is_day": True, "inv": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10.5, 68.0, 10.5],
                        "dimension": "minecraft:overworld",
                        "time_of_day": state["tod"],
                        "is_day": state["is_day"],
                        "in_water": False,
                        "alive": True,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["inv"])}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "advancements": [
                            {"id": "minecraft:story/root", "status": "completed"},
                            {"id": "minecraft:story/mine_stone", "status": "completed"},
                            {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                            {"id": "minecraft:story/smelt_iron", "status": "completed"},
                            {"id": "minecraft:story/iron_tools", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:husbandry/breed_an_animal", "status": "not_started"},
                            {"id": "minecraft:husbandry/plant_seed", "status": "not_started"},
                            {"id": "minecraft:story/shiny_gear", "status": "not_started"},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    wm = WorldMemory()
    wm.last_death = {"dim": "overworld", "pos": [30, 66, 20], "t": 100.0}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_c:
        # 1) Daytime + recent (<260s) + close + high-value run -> recover_death_drop (short budget)
        t1, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1.key == "recover_death_drop"
        assert t1.budget_class == "short"

        # 1b) BUG-149: Deep diamond-layer death (y=-54) with naked surface respawn (y=64, gap>=18, no carried pickaxe)
        # must NOT waste an episode on recover_death_drop even if a nearby cached chest holds a stone_pickaxe
        wm.last_death = {"dim": "overworld", "pos": [100, -54, 40], "t": 100.0}
        t1b_empty, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1b_empty.key == "bootstrap_stone_age"
        wm.containers[("overworld", 11, 68, 11)] = {"items": {"stone_pickaxe": 1}}
        t1b, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1b.key != "recover_death_drop"
        wm.containers.clear()

        # 1c) Deep death when bot already holds a stone_pickaxe still allows recover_death_drop
        state["inv"] = [{"name": "stone_pickaxe", "count": 1}]
        t1c, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1c.key == "recover_death_drop"
        assert t1c.budget_class == "short"
        state["inv"] = []

        # 2) Nighttime surface death -> must NOT run naked into night surface to recover corpse
        wm.last_death = {"dim": "overworld", "pos": [30, 66, 20], "t": 100.0}
        state["tod"] = 12000
        state["is_day"] = False
        t2, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t2.key == "bootstrap_stone_age"

        # 3) Dusk/night with iron_pickaxe -> skips daytime surface chores (breed_an_animal/plant_seed)
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "shield", "count": 1}]
        t3, _ = select_next_target(
            bot_c,
            world_memory=wm,
            now=150.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t3.key not in {"minecraft:husbandry/breed_an_animal", "minecraft:husbandry/plant_seed"}


def test_bug118_125_131_curriculum_dimension_bare_kill_and_night_prep() -> None:
    """Regression for BUG-118 (day1_night_iron_prep), BUG-125 (overworld nether/*), BUG-131 (naked kill_a_mob)."""
    state: dict[str, Any] = {
        "dim": "overworld",
        "pos": [0.5, 64.0, 0.5],
        "is_day": False,
        "tod": 14000,
        "armor_points": 0,
        "inv": [{"name": "diamond_pickaxe", "count": 1}, {"name": "shield", "count": 1}],
        "completed": [
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/iron_tools",
            "minecraft:story/obtain_armor",
            "minecraft:story/mine_diamond",
            "minecraft:story/shiny_gear",
            "minecraft:story/form_obsidian",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:story/deflect_arrow",
            "minecraft:adventure/kill_a_mob",
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": state["dim"], "pos": state["pos"],
                "health": 20.0, "food": 20, "is_day": state["is_day"], "time_of_day": state["tod"],
                "armor_points": state["armor_points"],
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": state["inv"]}})
        if path == "/advancements":
            advs = [{"id": a, "status": "completed"} for a in state["completed"]]
            advs.extend([
                {"id": "minecraft:nether/root", "status": "completed"},
                {"id": "minecraft:adventure/root", "status": "completed"},
                {"id": "minecraft:nether/find_fortress", "status": "not_started"},
                {"id": "minecraft:nether/obtain_ancient_debris", "status": "not_started"},
                {"id": "minecraft:adventure/kill_a_mob", "status": "not_started"},
            ])
            return httpx.Response(200, json={"ok": True, "result": {"ready": True, "advancements": advs}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="CurP0", transport=httpx.MockTransport(handler)) as bot_c:
        # BUG-125: in overworld, nether/find_fortress must NOT be selected even if enter_the_nether is completed
        t_overworld, _ = select_next_target(bot_c, enable_island_recon=False, recon_done=True)
        assert not t_overworld.key.startswith("minecraft:nether/"), f"unexpected nether target in overworld: {t_overworld.key}"

        # In the_nether, nether/find_fortress IS eligible and prioritized
        state["dim"] = "the_nether"
        t_nether, _ = select_next_target(bot_c, enable_island_recon=False, recon_done=True)
        assert t_nether.key == "minecraft:nether/find_fortress"

        # BUG-131: naked bare without iron_pickaxe/shield/armor must NOT be assigned kill_a_mob when iron is cooled
        state["dim"] = "overworld"
        state["inv"] = [{"name": "stone_pickaxe", "count": 1}]
        state["completed"] = ["minecraft:story/mine_stone", "minecraft:story/upgrade_tools"]
        t_bare, _ = select_next_target(
            bot_c,
            cooldowns={"bootstrap_iron_age": 9999.0},
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_bare.key != "minecraft:adventure/kill_a_mob"

        # BUG-118: day1_night_iron_prep must NOT tell bot to chop 12 logs on night surface or force fuel_item='coal'
        from mc_agent.harness.world_memory import WorldMemory
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "raw_iron", "count": 3}]
        state["completed"] = ["minecraft:story/mine_stone", "minecraft:story/upgrade_tools", "minecraft:story/smelt_iron", "minecraft:story/iron_tools"]
        t_n1, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=True,
            recon_done=True,
        )
        assert t_n1.key == "day1_night_iron_prep"
        assert "12 根原木" not in t_n1.task
        assert "严禁调 mc_skill_staircase(up)" in t_n1.task

        # BUG-139: nighttime without bed, >=3 wool, or known bed/village must NOT dispatch adventure/sleep_in_bed
        state["dim"] = "overworld"
        state["is_day"] = False
        state["tod"] = 14000
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "white_wool", "count": 2}]
        state["completed"] = [
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/iron_tools",
            "minecraft:story/obtain_armor",
            "minecraft:story/mine_diamond",
            "minecraft:story/shiny_gear",
            "minecraft:story/form_obsidian",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:story/deflect_arrow",
            "minecraft:adventure/kill_a_mob",
        ]
        t_no_bed, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_no_bed.key != "minecraft:adventure/sleep_in_bed"

        # With >=3 wool at night, sleep_in_bed IS allowed
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "white_wool", "count": 3}]
        t_has_wool, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_has_wool.key == "minecraft:adventure/sleep_in_bed"


def test_bug050_110_134_curriculum_suspended_dim_final_adv_retry_and_lava_paradox(tmp_path: Path) -> None:
    """Regression for BUG-050 (suspended nether target dropped in overworld), BUG-110 (final adv retry + confirmed fallback),
    and BUG-134 (enter_the_nether bans mine_dfs lava search and surfaces known lava POIs)."""
    from mc_agent.harness.curriculum import CURRICULUM_PLAYBOOKS, CurriculumSupervisor, SuspendedEpisode, _enrich_target_with_world_memory
    from mc_agent.harness.world_memory import WorldMemory

    # BUG-134: playbook and memory enrichment ban mine_dfs lava search & inject known lava POI
    wm = WorldMemory()
    wm.record_poi("lava", [120, 62, -40], dim="overworld")
    pb_en, boosted = _enrich_target_with_world_memory(
        CURRICULUM_PLAYBOOKS["minecraft:story/enter_the_nether"],
        world_memory=wm,
        bag_counts={"water_bucket": 1},
        chest_counts={},
        origin=[100.0, 64.0, -30.0],
        dim="overworld",
    )
    assert boosted is True
    assert "严禁用 mc_skill_mine_dfs 找岩浆池" in pb_en.task
    assert "记忆库岩浆" in pb_en.task

    # BUG-050 + BUG-110: suspended nether/find_fortress is discarded in overworld; final adv error preserves breakdown
    adv_calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20, "is_day": True, "time_of_day": 1000,
                "on_ground": True,
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1}]}})
        if path == "/advancements":
            adv_calls["n"] += 1
            if adv_calls["n"] >= 3:
                return httpx.Response(503, json={"ok": False, "error": {"code": "not_ready", "message": "portal transition"}})
            return httpx.Response(200, json={"ok": True, "result": {"ready": True, "advancements": [
                {"id": "minecraft:story/mine_stone", "status": "completed"},
                {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                {"id": "minecraft:story/smelt_iron", "status": "completed"},
                {"id": "minecraft:story/iron_tools", "status": "completed"},
                {"id": "minecraft:story/enter_the_nether", "status": "completed"},
                {"id": "minecraft:nether/root", "status": "completed"},
                {"id": "minecraft:nether/find_fortress", "status": "not_started"},
            ]}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="SupBug", transport=httpx.MockTransport(handler)) as bot_s:
        sup = CurriculumSupervisor(
            bot_s,
            chat_fn=lambda msgs, tools=None, **kw: {"role": "assistant", "content": "done"},
            runs_dir=tmp_path,
            duration_s=30.0,
            enable_pre_ensure=False,
            enable_island_recon=False,
        )
        # Seed a stale suspended Nether fortress target while bot is actually in overworld
        sup.suspended_episode = SuspendedEpisode(
            target=CURRICULUM_PLAYBOOKS["minecraft:nether/find_fortress"],
            remaining_calls=10,
            remaining_timeout_s=120.0,
            anchor_pos=[0.0, 64.0, 0.0],
            reason="preempted",
            progress_digest="",
        )
        summary = sup.run(max_episodes=1)
        assert summary["stop_reason"] == "episode_cap"
        assert summary["episodes"][0]["target"] != "minecraft:nether/find_fortress"
        # BUG-110: final query failed after retries -> preserved last confirmed breakdown and marked unconfirmed
        assert summary["final_advancements_confirmed"] is False
        assert summary["final_advancements_error"] is not None
        assert summary["final_score"] >= 50
        assert summary["score_breakdown"].get("minecraft:story/enter_the_nether") == 30


def test_bug086_ssl_rotation_preserves_cancel_and_bug087_transport_error_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for BUG-086 (ssl.SSLError during client rotation on cancel preserves RequestCancelled)
    and BUG-087 (httpx.ReadError during between-episode status poll is caught for bounded recovery)."""
    import ssl
    import mc_agent.harness.model as model_mod
    from mc_agent.harness.control import RequestCancelled
    from mc_agent.harness.curriculum import CurriculumSupervisor

    # 1) BUG-086: _abort_and_rotate_client swallows SSLError, keeps client alive, records error & cooldown
    class _DummyClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dummy = _DummyClient()
    monkeypatch.setattr(model_mod, "resolve_qwen_endpoint", lambda spec: type("R", (), {
        "base_url": "https://example.invalid/v1", "model": "m", "rpm_budget": 100,
        "api_key": "k", "endpoint": "https://example.invalid/v1/chat/completions", "extra_headers": (),
    })())
    created = {"n": 0}

    def fake_make_client(spec, base_url):
        created["n"] += 1
        if created["n"] == 1:
            return dummy
        raise ssl.SSLError("[X509] PEM lib")

    monkeypatch.setattr(model_mod, "_make_http_client", fake_make_client)
    qm = model_mod.QwenModel(model_mod.QwenSpec(post_cancel_cooldown_s=1.5))
    qm._abort_and_rotate_client()
    assert qm._current_client() is dummy
    assert dummy.closed is False
    assert qm._last_rotation_error is not None and "SSLError" in qm._last_rotation_error
    assert qm._cancel_cooldown_until > 0.0

    # Also verify _process_request preserves RequestCancelled even if on_cancel raises
    with pytest.raises(RequestCancelled):
        model_mod._process_request(
            model_mod.QwenSpec(),
            [{"role": "user", "content": "hi"}],
            None,
            False,
            on_event=None,
            control=lambda: True,
            on_cancel=lambda: (_ for _ in ()).throw(ssl.SSLError("boom")),
        )

    # 2) BUG-087: transient httpx.ReadError during between-episode status() recovers and completes episode
    status_calls = {"n": 0}
    monkeypatch.setattr("mc_agent.harness.curriculum.time.sleep", lambda _s: None)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            status_calls["n"] += 1
            if status_calls["n"] == 1:
                raise httpx.ReadError("[Errno 104] Connection reset by peer")
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20, "is_day": True, "time_of_day": 1000,
                "on_ground": True,
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"ready": True, "advancements": []}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bug087", transport=httpx.MockTransport(handler)) as bot_t:
        sup = CurriculumSupervisor(
            bot_t,
            chat_fn=lambda msgs, tools=None, **kw: {"role": "assistant", "content": "done"},
            runs_dir=tmp_path,
            duration_s=30.0,
            enable_pre_ensure=False,
            enable_island_recon=False,
        )
        summary = sup.run(max_episodes=1)
        assert summary["episodes_completed"] == 1
        assert summary["stop_reason"] == "episode_cap"


def test_bug042_150_cross_process_slot_gate_and_bug088_lodestone_threshold(tmp_path: Path) -> None:
    """Regression for BUG-042/150 (cross-process flock concurrency slot gate honors limit & cancel)
    and BUG-088 (lodestone task stops smelting at >=12 stone and reports exact deficit below 12)."""
    import threading
    import mc_agent.harness.model as model_mod
    from mc_agent.harness.control import RequestCancelled
    from mc_agent.harness.curriculum import CURRICULUM_PLAYBOOKS, _enrich_target_with_world_memory
    from mc_agent.harness.world_memory import WorldMemory

    # 1) BUG-042/150: hy4 defaults to 2 slots; 3rd concurrent waiter blocks until release or cancel
    res_ep = model_mod.ResolvedQwenEndpoint(
        endpoint="https://hy4.invalid/v1/chat/completions",
        base_url="https://hy4.invalid/v1",
        model="hy4",
        api_key="k",
    )
    grp, limit = model_mod._resolve_cross_process_concurrency(model_mod.QwenSpec(provider="hy4"), res_ep)
    assert limit == 2
    grp_nju, limit_nju = model_mod._resolve_cross_process_concurrency(model_mod.QwenSpec(provider="nju"), res_ep)
    assert limit_nju == 2
    assert grp_nju == grp
    l1 = model_mod._CrossProcessSlotLease(grp, 2, lock_dir=tmp_path)
    l2 = model_mod._CrossProcessSlotLease(grp, 2, lock_dir=tmp_path)
    l3 = model_mod._CrossProcessSlotLease(grp, 2, lock_dir=tmp_path)
    try:
        assert l1.acquire() >= 0.0
        assert l2.acquire() >= 0.0
        # 3rd lease cancels immediately when cancel_event is set
        ev = threading.Event()
        ev.set()
        with pytest.raises(RequestCancelled):
            l3.acquire(cancel_event=ev)
        # Releasing l1 lets l3 acquire cleanly
        l1.release()
        assert l3.acquire() >= 0.0
    finally:
        l1.release()
        l2.release()
        l3.release()

    # 2) BUG-088: lodestone task injects stop-smelt banner at >=12 stone and exact deficit at <12
    wm = WorldMemory()
    pb_full, boosted_full = _enrich_target_with_world_memory(
        CURRICULUM_PLAYBOOKS["minecraft:adventure/use_lodestone"],
        world_memory=wm,
        bag_counts={"stone": 14},
        chest_counts={},
        origin=[0.0, 64.0, 0.0],
        dim="overworld",
    )
    assert boosted_full is True
    assert "严禁再调 mc_skill_smelt 烧圆石" in pb_full.task

    pb_part, _ = _enrich_target_with_world_memory(
        CURRICULUM_PLAYBOOKS["minecraft:adventure/use_lodestone"],
        world_memory=wm,
        bag_counts={"stone": 9},
        chest_counts={},
        origin=[0.0, 64.0, 0.0],
        dim="overworld",
    )
    assert "仅缺 3 个" in pb_part.task


def test_bug030_051_084_102_shiny_gear_duration_underground_village_gate_and_recon_completion() -> None:
    """Regression for BUG-030 (shiny_gear post_ensure_task specifies max_duration_s=90),
    BUG-051 (day2_village_search blocked underground at Y<58), and BUG-084/102."""
    from mc_agent.harness.curriculum import CURRICULUM_PLAYBOOKS, select_next_target
    from mc_agent.harness.world_memory import WorldMemory

    # BUG-030: shiny_gear post_ensure_task & mine_diamond task specify max_duration_s=90
    assert "max_duration_s=90" in (CURRICULUM_PLAYBOOKS["minecraft:story/shiny_gear"].post_ensure_task or "")
    assert "max_duration_s=90" in CURRICULUM_PLAYBOOKS["minecraft:story/mine_diamond"].task

    # BUG-051: bot underground at Y=10 after shiny_gear must NOT be dispatched day2_village_search
    state = {"pos": [0.5, 10.0, 0.5]}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": list(state["pos"]), "health": 20.0, "food": 20, "is_day": True, "time_of_day": 2000,
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [
                {"name": "iron_pickaxe", "count": 1}, {"name": "diamond_boots", "count": 1},
            ]}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"ready": True, "advancements": [
                {"id": "minecraft:story/mine_stone", "status": "completed"},
                {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                {"id": "minecraft:story/smelt_iron", "status": "completed"},
                {"id": "minecraft:story/iron_tools", "status": "completed"},
                {"id": "minecraft:story/shiny_gear", "status": "completed"},
            ]}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bug051", transport=httpx.MockTransport(handler)) as bot_u:
        t_ug, _ = select_next_target(bot_u, world_memory=WorldMemory(), enable_island_recon=True, recon_done=True)
        assert t_ug.key != "day2_village_search"
        state["pos"] = [0.5, 65.0, 0.5]
        t_surf, _ = select_next_target(bot_u, world_memory=WorldMemory(), enable_island_recon=True, recon_done=True)
        assert t_surf.key == "day2_village_search"

