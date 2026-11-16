"""Bounded level excavation; pathfinding itself never needs to break blocks."""
from __future__ import annotations

import math
import time

from ..errors import ApiError

AIR = {"air", "cave_air", "void_air"}
FLUID = {"water", "lava", "flowing_water", "flowing_lava"}
GROUND = {"stone", "deepslate", "granite", "diorite", "andesite", "tuff", "calcite",
          "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "cobblestone", "cobbled_deepslate"}
ORE = {"coal_ore", "iron_ore", "copper_ore", "gold_ore", "redstone_ore", "lapis_ore",
       "diamond_ore", "emerald_ore"}
ORE |= {"deepslate_" + name for name in ORE}


def dig_level_tunnel(bot, direction="north", steps=4, stop_on_ore=True):
    # Lazy import avoids a library/tunnel import cycle.
    from .library import BLOCK_MIN_PICKAXE_TIER, _best_pickaxe_in_inv, _inventory_counts, _positive_inv_delta

    directions = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
    direction = str(direction).strip().lower()
    if isinstance(steps, float) and steps.is_integer():
        steps = int(steps)
    if direction not in directions or not isinstance(steps, int) or isinstance(steps, bool):
        return {"ok": False, "error": "bad_args", "requires_replan": True,
                "message": "direction 必须为 north/south/east/west；steps 必须为整数。"}
    steps = max(1, min(8, steps))
    dx, dz = directions[direction]
    before = _inventory_counts(bot)
    pick, tier = _best_pickaxe_in_inv(before)
    initial = bot.status()
    origin = initial.get("pos")
    last_death = (initial.get("last_death") or {}).get("t")
    minimum_health = initial.get("health")
    deadline = time.monotonic() + 35.0
    done, dug, ore_faces = 0, [], []
    reason, error = "completed", None
    blocker: dict | None = None
    hint: str | None = None

    def block(pos):
        result = bot.call("get_block", {"pos": list(pos)})
        name = result.get("name") or result.get("block")
        # Missing data is never evidence of air.
        if not name:
            raise ApiError("unknown_block", "无法确认前方方块", context={"pos": list(pos)})
        return str(name).removeprefix("minecraft:")

    def _first_match(coords, pred):
        for coord in coords:
            bname = exposed[coord]
            if pred(bname):
                return coord, bname
        return None, None

    def checkpoint():
        status = bot.status()
        if time.monotonic() >= deadline:
            return status, "time_budget"
        if not status.get("pos") or status.get("ready") is False or status.get("alive") is False:
            return status, "not_ready"
        if status.get("in_water") or status.get("in_lava"):
            return status, "entered_fluid"
        if (status.get("last_death") or {}).get("t") != last_death:
            return status, "body_changed"
        if isinstance(minimum_health, (int, float)) and isinstance(status.get("health"), (int, float)) and status["health"] < minimum_health:
            return status, "hurt"
        if status.get("on_ground") is not True:
            return status, "not_grounded"
        return status, None

    try:
        status, failure = checkpoint()
        if failure:
            reason = failure
        elif not pick:
            reason = "missing_pickaxe"
            hint = "背包内没有可用镐，请先合成镐后再水平开矿道。"
        else:
            bot.call("equip", {"item": pick, "slot": "hand"})
            for _ in range(steps):
                status, failure = checkpoint()
                if failure:
                    reason = failure
                    break
                x, y, z = (math.floor(float(v)) for v in status["pos"])
                nx, nz = x + dx, z + dz
                faces = [(nx, y, nz), (nx, y + 1, nz)]
                floor = block((nx, y - 1, nz))
                if floor not in GROUND | ORE:
                    reason = "unsafe_or_unknown_floor"
                    blocker = {"pos": [nx, y - 1, nz], "block": floor, "relation": "unsafe_floor"}
                    hint = f"前方脚下地板 {[nx, y - 1, nz]} 为 {floor}（空洞/流体或非常规支撑方块），无法继续向 {direction} 平推。"
                    break
                # Check every newly exposed neighbor, including ceiling, for
                # fluid or falling material before opening either face block.
                neighbors = {(px + ax, py + ay, pz + az) for px, py, pz in faces
                             for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))}
                ordered_probe = list(faces) + [p for p in sorted(neighbors) if p not in faces]
                exposed = {p: block(p) for p in ordered_probe}
                fluid_pos, fluid_name = _first_match(ordered_probe, lambda n: n in FLUID)
                if fluid_pos is not None:
                    reason = "fluid_adjacent"
                    blocker = {
                        "pos": list(fluid_pos),
                        "block": fluid_name,
                        "relation": "in_path" if fluid_pos in faces else "adjacent_fluid",
                    }
                    hint = f"前方邻接流体 {fluid_name}@{list(fluid_pos)}，已停挖防淹没，请换方向。"
                    break
                falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
                fall_pos, fall_name = _first_match(ordered_probe, lambda n: n in falling_set)
                if fall_pos is not None:
                    reason = "falling_material_adjacent"
                    blocker = {"pos": list(fall_pos), "block": fall_name, "relation": "falling_block"}
                    hint = f"前方邻接易塌方块 {fall_name}@{list(fall_pos)}，自动矿道停挖防塌方。"
                    break
                ore_faces = [{"name": exposed[p], "pos": list(p)} for p in faces if exposed[p] in ORE]
                if stop_on_ore and ore_faces:
                    reason = "ore_at_face"
                    break
                unsup_pos, unsup_name = _first_match(faces, lambda n: n not in AIR | GROUND | ORE)
                if unsup_pos is not None:
                    reason = "unsupported_face_block"
                    blocker = {"pos": list(unsup_pos), "block": unsup_name, "relation": "unsupported_face_block"}
                    hint = (
                        f"前方断面 {list(unsup_pos)} 为非常规方块 {unsup_name}（非普通岩石/泥土/矿石），"
                        f"自动矿道已停下；可据该方块类型改调 mc_dig({unsup_pos[0]}, {unsup_pos[1]}, {unsup_pos[2]}) 清理或换方向。"
                    )
                    break
                tier_pos, tier_name = _first_match(
                    faces,
                    lambda n: n not in AIR and BLOCK_MIN_PICKAXE_TIER.get(n, 1) > tier,
                )
                if tier_pos is not None:
                    req_t = BLOCK_MIN_PICKAXE_TIER.get(tier_name, 1)
                    reason = "insufficient_pickaxe_tier"
                    blocker = {
                        "pos": list(tier_pos),
                        "block": tier_name,
                        "relation": "insufficient_pickaxe_tier",
                        "required_tier": req_t,
                        "current_pickaxe": pick,
                        "current_tier": tier,
                    }
                    hint = f"前方 {tier_name}@{list(tier_pos)} 需要至少等级 {req_t} 的镐，当前最高为 {pick or '无镐'}(等级 {tier})。"
                    break
                # Head first; stay on the original floor until both blocks
                # are authoritatively air. Never follow a failed dig with move.
                for pos in reversed(faces):
                    status, failure = checkpoint()
                    if failure:
                        reason = failure
                        break
                    name = block(pos)
                    if name in AIR:
                        continue
                    bot.call("dig", {"pos": list(pos), "collect": False, "stop_on": ["hurt", "death"]})
                    rem = block(pos)
                    if rem not in AIR:
                        reason = "dig_not_confirmed"
                        blocker = {"pos": list(pos), "block": rem, "relation": "dig_not_confirmed"}
                        hint = f"挖掘 {list(pos)} 后方块仍为 {rem}，未能清除障碍。"
                        break
                    dug.append({"pos": list(pos), "block": name})
                if reason != "completed":
                    break
                status, failure = checkpoint()
                if failure:
                    reason = failure
                    break
                # Recheck support after digging. No bridge, jump or blind
                # control fallback is attempted on a failed path.
                post_floor = block((nx, y - 1, nz))
                if post_floor not in GROUND | ORE:
                    reason = "support_changed"
                    blocker = {"pos": [nx, y - 1, nz], "block": post_floor, "relation": "support_changed"}
                    hint = f"挖掘后脚下支撑 { [nx, y - 1, nz] } 变为 {post_floor}，已停步。"
                    break
                bot.call("move_to", {"x": nx + 0.5, "y": y, "z": nz + 0.5, "range": 0.3,
                                     "timeout": 4000, "stop_on": ["hurt", "death"]})
                time.sleep(0.15)
                status, failure = checkpoint()
                if failure:
                    reason = failure
                    break
                actual = status["pos"]
                if abs(float(actual[1]) - y) > 0.25 or math.dist([float(actual[0]), float(actual[2])], [nx + 0.5, nz + 0.5]) > 0.6:
                    reason = "movement_not_confirmed"
                    blocker = {"pos": [nx, y, nz], "actual_pos": actual, "relation": "movement_not_confirmed"}
                    hint = f"已挖开前格但未能走入 {[nx, y, nz]}（当前 {actual}），可能撞墙角/实体挡路。"
                    break
                done += 1
    except ApiError as exc:
        reason = "api_error"
        error = {"code": exc.code, "message": exc.message, **exc.context}
    after = _inventory_counts(bot)
    final = bot.status()
    result = {
        "ok": reason in {"completed", "ore_at_face"},
        "requires_replan": reason not in {"completed", "ore_at_face"},
        "direction": direction,
        "steps_requested": steps,
        "steps_done": done,
        "stop_reason": reason,
    }
    if blocker is not None:
        result["blocker"] = blocker
    if error is not None:
        result["last_error"] = error
    if hint is not None:
        result["hint"] = hint
    result.update({
        "ore_at_face": ore_faces,
        "pos_before": origin,
        "pos_after": final.get("pos"),
        "gained": _positive_inv_delta(before, after),
        "dug": dug,
        "inventory_after": after,
        "note": "仅 gained 是实际入包增量；挖掘数不是材料取得数。",
    })
    return result
