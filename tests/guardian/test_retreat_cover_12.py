from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("melee", ["far", "near", "unknown_distance", "scan_failed"])
def test_critical_arrow_reaction_checks_live_melee_clearance_before_cover(monkeypatch, melee):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    skeleton = {"id": 42, "name": "skeleton", "dist": 4.5, "pos": [5, 106, 0.5]}
    zombie = {"id": 43, "name": "zombie"}
    if melee != "unknown_distance":
        zombie["dist"] = 8.0 if melee == "far" else 4.8
    status = {"pos": [0.5, 106, 0.5], "dimension": "overworld", "health": 4.048, "food": 17,
              "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": skeleton}
    moves = []

    class HillBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": skeleton}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                if melee == "scan_failed":
                    raise httpx.ReadTimeout("hostile scan unavailable")
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [skeleton, zombie]}
            return {"ok": True}

    def cover(_env, shooter, *, other_shooters, allow_stationary):
        assert not other_shooters and shooter == skeleton["pos"] and not allow_stationary
        moves.append("natural_cover")
        return {"ok": True, "mode": "natural_cover"}

    def flee(_pos, **_kwargs):
        moves.append("flee")
        return {"ok": True, "mode": "flee", "moved_dist": 0.95}

    guard = GenerationGuard(HillBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_melee_hurt_at = 999.0
    guard._last_dig_cover_at = 990.0
    guard._last_dig_cover_block = (0, 106, 0)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    assert guard.poll()["requires_replan"]

    assert moves == (["natural_cover"] if melee == "far" else ["flee"])


@pytest.mark.parametrize("hits, cover_found", [(1, False), (2, True), (2, False)])
def test_repeated_arrow_hits_through_a_shield_prompt_cover_before_critical_health(monkeypatch, hits, cover_found):
    import time

    from mc_agent.skills.combat_env import CombatEnv

    near = {"name": "pillager", "id": 41, "dist": 5.8, "pos": [6.3, 63, 0.5]}
    far = {"name": "pillager", "id": 42, "dist": 7.3, "pos": [0.5, 63, -6.8]}
    status = {
        "pos": [0.5, 63, 0.5], "dimension": "overworld", "health": 12.0, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "blocking": True, "nearest_hostile": near,
    }
    cover_checks = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [
                    {"seq": seq, "type": "hurt", "source": "arrow", "source_entity": near if seq == 1 else far}
                    for seq in range(1, hits + 1) if seq > since
                ]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [near, far]}
            raise AssertionError(action)

    def cover(_env, pos, *, other_shooters, allow_stationary):
        cover_checks.append((pos, other_shooters, allow_stationary))
        if not cover_found:
            return None
        status["pos"] = [-1.7, 63, 0.5]
        return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_args: {"ok": False, "reason": "no_solid_wall"})
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", lambda *_args, **_kwargs: {"ok": False, "mode": "flee", "moved_dist": 0.0})
    if hits == 2:
        guard.last_reaction = time.monotonic()
    incident = guard.poll()

    assert cover_checks == ([(far["pos"], [near["pos"]], False)] if hits == 2 else [])
    assert incident["requires_replan"] is cover_found

