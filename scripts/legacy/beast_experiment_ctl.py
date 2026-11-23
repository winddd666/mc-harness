#!/usr/bin/env python3
"""Narrow, secret-free SSH operations for the authorized Beast test session."""
import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = '/home/xia/mc-beast'


def remote(code: str) -> dict:
    proc = subprocess.run(
        ['ssh', '-T', '-F', '/home/xzq/.ssh/config', 'nju-beast',
         'wsl', '-d', 'Ubuntu-22.04', '-u', 'xia', '--', 'python3', '-'],
        input=code, text=True, capture_output=True, timeout=45,
    )
    if proc.returncode:
        raise SystemExit(f'Remote inspection failed ({proc.returncode}): {proc.stderr[-1200:]}')
    return json.loads(proc.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['keep-inventory', 'rules', 'api-inspect', 'api-export', 'ground-fixture'])
    args = parser.parse_args()
    if args.operation == 'ground-fixture':
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8770/api/state', timeout=8) as response:
            state = json.load(response)
        if state.get('running'):
            raise SystemExit('Agent still running; refusing to apply fixture.')
        code = f'''
import pathlib,sys,json
sys.path.insert(0, {BASE!r}+'/mc/live_viewer')
from beast_ctl import _run_rcon_commands
results=_run_rcon_commands(pathlib.Path({BASE!r}+'/mc'),pathlib.Path({BASE!r}+'/.env.beast'),'127.0.0.1',25575,['gamerule keep_inventory'])
if not any('true' in row['reply'].lower() for row in results):
 raise SystemExit('keep_inventory was not verified true; fixture refused')
# Ground and two clear body blocks verified by the saved ground-scan fixture.
results += _run_rcon_commands(pathlib.Path({BASE!r}+'/mc'),pathlib.Path({BASE!r}+'/.env.beast'),'127.0.0.1',25575,['tp BeastBot_01 -162.5 71 -372.5'])
print(json.dumps({{'operation':'ground-fixture','assisted':True,'results':results}},ensure_ascii=False))
'''
    elif args.operation in ('keep-inventory', 'rules'):
        commands = ['gamerule keepInventory']
        if args.operation == 'keep-inventory':
            commands += ['gamerule keepInventory true', 'gamerule keepInventory']
        code = f'''
import pathlib,sys,json
sys.path.insert(0, {BASE!r}+'/mc/live_viewer')
from beast_ctl import _run_rcon_commands
commands={commands!r}
results=_run_rcon_commands(pathlib.Path({BASE!r}+'/mc'),pathlib.Path({BASE!r}+'/.env.beast'),'127.0.0.1',25575,commands)
if any('Unknown' in r['reply'] or 'Incorrect' in r['reply'] for r in results):
 results += _run_rcon_commands(pathlib.Path({BASE!r}+'/mc'),pathlib.Path({BASE!r}+'/.env.beast'),'127.0.0.1',25575,[c.replace('keepInventory','keep_inventory') for c in commands])
print(json.dumps({{'operation':{args.operation!r},'results':results}},ensure_ascii=False))
'''
    elif args.operation == 'api-export':
        code = f'''
import pathlib,json
root=pathlib.Path({BASE!r}+'/api')
paths=[root/'package.json']+list((root/'src').rglob('*.js'))
files={{str(p.relative_to(root)):p.read_text() for p in paths}}
for rel,name in [('node_modules/mineflayer-pathfinder/lib/goals.js','vendor/pathfinder-goals.js'),('node_modules/mineflayer-pathfinder/lib/movements.js','vendor/pathfinder-movements.js')]:
 p=root/rel
 if p.exists(): files[name]=p.read_text()
print(json.dumps({{'files':files}},ensure_ascii=False))
'''
    else:
        code = f'''
import pathlib,json
root=pathlib.Path({BASE!r}+'/api')
pkg=json.loads((root/'package.json').read_text())
files=[str(p.relative_to(root)) for p in root.rglob('*.js') if 'node_modules' not in p.parts and '.git' not in p.parts]
print(json.dumps({{'version':pkg.get('version'),'dependencies':pkg.get('dependencies'), 'entries':[p.name for p in root.iterdir()], 'js_files':files[:150]}},ensure_ascii=False))
'''
    result = remote(code)
    if args.operation == 'ground-fixture':
        destination = Path('experiments/20261115-until-1900/fixtures')
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (str(int(time.time())) + '-ground.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if args.operation == 'api-export':
        root = Path('experiments/20261115-until-1900/backend_snapshot')
        for name, content in result['files'].items():
            rel = Path(name)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('Unsafe source path')
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content)
        result = {'saved_files': len(result['files']), 'path': str(root)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
