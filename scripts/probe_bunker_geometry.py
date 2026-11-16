#!/usr/bin/env python3
"""Labeled edge-position bunker test, reusing a cached token and existing room."""
import json
import argparse
from pathlib import Path
import time
import urllib.request

from beast_experiment_ctl import remote
from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.skills.library import MacroSkills

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--name", default="bunker-edge-live-south")
parser.add_argument("--target", type=float, nargs=3, default=[-188.9, 58, -388.9])
args = parser.parse_args()
if Path(args.name).name != args.name:
    raise SystemExit("Probe name must not contain a path.")
destination = root / "experiments/20261115-until-1900" / (args.name + ".json")
if destination.exists():
    raise SystemExit("Probe already recorded; no duplicate terrain mutation.")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
    if json.load(response).get("running"):
        raise SystemExit("Console active; no probe started.")
settings = Settings.load(dotenv_path=root / ".env.beast")
cached = TokenStore(settings.state_dir / "tokens.json").get("BeastBot_01")
if cached is None:
    raise SystemExit("No cached token; login is not permitted by this probe.")
evidence = {"assisted": True, "target": args.target, "started_at": time.time(),
            "note": "Existing prepared room; corner teleport only, no health/effect/item grants."}
with BotSession(cached.token, bot_username="BeastBot_01", base_url=settings.mc_api_base_url, timeout=12) as bot:
    evidence["before"] = {"status": bot.status(), "inventory": bot.inventory()}
    evidence["teleport"] = remote('target=' + repr(args.target) + '\n' + '''
import pathlib,sys,json
base=pathlib.Path('/home/xia/mc-beast');sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _run_rcon_commands
rows=_run_rcon_commands(base/'mc',base/'.env.beast','127.0.0.1',25575,['tp BeastBot_01 '+' '.join(map(str,target))])
print(json.dumps({'commands':rows}))
''')
    time.sleep(0.3)
    evidence["positioned"] = bot.status()
    started = time.monotonic()
    evidence["result"] = MacroSkills(bot).dig_safety_bunker(urgent=True)
    evidence["duration"] = time.monotonic() - started
    evidence["after"] = {"status": bot.status(), "inventory": bot.inventory()}
evidence["ended_at"] = time.time()
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"saved": str(destination), "positioned": evidence["positioned"]["pos"],
                  "duration": evidence["duration"], "result": evidence["result"]}, ensure_ascii=False))
