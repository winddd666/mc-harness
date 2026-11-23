from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("receipt", ["confirmed", "unconfirmed", "partial_error"])
def test_partial_smelt_hands_off_same_furnace_output_to_window_tools(monkeypatch, receipt) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    furnace = [186840, 18, 184384]
    bag = {"raw_iron": 6, "coal": 1}
    loaded = output = 0
    writes = []

    def window():
        return {
            "window": "furnace", "target": "furnace",
            "sections": {
                "input": [{"name": "raw_iron", "count": loaded}] if loaded else [],
                "output": [{"name": "iron_ingot", "count": output}] if output else [],
            },
            "info": {"burning": loaded > 0},
        }

    def handler(request):
        nonlocal loaded, output
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": item, "count": n} for item, n in bag.items() if n]}
        elif route == "status":
            result = {"pos": [186839.5, 18.0, 184384.5], "dimension": "overworld", "health": 20, "food": 20}
        elif route == "find_blocks":
            result = {"blocks": [{"name": "furnace", "pos": furnace, "dist": 1}]}
        elif route == "view":
            assert body["target"] == {"pos": furnace}
            result = window()
        elif route == "act":
            assert body["target"] == {"pos": furnace}
            ops = body["ops"]
            writes.append(ops)
            if ops[0]["op"] == "put":
                assert ops == [
                    {"op": "put", "item": "raw_iron", "count": 6, "to": "input"},
                    {"op": "put", "item": "coal", "count": 1, "to": "fuel"},
                ]
                bag["raw_iron"] = bag["coal"] = 0
                loaded = 6
                result = {"done": [{"op": "put", "put": "raw_iron", "to": "input", "count": loaded}]}
            else:
                assert ops[0] == {"op": "take", "from": "output"} and output == 6
                done = []
                if receipt != "unconfirmed":
                    bag["iron_ingot"] = output
                    done.append({"op": "take", "from": "output", "took": {"iron_ingot": output}})
                    output = 0
                if receipt == "partial_error":
                    assert len(ops) == 2 and ops[1]["from"] == "invalid"
                    return httpx.Response(200, json={"ok": False, "error": {
                        "code": "bad_section", "done": done, "window": window(),
                    }})
                result = {"done": done, "window": window()}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    target = {"block": "furnace", "x": furnace[0], "y": furnace[1], "z": furnace[2]}
    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools._episode_deadline_mono = 14.0
        partial = json.loads(tools.mc_skill_smelt("raw_iron", count=6))
        assert partial["error"] == "partial_smelt" and partial["collected"] == []
        assert partial["furnace_pos"] == furnace and len(writes) == 1
        assert partial["next_actions"] == [
            {"name": "mc_window_view", "arguments": target},
            {"name": "mc_window_act", "arguments": {**target, "ops": [{"op": "take", "from": "output"}]}},
        ]
        bag["raw_iron"] = 1
        pending = json.loads(tools.mc_skill_smelt("raw_iron"))
        assert pending["error"] == "pending_smelt_batch" and pending["pending_count"] == 6
        assert pending["next_actions"] == partial["next_actions"] and len(writes) == 1

        loaded, output = 0, 6
        tools.world_memory.register_conversion_job(
            pos=furnace, input_item="raw_iron", output_item="iron_ingot", count=2,
            dim="the_nether", kind="sync_furnace",
        )
        inspect, take = partial["next_actions"]
        view = json.loads(getattr(tools, inspect["name"])(**inspect["arguments"]))
        assert view["result"]["sections"]["output"] == [{"name": "iron_ingot", "count": 6}]
        if receipt == "partial_error":
            take["arguments"]["ops"].append({"op": "take", "from": "invalid"})
        result = json.loads(getattr(tools, take["name"])(**take["arguments"]))

        assert len(writes) == 2
        assert result["ok"] is (receipt != "partial_error")
        confirmed = receipt != "unconfirmed"
        assert bag.get("iron_ingot", 0) == (6 if confirmed else 0)
        assert output == (0 if confirmed else 6)
        assert tools.world_memory.get_in_conversion_counts(dim="overworld") == ({} if confirmed else {"iron_ingot": 6})
        assert tools.world_memory.get_in_conversion_counts(dim="the_nether") == {"iron_ingot": 2}


@pytest.mark.parametrize("fuel_put_failed", [False, True])
def test_skill_smelt_recovers_tracked_output_before_mining(monkeypatch, fuel_put_failed) -> None:
    from mc_agent.harness.mc_tools import McToolset

    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    furnace = [1, 64, 0]
    phase = "smelt"
    takes = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "act":
            assert body["target"]["pos"] == furnace
            op = body["ops"][0]
            if op["op"] == "put":
                done = [{"op": "put", "to": "input", "put": "raw_iron", "count": 6}]
                if fuel_put_failed:
                    return httpx.Response(200, json={"ok": False, "error": {
                        "code": "section_full", "message": "fuel slot occupied", "done": done,
                    }})
            else:
                takes.append(phase)
                # The take receipt is authoritative when the preview is stale.
                done = [{"op": "take", "from": "output", "took": {"iron_ingot": 5 if phase == "smelt" else 1}}]
            result = {"done": done}
        elif route == "view":
            assert body["target"]["pos"] == furnace
            if phase == "mine":
                now[0] += 1.0
            result = {"sections": {
                "input": [{"name": "raw_iron", "count": 1}] if phase == "smelt" else [],
                "output": [{"name": "iron_ingot", "count": 6 if phase == "smelt" else 1}],
            }, "info": {"burning": phase == "smelt"}}
        else:
            result = {
                "inventory": {"items": [{"name": "raw_iron", "count": 6}, {"name": "coal", "count": 1}]},
                "find_blocks": {"blocks": [{"name": "furnace", "pos": furnace, "dist": 1}]},
                "status": {
                    "pos": [0.5, 64.0, 0.5], "health": 20, "food": 20,
                    "dimension": "the_nether" if phase == "other_dimension" else "overworld",
                },
            }[route]
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_smelt("raw_iron", count=6))
        jobs = toolset.world_memory.get_conversion_jobs()
        assert [(j["pos"], j["dim"], j["output_item"], j["remaining"]) for j in jobs] == [
            (furnace, "overworld", "iron_ingot", 6 if fuel_put_failed else 1),
        ]
        if fuel_put_failed:
            assert result["error"]["code"] == "section_full"
            assert result["error"]["furnace_pos"] == furnace
            assert takes == []
            return
        assert result["error"] == "partial_smelt"
        assert result["collected"] == [{"name": "iron_ingot", "count": 5}]

        def mine(**kwargs):
            assert takes == (["smelt", "mine"] if phase == "mine" else ["smelt"])
            assert kwargs["max_duration_s"] == (31.0 if phase == "mine" else 32.0)
            return {"ok": True}

        monkeypatch.setattr(toolset.skills, "mine_dfs", mine)
        phase = "other_dimension"
        assert json.loads(toolset.mc_skill_mine_dfs(target="diamond"))["ok"]
        phase = "mine"
        result = json.loads(toolset.mc_skill_mine_dfs(target="diamond"))
        assert result["furnace_recovered"] == {"pos": furnace, "took": {"iron_ingot": 1}}
        assert toolset.world_memory.get_conversion_jobs() == []


@pytest.mark.parametrize("receipt", ["empty", "absent", "no_took", "inventory_timeout", "take_timeout"])
def test_skill_smelt_keeps_4_plus_2_job_until_empty_take_receipt_is_verified(receipt) -> None:
    from mc_agent.harness.mc_tools import McToolset

    furnace = [-12268, 31, -6065]
    phase = "load"
    owned = 0
    fail_inventory = False
    operations = []

    def handler(request):
        nonlocal owned, fail_inventory
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            if fail_inventory:
                fail_inventory = False
                raise httpx.ReadTimeout("offline post-take inventory read", request=request)
            result = {"items": [
                {"name": "raw_iron", "count": 6}, {"name": "coal", "count": 1},
                {"name": "iron_ingot", "count": owned},
            ]}
        elif route == "status":
            result = {"pos": [-12267.5, 31.0, -6064.5], "health": 20, "food": 20, "dimension": "overworld"}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1.0}]}
        elif route == "view":
            count = {"load": 6, "waiting": 0, "empty_receipt": 2, "confirmed": max(0, 6 - owned)}[phase]
            result = {"sections": {"output": [{"name": "iron_ingot", "count": count}] if count else []}}
        elif route == "stop":
            result = {"released": True}
        elif route == "act":
            assert body["target"]["pos"] == furnace
            op = body["ops"][0]
            operations.append((phase, op["op"]))
            if phase == "load" and op["op"] == "put":
                result = {"done": [{"put": "raw_iron", "count": 6, "to": "input"}]}
            elif phase == "load":
                owned += 4
                result = {"done": [{"took": {"iron_ingot": 4}}]}
            else:
                if phase == "confirmed":
                    owned += 2
                    if receipt == "take_timeout":
                        raise httpx.ReadTimeout("offline lost take response", request=request)
                    fail_inventory = receipt == "inventory_timeout"
                result = {
                    "absent": {},
                    "no_took": {"done": [{"op": "take"}]},
                }.get(receipt, {"done": []})
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        first = json.loads(tools.mc_skill_smelt("raw_iron", count=6))
        assert first["collected"] == [{"name": "iron_ingot", "count": 4}]
        assert tools.world_memory.get_in_conversion_counts() == {"iron_ingot": 2}
        phase = "waiting"
        if receipt == "take_timeout":
            repeated = json.loads(tools.mc_skill_smelt("raw_iron", count=2))
            assert repeated["error"] == "pending_smelt_batch"
            assert repeated["furnace_pos"] == furnace and repeated["pending_count"] == 2
        assert tools.skills.collect_nearby_smelt_output(5.0) is None
        phase = "empty_receipt"
        assert tools.skills.collect_nearby_smelt_output(5.0) is None
        assert tools.world_memory.get_in_conversion_counts() == {"iron_ingot": 2}
        phase = "confirmed"
        if receipt in {"inventory_timeout", "take_timeout"}:
            assert tools.skills.collect_nearby_smelt_output(5.0) is None
            assert tools.world_memory.get_in_conversion_counts() == {"iron_ingot": 2}
        assert tools.skills.collect_nearby_smelt_output(5.0) == {"pos": furnace, "took": {"iron_ingot": 2}}
        assert tools.skills.collect_nearby_smelt_output(5.0) is None
        assert tools.world_memory.get_conversion_jobs() == []
        assert operations == [("load", "put"), ("load", "take"), ("empty_receipt", "take"), ("confirmed", "take")]


@pytest.mark.parametrize("code", ["no_window", "missing_item"])
def test_skill_smelt_tries_a_second_nearby_furnace_only_after_open_failure(code) -> None:
    from mc_agent.harness.mc_tools import McToolset

    near = [-5284, 42, -7713]
    alternate = [-5284, 43, -7713]
    far = [-5284, 49, -7713]
    actions = []

    def handler(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if operation == "act":
            actions.append(body)
            if body["target"]["pos"] == near:
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": code, "message": "furnace action failed"},
                })
        if operation == "view":
            assert body["target"]["pos"] == alternate
        responses = {
            "inventory": {"items": [{"name": "cobblestone", "count": 12}, {"name": "coal", "count": 2}]},
            "find_blocks": {"blocks": [
                {"pos": far, "dist": 9.1}, {"pos": alternate, "dist": 3.1}, {"pos": near, "dist": 2.2},
            ]},
            "status": {"pos": [-5283.5, 40, -7712.3], "health": 20, "food": 20},
            "act": {"done": [{"took": {"stone": 12}}]} if operation == "act" and body["ops"][0]["op"] == "take" else {},
            "view": {"sections": {"output": [{"name": "stone", "count": 12}]}},
        }
        return httpx.Response(200, json={"ok": True, "result": responses[operation]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_smelt("cobblestone", count=12, fuel_item="coal"))
        assert toolset.world_memory.get_conversion_jobs() == []

    if code == "no_window":
        assert [action["target"]["pos"] for action in actions] == [near, alternate, alternate]
        assert actions[0]["ops"] == actions[1]["ops"]
        assert actions[2]["ops"] == [{"op": "take", "from": "output", "count": 12}]
        assert result["ok"] is True
        assert result["furnace_pos"] == alternate
    else:
        assert [action["target"]["pos"] for action in actions] == [near]
        assert result["error"]["code"] == code


@pytest.mark.parametrize("site", ["new_empty", "new_burning", "new_lit", "new_with_fuel", "reused"])
def test_skill_smelt_only_reclaims_its_empty_unlit_underground_furnace(site) -> None:
    from mc_agent.harness.mc_tools import McToolset

    furnace = [1, 27, 0]
    present = site == "reused"
    burning = False
    fuel_left = False
    output = 0
    digs = []

    def handler(request):
        nonlocal present, burning, fuel_left, output
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [
                {"name": "raw_iron", "count": 2}, {"name": "coal", "count": 1},
                {"name": "furnace", "count": int(not present)}, {"name": "stone_pickaxe", "count": 1},
            ]}
        elif route == "status":
            result = {"pos": [0.5, 27.0, 0.5], "health": 20, "food": 20, "dimension": "overworld"}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1}]} if present else {"blocks": []}
        elif route == "place":
            present = True
            result = {"pos": furnace, "placed": "furnace"}
        elif route == "act":
            assert body["target"]["pos"] == furnace and present
            if body["ops"][0]["op"] == "put":
                output = 2
                result = {"done": [{"op": "put", "put": "raw_iron", "count": 2, "to": "input"}]}
            else:
                output = 0
                burning = site == "new_burning"
                fuel_left = site == "new_with_fuel"
                result = {"done": [{"op": "take", "took": {"iron_ingot": 2}}]}
        elif route == "view":
            assert body["target"]["pos"] == furnace and present
            result = {
                "window": "furnace", "info": {"burning": burning},
                "sections": {"input": [], "fuel": [{"name": "coal", "count": 1}] if fuel_left else [],
                             "output": [{"name": "iron_ingot", "count": output}] if output else []},
            }
        elif route == "get_block":
            result = {"name": "furnace" if present else "air", "properties": {"lit": site == "new_lit"}}
        elif route == "dig":
            assert present and not burning and not fuel_left and not output
            assert body["pos"] == furnace and body["collect"] is True
            digs.append(body["pos"])
            present = False
            result = {"dug": "furnace", "gained": {"furnace": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        result = json.loads(tools.mc_skill_smelt("raw_iron", count=2))
        assert result["ok"] is True
        if site == "new_empty":
            assert result["furnace_cleanup"] == {"pos": furnace, "removed": True, "recovered": 1}
            assert digs == [furnace]
        elif site == "new_burning":
            assert not digs
            burning = False
            recovered = tools.skills.collect_nearby_smelt_output(5.0)
            assert recovered["furnace_cleanup"] == {"pos": furnace, "removed": True, "recovered": 1}
            assert tools.skills.collect_nearby_smelt_output(5.0) is None
        else:
            assert not digs and present


def test_skill_smelt_stops_pending_furnace_view_after_timeout(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    calls = []

    def handler(request):
        nonlocal now
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "view":
            if calls.count(route) == 1:
                now += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("furnace view action still pending", request=request)
            return httpx.Response(200, json={
                "ok": False, "error": {"code": "busy", "message": "running window_view"},
            })
        responses = {
            "inventory": {"items": [{"name": "raw_iron", "count": 6}, {"name": "coal", "count": 1}]},
            "find_blocks": {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 1}]},
            "status": {"pos": [0.5, 64, 0.5], "health": 20, "food": 20},
            "act": {},
            "stop": {"released": False},
        }
        return httpx.Response(200, json={"ok": True, "result": responses[route]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_smelt("raw_iron", count=6, fuel_item="coal"))
        assert toolset.world_memory.get_in_conversion_counts() == {"iron_ingot": 6}

    assert now == pytest.approx(48.0)
    assert calls.count("act") == 1
    assert calls.count("view") == 1
    assert calls[-2:] == ["view", "stop"]
    assert result["error"] == "partial_smelt"
    assert result["fuel"] == {"item": "coal", "count": 1}
    assert result["collected"] == []


def test_smelt_collect_survives_read_timeout() -> None:
    """BUG-035: a transient ReadTimeout in world-job collection killed the run."""
    from mc_agent.harness.world_memory import WorldMemory

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5], "dimension": "overworld"}})
        if path == "move_to":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "view":
            raise httpx.ReadTimeout("offline furnace timeout", request=request)
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.world_memory = WorldMemory()
        result = skills.smelt_collect(furnace_pos=[1000, 64, 1000], wait_remaining_s=0)
    assert result["ok"] is False
    assert result["failures"]
    assert result["failures"][0]["error"] == "ReadTimeout"

