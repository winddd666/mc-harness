#!/usr/bin/env python3
"""Run one version-checked experiment through mc-console and preserve live evidence."""
from __future__ import annotations
import argparse
import collections
from datetime import datetime
import json
from pathlib import Path
import runpy
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'experiments/20261115-until-1900'
BASE = 'http://127.0.0.1:8770'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEADLINE = datetime.fromisoformat('2026-11-15T19:00:00+08:00').timestamp()


def api(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'}, method='GET' if body is None else 'POST',
    )
    with OPENER.open(request, timeout=12) as response:
        return json.load(response)


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def summarize(run_id: str, folder: Path, final: dict) -> None:
    report_path = REPO / 'runs' / (run_id + '.json')
    events_path = REPO / 'runs' / (run_id + '.events.jsonl')
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    events = [json.loads(s) for s in events_path.read_text().splitlines() if s] if events_path.exists() else []
    calls = {}
    for event in events:
        if event['type'] == 'tool.started':
            calls[event['call_id']] = {'name': event['function']['name'], 'started': event['elapsed'],
                                      'args': event['function'].get('arguments'), 'seq': event['seq']}
        elif event['type'] == 'tool.completed':
            call = calls.setdefault(event['call_id'], {})
            call['duration'] = round(event['elapsed'] - call.get('started', event['elapsed']), 3)
            try:
                call['result'] = json.loads(event.get('result', '').partition('\n[期间新事件]')[0])
            except ValueError:
                call['result'] = {'text': event.get('result')}
    notes = '\n'.join(m.get('content', '') for m in report.get('messages', []) if m.get('role') == 'tool')
    models = [e for e in events if e['type'] == 'model.completed']
    summary = {
        'run_id': run_id, 'status': report.get('status'), 'error': report.get('error'),
        'answer': report.get('answer'), 'actor_requests': report.get('actor_requests'),
        'elapsed': events[-1].get('elapsed') if events else None, 'runtime': report.get('runtime'),
        'survival_guard': report.get('survival_guard', False), 'guardian': report.get('guardian'),
        'death_notifications': notes.count('[死亡 pos='), 'hurt_notifications': notes.count('[受伤 health='),
        'model_seconds': sum(e.get('duration', 0) for e in models),
        'tool_counts': dict(collections.Counter(c.get('name') for c in calls.values())),
        'tool_calls': list(calls.values()), 'final_telemetry': final.get('telemetry'),
    }
    save(folder / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'tool_calls'}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('case')
    parser.add_argument('--prompt-file', required=True)
    parser.add_argument('--timeout', type=float, default=1200)
    parser.add_argument('--calls', type=int, default=70)
    parser.add_argument('--repeat-limit', type=int, default=5)
    parser.add_argument('--survival-guard', action='store_true')
    args = parser.parse_args()
    folder = ROOT / 'cases' / args.case
    if (folder / 'start.json').exists():
        raise SystemExit('Case already started; inspect its existing run before taking further action.')
    state = api('/api/state')
    if state.get('running'):
        raise SystemExit('Console already has an active run: ' + str(state.get('active_turn_id')))
    runtime_info = runpy.run_path(str(REPO / 'src/mc_agent/runtime.py'))['runtime_info']
    expected = runtime_info()['source_revision']
    if state.get('runtime', {}).get('source_revision') != expected:
        raise SystemExit('Loaded console revision does not match current source. Restart while idle first.')
    remaining = DEADLINE - time.time()
    if remaining < 60:
        raise SystemExit('Experiment deadline reached; no new task started.')
    body = {'text': Path(args.prompt_file).read_text().strip(), 'provider': 'nju',
            'max_calls': args.calls, 'enable_thinking': False, 'immediate': False,
            'run_timeout_s': min(args.timeout, remaining - 10)}
    body['survival_guard'] = args.survival_guard
    save(folder / 'request.json', body)
    save(folder / 'before.json', state)
    started = api('/api/command', body)
    if not started.get('ok') or started.get('mode') != 'started':
        save(folder / 'start-response.json', started)
        raise SystemExit('No fresh experiment task was started.')
    run_id = started['turn_id']
    save(folder / 'start.json', started)
    print(json.dumps({'case': args.case, 'run_id': run_id, 'runtime': state['runtime'], 'request': body}, ensure_ascii=False), flush=True)
    event_path = REPO / 'runs' / (run_id + '.events.jsonl')
    offset = 0
    names = {}
    repeated = None
    streak = 0
    last_print = 0
    stop_sent = False
    with (folder / 'telemetry.jsonl').open('a') as log:
        while True:
            try:
                current = api('/api/state')
            except Exception as exc:
                print(json.dumps({'poll_error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), flush=True)
                time.sleep(5)
                continue
            turn = next((t for t in current.get('turns', []) if t['turn_id'] == run_id), {})
            sample = {'observed_at': time.time(), 'running': current.get('running'),
                      'active_turn_id': current.get('active_turn_id'), 'telemetry': current.get('telemetry'),
                      'turn': {k: turn.get(k) for k in ('status', 'phase', 'actor_requests', 'run_timeout_s')}}
            log.write(json.dumps(sample, ensure_ascii=False) + '\n'); log.flush()
            if time.monotonic() - last_print >= 30:
                print(json.dumps(sample, ensure_ascii=False), flush=True)
                last_print = time.monotonic()
            if event_path.exists():
                with event_path.open('rb') as stream:
                    stream.seek(offset)
                    chunk = stream.read()
                end = chunk.rfind(b'\n')
                if end >= 0:
                    offset += end + 1
                    for line in chunk[:end + 1].splitlines():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        kind = event.get('type')
                        if kind == 'tool.started':
                            names[event['call_id']] = event.get('function') or {}
                        elif kind == 'tool.completed':
                            name = names.get(event['call_id'], {})
                            try:
                                result = json.loads(event.get('result', '').partition('\n[期间新事件]')[0])
                            except ValueError:
                                result = {'text': event.get('result')}
                            print(json.dumps({'seq': event['seq'], 'elapsed': event['elapsed'], 'tool': name,
                                              'result': result}, ensure_ascii=False)[:2400], flush=True)
                            error = result.get('error') or result.get('partial_error') or result.get('last_error')
                            signature = (name.get('name'), str(error)) if result.get('ok') is False else None
                            streak = streak + 1 if signature and signature == repeated else (1 if signature else 0)
                            repeated = signature
                        elif kind in ('mc.notable_events', 'run.finished', 'model.failed', 'guardian.incident', 'guardian.action.started', 'guardian.action.completed'):
                            print(json.dumps(event, ensure_ascii=False)[:1800], flush=True)
            active = current.get('active_turn_id') == run_id
            reason = 'deadline' if time.time() >= DEADLINE else ('repeated_failure' if streak >= args.repeat_limit else None)
            if active and reason and not stop_sent:
                save(folder / 'observer-stop.json', {'reason': reason, 'time': time.time(), 'response': api('/api/stop', {})})
                stop_sent = True
            if turn.get('status') not in ('running', None) and not active:
                save(folder / 'after.json', current)
                summarize(run_id, folder, current)
                return
            if not active and current.get('running'):
                save(folder / 'ownership-lost.json', sample)
                raise SystemExit('Another task became active; observer will not control it.')
            time.sleep(5)


if __name__ == '__main__':
    main()
