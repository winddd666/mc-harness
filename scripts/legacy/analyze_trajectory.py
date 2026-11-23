#!/usr/bin/env python3
"""Read-only accounting of saved/live JSONL; no Minecraft or model requests."""
import argparse
import collections
import json
from pathlib import Path
import statistics


def analyze(path):
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # A live writer may still be appending the last line.
    models, calls, failed, inventory = {}, {}, collections.Counter(), []
    notable = []
    for event in rows:
        kind, elapsed = event.get("type"), event.get("elapsed", 0)
        request = event.get("request")
        if kind == "run.started":
            for line in (event.get("initial_snapshot") or "").splitlines():
                if line.startswith("inventory:"):
                    value = json.loads(line.partition(":")[2])
                    inventory.append((elapsed, value.get("result", value), "initial"))
        if kind == "model.started":
            models[request] = {"start": elapsed}
        elif kind in {"model.text.delta", "model.tool.delta"}:
            models.setdefault(request, {}).setdefault("first_delta", elapsed)
        elif kind in {"model.completed", "model.failed"}:
            models.setdefault(request, {}).update(end=elapsed, kind=kind, cancelled=event.get("cancelled", False))
        elif kind == "tool.started":
            function = event.get("function") or {}
            calls[event["call_id"]] = {"name": function.get("name"), "start": elapsed, "arguments": function.get("arguments")}
        elif kind == "tool.completed":
            call = calls.setdefault(event["call_id"], {"start": elapsed})
            call.update(end=elapsed, duration=elapsed - call["start"])
            try:
                result = json.loads(event.get("result", "").partition("\n[期间新事件]")[0])
            except ValueError:
                result = {"unparsed": event.get("result")}
            call["result"] = result
            error = result.get("error") or result.get("last_error") or result.get("partial_error")
            if result.get("ok") is False or error or result.get("requires_replan"):
                code = error.get("code") if isinstance(error, dict) else error or result.get("stop_reason") or result.get("reason") or "unknown"
                failed[str(code)] += 1
            if call.get("name") == "mc_inventory" and result.get("ok"):
                inventory.append((elapsed, result.get("result", result), "tool"))
            for key in ("inventory_after", "inventory"):
                if isinstance(result.get(key), dict):
                    inventory.append((elapsed, result[key], "tool_result"))
        elif kind == "guardian.incident" and isinstance(event.get("inventory_after"), dict):
            inventory.append((elapsed, event["inventory_after"], "guardian"))
        elif kind == "mc.notable_events":
            notable.append(event.get("text", ""))

    waits = [value["end"] - value["start"] for value in models.values() if "end" in value and "start" in value]
    firsts = [value["first_delta"] - value["start"] for value in models.values() if "first_delta" in value and "start" in value]
    tool_times = collections.defaultdict(float)
    for call in calls.values():
        tool_times[call.get("name")] += call.get("duration", 0)
    milestones = {}
    for elapsed, inv, source in inventory:
        counts = collections.Counter()
        if isinstance(inv.get("items"), list):
            for item in inv["items"]:
                counts[item["name"]] += int(item.get("count", 0))
        else:
            counts.update({key: value for key, value in inv.items() if isinstance(value, int)})
        for item in ("crafting_table", "stone_pickaxe", "raw_iron", "iron_ingot", "furnace", "iron_pickaxe", "shield"):
            if counts[item] > 0:
                milestones.setdefault(item, {"first_observed_s": elapsed, "count": counts[item], "source": source})
    total = rows[-1].get("elapsed", 0) if rows else 0
    finished = next((event for event in reversed(rows) if event.get("type") == "run.finished"), {})
    return {"run": path.name, "elapsed_s": total, "status": finished.get("status", "running_or_incomplete"),
            "error": finished.get("error"), "source_revision": next((e.get("runtime", {}).get("source_revision") for e in rows if e.get("type") == "run.started"), None),
            "requests_started": len(models), "requests_finished": len(waits),
            "generation_wall_s_including_cancelled": round(sum(waits), 3),
            "generation_fraction": round(sum(waits) / total, 4) if total else None,
            "generation_median_s": round(statistics.median(waits), 3) if waits else None,
            "generation_max_s": round(max(waits), 3) if waits else None,
            "first_delta_median_s": round(statistics.median(firsts), 3) if firsts else None,
            "first_delta_max_s": round(max(firsts), 3) if firsts else None,
            "cancelled_generations": sum(bool(v.get("cancelled")) for v in models.values()),
            "tools": dict(collections.Counter(c.get("name") for c in calls.values())),
            "tool_seconds": {k: round(v, 3) for k, v in tool_times.items()},
            "failure_codes": dict(failed),
            "notable_deaths": sum(s.count("[死亡 pos=") for s in notable),
            "notable_hurts": sum(s.count("[受伤 health=") for s in notable),
            "material_milestones": milestones,
            "limits": "Milestones are observations, not proof of production. Initial inventory is inherited. Missing inventory observations do not prove absence. Generation time includes synchronous guard actions; use tool/guardian events to separate them."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = [analyze(path) for path in args.paths]
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)
