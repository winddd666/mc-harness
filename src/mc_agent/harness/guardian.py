"""Experimental deterministic reflexes while the model request is in flight.

The generation controller calls this synchronously, so it never competes with
the tool scheduler for actions or writes events from a background thread.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from ..errors import ApiError
from ..skills.library import MacroSkills
from ..skills.safety import SAFE_FOOD_PRIORITY

# Hurt sources that mean "my body is embedded in a block", not "something is attacking me".
# These need a carve-out reflex (unstick), never bunkering: a bunker needs a pickaxe that may
# already have been dropped on death, which is how a death loop turns into a fatal API error.
IN_WALL_HURT_SOURCES = frozenset({"in_wall", "suffocation", "cramming", "inside_block"})


class _GuardSession:
    def __init__(self, bot, checkpoint):
        self.bot = bot
        self.checkpoint = checkpoint

    def call(self, action, body=None, **kwargs):
        self.checkpoint()
        readonly = action in {"status", "events", "inventory", "get_entities", "get_block"}
        kwargs.update(allow_reauth=False, timeout_s=2.0 if readonly else 8.0)
        result = self.bot.call(action, body, **kwargs)
        self.checkpoint()
        return result

    def status(self):
        return self.call("status", method="GET")

    def inventory(self):
        return self.call("inventory", method="GET")


class GenerationGuard:
    def __init__(self, bot, events, *, checkpoint: Callable, interval_s: float = 0.75, geofence: Any | None = None):
        self.bot = _GuardSession(bot, checkpoint)
        self.events = events
        self.interval_s = interval_s
        self.geofence = geofence
        self.last_poll = float("-inf")
        self.last_reaction = float("-inf")
        self.previous_health: float | None = None
        self.previous_ready: bool | None = None
        self.cursor = int(getattr(bot, "_last_event_seq", 0) or 0)
        self.failures = 0
        self.disconnected = False
        self.stats = {"polls": 0, "incidents": 0, "actions": 0, "cancelled_generations": 0}

    def _action(self, name: str, operation: Callable) -> dict[str, Any]:
        started = time.monotonic()
        self.events.emit("guardian.action.started", action=name)
        try:
            result = operation()
            outcome = {"action": name, "result": result, "ok": result.get("ok", True)}
        except ApiError as error:
            outcome = {"action": name, "ok": False, "error": {"code": error.code, "message": error.message, **error.context}}
        except httpx.HTTPError as error:
            # A timed-out action may still be executing. Never start another
            # reflex or deliver the stale model response after that ambiguity.
            raise RuntimeError(f"生存响应 {name} 的网络结果不确定：{type(error).__name__}") from None
        self.stats["actions"] += 1
        self.events.emit("guardian.action.completed", duration=time.monotonic() - started, **outcome)
        return outcome

    def _drain_events_pages(self) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for _ in range(5):
            page = self.bot.call("events", method="GET", params={"since": self.cursor, "limit": 100})
            rows = [e for e in page.get("events", []) if int(e.get("seq", 0)) > self.cursor]
            rec_fn = getattr(self.bot.bot, "record_death_event", None)
            if callable(rec_fn):
                for ev in rows:
                    if str(ev.get("type") or "").lower() == "death":
                        rec_fn(ev.get("seq") if ev.get("seq") is not None else ev.get("t"))
            fresh.extend(rows)
            self.cursor = max([self.cursor, int(page.get("last_seq", 0))] + [int(e.get("seq", 0)) for e in rows])
            if not page.get("has_more") or not rows:
                break
        return fresh

    def poll(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if now - self.last_poll < self.interval_s:
            return None
        self.last_poll = now
        self.stats["polls"] += 1
        try:
            status = self.bot.status()
            fresh = self._drain_events_pages()
            self.failures = 0
        except (ApiError, httpx.HTTPError) as error:
            self.failures += 1
            if self.failures == 1:
                self.events.emit("guardian.observation_failed", error=type(error).__name__, code=getattr(error, "code", None))
            if self.failures >= 3:
                # 掉线是可观测的运行结果，不是程序 bug：标记后由主循环以 status="disconnected" 收尾。
                self.disconnected = True
            return None

        health = status.get("health")
        dropped = isinstance(health, (int, float)) and self.previous_health is not None and health < self.previous_health - 0.2
        ready = bool(status.get("ready", status.get("alive", True)))
        disconnected = self.previous_ready is True and not ready
        self.previous_ready = ready
        self.previous_health = float(health) if isinstance(health, (int, float)) else None
        hurts = [e for e in fresh if e.get("type") == "hurt"]
        deaths = [e for e in fresh if e.get("type") in {"death", "death_loop", "disconnected", "spawn"}]
        in_water = bool(status.get("in_water"))
        oxygen = status.get("oxygen")
        needs_float = in_water and not status.get("jumping") and (
            status.get("submerged") or not status.get("on_ground") or (isinstance(oxygen, (int, float)) and oxygen < 240)
        )
        food = status.get("food")
        needs_food = isinstance(food, (int, float)) and (food <= 16 or (isinstance(health, (int, float)) and health < 18 and food < 20))
        hurt = bool(hurts) or dropped
        if not (hurt or deaths or disconnected or needs_float or needs_food):
            return None
        if now - self.last_reaction < 3.0 and not deaths:
            return None

        actions: list[dict[str, Any]] = []
        reasons = []
        if hurt:
            reasons.append("fresh_hurt")
        if deaths or disconnected:
            reasons.append("death_or_connection_changed")
        if needs_float:
            reasons.append("water_float")
        if deaths or disconnected or not ready or status.get("alive") is False:
            # Never perform an old body's reflex against a newly respawned body.
            # The model must see the fresh position and retained inventory first.
            return self._incident(status, reasons, actions, fresh)
        if status.get("busy"):
            return self._incident(status, reasons + ["unexpected_action_still_busy"], actions, fresh)

        try:
            inv = self.bot.inventory() if hurt or needs_food else {}
        except (ApiError, httpx.HTTPError):
            inv = {}
        counts: dict[str, int] = {}
        for item in inv.get("items", []):
            counts[item["name"]] = counts.get(item["name"], 0) + int(item.get("count", 0))
        offhand = inv.get("offhand") or {}
        has_shield = counts.get("shield", 0) > 0 or offhand.get("name") == "shield"
        if needs_float:
            # Floating in place only delays drowning (live: 7 straight water_float reflexes, then
            # 10 drown ticks and death). Climb out onto dry land instead.
            actions.append(self._action("escape_water", lambda: MacroSkills(self.bot).escape_water()))
        if hurt and has_shield:
            attacker = (hurts[-1].get("source_entity") or hurts[-1].get("likely_attacker")) if hurts else status.get("nearest_hostile")
            if attacker and attacker.get("id") is not None:
                actions.append(self._action("face_attacker", lambda: self.bot.call("look", {"entity_id": attacker["id"]})))
            actions.append(self._action("raise_shield", lambda: self.bot.call("posture", {
                "shield": True, "jump": needs_float or bool(status.get("jumping")), "sneak": bool(status.get("sneaking")),
            })))
        elif hurt and not in_water and not status.get("in_lava"):
            attacker = (hurts[-1].get("source_entity") or hurts[-1].get("likely_attacker")) if hurts else status.get("nearest_hostile")
            hurt_src = str(hurts[-1].get("source") or "") if hurts else ""
            if hurt_src in IN_WALL_HURT_SOURCES:
                # Suffocating inside a block: carve ourselves out instead of digging a bunker
                # (a bunker needs a pickaxe, which a death may already have taken away).
                actions.append(self._action("unstick_in_wall", lambda: MacroSkills(self.bot).unstick()))
            else:
                raw_dist = (attacker or status.get("nearest_hostile") or {}).get("dist", 99.0)
                try:
                    nh_dist = float(raw_dist)
                except (TypeError, ValueError):
                    nh_dist = 99.0
                melee_or_airborne = (
                    (status.get("on_ground") is not True)
                    or (hurt_src in {"mob_attack", "player_explosion", "entity_explosion"})
                    or (nh_dist <= 4.5)
                )
                if not melee_or_airborne:
                    bunker = self._action("emergency_bunker", lambda: MacroSkills(self.bot).dig_safety_bunker(urgent=True))
                    actions.append(bunker)
                    if not bunker["ok"]:
                        actions.append(
                            self._action(
                                "combat_or_flee",
                                lambda: MacroSkills(self.bot).defend_or_flee(
                                    attacker=attacker,
                                    counts=counts,
                                    health=health,
                                    urgent=True,
                                    geofence=self.geofence,
                                ),
                            )
                        )
                else:
                    actions.append(
                        self._action(
                            "combat_or_flee",
                            lambda: MacroSkills(self.bot).defend_or_flee(
                                attacker=attacker,
                                counts=counts,
                                health=health,
                                urgent=True,
                                geofence=self.geofence,
                            ),
                        )
                    )
        if needs_food and (not hurt or any(a["action"] in {"emergency_bunker", "combat_or_flee"} and a["ok"] for a in actions)):
            best_food = next((name for name in SAFE_FOOD_PRIORITY if counts.get(name, 0) > 0), None)
            if best_food:
                reasons.append("needs_food")
                actions.append(self._action("eat", lambda: self.bot.call("use_item", {"item": best_food})))
        if not actions and not reasons:
            return None
        replan = bool(
            hurt
            or deaths
            or disconnected
            or needs_float
            or not ready
            or status.get("alive") is False
            or any(r != "needs_food" for r in reasons)
        )
        try:
            post_status = self.bot.status()
        except (ApiError, httpx.HTTPError):
            post_status = {**status, "ready": False, "alive": False, "busy": None}
            if "death_or_connection_changed" not in reasons:
                reasons.append("death_or_connection_changed")
            replan = True
        if actions:
            try:
                post_fresh = self._drain_events_pages()
            except (ApiError, httpx.HTTPError):
                post_fresh = []
            if post_fresh:
                fresh.extend(post_fresh)
                if any(e.get("type") in {"death", "death_loop", "disconnected", "spawn"} for e in post_fresh):
                    if "death_or_connection_changed" not in reasons:
                        reasons.append("death_or_connection_changed")
                    replan = True
        return self._incident(post_status, reasons, actions, fresh, requires_replan=replan)

    def _incident(self, status, reasons, actions, fresh, *, requires_replan: bool = True):
        self.last_reaction = time.monotonic()
        self.previous_health = status.get("health")
        self.stats["incidents"] += 1
        note = (
            "响应不代表整体安全；按最新状态重新规划，勿继续被中断的旧动作链。"
            if requires_replan
            else "后台已静默进食，未打断当前生成。"
        )
        incident = {
            "reasons": reasons, "actions": actions,
            "status": {k: status.get(k) for k in ("pos", "health", "food", "ready", "alive", "busy", "in_water", "blocking")},
            "event_seqs": [e.get("seq") for e in fresh if e.get("type") in {"hurt", "death", "spawn", "death_loop", "disconnected"}],
            "requires_replan": requires_replan,
            "note": note,
        }
        if requires_replan and status.get("ready") and status.get("alive") is not False:
            try:
                incident["inventory_after"] = self.bot.inventory()
            except (ApiError, httpx.HTTPError):
                incident["inventory_after"] = None
                incident["inventory_note"] = "动作后背包读取失败；重新查询，不得引用开局材料数量。"
        self.events.emit("guardian.incident", **incident)
        return incident
