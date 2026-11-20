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
    target_name: str | None,
) -> tuple[str, str | None]:
    """Choose a tactical combat mode from live nearby hostiles and inventory gear."""
    norm_filter = combat_v1.normalize_mob_name(target_name)
    ent = combat_v1.nearest_hostile_entity(adapter, radius=24, name_filter=norm_filter)  # type: ignore[arg-type]
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

    try:
        if resolved_mode == "auto":
            resolved_mode, resolved_target = _resolve_auto_mode(adapter, target_name)

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

        ok = not bool(raw.get("error"))
        return {
            "ok": ok,
            "mode": resolved_mode,
            "target_name": resolved_target,
            "elapsed_s": round(time.monotonic() - started, 2),
            "result": raw,
        }
    except _CombatBudgetExceeded:
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
        if not adapter._unwinding:
            _reset_combat_posture(bot)
