#!/usr/bin/env python3
"""One labeled two-HP generic injury during a confirmed live model worker."""
import argparse
import json
from pathlib import Path
import time
import urllib.request

from beast_experiment_ctl import remote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--case", default="case04-guarded-damage")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(args.case).name != args.case:
        raise SystemExit("Case must be a directory name.")
    folder = root / "experiments/20261115-until-1900/cases" / args.case
    destination = folder / "controlled-damage.json"
    if destination.exists():
        raise SystemExit("This injury was already attempted; refusing a duplicate.")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def state():
        with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
            return json.load(response)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = state()
        if current.get("active_turn_id") != args.run_id:
            raise SystemExit("Expected run is not active; no damage sent.")
        turn = next(t for t in current["turns"] if t["turn_id"] == args.run_id)
        if not turn.get("survival_guard"):
            raise SystemExit("Generation guard is not enabled; no damage sent.")
        pid = int(current["runtime"]["pid"])
        # mc-console launches the worker from its harness thread. Linux lists
        # children under the creating thread, not necessarily the main TID.
        children = set()
        for children_file in Path(f"/proc/{pid}/task").glob("*/children"):
            try:
                children.update(children_file.read_text().split())
            except FileNotFoundError:
                pass
        workers = [int(child) for child in children if Path(f"/proc/{child}/cmdline").exists()
                   and b"spawn_main" in Path(f"/proc/{child}/cmdline").read_bytes()]
        health = (current.get("telemetry") or {}).get("health")
        if workers and turn.get("phase") == "generating" and isinstance(health, (int, float)) and health >= 18:
            break
        time.sleep(0.1)
    else:
        raise SystemExit("No suitable live model wait was confirmed; no damage sent.")
    time.sleep(0.4)
    latest = state()
    active = next(t for t in latest["turns"] if t["turn_id"] == args.run_id)
    if latest.get("active_turn_id") != args.run_id or active.get("phase") != "generating":
        raise SystemExit("Model wait already ended; no damage sent.")
    evidence = {"assisted": True, "run_id": args.run_id, "model_worker_pids": workers,
                "before": latest.get("telemetry"), "issued_at": time.time(), "damage": 2, "type": "minecraft:generic"}
    folder.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    result = remote('''
import pathlib,sys,json
base=pathlib.Path('/home/xia/mc-beast'); sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _run_rcon_commands
rows=_run_rcon_commands(base/'mc',base/'.env.beast','127.0.0.1',25575,['damage BeastBot_01 2 minecraft:generic'])
print(json.dumps({'commands':rows},ensure_ascii=False))
''')
    evidence["result"] = result
    evidence["response_at"] = time.time()
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"run_id": args.run_id, "workers": workers, "result": result, "saved": str(destination)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
