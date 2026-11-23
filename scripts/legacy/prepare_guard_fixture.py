#!/usr/bin/env python3
"""Prepare an all-stone fixture without a connected bot, then recover directly into it."""
import argparse
import json
from pathlib import Path
import time
import urllib.request

from beast_experiment_ctl import remote

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "experiments/20261115-until-1900/fixtures"
COMMON = '''
import pathlib,sys,json,time,urllib.request
base=pathlib.Path('/home/xia/mc-beast'); sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _run_rcon_commands
run=lambda commands:_run_rcon_commands(base/'mc',base/'.env.beast','127.0.0.1',25575,commands)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "recover"])
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
        if json.load(response).get("running"):
            raise SystemExit("Agent active; fixture operation refused.")
    if args.operation == "prepare":
        code = COMMON + '''
attempts=[]
for cx,cy,cz in [(-182,58,-386),(-188,58,-390),(-190,62,-390),(-180,54,-400)]:
 positions=[(x,y,z) for x in range(cx-2,cx+3) for y in range(cy-1,cy+4) for z in range(cz-2,cz+3)]
 commands=[]
 for offset in range(0,len(positions),25):
  commands.append('execute '+''.join(f'if block {x} {y} {z} #minecraft:base_stone_overworld ' for x,y,z in positions[offset:offset+25])+'run time query gametime')
 tests=run(commands)
 attempts.append({'center':[cx,cy,cz],'all_stone':all('The time is ' in row['reply'] for row in tests),'tests':tests})
 if attempts[-1]['all_stone']: break
else:
 print(json.dumps({'verified_all_stone':False,'attempts':attempts})); sys.exit(0)
interior=[(x,y,z) for x in range(cx-1,cx+2) for y in range(cy,cy+3) for z in range(cz-1,cz+2)]
names=['stone','granite','diorite','andesite','deepslate','tuff']
probes=[(p,name) for p in interior for name in names]
rows=run([f'execute if block {x} {y} {z} minecraft:{name} run time query gametime' for (x,y,z),name in probes])
original=[{'pos':list(pos),'name':name} for (pos,name),row in zip(probes,rows) if 'The time is ' in row['reply']]
if len(original)!=27 or len({tuple(row['pos']) for row in original})!=27: raise SystemExit('Interior block snapshot incomplete; no carving performed')
results=run([f'fill {cx-1} {cy} {cz-1} {cx+1} {cy+2} {cz+1} minecraft:air'])
print(json.dumps({'assisted':True,'verified_all_stone':True,'verified_tag':'minecraft:base_stone_overworld','verified_volume':125,'center':[cx,cy,cz],'attempts':attempts,'commands':results,'original_blocks':original},ensure_ascii=False))
'''
    else:
        prepared = FOLDER / "guard-chamber-prepared.json"
        if not prepared.exists() or not json.loads(prepared.read_text()).get("verified_all_stone"):
            raise SystemExit("Fixture has not been verified and prepared.")
        center = json.loads(prepared.read_text())["center"]
        target = [center[0] + 0.5, center[1], center[2] + 0.5]
        code = COMMON + "target=" + repr(target) + "\n" + '''
cache=json.loads((base/'.state-beast/tokens.json').read_text()); token=cache['tokens']['beastbot_01']['token']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def api(path,body=None):
 req=urllib.request.Request('http://127.0.0.1:18900/api/v1/'+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='GET' if body is None else 'POST')
 with opener.open(req,timeout=8) as response: return json.load(response)
events=api('events?since=0&limit=500')['result']['events']
until=max([e.get('cooldown_until',0) for e in events if e.get('type')=='death_loop']+[0])
if time.time()*1000<until:
 print(json.dumps({'cooldown_active':True,'remaining_seconds':until/1000-time.time()})); sys.exit(0)
rule=run(['gamerule keep_inventory'])
if not any('true' in row['reply'].lower() for row in rule): raise SystemExit('keep_inventory not verified')
connected=api('connect',{})
teleports=[]
for _ in range(30):
 teleports+=run(['execute as BeastBot_01 run tp @s '+' '.join(map(str,target))])
 if 'Teleported' in teleports[-1]['reply']: break
 time.sleep(.1)
else: raise SystemExit('Could not place bot in the protected fixture')
state={}
for _ in range(30):
 state=api('status')
 if state.get('result',{}).get('ready'): break
 time.sleep(.25)
print(json.dumps({'assisted':True,'connected':connected,'teleports':teleports,'status':state,'inventory':api('inventory')},ensure_ascii=False))
'''
    result = remote(code)
    FOLDER.mkdir(parents=True, exist_ok=True)
    destination = FOLDER / ("guard-chamber-prepared.json" if args.operation == "prepare" else f"{int(time.time())}-guard-recovery.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    result.pop("original_blocks", None)
    if "attempts" in result:
        result["attempts"] = [{"center": row["center"], "all_stone": row["all_stone"]} for row in result["attempts"]]
    print(json.dumps({"operation": args.operation, "saved": str(destination), **result}, ensure_ascii=False)[:7000])


if __name__ == "__main__":
    main()
