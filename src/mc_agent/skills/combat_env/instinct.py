"""SubcorticalInstinctController & CombatControllerSlot (split from combat_env.py)."""

from __future__ import annotations

from mc_agent.skills.combat_env._base import (
    math,
    time,
    Any,
    Callable,
    Collection,
    Literal,
    Protocol,
    Sequence,
    TypedDict,
    httpx,
    ApiError,
    SAFE_FOOD_PRIORITY,
    auto_equip_survival_gear,
    water_oxygen_below,
    AIR_LIKE_BLOCKS,
    BREATHABLE_AIR_BLOCKS,
    WATER_BLOCKS,
    PASSABLE_TRANSPARENT_BLOCKS,
    HAZARD_BLOCK_TYPES,
    SURFACE_SHORE_PRIORITY_BLOCKS,
    SHORE_LAND_BLOCKS,
    NETHER_SHORE_BLOCKS,
    LAVA_SHORE_BLOCKS,
    TRAP_BLOCKS,
    SAFE_TACTICAL_FILLERS,
    MELEE_WEAPON_ORDER,
    LONE_COUNTERKILL_MOBS,
    CORNERED_PIGLIN_MAX_HEALTH,
    CORNERED_PIGLIN_REACH,
    PRE_HURT_MELEE_MOBS,
    RANGED_MOBS,
    WARDEN_EVADE_DISTANCE,
    CARDINAL_VECTORS,
    attack_feedback_hits,
    shot_release_confirmed,
    _clean_block_name,
    _is_passable,
    _is_los_transparent,
    _is_full_cover_block,
    _is_lava_block,
    _water_requires_surface,
    _combat_status_unavailable,
    _hitbox_columns,
    footing_blocks,
    _is_control_signal,
    retreat_made_progress,
    ranged_retreat_positions,
    melee_clear_for_projectile_cover,
    _is_solid_ground,
    aim_yaw_pitch,
    yaw_to_unit_xz,
    ThreatTrigger,
    CombatOutcome,
    CombatController,
    _TerrainProbeExpired,
    _CombatFortificationInterrupted,
)


SUBCORTICAL_COMBAT_BUDGET_S = 7.5


class SubcorticalInstinctController:
    """Built-in 0-LLM aggressive instinct controller prior to teammate's full `combat.py` plug-in.

    Implements the `/grill-me` locked-in rules:
    1. Creeper `<= 4.5m` (or `pre_hurt_creeper`): equip/latch shield facing creeper and retreat `> 6.0m`
       along `scan_safe_yaws` (or `full_seal` if inside a `1x2` shaft).
    2. Lone melee mob (`zombie`/`spider`/etc., `hostile_count <= 2`) + `HP > 10` + sword/axe available:
       equip best melee weapon and aggressively counter-kill (`0-LLM` loop until dead or low HP).
    3. Ranged shooter (`skeleton`/etc.), swarm (`>= 3`), or low HP (`<= 10`):
       - Inside `carved_shaft` with blocks -> `full_seal` (2 blocks break LOS in 0.5s);
       - Close isolated skeleton (`<= 9m`) on open ground with shield + melee weapon + `HP >= 14` -> shield-close & kill;
       - Otherwise -> `cover_pocket`, night/low-HP `dig_safety_bunker`, or `flee` along safe yaws.
    4. Post-engagement stabilization: if safe and `HP < 16` or `food <= 15`, eat 1 safe food item.
    """

    def __init__(self) -> None:
        self._recent_flee_streak = 0
        self._last_flee_at = float("-inf")

    @staticmethod
    def _seal_action(seal: dict[str, Any], *, action: str = "seal_shaft") -> dict[str, Any]:
        fields = ("reason", "direction", "placed", "placement_attempts", "seal_cells", "repositioned_for_seal")
        return {"action": action, "ok": bool(seal.get("ok")), **{
            key: seal[key] for key in fields if seal.get(key) is not None
        }}

    @staticmethod
    def _entity_dist(bot_pos: Sequence[float] | None, ent: dict[str, Any]) -> float:
        epos = ent.get("pos")
        if isinstance(bot_pos, (list, tuple)) and len(bot_pos) >= 3 and isinstance(epos, (list, tuple)) and len(epos) >= 3:
            try:
                return math.sqrt(sum((float(bot_pos[i]) - float(epos[i])) ** 2 for i in range(3)))
            except (TypeError, ValueError):
                pass
        try:
            return float(ent.get("dist", 99.0))
        except (TypeError, ValueError):
            return 99.0

    @staticmethod
    def _combat_life_lost(
        before: dict[str, Any], after: dict[str, Any], recovery: dict[str, Any] | None = None,
    ) -> bool:
        return bool(
            (recovery and (recovery.get("respawned") or recovery.get("mode") in {"respawned", "not_ready"}))
            or _combat_status_unavailable(after)
            or any(after.get(key) != before.get(key) for key in ("last_death", "dimension"))
        )

    @staticmethod
    def _is_falling_fast(status: dict[str, Any], origin: Sequence[float]) -> bool:
        if status.get("on_ground") is not False or status.get("in_water") or status.get("in_lava"):
            return False
        velocity = status.get("velocity")
        if (
            isinstance(velocity, (list, tuple)) and len(velocity) >= 2
            and isinstance(velocity[1], (int, float)) and velocity[1] <= -10.0
        ):
            return True
        pos = status.get("pos")
        return (
            isinstance(pos, (list, tuple)) and len(pos) >= 3
            and isinstance(pos[1], (int, float)) and pos[1] <= origin[1] - 2.5
        )

    def _find_live_hostiles(
        self, env: CombatEnv, radius: int = 16, *, timeout_s: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._scan_live_hostiles(env, radius, timeout_s=timeout_s) or []

    def _scan_live_hostiles(
        self, env: CombatEnv, radius: int, *, timeout_s: float | None = None,
    ) -> list[dict[str, Any]] | None:
        try:
            transport = {"timeout_s": timeout_s, "allow_reauth": False} if timeout_s is not None else {}
            res = env.bot.call("get_entities", {"radius": radius, "types": ["hostile"]}, **transport)
            entities = res.get("entities") if isinstance(res, dict) else None
            if not isinstance(entities, list):
                return None
            rows = [e for e in entities if isinstance(e, dict)]
            rows.sort(key=lambda e: float(e.get("dist", 999.0)))
            return rows
        except Exception as error:
            if _is_control_signal(error):
                raise
            return None

    @staticmethod
    def _other_creeper_positions(
        hostiles: Sequence[dict[str, Any]], target_id: int | None, *, include_occluded: bool = False,
    ) -> list[Sequence[float]]:
        positions = []
        for hostile in hostiles:
            point = hostile.get("pos")
            if (
                hostile.get("id") != target_id and _clean_block_name(hostile.get("name")) == "creeper"
                and (include_occluded or not hostile.get("blocked_by_wall"))
                and isinstance(point, (list, tuple)) and len(point) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
            ):
                positions.append(point)
        return positions

    def _creeper_within_clearance(
        self, point: Sequence[float] | None, threat: dict[str, Any], released_creeper_id: int | None,
    ) -> bool:
        if _clean_block_name(threat.get("name")) != "creeper":
            return False
        clearance = 6.5 if released_creeper_id is not None and threat.get("id") == released_creeper_id else 5.5
        return self._entity_dist(point, threat) <= clearance

    def _verify_creeper_followup(
        self, env: CombatEnv, before: dict[str, Any], released_creeper_id: int | None, *, extended_clearance: bool,
    ) -> dict[str, Any]:
        unconfirmed = {"ok": False, "reason": "blast_clearance_unconfirmed"}
        after = env._safe_status({"ready": False})
        if after.get("alive") is False or after.get("last_death") != before.get("last_death"):
            return {"ok": False, "respawned": True}
        if after.get("ready") is False or after.get("dimension") != before.get("dimension"):
            return unconfirmed
        scan = self._scan_live_hostiles(env, radius=8, timeout_s=0.6)
        if extended_clearance:
            settled = env._safe_status({"ready": False}, timeout_s=0.25)
            if settled.get("alive") is False or (settled.get("pos") and any(
                settled.get(key) != before.get(key) for key in ("dimension", "last_death")
            )):
                return {"ok": False, "respawned": True}
            if not env._on_same_footing(after, settled):
                return unconfirmed
            after = settled
        threats = list(scan or [])
        nearest = after.get("nearest_hostile")
        if isinstance(nearest, dict):
            threats.append(nearest)
        distance = min((
            self._entity_dist(after.get("pos"), threat) for threat in threats
            if self._creeper_within_clearance(after.get("pos"), threat, released_creeper_id)
        ), default=math.inf)
        if math.isfinite(distance):
            return {"ok": False, "reason": "creeper_still_close", "creeper_distance": distance}
        return unconfirmed if scan is None else {"ok": True}

    def _place_creeper_cover(
        self, env: CombatEnv, status: dict[str, Any], creeper_id: int, threat: Sequence[float],
        counts: dict[str, int], *, deadline: float,
    ) -> dict[str, Any]:
        unconfirmed = {"ok": False, "reason": "blast_cover_unconfirmed"}
        try:
            cover = env._place_stationary_cover(status, threat, counts, creeper_id=creeper_id, deadline=deadline)
        except _TerrainProbeExpired:
            return unconfirmed
        if not cover.get("ok"):
            return cover
        hostiles = self._scan_live_hostiles(env, radius=8, timeout_s=0.3)
        settled = env._safe_status({"ready": False}, timeout_s=0.25)
        current = hostiles[0] if hostiles and len(hostiles) == 1 else {}
        current_pos = current.get("pos")
        if (
            not env._on_same_footing(status, settled) or current.get("id") != creeper_id
            or _clean_block_name(current.get("name")) != "creeper" or current.get("alive") is False
            or not isinstance(current_pos, (list, tuple)) or len(current_pos) < 3
            or not all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in current_pos[:3])
        ):
            return {**cover, **unconfirmed}
        env.invalidate_block_cache()
        try:
            confirmed = env._creeper_cover_blocks_exposure(settled["pos"], current_pos, cover["cover_cells"], deadline=deadline)
        except _TerrainProbeExpired:
            confirmed = False
        return cover if confirmed else {**cover, **unconfirmed}

    @staticmethod
    def _other_melee_positions(hostiles: Sequence[dict[str, Any]], target_id: int | None) -> list[Sequence[float]]:
        positions = []
        for hostile in hostiles:
            pos = hostile.get("pos")
            if (
                hostile.get("id") != target_id and _clean_block_name(hostile.get("name")) in PRE_HURT_MELEE_MOBS
                and not hostile.get("blocked_by_wall") and isinstance(pos, (list, tuple)) and len(pos) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            ):
                positions.append(pos)
        return positions

    def ranked_melee_retreat(
        self, env: CombatEnv, status: dict[str, Any], target_id: int, *,
        critical: bool, verify_melee_clearance: bool = False, live_hostiles: list[dict[str, Any]] | None = None,
        melee_clearance: float = 3.5,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        hostiles = self._find_live_hostiles(env, radius=12, timeout_s=0.35) if live_hostiles is None else live_hostiles
        target = next((entity for entity in hostiles if entity.get("id") == target_id), {})
        point, target_pos = status.get("pos"), target.get("pos")
        if not point or not (
            target.get("alive") is not False and target.get("blocked_by_wall") is not True
            and isinstance(target_pos, (list, tuple)) and len(target_pos) >= 3
            and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in target_pos[:3])
        ):
            return {"ok": False, "reason": "melee_pursuer_unconfirmed"}, status
        start_dist = self._entity_dist(point, target)
        if verify_melee_clearance and (
            _clean_block_name(target.get("name")) not in PRE_HURT_MELEE_MOBS or not 0 <= start_dist <= melee_clearance
        ):
            return {"ok": False, "reason": "melee_pursuer_unconfirmed"}, status
        if critical and any(
            _clean_block_name(entity.get("name")) == "creeper" and self._entity_dist(point, entity) <= 5.5
            for entity in hostiles
        ):
            return {"ok": False, "reason": "creeper_still_close"}, status

        exposed = [entity for entity in hostiles if not entity.get("blocked_by_wall")]
        guarded = hostiles if critical else [entity for entity in hostiles if entity.get("blocked_by_wall")]
        shooters = [position for _, position in ranged_retreat_positions(point, exposed, max_distance=12.0)]
        life = (status.get("dimension"), status.get("last_death"))
        retreat = env.flee(
            target_pos, distance=4.5 if critical else 8.5, urgent=True, wait_for_ground=True, allow_shaft_seal=False,
            other_shooters=shooters, primary_melee=bool(shooters),
            occluded_shooters=[position for _, position in ranged_retreat_positions(point, guarded, max_distance=12.0)],
            other_melee=self._other_melee_positions(hostiles, target_id),
            **({
                "other_creepers": self._other_creeper_positions(hostiles, target_id, include_occluded=True),
                "keep_blast_clearance": True, "allow_second_leg": False,
            } if critical else {}),
            **({"expected_life": life} if verify_melee_clearance else {}),
        )
        after = env._safe_status(status)
        if critical and retreat.get("ok"):
            nearest = after.get("nearest_hostile")
            nearest = nearest if isinstance(nearest, dict) else {}
            target_after = nearest if nearest.get("id") == target_id else target
            if (
                not after.get("pos") or after.get("on_ground") is not True
                or after.get("ready") is False or after.get("alive") is False
                or after.get("in_water") or after.get("in_lava")
                or (after.get("dimension"), after.get("last_death")) != life
                or self._entity_dist(after.get("pos"), target_after) <= start_dist
                or (_clean_block_name(nearest.get("name")) == "creeper" and self._entity_dist(after.get("pos"), nearest) <= 5.5)
            ):
                retreat.update(ok=False, reason="melee_clearance_unconfirmed")
            if verify_melee_clearance:
                distance_after = nearest.get("dist") if nearest.get("id") == target_id else None
                if isinstance(distance_after, (int, float)) and math.isfinite(distance_after) and distance_after >= 0:
                    retreat.update(
                        target_distance_before=start_dist, target_distance_after=distance_after,
                        target_in_melee_range=distance_after <= 3.5,
                    )
                    if distance_after <= melee_clearance:
                        retreat.update(ok=False, reason="melee_still_close")
                else:
                    retreat.update(ok=False, reason="melee_clearance_unconfirmed")
        return retreat, after

    def _equip_best_weapon_and_shield(
        self,
        env: CombatEnv,
        counts: dict[str, int],
        has_shield: bool,
        held_name: str | None,
    ) -> str | None:
        best_weapon = next((w for w in MELEE_WEAPON_ORDER if counts.get(w, 0) > 0 or held_name == w), None)
        if best_weapon and held_name != best_weapon:
            try:
                env.bot.call("equip", {"item": best_weapon, "slot": "hand"})
            except ApiError:
                pass
        if has_shield:
            try:
                inv = env.bot.call("inventory", method="GET")
                off_nm = _clean_block_name((inv.get("offhand") or {}).get("name"))
                if off_nm != "shield" and counts.get("shield", 0) > 0:
                    env.bot.call("equip", {"item": "shield", "slot": "off-hand"})
            except Exception as error:
                if _is_control_signal(error):
                    raise
        return best_weapon

    @staticmethod
    def _latch_shield_at_threat(env: CombatEnv, target_id: int | None) -> bool:
        """Face `target_id` when known and keep the shield raised."""
        if target_id is not None:
            try:
                env.bot.call("look", {"entity_id": int(target_id)})
            except (ApiError, httpx.HTTPError):
                pass
        try:
            env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
            return True
        except (ApiError, httpx.HTTPError):
            return False

    def _cover_after_projectile_stall(
        self, env: CombatEnv, before: dict[str, Any], shooter_id: int, *, deadline: float,
        max_health: float | None, landing: dict[str, Any] | None = None, allow_short_step: bool = False,
    ) -> dict[str, Any] | None:
        if landing is None:
            landing = env._safe_status({"ready": False}, timeout_s=0.3)
        health = landing.get("health")
        pos = landing.get("pos")
        if (
            landing.get("ready") is not True or landing.get("alive") is False or landing.get("on_ground") is not True
            or not pos or not isinstance(health, (int, float)) or not math.isfinite(health) or health <= 0
            or (max_health is not None and health > max_health)
            or any(landing.get(key) for key in ("busy", "in_water", "in_lava", "submerged", "on_fire"))
            or any(landing.get(key) != before.get(key) for key in ("dimension", "last_death"))
        ):
            return None
        entities = self._scan_live_hostiles(env, radius=16, timeout_s=0.35)
        if not entities or not melee_clear_for_projectile_cover(entities):
            return None
        shooters = ranged_retreat_positions(pos, entities, max_distance=16.0)
        target = next((point for entity_id, point in shooters if entity_id == shooter_id), None)
        if target is None:
            return None
        try:
            return env.seek_natural_cover(
                target, other_shooters=[point for entity_id, point in shooters if entity_id != shooter_id],
                verify_live_shooters=True, expected_life=(landing.get("dimension"), landing.get("last_death")),
                expected_shooter_id=shooter_id, deadline=deadline,
                **({"allow_short_step": True} if allow_short_step else {}),
            )
        except _TerrainProbeExpired:
            return {"ok": False, "mode": "natural_cover", "reason": "time_budget"}

    def _place_melee_cover(
        self, env: CombatEnv, before: dict[str, Any], target_id: int, counts: dict[str, int],
    ) -> dict[str, Any]:
        def find_target(*, visible: bool) -> Sequence[float] | None:
            hostiles = self._scan_live_hostiles(env, radius=6, timeout_s=0.35)
            if not hostiles or len(hostiles) != 1:
                return None
            target = hostiles[0]
            pos = target.get("pos")
            if (
                target.get("id") != target_id or target.get("alive") is False
                or not isinstance(pos, (list, tuple)) or len(pos) < 3
                or not all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            ):
                return None
            occluded = target.get("blocked_by_wall")
            if visible and occluded is not False:
                if occluded is True:
                    return None
                env.invalidate_block_cache()
                try:
                    if not env.has_los(current["pos"], pos, deadline=time.monotonic() + 0.45)["clear"]:
                        return None
                except _TerrainProbeExpired:
                    return None
            return pos

        current = env._safe_status({"ready": False})
        if not env._on_same_footing(before, current):
            return {"ok": False, "reason": "landing_changed"}
        target_pos = find_target(visible=True)
        if target_pos is None:
            return {"ok": False, "reason": "melee_target_unconfirmed"}
        result = env._place_stationary_cover(current, target_pos, counts, melee=True)
        if not result.get("ok"):
            return result
        target_after = find_target(visible=False)
        settled = env._safe_status({"ready": False})
        env.invalidate_block_cache()
        if (
            target_after is None or not env._on_same_footing(current, settled)
            or not env._blocks_shot_across_hitbox(settled["pos"], target_after)
        ):
            return {**result, "ok": False, "reason": "melee_cover_unconfirmed"}
        return result

    def strike_cornered_hostile(
        self, env: CombatEnv, target_id: int, counts: dict[str, int], held_name: str | None,
        *, status: dict[str, Any] | None = None, visible_piglin: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status is None:
            status = env._safe_status({"ready": False})
        health = status.get("health")
        nearest = status.get("nearest_hostile")
        max_health, reach = 6.0, 1.25
        if visible_piglin is not None:
            if (
                str(status.get("dimension") or "").removeprefix("minecraft:") != "the_nether"
                or status.get("ready") is not True or status.get("alive") is not True
                or status.get("busy") or status.get("on_fire") or held_name not in MELEE_WEAPON_ORDER
                or not isinstance(nearest, dict) or _clean_block_name(nearest.get("name")) != "piglin"
                or nearest.get("blocked_by_wall") is True
                or _clean_block_name(visible_piglin.get("name")) != "piglin" or visible_piglin.get("id") != target_id
                or not 0 <= self._entity_dist(status.get("pos"), visible_piglin) <= CORNERED_PIGLIN_REACH
            ):
                return None
            max_health, reach = CORNERED_PIGLIN_MAX_HEALTH, CORNERED_PIGLIN_REACH
        if (
            not isinstance(health, (int, float)) or not 0 < health <= max_health
            or status.get("ready") is False or status.get("alive") is False
            or status.get("on_ground") is not True or status.get("in_water") or status.get("in_lava")
            or not isinstance(nearest, dict) or nearest.get("id") != target_id
            or not 0 <= self._entity_dist(status.get("pos"), nearest) <= reach
        ):
            return None
        weapon = self._equip_best_weapon_and_shield(env, counts, False, held_name)
        return self._attack_once_in_place(env, target_id, weapon)

    @staticmethod
    def _attack_once_in_place(env: CombatEnv, target_id: int, weapon: str | None) -> dict[str, Any]:
        try:
            result = env.bot.call(
                "attack",
                {
                    "entity_id": target_id,
                    "max_hits": 1,
                    "timeout": 300,
                    "interval_ms": 100,
                    "stop_on": ["death"],
                    "chase": False,
                    "feedback": True,
                },
                timeout_s=0.9,
                allow_reauth=False,
            )
        except ApiError as err:
            return {"action": "cornered_melee_strike", "ok": False, "code": err.code}
        return {
            "action": "cornered_melee_strike", "entity_id": target_id, "weapon": weapon,
            "ok": bool(result.get("killed") or attack_feedback_hits(result)), "killed": bool(result.get("killed")),
            **({"hit_log": result["hit_log"]} if isinstance(result.get("hit_log"), list) else {}),
        }

    def _verify_warden_clearance(
        self, env: CombatEnv, before: dict[str, Any], after: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        pos = after.get("pos")
        unconfirmed = {"ok": False, "reason": "warden_clearance_unconfirmed"}
        if not pos or after.get("ready") is not True or after.get("on_ground") is not True:
            return unconfirmed, []
        if after.get("alive") is False or any(after.get(key) != before.get(key) for key in ("dimension", "last_death")):
            return {"ok": False, "respawned": True}, []
        if after.get("in_lava") or after.get("in_water"):
            return unconfirmed, []

        hostiles = self._scan_live_hostiles(env, radius=24, timeout_s=0.4)
        if hostiles is None:
            return unconfirmed, []
        wardens = [entity for entity in hostiles if _clean_block_name(entity.get("name")) == "warden"]
        nearest = after.get("nearest_hostile")
        if isinstance(nearest, dict) and _clean_block_name(nearest.get("name")) == "warden":
            nearest_id = nearest.get("id")
            if nearest_id is None or not any(entity.get("id") == nearest_id for entity in wardens):
                return unconfirmed, []
        positions = [entity.get("pos") for entity in wardens]
        if any(
            not isinstance(point, (list, tuple)) or len(point) < 3
            or not all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
            for point in positions
        ):
            return unconfirmed, []
        distance = min((math.hypot(pos[0] - point[0], pos[2] - point[2]) for point in positions), default=math.inf)
        if distance < WARDEN_EVADE_DISTANCE:
            return {"ok": False, "reason": "warden_still_in_range", "warden_distance": round(distance, 2)}, hostiles
        return {"ok": True}, hostiles

    def _continue_warden_retreat(
        self, env: CombatEnv, before: dict[str, Any], after: dict[str, Any],
        warden_id: int, hostiles: list[dict[str, Any]], *, deadline: float,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        origin, start = before["pos"], after["pos"]
        nearest = after.get("nearest_hostile")
        if (
            start[1] >= 0 or abs(start[1] - origin[1]) > 0.1 or after.get("busy")
            or not isinstance(nearest, dict) or nearest.get("id") != warden_id
        ):
            return after, None
        target = next((entity for entity in hostiles if entity.get("id") == warden_id
                       and _clean_block_name(entity.get("name")) == "warden" and entity.get("alive") is not False), None)
        if target is None or abs(target["pos"][1] - start[1]) > 1.5:
            return after, None
        yaw, _ = aim_yaw_pitch(origin, start)
        ux, uz = yaw_to_unit_xz(yaw)
        dx, dz = start[0] - target["pos"][0], start[2] - target["pos"][2]
        separation = dx * ux + dz * uz
        if separation < max(1.0, 0.75 * math.hypot(dx, dz)):
            return after, None

        other_threats = []
        for entity in hostiles:
            if entity is target:
                continue
            point = entity.get("pos")
            if not isinstance(point, (list, tuple)) or len(point) < 3 or not all(
                isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3]
            ):
                return after, None
            name = _clean_block_name(entity.get("name"))
            radius = 16.0 if name in RANGED_MOBS or name in {"warden", "drowned"} else 6.0
            other_threats.append((point[0] - start[0], point[1] - start[1], point[2] - start[2], radius))

        def clear_of_other_hostiles(length: float) -> bool:
            for tx, ty, tz, radius in other_threats:
                along = min(length, max(0.0, tx * ux + tz * uz))
                if math.hypot(tx - ux * along, ty, tz - uz * along) < radius:
                    return False
            return True

        now = time.monotonic()
        probe_deadline = min(now + 0.85, deadline - 2.5)
        if now >= probe_deadline:
            return after, None
        env.invalidate_block_cache()
        foot_y = math.floor(start[1] + 0.05)
        desired = min(12.0, max(3.5, math.ceil(WARDEN_EVADE_DISTANCE + 0.9 - separation)))
        checked_length = 0.0
        for length in dict.fromkeys((3.5, min(7.5, desired), desired)):
            if not clear_of_other_hostiles(length):
                break
            try:
                if env.probe_retreat(start, yaw, length, deadline=probe_deadline) != (foot_y, False):
                    break
            except _TerrainProbeExpired:
                break
            checked_length = length
        if not checked_length:
            return after, None

        current = env._safe_status({**after, "ready": False}, timeout_s=0.25)
        pos = current.get("pos")
        threat = current.get("nearest_hostile")
        if (
            current.get("ready") is not True or current.get("alive") is False or current.get("on_ground") is not True
            or any(current.get(key) for key in ("in_water", "in_lava", "submerged", "busy"))
            or any(current.get(key) != after.get(key) for key in ("dimension", "last_death"))
            or not isinstance(threat, dict) or threat.get("id") != warden_id or not pos or abs(pos[1] - start[1]) > 0.1
        ):
            return current, None
        dx, dz = pos[0] - start[0], pos[2] - start[2]
        if abs(dx * uz - dz * ux) > 0.15 or not -0.1 <= dx * ux + dz * uz <= 1.25:
            return current, None
        # Leave time to stop a timed-out path, refresh status, and rescan for the Warden.
        duration = min(2.5, deadline - time.monotonic() - 1.25)
        if duration < 1.0:
            return current, None
        try:
            env.bot.call("move_to", {
                "x": start[0] + ux * checked_length, "y": foot_y, "z": start[2] + uz * checked_length,
                "range": 0.7, "timeout": int(duration * 1000), "stop_on": ["death"],
            }, timeout_s=duration + 0.2, allow_reauth=False)
        except httpx.HTTPError:
            env._best_effort_stop(timeout_s=0.18)
        except ApiError:
            pass
        landed = env._safe_status({**current, "ready": False}, timeout_s=0.25)
        verdict, _ = self._verify_warden_clearance(env, before, landed)
        end = landed.get("pos") or pos
        moved = 0.0 if verdict.get("respawned") else math.hypot(end[0] - pos[0], end[2] - pos[2])
        if verdict.get("ok") and (abs(end[1] - start[1]) > 0.1 or moved > checked_length + 1.0):
            verdict = {"ok": False, "reason": "warden_clearance_unconfirmed"}
        return landed, {"action": "continue_warden_retreat", "pos_after": end, "moved_dist": round(moved, 2), **verdict}

    def on_encounter(self, env: CombatEnv, trigger: ThreatTrigger) -> CombatOutcome:
        actions: list[dict[str, Any]] = []
        deadline = time.monotonic() + SUBCORTICAL_COMBAT_BUDGET_S
        active_deadline = getattr(env.bot, "active_deadline", None)
        if isinstance(active_deadline, (int, float)):
            deadline = min(deadline, active_deadline)
        st = env.bot.status()
        pos = st.get("pos")
        health = st.get("health")
        if (
            _combat_status_unavailable(st)
            or not isinstance(pos, (list, tuple)) or len(pos) < 3
        ):
            return {
                "handled": False,
                "mode": "life_unavailable",
                "summary": "角色已死亡或状态尚未就绪，等待刷新后重新规划",
                "actions": [],
                "status_after": st,
                "requires_replan": True,
                "latched_guard": False,
            }
        if bool(st.get("in_lava")):
            esc_lava = env.escape_lava()
            st_lava_after = env.bot.status()
            return {
                "handled": bool(esc_lava.get("ok")),
                "mode": "lava_escape",
                "summary": f"处于岩浆中，优先执行脱困（ok={esc_lava.get('ok')} in_lava={esc_lava.get('in_lava')}）",
                "actions": [{"action": "escape_lava", **esc_lava}],
                "status_after": st_lava_after,
                "requires_replan": True,
                "latched_guard": False,
            }
        if not st.get("in_water") and (
            _clean_block_name(trigger.get("primary_type")) == "warden" or trigger.get("hurt_source") == "sonic_boom"
        ):
            nearby = self._find_live_hostiles(env, radius=16, timeout_s=0.6)
            warden = next((entity for entity in nearby if _clean_block_name(entity.get("name")) == "warden"), {})
            warden_pos = warden.get("pos")
            if warden_pos is None and _clean_block_name(trigger.get("primary_type")) == "warden":
                warden_pos = trigger.get("primary_pos")
            if st.get("blocking"):
                try:
                    env.bot.call("posture", {"shield": False}, timeout_s=0.35, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
            retreat = env.flee(
                warden_pos, distance=WARDEN_EVADE_DISTANCE, urgent=True, allow_shaft_seal=False,
                wait_for_ground=True, use_nearest_hostile=False, min_threat_distance=WARDEN_EVADE_DISTANCE,
            )
            after = env._safe_status({"ready": False})
            continuation = None
            if retreat.get("ok"):
                verdict, hostiles = self._verify_warden_clearance(env, st, after)
                retreat = {**retreat, **verdict}
                warden_id = warden.get("id")
                if (
                    verdict.get("reason") == "warden_still_in_range" and retreat.get("moved_dist", 0) >= 3.5
                    and isinstance(warden_id, int)
                ):
                    after, continuation = self._continue_warden_retreat(env, st, after, warden_id, hostiles, deadline=deadline)
            actions.append({"action": "retreat_from_warden", **retreat})
            if continuation is not None:
                actions.append(continuation)
            return {
                "handled": bool(actions[-1].get("ok")), "mode": "warden_evade",
                "summary": "监守者威胁，已尝试沿安全落脚点撤离，先远离当前位置再恢复行动",
                "actions": actions, "status_after": after,
                "requires_replan": True, "latched_guard": False,
            }
        hp = float(health or 20.0)
        try:
            inv0 = env.bot.call("inventory", method="GET")
        except Exception as error:
            if _is_control_signal(error):
                raise
            inv0 = {}
        counts, has_shield, held_name = env._inventory_counts_and_shield(inv0)
        equipped_gear = []
        if isinstance(inv0, dict) and (isinstance(inv0.get("armor"), dict) or "offhand" in inv0):
            equipped_gear = auto_equip_survival_gear(env.bot, inv0, allow_armor=True, allow_shield=has_shield)

        hostiles = self._find_live_hostiles(env, radius=16)
        nh = st.get("nearest_hostile") if isinstance(st.get("nearest_hostile"), dict) else None
        primary: dict[str, Any] | None = None
        trig_id = trigger.get("primary_entity_id")
        creeper_warning = trigger.get("source") == "pre_hurt_creeper"
        confirmed_melee_hurt = (
            trigger.get("source") == "tool_interrupted_hurt"
            and trigger.get("hurt_source") in {"mob_attack", "player_attack", "spear"}
        )

        def eligible_attacker(entity: dict[str, Any]) -> bool:
            return not (
                confirmed_melee_hurt and _clean_block_name(entity.get("name")) == "creeper"
                and self._entity_dist(pos, entity) > 8.5
            )

        attackers = [entity for entity in hostiles if eligible_attacker(entity)]
        trigger_attacker = {
            "id": trig_id, "name": trigger.get("primary_type"), "pos": trigger.get("primary_pos"),
            "dist": trigger.get("distance"),
        }
        trigger_is_eligible = eligible_attacker(trigger_attacker)
        nearby_creeper = next((
            e for e in hostiles
            if _clean_block_name(e.get("name")) == "creeper" and self._entity_dist(pos, e) <= 5.5
        ), None)
        if trig_id is not None:
            primary = next((e for e in attackers if e.get("id") == trig_id), None)
        if creeper_warning and (primary is None or _clean_block_name(primary.get("name")) != "creeper"):
            # Keep the blast direction if the triggering creeper drops out of the scan.
            primary = nearby_creeper
            if (
                primary is None and nh and _clean_block_name(nh.get("name")) == "creeper"
                and (nh.get("id") == trig_id or self._entity_dist(pos, nh) <= 5.5)
            ):
                primary = nh
            if primary is None:
                primary = {
                    "id": trig_id, "name": "creeper", "pos": trigger.get("primary_pos"), "dist": trigger.get("distance"),
                }
        elif not creeper_warning and nearby_creeper is not None:
            primary = nearby_creeper
        if primary is None and confirmed_melee_hurt and trigger_is_eligible and trig_id is not None:
            primary = trigger_attacker
        if primary is None and attackers:
            primary = attackers[0]
        if primary is None and nh and nh.get("name") and eligible_attacker(nh):
            primary = nh

        fallback_attacker = trigger_attacker if trigger_is_eligible else {}
        mob_name = _clean_block_name((primary or {}).get("name") or fallback_attacker.get("name") or "")
        mob_id = primary.get("id") if primary is not None else fallback_attacker.get("id")
        mob_pos = (primary or {}).get("pos")
        if not mob_pos and (mob_name != "creeper" or mob_id == trig_id):
            mob_pos = fallback_attacker.get("pos")
        try:
            mob_dist = float((primary or {}).get("dist") or trigger.get("distance") or 99.0)
        except (TypeError, ValueError):
            mob_dist = 99.0

        any_creeper_close = mob_name == "creeper"
        # Count close-range engaged hostiles (<=8.5m) for swarm/solo gates so mobs outside enclosed
        # walls (`10..16m`) never trick a sword+shield bot into pure circle-fleeing inside a room.
        nearby_hostiles = [e for e in hostiles if float(e.get("dist", 99.0)) <= 8.5]
        other_melee = (
            self._other_melee_positions(nearby_hostiles, mob_id)
            if any_creeper_close or mob_name in PRE_HURT_MELEE_MOBS else []
        )
        mode = "stabilize"
        latched_guard = False
        enclosure = None

        in_water = bool(st.get("in_water"))

        shielded_arrow_interrupt = (
            has_shield and not in_water and not st.get("on_fire")
            and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and trigger.get("source") == "tool_interrupted_hurt" and trigger.get("hurt_source") == "arrow"
            and mob_name in RANGED_MOBS and isinstance(mob_id, int) and mob_id == trig_id
        )
        if shielded_arrow_interrupt and hp <= 6.0:
            actions.append({"action": "raise_shield_entry", "ok": self._latch_shield_at_threat(env, mob_id)})
        elif (
            not in_water
            and has_shield
            and not st.get("blocking")
            and not (any_creeper_close or creeper_warning)
            and trigger.get("source") in {"pre_hurt_ranged", "pre_hurt_melee"}
        ):
            if mob_id is not None:
                try:
                    env.bot.call("look", {"entity_id": int(mob_id)})
                except ApiError:
                    pass
            try:
                env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                actions.append({"action": "raise_shield_entry", "ok": True})
            except ApiError:
                pass

        # 0) Underwater drowning emergency: surface immediately instead of burning oxygen in combat!
        if _water_requires_surface(st, oxygen_bubbles=12):
            mode = "water_surface"
            esc = env.surface_from_water(st)
            actions.append({"action": "water_surface", **esc})
            if not esc.get("respawned") and (not esc.get("ok") or esc.get("in_water")):
                shore = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield, shield_target_id=mob_id)
                actions.append({"action": "water_surface_shore", "ok": bool(shore.get("ok"))})

        # 1) Creeper emergency: NEVER melee in blast radius — shield & retreat / seal shaft
        elif any_creeper_close or creeper_warning:
            mode = "creeper_evade"
            creeper_ent = primary
            c_pos = mob_pos
            if has_shield:
                offhand = inv0.get("offhand") if isinstance(inv0, dict) else None
                offhand_name = _clean_block_name(offhand.get("name")) if isinstance(offhand, dict) else ""
                if offhand_name != "shield" and counts.get("shield", 0) > 0 and "equip:shield" not in equipped_gear:
                    try:
                        env.bot.call("equip", {"item": "shield", "slot": "off-hand"})
                    except (ApiError, httpx.HTTPError):
                        pass
                raised = self._latch_shield_at_threat(env, mob_id)
                actions.append({"action": "raise_shield_creeper", "ok": raised})
            snap = env.arena_snapshot(pos)
            sealed_ok = False
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.fortify(st, lambda: env.build_tactical(
                    "full_seal", threat_pos=c_pos,
                    **({"creeper_seal_life": (st.get("dimension"), st.get("last_death"))}
                       if not has_shield and c_pos is not None else {}),
                ))
                sealed_ok = bool(seal.get("ok"))
                actions.append(self._seal_action(seal, action="seal_creeper_shaft"))
                if sealed_ok:
                    st_chk_seal = env.bot.status()
                    post_creepers = [
                        e
                        for e in self._find_live_hostiles(env, radius=8)
                        if _clean_block_name(e.get("name")) == "creeper"
                        and self._entity_dist(st_chk_seal.get("pos"), e) <= 4.5
                    ]
                    if post_creepers:
                        sealed_ok = False
            if not sealed_ok:
                other_creepers = self._other_creeper_positions(hostiles, mob_id)
                rank_melee_clearance = bool(other_melee) and not any(
                    hostile is not primary and _clean_block_name(hostile.get("name")) == "creeper"
                    for hostile in nearby_hostiles
                )
                flee_res = env.flee(
                    c_pos, distance=11.5, urgent=True, allow_shaft_seal=False, wait_for_ground=True,
                    **({"other_creepers": other_creepers} if other_creepers and c_pos else {}),
                    **({"other_melee": other_melee, "avoid_blast": True} if rank_melee_clearance else {}),
                    **({"initial_landing_checks": 5} if not has_shield and st.get("on_ground") is False else {}),
                    **({"use_nearest_hostile": False} if not c_pos else {}),
                )
                st_chk = env._safe_status({**st, "ready": False})
                changed_life = (
                    bool(flee_res.get("respawned")) or st_chk.get("alive") is False
                    or st_chk.get("last_death") != st.get("last_death")
                    or st_chk.get("dimension") != st.get("dimension")
                )
                can_continue = not changed_life and st_chk.get("ready") is not False and bool(st_chk.get("pos"))
                released_creeper_id = None
                if (
                    not has_shield and creeper_warning and trigger.get("action_released") is True
                    and isinstance(mob_id, int) and mob_id == trig_id
                    and not any(st_chk.get(key) for key in ("in_water", "in_lava", "submerged"))
                ):
                    released_creeper_id = mob_id

                post_scan = self._scan_live_hostiles(env, radius=8, timeout_s=0.6) if can_continue else None
                post_h = post_scan or []
                scanned_creeper = next((
                    e for e in post_h if self._creeper_within_clearance(st_chk.get("pos"), e, released_creeper_id)
                ), None)
                close_creeper = scanned_creeper
                if close_creeper is None and can_continue:
                    nh_chk = st_chk.get("nearest_hostile")
                    if isinstance(nh_chk, dict) and self._creeper_within_clearance(st_chk.get("pos"), nh_chk, released_creeper_id):
                        close_creeper = nh_chk
                close_dist = self._entity_dist(st_chk.get("pos"), close_creeper) if close_creeper else None
                extended_clearance = close_dist is not None and close_dist > 5.5
                scan_missing_target = extended_clearance and scanned_creeper is None
                retreat_ok = bool(flee_res.get("ok"))
                clearance_confirmed = post_scan is not None and close_creeper is None
                retreat_reason = flee_res.get("reason")
                if retreat_ok and can_continue and (scan_missing_target or (post_scan is None and close_creeper is None)):
                    retreat_reason = "blast_clearance_unconfirmed"
                actions.append({
                    "action": "retreat_from_creeper", "ok": retreat_ok and can_continue and clearance_confirmed,
                    "moved": flee_res.get("moved_dist"),
                    **({"reason": retreat_reason} if retreat_reason else {}),
                    **({"creeper_distance": close_dist} if close_dist is not None else {}),
                    **({"respawned": True} if changed_life else {}),
                })
                close_pos = close_creeper.get("pos") if close_creeper and not scan_missing_target else None
                if (
                    st_chk.get("in_water") and not st_chk.get("in_lava")
                    and can_continue
                    and time.monotonic() < deadline
                ):
                    shore = env.escape_deep_water_to_shore(
                        close_pos or c_pos, keep_shield=has_shield,
                        shield_target_id=(close_creeper or creeper_ent or {}).get("id"),
                        avoid_close_threat=True,
                    )
                    actions.append({"action": "creeper_water_retreat", **shore})
                    st_chk = env._safe_status({"ready": False})
                    latched_guard = bool(
                        has_shield and st_chk.get("blocking") and not shore.get("respawned")
                        and st_chk.get("ready") is not False and st_chk.get("alive") is not False
                        and st_chk.get("last_death") == st.get("last_death")
                        and st_chk.get("dimension") == st.get("dimension")
                    )
                elif (
                    not has_shield and isinstance(close_pos, (list, tuple)) and len(close_pos) >= 3
                    and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in close_pos[:3])
                    and can_continue and retreat_made_progress(retreat_ok, flee_res.get("moved_dist"), retreat_reason)
                    and st_chk.get("on_ground") is True and not st_chk.get("in_water") and not st_chk.get("in_lava")
                    and time.monotonic() < deadline
                    and (not extended_clearance or (not st_chk.get("busy") and time.monotonic() + 1.3 < deadline))
                ):
                    other_close_creepers = self._other_creeper_positions(post_h, close_creeper.get("id"))
                    pursuers = self._other_melee_positions(post_h, close_creeper.get("id"))
                    followup = env.flee(
                        close_pos, distance=6.0, urgent=True, allow_shaft_seal=False, wait_for_ground=True,
                        other_creepers=other_close_creepers, keep_blast_clearance=True,
                        **({"other_melee": pursuers} if pursuers else {}),
                        **({
                            "expected_life": (st_chk.get("dimension"), st_chk.get("last_death")),
                            "allow_second_leg": False, "terrain_deadline": time.monotonic() + 0.6,
                        } if extended_clearance else {}),
                    )
                    if followup.get("ok"):
                        followup.update(self._verify_creeper_followup(
                            env, st, released_creeper_id, extended_clearance=extended_clearance,
                        ))
                    actions.append({"action": "creeper_followup_retreat", **followup})
                elif (
                    creeper_warning and not has_shield and snap["enclosure"] != "carved_shaft"
                    and retreat_reason == "no_safe_retreat" and flee_res.get("moved_dist") == 0.0
                    and can_continue and not _combat_status_unavailable(st_chk)
                    and st_chk.get("on_ground") is True and st_chk.get("alive") is True and st_chk.get("ready") is True
                    and not any(st_chk.get(key) for key in ("busy", "in_water", "in_lava", "submerged", "on_fire"))
                    and len(post_h) == 1 and scanned_creeper is not None and scanned_creeper.get("id") == mob_id == trig_id
                    and scanned_creeper.get("alive") is not False and isinstance(mob_id, int)
                    and close_dist is not None and 1.6 <= close_dist <= 3.5
                    and isinstance(close_pos, (list, tuple)) and len(close_pos) >= 3
                    and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in close_pos[:3])
                    and env.arrow_cover_material(counts) is not None and time.monotonic() + 0.9 < deadline
                ):
                    cover = self._place_creeper_cover(
                        env, st_chk, mob_id, close_pos, counts, deadline=min(deadline, time.monotonic() + 1.9),
                    )
                    actions.append({"action": "place_blast_cover", **cover})
                if (
                    (not retreat_ok or not clearance_confirmed)
                    and can_continue
                    and has_shield
                    and not st_chk.get("in_water")
                ):
                    cid = (close_creeper or creeper_ent or {}).get("id")
                    latched_guard = self._latch_shield_at_threat(env, int(cid) if cid is not None else None)
                elif retreat_ok and can_continue and has_shield and st_chk.get("blocking"):
                    nearest = st_chk.get("nearest_hostile")
                    nearest = nearest if isinstance(nearest, dict) else {}
                    health_after = st_chk.get("health")
                    name = _clean_block_name(nearest.get("name"))
                    distance = self._entity_dist(st_chk.get("pos"), nearest)
                    guard_melee = name in PRE_HURT_MELEE_MOBS and distance <= 5.5
                    guard_ranged = name in RANGED_MOBS and distance <= 16.0 and not nearest.get("blocked_by_wall")
                    if (
                        isinstance(health_after, (int, float)) and 0 < health_after <= 8.0
                        and st_chk.get("on_ground") is True and not (st_chk.get("in_water") or st_chk.get("in_lava"))
                        and st_chk.get("ready") is not False and st_chk.get("alive") is not False and st_chk.get("pos")
                        and all(st_chk.get(key) == st.get(key) for key in ("last_death", "dimension"))
                        and isinstance(nearest.get("id"), int) and (guard_melee or guard_ranged)
                    ):
                        latched_guard = self._latch_shield_at_threat(env, nearest["id"])
                        action = "guard_ranged_after_creeper_retreat" if guard_ranged else "guard_melee_after_creeper_retreat"
                        actions.append({"action": action, "ok": latched_guard, "entity_id": nearest["id"]})

        # 2) Lone/pair melee hostile + healthy + weapon -> 0-LLM counter-kill loop!
        elif (
            mob_name in LONE_COUNTERKILL_MOBS
            and hp > 10.0
            and len(nearby_hostiles) <= 2
            and any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
        ):
            weapon = self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            mode = "counter_kill"
            killed = False
            retreat_action = None
            for _ in range(5):
                if time.monotonic() >= deadline:
                    break
                st_cur = env.bot.status()
                if (
                    st_cur.get("ready") is False or st_cur.get("alive") is False
                    or any(st_cur.get(key) != st.get(key) for key in ("dimension", "last_death"))
                ):
                    mode = "life_unavailable"
                    break
                if self._is_falling_fast(st_cur, pos):
                    mode = "falling"
                    break
                hp_cur = float(st_cur.get("health") or 20.0)
                if _water_requires_surface(st_cur, oxygen_bubbles=10):
                    nearest = st_cur.get("nearest_hostile")
                    nearest = nearest if isinstance(nearest, dict) else {}
                    close_melee = (
                        hp_cur <= 12.0 and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                        and not nearest.get("blocked_by_wall") and self._entity_dist(st_cur.get("pos"), nearest) <= 3.5
                    )
                    unavailable = {**st_cur, "ready": False}
                    if close_melee:
                        attacker_id = nearest.get("id")
                        attacker_pos = nearest.get("pos") or (mob_pos if attacker_id == mob_id else None)
                        esc_cur = env.escape_deep_water_to_shore(
                            attacker_pos, keep_shield=has_shield, shield_target_id=attacker_id,
                            surface_first=True, avoid_close_threat=attacker_pos is not None, under_attack=True,
                        )
                        actions.append({"action": "water_surface_shore", **esc_cur})
                    else:
                        esc_cur = env.surface_from_water(st_cur)
                        actions.append({"action": "water_surface_abort", **esc_cur})
                    st_w = env._safe_status(unavailable)
                    if self._combat_life_lost(st_cur, st_w, esc_cur):
                        mode = "life_unavailable"
                        break
                    if not close_melee and (
                        not esc_cur.get("ok") or esc_cur.get("in_water") or st_w.get("in_water")
                    ):
                        shore_w = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield, shield_target_id=mob_id)
                        actions.append({"action": "water_surface_shore", "ok": bool(shore_w.get("ok"))})
                        st_w = env._safe_status(unavailable)
                        if self._combat_life_lost(st_cur, st_w, shore_w):
                            mode = "life_unavailable"
                            break
                    nh_w = st_w.get("nearest_hostile")
                    nh_w_dist = float(nh_w.get("dist", 99.0)) if isinstance(nh_w, dict) and nh_w.get("name") else 99.0
                    if has_shield and st_w.get("alive") is not False and not st_w.get("in_water") and (nh_w_dist <= 8.0 or not esc_cur.get("ok")):
                        tid_w = nh_w.get("id") if isinstance(nh_w, dict) and nh_w.get("id") is not None else mob_id
                        latched_guard = self._latch_shield_at_threat(env, int(tid_w) if tid_w is not None else None)
                    mode = "water_surface_abort"
                    break
                if hp_cur <= 8.0:
                    flee_low = env.flee(mob_pos, distance=12.0, urgent=True)
                    actions.append({"action": "low_hp_abort_flee", "ok": bool(flee_low.get("ok"))})
                    if not flee_low.get("ok") and not flee_low.get("respawned") and has_shield:
                        st_lw = env._safe_status(st_cur)
                        if st_lw.get("alive") is not False and not st_lw.get("in_water"):
                            latched_guard = self._latch_shield_at_threat(env, int(mob_id) if mob_id is not None else None)
                    mode = "low_hp_disengage"
                    break
                sighting = self._scan_live_hostiles(env, radius=10)
                cur_hostiles = sighting or []
                if any(_clean_block_name(e.get("name")) == "creeper" and float(e.get("dist", 99.0)) <= 5.0 for e in cur_hostiles):
                    flee_cr = env.flee(mob_pos, distance=11.0, urgent=True)
                    actions.append({"action": "creeper_intruded_flee", "ok": bool(flee_cr.get("ok"))})
                    mode = "creeper_evade"
                    break
                target_ent = (
                    next((e for e in cur_hostiles if e.get("id") == mob_id), None)
                    if mob_id is not None
                    else (cur_hostiles[0] if cur_hostiles else None)
                )
                if target_ent is None and mob_id is None:
                    killed = True
                    break
                if target_ent is None and sighting is not None:
                    actions.append({"action": "melee_attack", "entity_id": mob_id, "ok": False, "code": "target_not_visible"})
                    break
                eff_id = int(target_ent["id"]) if isinstance(target_ent, dict) and target_ent.get("id") is not None else mob_id
                if eff_id is None:
                    break
                if target_ent is not None and target_ent.get("blocked_by_wall") is True:
                    actions.append({"action": "melee_attack", "entity_id": eff_id, "ok": False, "code": "blocked_by_wall"})
                    break
                # Keep part of the slot budget for the reply, final status and control release.
                remaining = deadline - time.monotonic()
                if remaining < 1.5:
                    break
                attack_ms = min(2600, int((remaining - 0.9) * 1000))
                vulnerable_counter = not has_shield and hp_cur <= 12.0
                stop_on = ["hurt", "death"] if vulnerable_counter else ["death"]
                attack_body: dict[str, Any] = {
                    "entity_id": eff_id, "timeout": attack_ms, "stop_on": stop_on, "feedback": True,
                }
                target_distance = self._entity_dist(st_cur.get("pos"), target_ent) if target_ent is not None else math.inf
                if target_distance <= 3.2:
                    attack_body["chase"] = False
                if target_distance <= 2.5:
                    # A close counter can return after two swings. Reserve the longest sword/axe
                    # interval (1250 ms) for a final server-side sleep if the target slips out of reach.
                    attack_body.update(timeout=max(300, attack_ms - 1250), max_hits=2)
                try:
                    atk = env.bot.call(
                        "attack", attack_body,
                        timeout_s=attack_ms / 1000 + 0.35, allow_reauth=False,
                    )
                    hits = atk.get("hits")
                    contacted = attack_feedback_hits(atk)
                    killed = bool(atk.get("killed"))
                    actions.append({
                        "action": "melee_attack", "weapon": weapon, "entity_id": eff_id,
                        "ok": killed or bool(contacted), "killed": killed,
                        **({"code": "attack_hit_unconfirmed"} if not killed and contacted is None else {}),
                        **({"hits": hits} if isinstance(hits, (int, float)) and math.isfinite(hits) else {}),
                        **({"connected_hits": atk["connected_hits"]} if "connected_hits" in atk else {}),
                        **({"chase": False} if attack_body.get("chase") is False else {}),
                        **({"hit_log": atk["hit_log"]} if isinstance(atk.get("hit_log"), list) else {}),
                    })
                    if killed:
                        break
                    if vulnerable_counter and atk.get("interrupted_by") == "hurt":
                        retreat_action = "low_hp_abort_flee"
                        break
                    if not contacted:
                        retreat_action = "unreachable_flee"
                        break
                except ApiError as err:
                    actions.append({"action": "melee_attack", "ok": False, "code": err.code})
                    if vulnerable_counter and err.code in {"hurt", "interrupted"}:
                        retreat_action = "low_hp_abort_flee"
                        break
                    if err.code in {"entity_not_found", "invalid_entity", "target_dead"}:
                        killed = True
                        break
                    if err.code in {"timeout", "attack_zero_hits", "path_not_found", "no_path"}:
                        retreat_action = "attack_timeout_flee"
                    break
                if not cur_hostiles:
                    killed = True
                    break
            if retreat_action is not None:
                after_attack = env._safe_status({})
                interrupted_by_hurt = retreat_action == "low_hp_abort_flee"
                mode = "low_hp_disengage" if interrupted_by_hurt else "cover_or_flee"
                if (
                    not after_attack.get("pos") or after_attack.get("ready") is False or after_attack.get("alive") is False
                    or any(after_attack.get(key) != st.get(key) for key in ("dimension", "last_death"))
                ):
                    mode = "life_unavailable"
                elif self._is_falling_fast(after_attack, pos):
                    mode = "falling"
                elif not interrupted_by_hurt or not (after_attack.get("in_water") or after_attack.get("in_lava")):
                    retreat = env.flee(
                        mob_pos, distance=12.0 if interrupted_by_hurt else 10.0, urgent=True,
                        **({"wait_for_ground": True} if interrupted_by_hurt else {}),
                    )
                    actions.append({"action": retreat_action, "ok": bool(retreat.get("ok"))})
            elif killed:
                mode = "counter_killed"
                self._recent_flee_streak = 0

        # 3) Ranged shooter / swarm / low-HP / unarmed -> seal shaft, close-rush with shield, bunker, or cover/flee
        else:
            self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            snap = env.arena_snapshot(pos)
            enclosure = snap["enclosure"]
            has_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.fortify(st, lambda: env.build_tactical("full_seal", threat_pos=mob_pos))
                actions.append(self._seal_action(seal))
                mode = "sealed_shaft" if seal.get("ok") else "flee"
                if not seal.get("ok"):
                    fl = env.flee(mob_pos, distance=12.0, urgent=True, allow_shaft_seal=False)
                    actions.append({
                        "action": "flee", "ok": bool(fl.get("ok")), "moved": fl.get("moved_dist"),
                        **({"reason": fl["reason"]} if fl.get("reason") else {}),
                        **({"respawned": True} if fl.get("respawned") else {}),
                    })
                    if has_shield and not fl.get("respawned"):
                        landing = env._safe_status({"ready": False})
                        nearest = landing.get("nearest_hostile")
                        nearest = nearest if isinstance(nearest, dict) else {}
                        name = _clean_block_name(nearest.get("name"))
                        distance = self._entity_dist(landing.get("pos"), nearest)
                        close_melee = name in PRE_HURT_MELEE_MOBS and distance <= 3.5
                        health = landing.get("health")
                        exposed_to_shooter = (
                            not fl.get("ok") and landing.get("blocking") is False
                            and mob_name in RANGED_MOBS and name in RANGED_MOBS and distance <= 8.0
                            and isinstance(health, (int, float)) and 0 < health <= 10.0
                        )
                        if (
                            (close_melee or exposed_to_shooter) and isinstance(nearest.get("id"), int)
                            and landing.get("ready") is not False and landing.get("alive") is not False
                            and not landing.get("in_water") and not landing.get("in_lava")
                            and landing.get("last_death") == st.get("last_death")
                            and landing.get("dimension") == st.get("dimension")
                        ):
                            latched_guard = self._latch_shield_at_threat(env, nearest["id"])
                            action = "guard_melee_after_retreat" if close_melee else "guard_ranged_after_failed_seal"
                            actions.append({"action": action, "ok": latched_guard, "entity_id": nearest["id"]})
            elif (
                mob_name in RANGED_MOBS
                and mob_name != "blaze"
                and trigger.get("source") != "guard_poll_hurt"
                and trigger.get("hurt_source") not in {"arrow", "trident", "fireball", "small_fireball", "wither_skull"}
                and has_shield
                and has_weapon
                and hp >= 14.0
                and len(nearby_hostiles) <= 1
                and mob_dist <= 5.0
                and mob_id is not None
            ):
                mode = "shield_rush_ranged"
                try:
                    env.bot.call("look", {"entity_id": int(mob_id)})
                    env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                    env.chase_to_range(int(mob_id), range_m=2.6, timeout_ms=1400)
                    st_mid_rush = env.bot.status()
                    hp_mid_rush = float(st_mid_rush.get("health") or 20.0)
                    if st_mid_rush.get("alive") is False or hp_mid_rush <= 10.0 or st_mid_rush.get("in_water"):
                        fl_abort = env.flee(mob_pos, distance=12.0, urgent=True)
                        actions.append({"action": "rush_low_hp_flee", "ok": bool(fl_abort.get("ok"))})
                        mode = "cover_or_flee"
                    else:
                        env.bot.call("posture", {"shield": False, "sneak": False, "jump": False})
                        for _ in range(3):
                            remaining = deadline - time.monotonic()
                            if remaining < 1.5:
                                break
                            attack_ms = min(2400, int((remaining - 0.9) * 1000))
                            atk_r = env.bot.call(
                                "attack", {"entity_id": int(mob_id), "timeout": attack_ms, "stop_on": ["death"], "feedback": True},
                                timeout_s=attack_ms / 1000 + 0.35, allow_reauth=False,
                            )
                            contacted = attack_feedback_hits(atk_r)
                            killed_r = bool(atk_r.get("killed"))
                            actions.append({
                                "action": "shield_rush_attack", "ok": killed_r or bool(contacted), "killed": killed_r,
                                **({"code": "attack_hit_unconfirmed"} if not killed_r and contacted is None else {}),
                            })
                            if killed_r or contacted == 0:
                                break
                            st_rk = env.bot.status()
                            if contacted is None:
                                nearest = st_rk.get("nearest_hostile")
                                offhand = st_rk.get("offhand")
                                if (
                                    not self._combat_life_lost(st, st_rk) and st_rk.get("on_ground") is True
                                    and not any(st_rk.get(key) for key in ("in_water", "in_lava", "busy"))
                                    and isinstance(offhand, dict) and _clean_block_name(offhand.get("name")) == "shield"
                                    and isinstance(nearest, dict) and nearest.get("id") == mob_id
                                    and not nearest.get("blocked_by_wall") and self._entity_dist(st_rk.get("pos"), nearest) <= 5.0
                                    and time.monotonic() + 0.7 < deadline
                                ):
                                    latched_guard = self._latch_shield_at_threat(env, int(mob_id))
                                    actions.append({"action": "guard_after_unconfirmed_attack", "ok": latched_guard})
                                break
                            if st_rk.get("alive") is False or float(st_rk.get("health") or 20.0) <= 10.0:
                                break
                except ApiError as err:
                    actions.append({"action": "shield_rush_attack", "ok": False, "code": err.code})
                    if err.code not in {"no_such_entity", "entity_not_found", "invalid_entity", "target_dead"}:
                        fl = env.flee(mob_pos, distance=12.0, urgent=True)
                        actions.append({"action": "fallback_flee", "ok": bool(fl.get("ok"))})
            else:
                mode = "cover_or_flee"
                covered = False
                pocket_timed_out = False
                exposed_melee = (
                    mob_name in PRE_HURT_MELEE_MOBS and mob_dist <= 3.5
                    and (trigger.get("source") == "pre_hurt_melee" or confirmed_melee_hurt)
                    and not (primary or {}).get("blocked_by_wall")
                )
                if (
                    snap["enclosure"] in {"open_cave", "carved_shaft"}
                    and env.skills is not None
                    and not exposed_melee
                    and mob_name not in {"blaze", "ghast"}
                    and trigger.get("hurt_source") not in {"fireball", "small_fireball", "wither_skull"}
                ):
                    cov = env.fortify(st, lambda: env.build_tactical("cover_pocket"))
                    if cov.get("ok"):
                        covered = True
                        mode = "cover_pocket"
                        actions.append({"action": "cover_pocket", "ok": True})
                    elif cov.get("reason") in {"cover_not_entered", "fortification_budget"}:
                        pocket_timed_out = cov["reason"] == "fortification_budget"
                        actions.append({"action": "cover_pocket", "ok": False, "reason": cov["reason"]})
                now_flee = time.monotonic()
                if now_flee - self._last_flee_at > 25.0:
                    self._recent_flee_streak = 0
                # A vertical bunker takes several digs while a surface attacker can close in.
                exposed_to_lethal_melee = (
                    snap["enclosure"] == "surface"
                    and hp <= 4.0
                    and not has_shield
                    and exposed_melee
                )
                should_bunker = (
                    not covered
                    and not exposed_to_lethal_melee
                    and env.skills is not None
                    and hasattr(env.skills, "dig_safety_bunker")
                    and not any_creeper_close
                    and st.get("on_ground") is True
                    and (
                        hp <= 10.0
                        or (
                            not has_weapon
                            and st.get("is_day") is False
                            and (hp <= 14.0 or len(hostiles) >= 2 or self._recent_flee_streak >= 1)
                        )
                    )
                )
                if should_bunker:
                    try:
                        bunk = env.fortify(st, lambda: env.skills.dig_safety_bunker(urgent=True))
                    except ApiError as err:
                        if err.code != "missing_item":
                            raise
                        actions.append({"action": "emergency_bunker", "ok": False, "code": err.code})
                    else:
                        if bunk.get("ok"):
                            covered = True
                            mode = "bunker_sealed"
                            self._recent_flee_streak = 0
                            actions.append({"action": "emergency_bunker", "ok": True})
                        elif bunk.get("reason") == "fortification_budget":
                            actions.append({"action": "emergency_bunker", "ok": False, "reason": bunk["reason"]})
                if not covered:
                    check_enderman_guard = (
                        mob_name == "enderman" and isinstance(mob_id, int) and mob_id == trig_id
                        and confirmed_melee_hurt and trigger.get("hurt_source") == "mob_attack"
                    )
                    if has_shield and mob_id is not None:
                        try:
                            if check_enderman_guard:
                                if (
                                    isinstance(mob_pos, (list, tuple)) and len(mob_pos) >= 3
                                    and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in mob_pos[:3])
                                ):
                                    yaw, pitch = aim_yaw_pitch(pos, mob_pos, dst_height=0.75)
                                    env.bot.call("look", {"yaw": yaw, "pitch": pitch})
                            else:
                                env.bot.call("look", {"entity_id": int(mob_id)})
                            env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        except (ApiError, httpx.HTTPError):
                            pass
                    other_shooters = []
                    if not has_shield and (mob_name in RANGED_MOBS or exposed_melee):
                        shooters = [
                            hostile for hostile in hostiles if hostile is not primary
                            and (not exposed_melee or not hostile.get("blocked_by_wall"))
                        ]
                        other_shooters = [
                            shooter_pos for _, shooter_pos in ranged_retreat_positions(
                                pos, shooters, max_distance=12.0,
                            )
                        ]
                    unsettled_under_attack = st.get("on_ground") is False and (
                        exposed_melee or trigger.get("source") == "tool_interrupted_hurt"
                    )
                    fl = env.flee(
                        mob_pos, distance=14.0, urgent=True,
                        wait_for_ground=mob_name in RANGED_MOBS or hp <= 10.0 or not has_weapon or unsettled_under_attack,
                        **({"use_nearest_hostile": False} if not trigger_is_eligible else {}),
                        **({"other_shooters": other_shooters} if other_shooters else {}),
                        **({"primary_melee": True} if exposed_melee and other_shooters else {}),
                        **({"other_melee": other_melee} if other_melee else {}),
                    )
                    flee_ok = bool(fl.get("ok"))
                    sealed = flee_ok and fl.get("mode") == "sealed" and not fl.get("respawned")
                    if sealed:
                        mode = "sealed_shaft"
                        actions.append(self._seal_action(fl.get("seal") or {"ok": True}))
                        self._recent_flee_streak = 0
                    else:
                        actions.append({
                            "action": "flee", "ok": flee_ok, "moved": fl.get("moved_dist"),
                            **({"reason": fl["reason"]} if fl.get("reason") else {}),
                            **({"respawned": True} if fl.get("respawned") else {}),
                        })
                        self._recent_flee_streak += 1
                        self._last_flee_at = now_flee
                    if (
                        not flee_ok and fl.get("reason") == "no_safe_retreat" and not fl.get("respawned")
                        and fl.get("moved_dist") == 0.0 and enclosure == "open_cave" and exposed_melee
                        and not has_shield and not has_weapon and env.arrow_cover_material(counts) is not None
                        and mob_name in {"zombie", "zombie_villager", "husk"} and isinstance(mob_id, int)
                        and time.monotonic() + 2.0 < deadline
                    ):
                        cover = env.fortify(st, lambda: self._place_melee_cover(env, st, mob_id, counts))
                        actions.append({"action": "place_melee_cover", **cover})
                        if cover.get("ok"):
                            mode = "placed_cover"
                            self._recent_flee_streak = 0
                    stalled_under_arrows = (
                        not flee_ok and fl.get("reason") == "no_safe_retreat" and hp <= 4.0
                        and trigger.get("hurt_source") == "arrow"
                    )
                    close_shooter_at_exit = (
                        trigger.get("staircase_surface_exit") is True and not has_shield
                        and mob_name in RANGED_MOBS and isinstance(mob_id, int) and mob_id == trig_id
                        and 0 < mob_dist <= 3.5
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                    )
                    close_shooter_after_pocket_timeout = (
                        pocket_timed_out and not stalled_under_arrows and not has_shield and 0 < hp <= 6.0
                        and enclosure == "open_cave" and fl.get("moved_dist") == 0.0
                        and mob_name in {"skeleton", "stray", "bogged", "pillager"}
                        and isinstance(mob_id, int) and mob_id == trig_id and 0 < mob_dist <= 3.5
                        and trigger.get("source") in {"pre_hurt_ranged", "tool_interrupted_hurt"}
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                    )
                    close_shooter_without_cover_material = (
                        trigger.get("source") == "pre_hurt_ranged" and not has_shield and 0 < hp <= 10.0
                        and mob_name in {"skeleton", "stray", "bogged", "pillager"}
                        and isinstance(mob_id, int) and mob_id == trig_id and 0 < mob_dist <= 3.5
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                        and fl.get("moved_dist") == 0.0
                        and env.arrow_cover_material({item: count for item, count in counts.items() if count >= 2}) is None
                    )
                    if (
                        (shielded_arrow_interrupt or close_shooter_at_exit or close_shooter_after_pocket_timeout
                         or close_shooter_without_cover_material)
                        and not flee_ok and not fl.get("respawned")
                        and fl.get("reason") == "no_safe_retreat" and time.monotonic() + 2.0 < deadline
                    ):
                        max_cover_health = 10.0 if close_shooter_without_cover_material else 6.0
                        natural_cover = self._cover_after_projectile_stall(
                            env, st, mob_id, deadline=min(deadline - 0.7, time.monotonic() + 2.25),
                            max_health=None if close_shooter_at_exit else max_cover_health,
                            allow_short_step=close_shooter_without_cover_material,
                        )
                        if natural_cover is not None:
                            actions.append({"action": "natural_cover", **natural_cover})
                            if natural_cover.get("ok"):
                                mode = "natural_cover"
                                self._recent_flee_streak = 0
                    moved = fl.get("moved_dist")
                    short_ranged_retreat = (
                        flee_ok and not sealed and trigger.get("source") == "pre_hurt_ranged"
                        and isinstance(moved, (int, float)) and 0 < moved < 2.0
                    )
                    short_landing = None
                    natural_cover = None
                    if (
                        short_ranged_retreat and not fl.get("respawned") and not has_shield
                        and mob_name in RANGED_MOBS and isinstance(mob_id, int) and mob_id == trig_id
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                        and time.monotonic() + 2.0 < deadline
                    ):
                        short_landing = env._safe_status({"ready": False})
                    short_landing_hp = (short_landing or {}).get("health")
                    if isinstance(short_landing_hp, (int, float)) and 0 < short_landing_hp <= 4.0:
                        natural_cover = self._cover_after_projectile_stall(
                            env, st, mob_id, deadline=min(deadline - 0.7, time.monotonic() + 2.25),
                            max_health=4.0, landing=short_landing,
                        )
                    elif (
                        (stalled_under_arrows or short_ranged_retreat) and not fl.get("respawned")
                        and not has_shield and mob_name in RANGED_MOBS
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                        and isinstance(mob_pos, (list, tuple)) and len(mob_pos) >= 3
                        and time.monotonic() + 2.0 < deadline
                        and not any(
                            _clean_block_name(hostile.get("name")) in PRE_HURT_MELEE_MOBS
                            and self._entity_dist(pos, hostile) <= 3.5
                            for hostile in nearby_hostiles
                        )
                    ):
                        cover_after_retreat = stalled_under_arrows
                        if short_ranged_retreat:
                            landing = short_landing if short_landing is not None else env._safe_status({"ready": False})
                            landing_hp = landing.get("health")
                            nearest = landing.get("nearest_hostile")
                            close_melee = (
                                isinstance(nearest, dict) and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                                and self._entity_dist(landing.get("pos"), nearest) <= 3.5
                            )
                            cover_after_retreat = (
                                isinstance(landing_hp, (int, float)) and 0 < landing_hp <= 7.0
                                and landing.get("on_ground") is True and landing.get("ready") is not False
                                and landing.get("alive") is not False and not (landing.get("in_water") or landing.get("in_lava"))
                                and landing.get("last_death") == st.get("last_death") and landing.get("dimension") == st.get("dimension")
                                and not close_melee and time.monotonic() + 2.0 < deadline
                            )
                        if cover_after_retreat:
                            natural_cover = env.seek_natural_cover(
                                mob_pos, other_shooters=other_shooters, allow_stationary=short_ranged_retreat,
                                verify_live_shooters=True,
                            )
                    if natural_cover is not None:
                        actions.append({"action": "natural_cover", **natural_cover})
                        if natural_cover.get("ok"):
                            mode = "natural_cover"
                            self._recent_flee_streak = 0
                    check_ranged_guard = flee_ok and fl.get("mode") == "flee" and mob_name in RANGED_MOBS
                    check_melee_guard = flee_ok and (sealed or fl.get("mode") == "flee") and mob_name in PRE_HURT_MELEE_MOBS
                    if (
                        check_enderman_guard and has_shield and not sealed and not fl.get("respawned")
                        and time.monotonic() + 1.8 < deadline
                    ):
                        pursuit = env.guard_enderman_pursuit(st, mob_id)
                        actions.append({"action": "guard_enderman_pursuit", **pursuit})
                        latched_guard = pursuit.get("ok") is True
                    if (not flee_ok or check_ranged_guard or check_melee_guard) and not fl.get("respawned") and has_shield:
                        st_fl = env._safe_status({"ready": False})
                        if (
                            not flee_ok and fl.get("reason") == "no_safe_retreat"
                            and st_fl.get("blocking") and st_fl.get("dimension") == st.get("dimension") == "overworld"
                            and st_fl.get("last_death") == st.get("last_death")
                            and held_name in MELEE_WEAPON_ORDER and mob_name in LONE_COUNTERKILL_MOBS
                            and isinstance(mob_id, int) and len(nearby_hostiles) == 1
                            and nearby_hostiles[0].get("id") == mob_id and not nearby_hostiles[0].get("blocked_by_wall")
                            and time.monotonic() + 1.25 < deadline
                        ):
                            strike = self.strike_cornered_hostile(env, mob_id, counts, held_name, status=st_fl)
                            if strike is not None:
                                actions.append(strike)
                                st_fl = env._safe_status({"ready": False})
                        nearest_raw = st_fl.get("nearest_hostile")
                        nearest = nearest_raw if isinstance(nearest_raw, dict) else {}
                        nearest_id = nearest.get("id")
                        nearest_dist = nearest.get("dist")
                        health_after = st_fl.get("health")
                        low_health_after = isinstance(health_after, (int, float)) and health_after <= 10.0
                        ranged_still_exposed = (
                            isinstance(nearest_id, int)
                            and isinstance(nearest_dist, (int, float))
                            and (
                                (check_ranged_guard and low_health_after and nearest_id == mob_id and nearest_dist <= 12.0)
                                or (
                                    (check_ranged_guard or (check_melee_guard and low_health_after))
                                    and st_fl.get("blocking") and nearest_dist <= 3.5
                                    and _clean_block_name(nearest.get("name")) in RANGED_MOBS
                                )
                            )
                        )
                        same_close_pursuit = (
                            trigger.get("source") == "pre_hurt_melee" and 0 < mob_dist <= 1.25
                            and nearest_id == mob_id and nearest.get("blocked_by_wall") is not True
                        )
                        effects = st_fl.get("effects")
                        offhand = st_fl.get("offhand")
                        # A short retreat can leave the shield up while the same skeleton is still within reach.
                        wither_pursuit = (
                            mob_name == "wither_skeleton" and nearest_id == mob_id
                            and _clean_block_name(nearest.get("name")) == "wither_skeleton"
                            and nearest.get("blocked_by_wall") is not True
                            and str(st_fl.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
                            and st_fl.get("ready") is True and st_fl.get("alive") is True and st_fl.get("on_ground") is True
                            and isinstance(health_after, (int, float)) and health_after > 0
                            and isinstance(offhand, dict) and _clean_block_name(offhand.get("name")) == "shield"
                            and isinstance(effects, list)
                            and any(isinstance(effect, dict) and _clean_block_name(effect.get("name")) == "wither" for effect in effects)
                        )
                        critical_pursuit = (
                            same_close_pursuit and isinstance(health_after, (int, float)) and 0 < health_after <= 4.0
                            and st_fl.get("on_ground") is True
                        )
                        guard_range = 5.5 if critical_pursuit else 3.5
                        melee_still_close = (
                            check_melee_guard and st_fl.get("blocking")
                            and (low_health_after or sealed or same_close_pursuit or wither_pursuit)
                            and isinstance(nearest_id, int) and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                            and isinstance(nearest_dist, (int, float)) and 0 <= nearest_dist <= guard_range
                        )
                        if (
                            (not flee_ok or ranged_still_exposed or melee_still_close)
                            and not _combat_status_unavailable(st_fl)
                            and not st_fl.get("in_water")
                            and not st_fl.get("in_lava")
                            and st_fl.get("last_death") == st.get("last_death")
                            and st_fl.get("dimension") == st.get("dimension")
                        ):
                            guard_target = nearest_id if ranged_still_exposed or melee_still_close else mob_id
                            if not latched_guard:
                                latched_guard = self._latch_shield_at_threat(
                                    env, int(guard_target) if guard_target is not None and not check_enderman_guard else None,
                                )
                            if melee_still_close:
                                actions.append({"action": "guard_melee_after_retreat", "ok": latched_guard, "entity_id": guard_target})
                            elif check_melee_guard and ranged_still_exposed:
                                actions.append({"action": "guard_ranged_after_melee_retreat", "ok": latched_guard, "entity_id": guard_target})
                    elif (
                        not flee_ok and fl.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                        and not fl.get("respawned") and not has_shield and has_weapon
                        and mob_name in LONE_COUNTERKILL_MOBS and isinstance(mob_id, int)
                        and len(nearby_hostiles) == 1 and time.monotonic() < deadline
                    ):
                        strike = self.strike_cornered_hostile(env, mob_id, counts, held_name)
                        if strike is not None:
                            actions.append(strike)

        # 4) Check for a surviving attacker before handing back control or eating.
        has_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
        st_post = env._safe_status({"ready": False} if mode == "life_unavailable" else st)
        if mode in {"water_surface", "water_surface_abort"} and self._combat_life_lost(st, st_post):
            mode = "life_unavailable"
            latched_guard = False
        nh_post = st_post.get("nearest_hostile")
        retreat_hp = st_post.get("health")
        retreats = [a for a in actions if a["action"] == "flee" and not a.get("respawned")]
        night_pursuit = (
            enclosure == "surface" and st_post.get("is_day") is False
            and trigger.get("source") == "pre_hurt_melee" and mob_name in PRE_HURT_MELEE_MOBS
            and 0 < mob_dist <= 3.5
            and isinstance(retreat_hp, (int, float)) and retreat_hp > 12.0
            and any(a["ok"] and isinstance(a.get("moved"), (int, float)) and a["moved"] >= 4.0 for a in retreats)
        )
        critical_creeper_pursuit = (
            mode == "creeper_evade" and isinstance(retreat_hp, (int, float)) and 0 < retreat_hp <= 4.0
            and any(
                action["action"] in {"retreat_from_creeper", "creeper_followup_retreat"} and action["ok"]
                for action in actions
            )
        )
        longer_critical_retreat = (
            not has_shield and not has_weapon and isinstance(retreat_hp, (int, float)) and 0 < retreat_hp <= 2.0
            and not st_post.get("busy")
            and any(a["ok"] and isinstance(a.get("moved"), (int, float)) and a["moved"] >= 4.0 for a in retreats)
        )
        critical_melee_pursuit = (
            mode == "cover_or_flee" and (enclosure == "surface" or (enclosure == "open_cave" and longer_critical_retreat))
            and str(st_post.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and trigger.get("source") == "pre_hurt_melee" and mob_name in PRE_HURT_MELEE_MOBS
            and isinstance(mob_id, int) and mob_id == trig_id and 0 < mob_dist <= 3.5
            and isinstance(retreat_hp, (int, float)) and 0 < retreat_hp <= 4.0 and not st_post.get("on_fire")
            and isinstance(nh_post, dict) and nh_post.get("id") == mob_id
            and any(a["ok"] and isinstance(a.get("moved"), (int, float)) and a["moved"] >= 1.5 for a in retreats)
        )
        extended_melee_pursuit = critical_melee_pursuit and longer_critical_retreat
        critical_followup = critical_creeper_pursuit or critical_melee_pursuit
        ranked_followup = night_pursuit or critical_followup
        followup_budget, movement_budget = (1.5, 0.8)
        followup_range = 0.0
        if night_pursuit:
            followup_range = 8.0
            followup_budget, movement_budget = (4.1, 3.5)
        elif critical_followup:
            followup_range = 5.5 if critical_creeper_pursuit else 3.5
            if extended_melee_pursuit:
                followup_range = 8.0
            followup_budget, movement_budget = (2.2, 1.5)
        elif isinstance(retreat_hp, (int, float)) and 0.0 < retreat_hp <= 12.0:
            followup_range = 3.5 if retreat_hp <= 10.0 else 1.5
        elif (
            isinstance(retreat_hp, (int, float)) and retreat_hp > 12.0
            and trigger.get("source") == "pre_hurt_melee" and mob_name in PRE_HURT_MELEE_MOBS
            and 0 < mob_dist <= 3.5
            and isinstance(nh_post, dict) and nh_post.get("id") == mob_id
            and any(
                isinstance(a.get("moved"), (int, float)) and 0 < a["moved"] <= 2.0
                and retreat_made_progress(a["ok"], a["moved"], a.get("reason"))
                for a in retreats
            )
        ):
            # A short step does not disengage when the same attacker stays in melee reach.
            followup_range = min(mob_dist, 2.0)
        if (
            (mode in {"cover_or_flee", "flee"} or critical_creeper_pursuit) and not has_shield and not has_weapon
            and followup_range > 0
            and (critical_creeper_pursuit or any(retreat_made_progress(a["ok"], a.get("moved"), a.get("reason")) for a in retreats))
            and isinstance(nh_post, dict) and _clean_block_name(nh_post.get("name")) in PRE_HURT_MELEE_MOBS
            and isinstance(nh_post.get("id"), int) and self._entity_dist(st_post.get("pos"), nh_post) <= followup_range
            and st_post.get("on_ground") is True and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and st_post.get("last_death") == st.get("last_death") and st_post.get("dimension") == st.get("dimension")
            and time.monotonic() + followup_budget < deadline
        ):
            target_id = nh_post["id"]
            live_hostiles = self._find_live_hostiles(env, radius=12 if ranked_followup else 5, timeout_s=0.6)
            target = next((entity for entity in live_hostiles if entity.get("id") == target_id), {})
            target_pos = target.get("pos")
            renewed_blast_threat = critical_followup and any(
                _clean_block_name(entity.get("name")) == "creeper" and self._entity_dist(st_post.get("pos"), entity) <= 5.5
                for entity in live_hostiles
            )
            start_dist = self._entity_dist(st_post.get("pos"), target)
            if (
                st_post.get("pos") and not renewed_blast_threat
                and time.monotonic() + movement_budget < deadline
                and isinstance(target_pos, (list, tuple)) and len(target_pos) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in target_pos[:3])
                and target.get("blocked_by_wall") is not True
                and start_dist <= followup_range
            ):
                if ranked_followup:
                    retreat, st_post = self.ranked_melee_retreat(
                        env, st_post, target_id, critical=critical_followup,
                        verify_melee_clearance=critical_melee_pursuit, live_hostiles=live_hostiles,
                        melee_clearance=followup_range,
                    )
                    retreat = {"entity_id": target_id, **retreat}
                else:
                    retreat = env.step_away_from_melee(
                        st_post, target_pos, deadline=min(deadline, time.monotonic() + 1.5), target_id=target_id,
                    )
                    st_post = env._safe_status(st_post)
                nh_post = st_post.get("nearest_hostile")
                actions.append({"action": "followup_melee_retreat", **retreat})
        if mode == "creeper_evade" and (
            st_post.get("alive") is False
            or any(st_post.get(key) != st.get(key) for key in ("last_death", "dimension"))
        ):
            for action in actions:
                if action["action"] in {
                    "retreat_from_creeper", "creeper_followup_retreat", "creeper_intruded_flee", "followup_melee_retreat",
                    "place_blast_cover",
                }:
                    action.update(ok=False, respawned=True)
        if (
            mode == "counter_killed" and float(st_post.get("health") or 20.0) <= 8.0
            and not self._is_falling_fast(st_post, pos)
            and isinstance(nh_post, dict) and nh_post.get("name") and nh_post.get("id") != mob_id
            and self._entity_dist(st_post.get("pos"), nh_post) <= 5.0
            and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and all(st_post.get(key) == st.get(key) for key in ("last_death", "dimension"))
        ):
            survivor_id = nh_post.get("id")
            if has_shield and st_post.get("on_ground") is False and time.monotonic() + 0.35 < deadline:
                latched_guard = self._latch_shield_at_threat(env, survivor_id)
            survivor_pos = nh_post.get("pos")
            if survivor_pos is None and survivor_id is not None and time.monotonic() + 1.15 < deadline:
                survivors = self._find_live_hostiles(env, radius=6, timeout_s=0.5)
                survivor = next((hostile for hostile in survivors if hostile.get("id") == survivor_id), {})
                survivor_pos = survivor.get("pos")
            retreat = {"ok": False, "reason": "time_budget"}
            if time.monotonic() + 1.15 < deadline:
                retreat = env.flee(
                    survivor_pos, distance=12.0, urgent=True, wait_for_ground=True,
                    allow_second_leg=time.monotonic() + 3.5 < deadline,
                )
            actions.append({"action": "low_hp_abort_flee", "entity_id": survivor_id, **retreat})
            mode = "low_hp_disengage"
            st_post = env._safe_status(st_post)
            nearest = st_post.get("nearest_hostile")
            pursuing = (
                isinstance(nearest, dict) and isinstance(nearest.get("id"), int)
                and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                and not nearest.get("blocked_by_wall") and self._entity_dist(st_post.get("pos"), nearest) <= 5.5
            )
            life_unavailable = (
                retreat.get("respawned") or st_post.get("ready") is False or st_post.get("alive") is False
                or any(st_post.get(key) != st.get(key) for key in ("last_death", "dimension"))
            )
            if life_unavailable:
                mode = "life_unavailable"
            same_dry_life = not life_unavailable and not st_post.get("in_water") and not st_post.get("in_lava")
            if not same_dry_life or (retreat.get("ok") and not pursuing):
                latched_guard = False
            elif has_shield and time.monotonic() + 0.35 < deadline:
                target_id = nearest["id"] if pursuing else survivor_id
                latched_guard = self._latch_shield_at_threat(env, target_id)
                actions.append({"action": "guard_surviving_melee", "ok": latched_guard, "entity_id": target_id})
        hp_post = float(st_post.get("health") or 20.0)
        food_post = int(st_post.get("food") or 20)
        nh_post = st_post.get("nearest_hostile")
        nh_post_name = _clean_block_name(nh_post.get("name")) if isinstance(nh_post, dict) and nh_post.get("name") else ""
        nh_post_d = float(nh_post.get("dist", 99.0)) if nh_post_name else 99.0
        surviving_shooter = nh_post_name in RANGED_MOBS and nh_post_d <= 12.0
        surviving_melee = nh_post_name in PRE_HURT_MELEE_MOBS and nh_post_d <= 3.5
        life_lost = self._combat_life_lost(st, st_post)
        if (
            mode == "counter_killed" and not life_lost and has_shield and st_post.get("blocking")
            and (surviving_shooter or surviving_melee)
            and isinstance(nh_post.get("id"), int) and nh_post["id"] != mob_id
            and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and st_post.get("last_death") == st.get("last_death")
            and st_post.get("dimension") == st.get("dimension")
        ):
            survivor_id = nh_post["id"]
            latched_guard = self._latch_shield_at_threat(env, survivor_id)
            action = "guard_surviving_shooter" if surviving_shooter else "guard_surviving_melee"
            actions.append({"action": action, "ok": latched_guard, "entity_id": survivor_id})
        # Eating takes about two seconds. Leave room to confirm status and release controls.
        food_start_deadline = deadline - 2.5
        food_melee_clearance = 6.0
        if extended_melee_pursuit and isinstance(nh_post, dict) and nh_post.get("id") == mob_id:
            food_melee_clearance = followup_range
        if (
            env.post_combat_food
            and not life_lost
            and mode not in {"falling", "life_unavailable"}
            and not latched_guard
            and time.monotonic() < food_start_deadline
            and st_post.get("ready", True)
            and st_post.get("alive") is not False
            and st_post.get("on_ground") is not False
            and not st_post.get("in_water")
            and not st_post.get("in_lava")
            and nh_post_d > food_melee_clearance
            and not ((nh_post_name in RANGED_MOBS or nh_post_name == "ghast") and nh_post_d <= 16.0)
            and food_post < 20
            and (hp_post < 16.0 or food_post <= 15)
        ):
            counts_now, _, _ = env._inventory_counts_and_shield()
            best_food = next((f for f in SAFE_FOOD_PRIORITY if counts_now.get(f, 0) > 0), None)
            if best_food and time.monotonic() < food_start_deadline:
                try:
                    env.bot.call("use_item", {"item": best_food})
                    actions.append({"action": "post_combat_eat", "item": best_food, "ok": True})
                    st_post = env.bot.status()
                    life_lost = self._combat_life_lost(st, st_post)
                except ApiError:
                    pass

        if life_lost:
            latched_guard = False
            if mode != "creeper_evade" and _combat_status_unavailable(st_post):
                mode = "life_unavailable"
        final_health = st_post.get("health")
        release_unready_shield = (
            st_post.get("ready") is False and st_post.get("alive") is True and st_post.get("blocking") is True
            and isinstance(final_health, (int, float)) and final_health > 0
            and not any(st_post.get(key) for key in ("in_water", "submerged", "in_lava"))
            and all(st_post.get(key) == st.get(key) for key in ("last_death", "dimension"))
        )
        summary = (
            f"遭遇 {mob_name or 'hostile'}(d={round(mob_dist, 1)}m, trigger={trigger.get('source')})，"
            f"已执行 0-LLM 小脑接管[{mode}]，当前 HP={st_post.get('health')} pos={st_post.get('pos')}"
        )
        if mode == "falling":
            summary = f"快速下坠期间已暂停反击，当前 HP={st_post.get('health')} pos={st_post.get('pos')}"
        if life_lost or mode == "life_unavailable":
            summary = "交战期间角色已死亡或状态尚未就绪，等待刷新后重新规划"
        return {
            **({"ok": False} if life_lost or mode == "life_unavailable" else {}),
            "handled": bool(actions) and not life_lost and mode != "life_unavailable",
            "mode": mode,
            "summary": summary,
            "actions": actions,
            "status_after": st_post,
            "requires_replan": True,
            "latched_guard": latched_guard,
            **({"release_unready_shield": True} if release_unready_shield else {}),
        }


class CombatControllerSlot:
    """Pluggable L4 slot enforcing entry `/stop` cleanup and exit `/posture {}` reset."""

    def __init__(self, controller: CombatController | None = None) -> None:
        self.controller: CombatController = controller or SubcorticalInstinctController()

    def register(self, controller: CombatController) -> None:
        self.controller = controller

    def handle(self, env: CombatEnv, trigger: ThreatTrigger) -> CombatOutcome:
        # 1. Entry rule: clear residual pathfinder/mining lock & stuck keys
        for cleanup_fn in (
            lambda: env.bot.call("stop", {}),
            lambda: env.bot.call("control", {"wait": False}),
        ):
            try:
                cleanup_fn()
            except Exception as error:
                if _is_control_signal(error):
                    raise

        outcome: CombatOutcome = {
            "handled": False,
            "mode": "none",
            "summary": "",
            "actions": [],
            "status_after": {},
            "requires_replan": True,
            "latched_guard": False,
        }
        try:
            outcome = self.controller.on_encounter(env, trigger)
            return outcome
        except _CombatFortificationInterrupted as interrupted:
            outcome = {
                "handled": False,
                "mode": "life_unavailable" if interrupted.life_unavailable else "combat_timeout",
                "summary": (
                    "战斗工事期间角色状态已变化，请按最新状态重新规划" if interrupted.life_unavailable
                    else "战斗工事超时，已尝试停止原动作，请按最新状态重新规划"
                ),
                "actions": [],
                "status_after": interrupted.status,
                "requires_replan": True,
                "latched_guard": False,
            }
            return outcome
        finally:
            # 2. Exit rule: NEVER leave shield/sneak/jump stuck at 30% speed unless explicitly latched
            st_end = outcome.get("status_after")
            unavailable = (
                outcome.get("mode") == "life_unavailable"
                or (isinstance(st_end, dict) and _combat_status_unavailable(st_end))
            )
            resets: list[tuple[str, dict[str, Any]]] = []
            if not unavailable and not outcome.get("latched_guard", False):
                wet_submerged = isinstance(st_end, dict) and bool(st_end.get("in_water")) and (
                    st_end.get("on_ground") is False or _water_requires_surface(st_end, oxygen_bubbles=16)
                )
                posture_payload: dict[str, Any] = {"shield": False, "sneak": False, "jump": True} if wet_submerged else {}
                resets.extend((("control", {"wait": False}), ("posture", posture_payload)))
            elif outcome.get("release_unready_shield"):
                resets.append(("posture", {}))
            for action, payload in resets:
                try:
                    env.bot.call(action, payload)
                except Exception as error:
                    if _is_control_signal(error):
                        raise

