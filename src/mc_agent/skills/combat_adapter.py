"""Adapter bridging `mc_agent.bot.BotSession` to `combat_v1` tactical combat skills.

Preserves single-token exclusivity (never creates `SimpleBotClient` or calls `/auth/login`),
enforces `VoidGeofence` on both `(x, z)` and `entity_id` chases, lets `GenerationGuard`
preemptions and critical session errors unwind cleanly without extra cleanup calls,
caps long-running skills (such as 180s `pillar`) to an external hard deadline, and
resets lingering shield/sneak/jump postures on normal exit.
"""

from __future__ import annotations

import math
import time
from typing import Any

from ..bot import BotSession
from ..errors import ApiError as McApiError, AuthError, BotNotReadyError, DeathLoopCooldownError
from ..harness.geofence import VoidGeofence
from . import combat_v1
from .combat_env import CombatEnv, _TerrainProbeExpired

VALID_COMBAT_MODES = frozenset(
    {
        "auto",
        "melee",
        "rush",
        "creeper",
        "skeleton_shield",
        "skeleton_bow",
        "pillar",
        "crit",
        "mcrit",
        "wtap",
        "gap_foot",
        "gap_head",
    }
)

_RANGED_MOBS = frozenset({"skeleton", "stray", "bogged", "pillager", "blaze", "ghast"})
_ARROWS = frozenset({"arrow", "spectral_arrow", "tipped_arrow"})


class _CombatBudgetExceeded(BaseException):
    """Unwind `combat_v1` loops (including 180s `pillar`) when the external hard budget expires."""


class BotSessionCombatAdapter:
    """Thin protocol adapter presenting `SimpleBotClient`'s 4-method surface over `BotSession`."""

    def __init__(
        self,
        bot: BotSession,
        *,
        geofence: VoidGeofence | None = None,
        hard_deadline_mono: float | None = None,
    ) -> None:
        self._bot = bot
        self._geofence = geofence
        self._hard_deadline_mono = hard_deadline_mono
        self._last_attack_at: float | None = None
        self._last_hand_equip_at: float | None = None
        self._melee_weapon: str = ""
        self._entity_xz: dict[int, tuple[float, float]] = {}
        self._unwinding: bool = False
        self.blocked_geofence_calls: int = 0

    def _check_budget(self) -> float | None:
        if self._hard_deadline_mono is None:
            return None
        rem = self._hard_deadline_mono - time.monotonic()
        if rem <= 0.15:
            self._unwinding = True
            raise _CombatBudgetExceeded()
        return rem

    def melee_ready_in(self, *, attack_lead_s: float = 0.0) -> float:
        anchors = [t for t in (self._last_attack_at, self._last_hand_equip_at) if t is not None]
        if not anchors:
            return 0.0
        return max(
            0.0,
            max(anchors)
            + combat_v1.weapon_cooldown_s(self._melee_weapon)
            + combat_v1.ATTACK_MARGIN_S
            - time.monotonic()
            - attack_lead_s,
        )

    def prepare_melee(self, *, attack_lead_s: float = 0.0, wait: bool = True) -> float:
        if not math.isfinite(attack_lead_s) or attack_lead_s < 0:
            raise ValueError("attack_lead_s must be finite and non-negative")
        inv = self.call("inventory", method="GET")
        held = inv.get("held") or {}
        weapon = str(held.get("name") or "")
        now = time.monotonic()
        if weapon != self._melee_weapon:
            self._melee_weapon = weapon
            self._last_hand_equip_at = now
        delay = self.melee_ready_in(attack_lead_s=attack_lead_s)
        if delay and wait:
            rem = self._check_budget()
            if rem is not None and delay > rem:
                self._unwinding = True
                raise _CombatBudgetExceeded()
            time.sleep(delay)
        return delay

    def melee_control(self, body: dict[str, Any], *, prepared: bool = False) -> dict[str, Any]:
        if not prepared:
            self.prepare_melee()
        body = dict(body)
        body.setdefault(
            "hit_interval_ms",
            math.ceil((combat_v1.weapon_cooldown_s(self._melee_weapon) + combat_v1.ATTACK_MARGIN_S) * 1000),
        )
        return self.call("control", body)

    def call(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        endpoint = path.strip("/")
        # During preemption / hard-budget unwind, short-circuit skill `finally` cleanup calls
        # so GenerationGuard or our outer handler can immediately take over.
        if self._unwinding:
            return {"ok": True}
        rem = self._check_budget()
        eff_timeout = min(float(timeout), rem) if rem is not None else float(timeout)

        if endpoint == "move_to" and self._geofence is not None and self._geofence.enabled and isinstance(body, dict):
            x, z = body.get("x"), body.get("z")
            if (x is None or z is None) and body.get("entity_id") is not None:
                ent_xz = self._entity_xz.get(int(body["entity_id"]))
                if ent_xz is not None:
                    x, z = ent_xz
            if x is not None and z is not None:
                blocked = self._geofence.check_point(float(x), float(z), action="mc_skill_combat")
                if blocked is not None:
                    self.blocked_geofence_calls += 1
                    raise combat_v1.ApiError("void_geofence_blocked", blocked["error"]["message"], blocked["error"])

        try:
            result = self._bot.call(endpoint, body, method=method, timeout_s=eff_timeout)
        except (AuthError, DeathLoopCooldownError, BotNotReadyError):
            self._unwinding = True
            raise
        except McApiError as exc:
            raise combat_v1.ApiError(exc.code, exc.message, exc.context) from None
        except BaseException:
            # Includes GenerationGuard's ReflexPreempted / _RecoveryBudgetExceeded / KeyboardInterrupt.
            self._unwinding = True
            raise

        if endpoint == "get_entities" and isinstance(result, dict):
            for ent in result.get("entities") or []:
                if isinstance(ent, dict) and isinstance(ent.get("id"), int):
                    pos = ent.get("pos") or []
                    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                        self._entity_xz[int(ent["id"])] = (float(pos[0]), float(pos[2]))

        if endpoint == "equip" and (body or {}).get("slot", "hand") == "hand":
            self._last_hand_equip_at = time.monotonic()
        if endpoint == "control" and (body or {}).get("hit"):
            extra_s = 0.0
            if (body or {}).get("wait") is False:
                count = int((body or {}).get("hit", 1))
                interval = result.get("hit_interval_ms", (body or {}).get("hit_interval_ms"))
                period = float(interval) / 1000 if interval is not None else combat_v1.weapon_cooldown_s(self._melee_weapon)
                extra_s = max(0, count - 1) * period
            self._last_attack_at = time.monotonic() + extra_s
        return result


def _resolve_auto_mode(
    adapter: BotSessionCombatAdapter,
    norm_filter: str | None,
    ent: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Choose a tactical combat mode from live nearby hostiles and inventory gear."""
    if ent is None:
        return ("melee", norm_filter)
    mob = str(ent.get("name") or "").strip().lower().removeprefix("minecraft:")
    if mob == "creeper":
        return ("creeper", "creeper")

    inv = adapter.call("inventory", method="GET")
    items = inv.get("items") or []
    offhand_name = str((inv.get("offhand") or {}).get("name") or "")
    has_shield = offhand_name == "shield" or any(
        isinstance(it, dict) and str(it.get("name") or "") == "shield" for it in items
    )
    has_bow = any(isinstance(it, dict) and str(it.get("name") or "") == "bow" for it in items)
    has_arrow = any(isinstance(it, dict) and "arrow" in str(it.get("name") or "") for it in items)

    if mob in _RANGED_MOBS:
        if has_shield:
            return ("rush", mob)
        if mob == "skeleton" and has_bow and has_arrow:
            return ("skeleton_bow", mob)
        return ("melee", mob)
    return ("rush" if has_shield else "melee", mob or norm_filter)


def _combat_position(raw: Any) -> tuple[float, float, float] | None:
    if isinstance(raw, (list, tuple)) and len(raw) == 3 and all(
        type(coord) in (float, int) and math.isfinite(coord) for coord in raw
    ):
        return (raw[0], raw[1], raw[2])
    return None


def _blaze_error(
    result: dict[str, Any], code: str, message: str,
) -> dict[str, Any]:
    return {**result, "error": {"code": code, "message": message}}


def _fight_nether_blaze(
    adapter: BotSessionCombatAdapter,
    env: CombatEnv,
    status: dict[str, Any],
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    """Engage from verified footing without chasing a blaze off a bridge."""
    target_id = target.get("id") if target else None
    result: dict[str, Any] = {"target_id": target_id, "killed": False}
    if type(target_id) is not int:
        return _blaze_error(result, "no_target", "未观察到可交战的烈焰人，请从掩体后重新扫描。")
    target_pos = _combat_position(target.get("pos"))
    pos = _combat_position(status.get("pos"))
    life = (status.get("dimension"), status.get("last_death"))
    if pos is None or target_pos is None:
        return _blaze_error(result, "combat_position_unconfirmed", "烈焰人或脚下站位未确认，请复查敌情和站位。")
    if target.get("blocked_by_wall") is True:
        return _blaze_error(result, "blocked_by_wall", "烈焰人被挡住，请等它进入视野或从已确认安全的掩体接战。")

    inventory = adapter.call("inventory", method="GET", timeout=0.8)
    counts, _, _ = env._inventory_counts_and_shield(inventory)
    ammo = sum(counts.get(name, 0) for name in _ARROWS)
    ranged = next((name for name in ("bow", "crossbow") if ammo and counts.get(name)), None)

    def can_fight_from_here(current: dict[str, Any]) -> bool:
        point = _combat_position(current.get("pos"))
        return bool(
            (current.get("dimension"), current.get("last_death")) == life
            and current.get("alive") is True and current.get("ready") is True
            and current.get("on_ground") is True and point is not None and math.dist(pos, point) <= 0.25
            and not any(current.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
        )

    try:
        if not can_fight_from_here(status) or not env.has_safe_footing(pos, deadline=time.monotonic() + 0.6):
            return _blaze_error(result, "unsafe_landing", "脚下支撑未确认，请在有实心支撑的位置接战。")
        if not ranged and (
            math.dist(pos, target_pos) > 2.5 or math.floor(pos[1] + 0.05) != math.floor(target_pos[1] + 0.05)
            or not env.has_safe_footing(target_pos, deadline=time.monotonic() + 0.6)
        ):
            return _blaze_error(
                result, "blaze_requires_ranged",
                "缺少可用弓箭，且烈焰人尚未在同层、有支撑的近战距离内。留在掩体后等它靠近。",
            )
    except _TerrainProbeExpired:
        return _blaze_error(result, "combat_footing_unconfirmed", "站位支撑读取未完成，请在确认实心落脚后接战。")

    if not ranged:
        if not can_fight_from_here(adapter.call("status", method="GET", timeout=0.6)):
            return _blaze_error(result, "combat_position_changed", "接战前站位或生命已变化，请复查。")
        remaining = adapter._check_budget()
        if remaining is not None and remaining < 1.85:
            return _blaze_error(result, "time_budget", "剩余战斗时间不足以完成单次近战。")
        attack = adapter.call("attack", {
            "entity_id": target_id, "chase": False, "max_hits": 1,
            "timeout": 1500, "feedback": True, "stop_on": ["hurt", "death"],
        }, timeout=1.85)
        result.update(attack, strategy="stationary_melee", chase=False)
        if not result.get("killed") and not (type(attack.get("hits")) in (int, float) and attack["hits"] > 0):
            return _blaze_error(result, "attack_zero_hits", "原地挥击未确认命中，请等烈焰人进入实际击距。")
        return result

    shots: list[dict[str, Any]] = []
    result.update(strategy="ranged", weapon=ranged, shots=0, hits_landed=0, shot_receipts=shots)
    for shot_index in range(min(ammo, 4)):
        if shot_index:
            entities = adapter.call("get_entities", {"radius": 32, "types": ["blaze"]}, timeout=0.6).get("entities") or []
            live = next((entry for entry in entities if isinstance(entry, dict) and entry.get("id") == target_id), None)
            if live is None or live.get("blocked_by_wall") is True:
                result["end_reason"] = "target_missing" if live is None else "blocked_by_wall"
                break
        if not can_fight_from_here(adapter.call("status", method="GET", timeout=0.6)):
            return _blaze_error(result, "combat_position_changed", "射击前站位或生命已变化，请复查。")
        remaining = adapter._check_budget()
        if remaining is not None and remaining < 1.7:
            if shots:
                break
            return _blaze_error(result, "time_budget", "剩余战斗时间不足以完成蓄力。")
        try:
            shot = adapter.call("shoot", {
                "weapon": ranged, "entity_id": target_id,
                "charge_ms": 1000 if ranged == "bow" else 1250, "stop_on": ["hurt", "death"],
            }, timeout=2.5)
        except combat_v1.ApiError as error:
            return _blaze_error(result, error.code, error.message)
        shots.append(shot)
        if shot.get("fired") is not True:
            return _blaze_error(result, "shoot_unconfirmed", "未确认射出箭矢，请复查弓、弹药和视线。")
        result["shots"] += 1
        before, after = shot.get("target_health_before"), shot.get("target_health_after")
        if type(before) in (int, float) and type(after) in (int, float) and after < before:
            result["hits_landed"] += 1
        if shot.get("killed") is True or (type(after) in (int, float) and after <= 0):
            result["killed"] = True
            break
        if shot.get("interrupted_by"):
            return _blaze_error(result, "interrupted", "射击被打断，请复查当前威胁。")
    return result


def _reset_combat_posture(bot: BotSession) -> None:
    """Clear lingering shield/sneak/jump posture (e.g. left by skel shield or creeper fallback)."""
    for endpoint, body in (
        ("stop", {}),
        ("posture", {"sneak": False, "shield": False, "jump": False}),
    ):
        try:
            bot.call(endpoint, body, timeout_s=2.0, allow_reauth=False)
        except Exception:
            pass


def run_combat_skill(
    bot: BotSession,
    *,
    mode: str = "auto",
    target_name: str | None = None,
    max_time_s: float = 20.0,
    pillar_height: int = 3,
    geofence: VoidGeofence | None = None,
    episode_deadline_mono: float | None = None,
) -> dict[str, Any]:
    """Execute a bounded `combat_v1` tactical skill on an existing `BotSession`."""
    norm_mode = str(mode or "auto").strip().lower()
    if norm_mode not in VALID_COMBAT_MODES:
        return {
            "ok": False,
            "requires_replan": True,
            "error": {
                "code": "bad_args",
                "message": f"未知战斗模式 mode={mode!r}，可选：{sorted(VALID_COMBAT_MODES)}",
            },
        }

    eff_time = max(3.0, min(30.0, float(max_time_s)))
    now = time.monotonic()
    if episode_deadline_mono is not None:
        rem_ep = episode_deadline_mono - now - 4.0
        if rem_ep <= 2.0:
            return {
                "ok": False,
                "requires_replan": False,
                "error": {
                    "code": "episode_deadline_imminent",
                    "message": "本集剩余时间不足 6s，已跳过战术战斗以避免超时越界。",
                },
            }
        eff_time = min(eff_time, max(2.0, rem_ep))

    hard_deadline = now + eff_time + 1.5
    adapter = BotSessionCombatAdapter(bot, geofence=geofence, hard_deadline_mono=hard_deadline)
    resolved_mode = norm_mode
    resolved_target = target_name
    started = now
    stationary_blaze = False

    try:
        target = None
        norm_filter = combat_v1.normalize_mob_name(target_name) if resolved_mode in {"auto", "melee", "rush"} else None
        if resolved_mode == "auto" or (resolved_mode in {"melee", "rush"} and norm_filter in {None, "blaze"}):
            target = combat_v1.nearest_hostile_entity(
                adapter, radius=32 if resolved_mode == "rush" else 24, name_filter=norm_filter,
            )  # type: ignore[arg-type]
            observed_name = str(target.get("name") or "").removeprefix("minecraft:") if target else norm_filter
            if observed_name == "blaze":
                status = adapter.call("status", method="GET", timeout=0.6)
                if str(status.get("dimension") or "").removeprefix("minecraft:") in {"the_nether", "nether"}:
                    stationary_blaze = True
                    resolved_target = "blaze"
                    raw = _fight_nether_blaze(adapter, CombatEnv(bot, geofence=geofence), status, target)
                    receipt = {
                        "ok": "error" not in raw, "mode": resolved_mode, "target_name": resolved_target,
                        "elapsed_s": round(time.monotonic() - started, 2), "result": raw,
                    }
                    if "error" in raw:
                        receipt.update(requires_replan=True, error=raw["error"])
                    return receipt
        if resolved_mode == "auto":
            resolved_mode, resolved_target = _resolve_auto_mode(adapter, norm_filter, target)

        if resolved_mode == "melee":
            raw = combat_v1.action_melee(adapter, target_name=resolved_target, max_time_s=eff_time)  # type: ignore[arg-type]
        elif resolved_mode == "rush":
            raw = combat_v1.action_rush(adapter, target_name=resolved_target, max_time_s=eff_time)  # type: ignore[arg-type]
        elif resolved_mode == "creeper":
            raw = combat_v1.action_kite_creeper(adapter, max_time_s=eff_time)  # type: ignore[arg-type]
        elif resolved_mode == "skeleton_shield":
            raw = combat_v1.action_skeleton_duel(adapter, strategy="shield", max_time_s=eff_time)  # type: ignore[arg-type]
        elif resolved_mode == "skeleton_bow":
            raw = combat_v1.action_skeleton_duel(adapter, strategy="bow", max_time_s=eff_time)  # type: ignore[arg-type]
        elif resolved_mode == "pillar":
            h = max(1, min(4, int(pillar_height)))
            raw = combat_v1.action_pillar_up_and_clear(adapter, height=h)  # type: ignore[arg-type]
        elif resolved_mode == "crit":
            raw = combat_v1.action_jump_crit(adapter, delay_s=0.35, moving=False)  # type: ignore[arg-type]
        elif resolved_mode == "mcrit":
            raw = combat_v1.action_jump_crit(adapter, delay_s=0.35, moving=True)  # type: ignore[arg-type]
        elif resolved_mode == "wtap":
            raw = combat_v1.action_wtap_knockback(adapter)  # type: ignore[arg-type]
        elif resolved_mode == "gap_foot":
            raw = combat_v1.action_gap_slash(adapter, mode="foot", max_time_s=eff_time)  # type: ignore[arg-type]
        else:  # gap_head
            raw = combat_v1.action_gap_slash(adapter, mode="head", max_time_s=eff_time)  # type: ignore[arg-type]

        receipt = {
            "ok": not bool(raw.get("error")),
            "mode": resolved_mode,
            "target_name": resolved_target,
            "elapsed_s": round(time.monotonic() - started, 2),
            "result": raw,
        }
        connected = raw.get("hits_connected", raw.get("hits"))
        if (
            receipt["ok"] and resolved_mode in {"melee", "rush"} and raw.get("end_reason") == "timeout"
            and not raw.get("killed") and type(connected) in (int, float) and connected == 0
        ):
            # The reactive loop also uses `hits` for swings. Keep those attempts visible
            # without counting them as progress in the actor's repeat guard.
            attempts = raw.get("hits")
            if type(attempts) in (int, float) and attempts > 0:
                receipt["result"] = {"swings": attempts, **raw, "hits": 0}
            receipt.update(
                ok=False, requires_replan=True,
                error={
                    "code": "time_budget",
                    "message": f"战术战斗 {resolved_mode} 在时限内未命中目标。请复查敌人与落脚点，改路线、换掩体或撤退。",
                },
            )
        return receipt
    except _CombatBudgetExceeded:
        if not stationary_blaze:
            _reset_combat_posture(bot)
        return {
            "ok": False,
            "mode": resolved_mode,
            "target_name": resolved_target,
            "elapsed_s": round(time.monotonic() - started, 2),
            "requires_replan": True,
            "error": {
                "code": "time_budget",
                "message": f"战术战斗 {resolved_mode} 达到 {round(eff_time, 1)}s 硬预算上限已安全中止，请根据当前血量与敌情决定续战或撤离。",
            },
        }
    except combat_v1.ApiError as exc:
        if not stationary_blaze:
            _reset_combat_posture(bot)
        return {
            "ok": False,
            "mode": resolved_mode,
            "target_name": resolved_target,
            "elapsed_s": round(time.monotonic() - started, 2),
            "requires_replan": True,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "detail": getattr(exc, "raw", getattr(exc, "detail", None)),
            },
        }
    finally:
        # On normal completion (not preemption unwind), reset lingering shield/sneak/jump posture.
        if not stationary_blaze and not adapter._unwinding:
            _reset_combat_posture(bot)
