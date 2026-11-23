#!/usr/bin/env python3
"""Controlled one-time API maintenance: source hashes, stopped consumers, one login, synchronized caches."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

from mc_agent.auth import BotTokenRecord, TokenStore
from mc_agent.config import Settings

from beast_experiment_ctl import remote

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "experiments/20261115-until-1900/backend_patched"
REPORT = ROOT / "experiments/20261115-until-1900/backend-maintenance.json"
BASE = "/home/xia/mc-beast"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STATE = {}


def checkpoint(phase, **data):
    STATE.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(), **data)
    REPORT.write_text(json.dumps(STATE, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"phase": phase, **data}, ensure_ascii=False), flush=True)


def preflight():
    with OPENER.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
        console = json.load(response)
    if console.get("running"):
        raise SystemExit("An agent is active; no maintenance authorized by this script.")
    hashes = json.loads((PATCH / "manifest.json").read_text())
    result = remote(f'''
import pathlib,json,os,urllib.request,time,hashlib
base=pathlib.Path({BASE!r}); hashes={hashes!r}
for name,rec in hashes.items():
 p=base/'api'/name
 actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 if actual != rec['before']: raise SystemExit('Remote source changed: '+name)
for rel in ['.env.beast','.state-beast/tokens.json','api/config.json','leaves/server.properties']:
 if (base/rel).stat().st_mode & 0o777 != 0o600: raise SystemExit('Secret file mode is not 0600: '+rel)
cfg=json.loads((base/'api/config.json').read_text())
if cfg.get('auth',{{}}).get('type') != 'offline': raise SystemExit('Unexpected auth provider')
cache=json.loads((base/'.state-beast/tokens.json').read_text())
record=cache['tokens']['beastbot_01']
request=urllib.request.Request('http://127.0.0.1:18900/api/v1/events?since=0&limit=500',headers={{'Authorization':'Bearer '+record['token']}})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
with opener.open(request,timeout=8) as response: events=json.load(response)['result']['events']
until=max([e.get('cooldown_until',0) for e in events if e.get('type')=='death_loop']+[0])
if time.time()*1000<until: raise SystemExit('Death-loop cooldown still active; maintenance refused')
api=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try:
  cwd=os.readlink(p/'cwd'); argv=[x.decode() for x in (p/'cmdline').read_bytes().split(b'\\x00') if x]
 except OSError: continue
 if cwd==str(base/'api') and argv and pathlib.Path(argv[0]).name=='node' and str(base/'api/src/index.js') in argv:
  api.append({{'pid':int(p.name),'cwd':cwd,'argv':argv}})
if len(api)!=1: raise SystemExit('Expected exactly one managed API process')
print(json.dumps({{'api':api[0],'cooldown_until':until,'source_hashes_verified':True,'secret_modes_verified':True}}))
''')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["preflight", "deploy"])
    args = parser.parse_args()
    if REPORT.exists():
        old = json.loads(REPORT.read_text())
        if old.get("phase") not in ("preflight", "complete"):
            raise SystemExit("Prior maintenance is incomplete. Inspect its checkpoint before recovery; do not log in again.")
        if old.get("phase") == "complete":
            raise SystemExit("This patch has already been deployed.")
    checked = preflight()
    checkpoint("preflight", preflight=checked)
    if args.operation == "preflight":
        return
    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    local_cache = settings.state_dir / "tokens.json"
    backup = local_cache.with_name("tokens.before-action-lifecycle.json")
    if not backup.exists():
        shutil.copy2(local_cache, backup)
        backup.chmod(0o600)
    checkpoint("stopping_local_consumers")
    subprocess.run(["bash", str(ROOT / "scripts/mc_services.sh"), "stop"], check=True)
    statuses = subprocess.run(["systemctl", "--user", "show", "mc-live-proxy.service", "mc-console.service", "-p", "ActiveState", "--value"], capture_output=True, text=True, check=True).stdout.split()
    if any(value != "inactive" for value in statuses):
        raise SystemExit("Local consumers did not stop; no login attempted.")
    checkpoint("local_consumers_stopped")
    hashes = json.loads((PATCH / "manifest.json").read_text())
    contents = {name: (PATCH / name).read_text() for name in hashes}
    payload = {"hashes": hashes, "files": contents, "api": checked["api"]}
    # The SSH response contains the replacement token only in this process's
    # captured memory. Never checkpoint or print that response.
    result = remote(f'''
import pathlib,json,sys,os,signal,time,subprocess,shutil,socket,urllib.request,datetime
base=pathlib.Path({BASE!r}); payload={payload!r}
sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _list_managed_pids
old_live=_list_managed_pids(18765)['mc_live']
for pid in old_live:
 try: os.kill(pid,signal.SIGTERM)
 except ProcessLookupError: pass
time.sleep(1)
for pid in old_live:
 try: os.kill(pid,signal.SIGKILL)
 except ProcessLookupError: pass
if _list_managed_pids(18765)['mc_live']: raise SystemExit('Remote viewer did not stop; login refused')
api=payload['api']; node=api['argv'][0]
backup=base/'api/backups/20261115-action-lifecycle'
backup.mkdir(parents=True,exist_ok=True)
for name,content in payload['files'].items():
 dest=base/'api'/name; pending=dest.with_name(dest.stem+'.pending.js')
 if dest.exists():
  saved=backup/dest.name
  if not saved.exists(): shutil.copy2(dest,saved)
 pending.write_text(content)
 subprocess.run([node,'--check',str(pending)],check=True,capture_output=True)
for name in payload['files']:
 dest=base/'api'/name; pending=dest.with_name(dest.stem+'.pending.js'); os.replace(pending,dest)
os.kill(api['pid'],signal.SIGTERM)
for _ in range(30):
 if not pathlib.Path('/proc/'+str(api['pid'])).exists(): break
 time.sleep(.1)
else: os.kill(api['pid'],signal.SIGKILL)
log=(base/'logs/api.log').open('ab',buffering=0)
proc=subprocess.Popen(api['argv'],cwd=api['cwd'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
for _ in range(50):
 try:
  socket.create_connection(('127.0.0.1',18900),timeout=.2).close(); break
 except OSError: time.sleep(.1)
else: raise SystemExit('Patched API did not open its port')
cfg=json.loads((base/'api/config.json').read_text())
request=urllib.request.Request('http://127.0.0.1:18900/api/v1/auth/login',data=json.dumps({{'username':'BeastBot_01','password':cfg['auth'].get('password','')}}).encode(),headers={{'Content-Type':'application/json'}},method='POST')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
with opener.open(request,timeout=15) as response: authenticated=json.load(response)
if not authenticated.get('ok') or not authenticated.get('token'): raise SystemExit('Controlled login failed')
record={{'bot_username':'BeastBot_01','token':authenticated['token'],'expires_at':authenticated.get('expires_at'),'login_username':'BeastBot_01','obtained_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}}
cachepath=base/'.state-beast/tokens.json'; saved=cachepath.with_name('tokens.before-action-lifecycle.json')
if not saved.exists(): shutil.copy2(cachepath,saved); saved.chmod(0o600)
cache=json.loads(cachepath.read_text()); cache['tokens']['beastbot_01']=record
pending=cachepath.with_name('tokens.maintenance.json')
fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as handle: json.dump(cache,handle)
pending.chmod(0o600); os.replace(pending,cachepath)
print(json.dumps({{'record':record,'api_pid':proc.pid,'stopped_viewer_pids':old_live,'backup':str(backup),'remote_cache_updated':True}}))
''')
    TokenStore(local_cache).put(BotTokenRecord.from_dict(result.pop("record")))
    checkpoint("token_caches_synchronized", **result, local_cache_mode=oct(local_cache.stat().st_mode & 0o777))
    restored = remote(f'''
import pathlib,sys,json
base=pathlib.Path({BASE!r}); sys.path.insert(0,str(base/'mc/live_viewer'))
from beast_ctl import _spawn_detached
pid=_spawn_detached([str(base/'mc/.venv/bin/mc-live'),'--host','0.0.0.0','--port','18765','--env-file',str(base/'.env.beast'),'--bot','BeastBot_01','--viewer-url','http://127.0.0.1:8766','--director-url','http://127.0.0.1:8767'],cwd=base/'mc',log_path=base/'logs/mc_live.log')
print(json.dumps({{'mc_live_pid':pid}}))
''')
    checkpoint("remote_viewer_restored", **restored)
    subprocess.run(["bash", str(ROOT / "scripts/mc_services.sh"), "start"], check=True)
    for _ in range(30):
        try:
            with OPENER.open("http://127.0.0.1:8770/api/state", timeout=3) as response:
                state = json.load(response)
            if state.get("runtime"):
                checkpoint("complete", runtime=state["runtime"], telemetry=state.get("telemetry"))
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("Services started but console health unverified; inspect before another operation.")


if __name__ == "__main__":
    main()
