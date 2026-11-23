from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("reentry", ["stalled", "confirmed_landing", "new_life"])
def test_water_escape_changes_bank_after_dry_airborne_lip_attempt(monkeypatch, reentry):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [-5211.5, 24, -7783.5]
    alternate = [-5209.5, 23, -7784.5]
    below_bank = [-5211.5, 23.5, -7784.3]
    status = {
        "pos": [-5211.5, 23, -7784.3], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class CavePoolBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return self.status()
            if endpoint == "move_to":
                target = [body[key] for key in ("x", "y", "z")]
                attempts.append(target)
                if len(attempts) > 2:
                    status.update(pos=target, in_water=False, on_ground=True)
            if endpoint == "control" and body.get("forward") and len(attempts) == 1:
                status.update(pos=below_bank, in_water=False, on_ground=False)
            return {"ok": True}

    env = CombatEnv(CavePoolBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda _pos, *, excluded_shores, **_: (
        alternate if tuple(bank) in excluded_shores else bank
    ))

    first = env.escape_deep_water_to_shore()
    assert first["reason"] == "shore_landing_unconfirmed" and first["in_water"] is False
    assert attempts == [bank, bank]
    if reentry == "confirmed_landing":
        env.remember_dry_bank({**status, "pos": bank, "on_ground": True})
    if reentry == "new_life":
        status["last_death"] = {"t": 2}
    status.update(pos=[-5211.5, 23, -7784.3], in_water=True)

    second = env.escape_deep_water_to_shore()
    assert second["ok"] is True
    assert attempts[-1] == (alternate if reentry == "stalled" else bank)
    assert ("shore_lip_stalled" in second["actions"]) is (reentry == "stalled")


@pytest.mark.parametrize("approach", ["below_bank", "beside_bank"])
def test_w21_water_escape_distinguishes_reaching_the_bank_column_from_landing(approach):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [261034.5, 63.0, 253597.5]
    alternative = [261037.5, 63.0, 253597.5]
    floors = {(int(x), 62, int(z)) for x, _, z in (bank, alternative)}
    status = {
        "pos": [261034.5, 62.0, 253589.5], "in_water": True, "on_ground": False, "submerged": False,
        "last_death": {"t": 1}, "dimension": "overworld", "alive": True, "ready": True,
    }
    moves = []
    hops = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": floor} for floor in sorted(floors)]}
            if action == "get_block":
                return {"name": "grass_block" if tuple(body["pos"]) in floors else "air"}
            if action == "move_to":
                target = [body[key] for key in ("x", "y", "z")]
                moves.append(target)
                if target == alternative:
                    status.update(pos=alternative, in_water=False, on_ground=True)
                else:
                    x = bank[0] if approach == "below_bank" else bank[0] + 0.8
                    status.update(pos=[x, 62.0, bank[2]])
            if action == "control" and body.get("forward"):
                hops.append(body["ms"])
                if approach == "beside_bank":
                    status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(BankBot())
    first = env.escape_deep_water_to_shore()
    assert first["shore_target"] == bank
    if approach == "beside_bank":
        assert hops == [450]
        assert first["ok"] is True and first["pos_after"] == bank
    else:
        assert hops == []
        assert first["ok"] is False and first["in_water"] is True
        assert "shore_lip_directly_below" in first["actions"]
        second = env.escape_deep_water_to_shore()
        assert second["ok"] is True and second["pos_after"] == alternative
        assert moves == [bank, alternative]


@pytest.mark.parametrize("landing", ["grounded", "water", "respawned", "airborne"])
@pytest.mark.parametrize("early_exit", [False, True])
def test_directed_shore_rechecks_an_airborne_bot_over_the_bank(monkeypatch, landing, early_exit):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    bank = [0.5, 63.0, 14.5] if early_exit else [0.5, 63.0, 4.5]
    actual_landing = [0.5, 63.0, 4.5]
    status = {
        "pos": [0.5, 62.2, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    moves = []
    jump = []

    class BankBot:
        def status(self):
            if clock[0] >= 1000.15:
                if landing == "grounded":
                    status.update(in_water=False, on_ground=True, pos=actual_landing)
                elif landing == "water":
                    status.update(in_water=True, on_ground=False)
                elif landing == "respawned":
                    status.update(pos=[2.5, 70, 2.5], last_death={"t": 2})
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "move_to":
                moves.append(body)
                status.update(pos=[0.5, 63.5, 4.5], in_water=False, on_ground=False)
                if early_exit:
                    raise ApiError("timeout", "movement ended before reaching the requested bank")
            if action == "posture":
                jump.append(body.get("jump"))
            return {"ok": True}

    env = CombatEnv(BankBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank)
    outcome = env.escape_deep_water_to_shore()

    assert len(moves) == (1 if early_exit else 2)
    assert 1000.15 <= clock[0] <= 1000.31
    assert outcome["ok"] is (landing == "grounded")
    if landing == "water":
        assert outcome["in_water"] is True and jump[-1] is True
    elif landing == "respawned":
        assert outcome["mode"] == "respawned"
    elif landing == "airborne":
        assert outcome["reason"] == "shore_landing_unconfirmed"


@pytest.mark.parametrize("landing", ["grounded", "water", "respawned", "still_airborne"])
def test_shore_recovery_confirms_landing_when_the_generic_escape_already_left_water(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    bank = [0.5, 63, 0.5]
    status = {
        "pos": [0.5, 63.5, 0.5], "dimension": "overworld", "ready": True, "alive": True,
        "in_water": False, "on_ground": False, "last_death": {"t": 1},
    }
    moves = []

    class ShoreBot:
        def status(self):
            if clock[0] >= 1000.15 and not moves:
                if landing == "grounded":
                    status.update(pos=bank, on_ground=True)
                elif landing == "water":
                    status.update(pos=[0.5, 62, -1.0], in_water=True)
                elif landing == "respawned":
                    status.update(pos=[1.5, 65, 0.5], last_death={"t": 2}, on_ground=True)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "move_to":
                moves.append([body[key] for key in ("x", "y", "z")])
                status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank)

    outcome = env.escape_deep_water_to_shore()

    assert 1000.15 <= clock[0] <= 1000.31
    assert outcome["ok"] is (landing in {"grounded", "water"})
    assert moves == ([bank] if landing == "water" else [])
    if landing == "respawned":
        assert outcome["mode"] == "respawned"
    elif landing == "still_airborne":
        assert outcome["mode"] == "shore_landing_unconfirmed"

