#!/usr/bin/env python3
"""Small, explicitly assisted underground fixture; snapshot natural terrain before changing it."""
import argparse
import json
from pathlib import Path
import time
import urllib.request

from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from beast_experiment_ctl import remote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["inspect", "apply"])
    parser.add_argument("--center", nargs=3, type=int, default=[-170, 63, -380], metavar=("X", "Y", "Z"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
        if json.load(response).get("running"):
            raise SystemExit("Agent still active; fixture refused.")
    settings = Settings.load(dotenv_path=root / ".env.beast")
    record = TokenStore(settings.state_dir / "tokens.json").get("BeastBot_01")
    if record is None:
        raise SystemExit("No cached token. No login attempted.")
    natural = {"stone", "dirt", "grass_block", "andesite", "diorite", "granite", "deepslate", "tuff", "gravel", "air", "cave_air"}
    cx, cy, cz = args.center
    bounds = [[cx - 2, cy - 1, cz - 2], [cx + 2, cy + 3, cz + 2]]
    blocks = []
    with BotSession(record.token, bot_username="BeastBot_01", base_url=settings.mc_api_base_url, timeout=8) as bot:
        before = {"status": bot.status(), "inventory": bot.inventory()}
        for x in range(cx - 2, cx + 3):
            for y in range(cy - 1, cy + 4):
                for z in range(cz - 2, cz + 3):
                    block = bot.call("get_block", {"pos": [x, y, z]}, allow_reauth=False)
                    blocks.append(block)
        found = sorted({block["name"] for block in blocks})
        if any(name not in natural for name in found):
            raise SystemExit("Fixture would overwrite a non-natural block/fluid/ore; refused. Types: " + str(found))
    folder = root / "experiments/20261115-until-1900/fixtures"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{int(time.time())}-chamber.json"
    evidence = {"assisted": True, "operation": args.operation, "bounds": bounds,
                "before": before, "original_blocks": blocks, "granted_items": []}
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    if args.operation == "apply":
        commands = [f'fill {cx-2} {cy-1} {cz-2} {cx+2} {cy+3} {cz+2} minecraft:stone',
                    f'fill {cx-1} {cy} {cz-1} {cx+1} {cy+2} {cz+1} minecraft:air',
                    f'tp BeastBot_01 {cx+0.5} {cy} {cz+0.5}']
        result = remote(f'''
import pathlib,sys,json
base=pathlib.Path('/home/xia/mc-beast'); sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _run_rcon_commands
run=lambda commands:_run_rcon_commands(base/'mc',base/'.env.beast','127.0.0.1',25575,commands)
rule=run(['gamerule keep_inventory'])
if not any('true' in row['reply'].lower() for row in rule): raise SystemExit('keep_inventory not verified')
commands={commands!r}
results=run(commands)
print(json.dumps({{'rules':rule,'commands':results}},ensure_ascii=False))
''')
        evidence["result"] = result
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"assisted": True, "types_before": found, "blocks_saved": len(blocks),
                      "saved": str(destination), "result": evidence.get("result")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
