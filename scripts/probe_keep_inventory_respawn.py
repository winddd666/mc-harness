#!/usr/bin/env python3
"""Exactly one labeled death; compare retained inventory after protected respawn."""
import json
from pathlib import Path
import urllib.request

from beast_experiment_ctl import remote

root = Path(__file__).resolve().parents[1]
destination = root / "experiments/20261115-until-1900/controlled-respawn.json"
if destination.exists():
    raise SystemExit("Controlled death was already attempted; no duplicate.")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
    if json.load(response).get("running"):
        raise SystemExit("Console active; no death sent.")
# Persist intent before the irreversible action, so an interrupted runner never
# silently retries the kill. No item, health or survival-effect grant is used.
destination.write_text(json.dumps({"attempted": True, "verified": False, "assisted": True,
                                    "note": "One deliberate death followed by a protected-room teleport; not autonomous recovery."}, indent=2) + "\n")
result = remote('''
import pathlib,sys,json,time,urllib.request,collections
base=pathlib.Path('/home/xia/mc-beast');sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _run_rcon_commands
run=lambda commands:_run_rcon_commands(base/'mc',base/'.env.beast','127.0.0.1',25575,commands)
token=json.loads((base/'.state-beast/tokens.json').read_text())['tokens']['beastbot_01']['token']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def api(path):
 req=urllib.request.Request('http://127.0.0.1:18900/api/v1/'+path,headers={'Authorization':'Bearer '+token})
 with opener.open(req,timeout=6) as response:return json.load(response)
rule=run(['gamerule keep_inventory'])
if not any('true' in row['reply'].lower() for row in rule):raise SystemExit('keep_inventory not confirmed; no kill')
before={'status':api('status'),'inventory':api('inventory')}
if not before['status'].get('result',{}).get('ready') or before['status']['result'].get('busy'):raise SystemExit('Bot not ready/idle; no kill')
start=api('events?since=0&limit=500')['result']['last_seq']
issued=time.time();commands=run(['kill BeastBot_01'])
rows=[];seen_spawn=False
for _ in range(70):
 page=api('events?since='+str(start)+'&limit=100')['result']
 fresh=[e for e in page.get('events',[]) if e.get('seq',0)>start]
 rows+=fresh
 start=max([start]+[e.get('seq',0) for e in fresh])
 if any(e.get('type')=='death_loop' for e in fresh):raise SystemExit('Death-loop protection unexpectedly activated; no bypass')
 if any(e.get('type')=='spawn' for e in fresh):
  seen_spawn=True
  commands+=run(['tp BeastBot_01 -187.5 58 -389.5'])
  break
 time.sleep(.08)
after={'status':api('status'),'inventory':api('inventory')}
immediate_after=after
for _ in range(35):
 if after['status'].get('result',{}).get('ready'):break
 time.sleep(.2)
 after={'status':api('status'),'inventory':api('inventory')}
def counts(value):
 result=collections.Counter()
 for item in value.get('result',{}).get('items',[]):result[item['name']]+=item['count']
 return dict(result)
b,a=counts(before['inventory']),counts(after['inventory'])
verified=seen_spawn and a==b and bool(a) and after['status'].get('result',{}).get('ready')
print(json.dumps({'assisted':True,'verified':verified,'issued_at':issued,'before':before,'after':after,'immediate_after':immediate_after,
                  'before_counts':b,'after_counts':a,'commands':commands,'events':rows,'rule':rule,
                  'note':'One controlled kill, natural respawn, protected-room teleport; no item grant or health command.'},ensure_ascii=False))
''')
destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"saved": str(destination), "verified": result["verified"],
                  "before_counts": result["before_counts"], "after_counts": result["after_counts"],
                  "health_after": result["after"]["status"].get("result", {}).get("health")}, ensure_ascii=False))
