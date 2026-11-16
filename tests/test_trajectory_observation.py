"""Regressions for observing runs while they are still being written (no sockets)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.mc_tools import McToolset
from mc_agent.live.server import LiveCoordinator
from mc_agent.live.state import ReplayIncrementer


def test_fatal_action_still_records_terminal_death_without_companion():
    polls = 0
    seen = []

    def handle(request):
        nonlocal polls
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "events":
            polls += 1
            result = {"events": [], "last_seq": 0} if polls == 1 else {
                "events": [{"seq": 1, "type": "death", "pos": [1, 70, 1]}] if polls == 2 else [],
                "last_seq": 1,
            }
        elif action == "status":
            result = {"pos": [1, 70, 1], "health": 2, "alive": True}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    def chat(messages, tools=None, **kwargs):
        return {"role": "assistant", "tool_calls": [{"id": "stabilize", "function": {
            "name": "mc_skill_stabilize", "arguments": "{}",
        }}]}

    with BotSession("test", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("terminal death", bot, chat_fn=chat, max_calls=2,
                              include_initial_snapshot=False, on_event=seen.append)
    assert report["status"] == "runtime_error"
    notes = [e for e in seen if e["type"] == "mc.notable_events"]
    assert len(notes) == 1
    assert "[死亡 pos=[1, 70, 1]" in notes[0]["text"]
    assert notes[0]["seq"] < next(e["seq"] for e in seen if e["type"] == "run.finished")


@pytest.mark.parametrize("entity_count", [1, 2, 7])
def test_wall_occlusion_never_proves_global_safety(entity_count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith('/status'):
            result = {
                'pos': [0.5, 64, 0.5], 'health': 20, 'ready': True,
                'nearest_hostile': {'id': 1, 'name': 'skeleton', 'dist': 4},
            }
        elif path.endswith('/get_entities'):
            result = {'entities': [
                {'id': i, 'name': 'skeleton', 'type': 'hostile',
                 'pos': [4.5, 64, 0.5] if i == 1 else [0.5, 64, i + 3.5]}
                for i in range(1, entity_count + 1)
            ]}
        elif path.endswith('/get_block'):
            result = {'name': 'stone' if body['pos'] in ([1, 65, 0], [0, 66, 0]) else 'air'}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={'ok': True, 'result': result})

    with BotSession('test-token', transport=httpx.MockTransport(handler)) as bot:
        st = json.loads(McToolset(bot).mc_status())['result']
    assessment = st['threat_assessment']
    assert st['enclosure']['roofed_pocket'] is True
    assert assessment['hostile_blocked_by_wall'] is True
    assert assessment['safe_in_place'] is False
    assert assessment['hostiles_checked'] == min(entity_count, 5)
    assert assessment['assessment_complete'] is (entity_count <= 5)
    assert assessment['all_checked_hostiles_occluded'] is (entity_count == 1)
    if entity_count > 1:
        assert any(not e['blocked_by_wall'] for e in assessment['nearby_hostiles'])


def test_failed_hostile_scan_keeps_safety_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/get_entities'):
            return httpx.Response(503, json={'ok': False, 'error': {'code': 'not_ready', 'message': 'loading'}})
        if request.url.path.endswith('/status'):
            return httpx.Response(200, json={'ok': True, 'result': {
                'pos': [0, 64, 0], 'nearest_hostile': {'id': 1},
            }})
        return httpx.Response(200, json={'ok': True, 'result': {'name': 'air'}})

    with BotSession('test-token', transport=httpx.MockTransport(handler)) as bot:
        assessment = json.loads(McToolset(bot).mc_status())['result']['threat_assessment']
    assert assessment['hostile_blocked_by_wall'] is None
    assert assessment['safe_in_place'] is False
    assert assessment['assessment_complete'] is False


def test_running_jsonl_contains_snapshot_death_and_spawn_without_companion() -> None:
    events_queries = 0
    turns = 0
    inc = ReplayIncrementer('running.events.jsonl')
    emitted_frames = []

    def observe(event: dict) -> None:
        emitted_frames.extend(inc.feed_events([event]))

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal events_queries
        path = request.url.path
        if path.endswith('/events'):
            events_queries += 1
            result = {'last_seq': 0, 'events': []} if events_queries == 1 else {
                'last_seq': 3, 'events': [
                    {'seq': 1, 'type': 'hurt', 'health': 2, 'source': 'arrow'},
                    {'seq': 2, 'type': 'death', 'pos': [1, 64, 1]},
                    {'seq': 3, 'type': 'spawn', 'pos': [10, 70, 10]},
                ],
            }
        elif path.endswith('/status'):
            result = {'pos': [1, 64, 1], 'health': 20, 'food': 20, 'ready': True}
        elif path.endswith('/inventory'):
            result = {'items': [{'name': 'stone_pickaxe', 'count': 1}],
                      'held': {'name': 'stone_pickaxe', 'count': 1}}
        elif path.endswith('/find_blocks'):
            result = {'blocks': []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={'ok': True, 'result': result})

    def chat(messages, tools=None, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 1:
            # Already observable before the first model request, while no report exists.
            assert inc.status['pos'] == [1, 64, 1]
            assert inc.inventory['items'][0]['name'] == 'stone_pickaxe'
            return {'role': 'assistant', 'tool_calls': [
                {'id': 'status1', 'type': 'function',
                 'function': {'name': 'mc_status', 'arguments': '{}'}},
            ]}
        notable = [f for f in emitted_frames if f['kind'] == 'game_events']
        assert len(notable) == 1
        assert [e['type'] for e in notable[0]['data']['game_events']] == ['hurt', 'death', 'spawn']
        assert inc.status['pos'] == [10, 70, 10]
        assert inc.inventory['items'] == []
        assert inc.status['held'] is None
        assert notable[0]['state_patch']['health'] == 20
        assert notable[0]['state_patch']['status']['pos'] == [10, 70, 10]
        assert '已重生' in messages[-1]['content']
        return {'role': 'assistant', 'content': '已重生，先恢复装备。'}

    with BotSession('test-token', bot_username='TestBot', transport=httpx.MockTransport(handler)) as bot:
        report = run_mc_agent('测试实时观测', bot, chat_fn=chat, stream=False, max_calls=4, on_event=observe)
    assert report['status'] == 'answered'
    assert report['events'][0]['initial_snapshot'].startswith('[开局实时状态快照]')
    assert len([e for e in report['events'] if e['type'] == 'mc.notable_events']) == 1
    # Loading the companion at replay time must not apply the same events twice.
    replay = ReplayIncrementer('running.events.jsonl', companion_report=report)
    replay.feed_events(report['events'])
    assert replay.to_bundle() == inc.to_bundle()
    assert len([m for m in inc.markers if m['kind'] == 'death']) == 1


@pytest.mark.parametrize(('history_size', 'include_has_more'), [(250, True), (1100, True), (250, False)])
def test_live_event_priming_consumes_all_history_before_alerts(
    tmp_path: Path, history_size: int, include_has_more: bool,
) -> None:
    async def run() -> None:
        history = [{'seq': i, 'type': 'death', 'pos': [0, 64, 0]} for i in range(1, history_size + 1)]
        requests = []
        broadcasts = []

        def handler(request: httpx.Request) -> httpx.Response:
            since = int(request.url.params['since'])
            requests.append(since)
            pending = [e for e in history if e['seq'] > since]
            page = pending[:100]
            result = {
                'events': page, 'last_seq': page[-1]['seq'] if page else len(history),
            }
            if include_has_more:
                result['has_more'] = len(pending) > 100
            return httpx.Response(200, json={'ok': True, 'result': result})

        coord = LiveCoordinator(
            settings=Settings(state_dir=tmp_path / '.state'), runs_dir=tmp_path,
            mc_transport=httpx.MockTransport(handler), enable_mc_polling=False,
        )
        async def capture(payload):
            broadcasts.append(payload)
        coord.broadcast = capture
        try:
            coord.live_state.apply_polled_status({'pos': [0, 64, 0], 'ready': True})
            await coord._poll_mc_events('test-token')
            if history_size > 1000:
                assert coord._events_primed is False
                assert coord._own_event_cursor == 1000
                await coord._poll_mc_events('test-token')
            assert coord._events_primed is True
            assert coord._own_event_cursor == history_size
            assert broadcasts == []
            assert coord.live_state.markers == []
            # After attach, multiple pages of real events must all be delivered once.
            new = [{'seq': history_size + i, 'type': 'hurt', 'health': 16} for i in range(1, 151)]
            history.extend(new)
            await coord._poll_mc_events('test-token')
            delivered = [e for msg in broadcasts for e in msg['events']]
            assert delivered == new
            await coord._poll_mc_events('test-token')
            assert [e for msg in broadcasts for e in msg['events']] == new
            assert requests == sorted(requests)
        finally:
            await coord.stop()
    asyncio.run(run())


def test_notable_death_preserves_inventory_already_observed_after_respawn() -> None:
    inc = ReplayIncrementer('inventory.events.jsonl')
    inv = {'items': [{'name': 'oak_log', 'count': 2}], 'held': None}
    frames = inc.feed_events([
        {'seq': 1, 'elapsed': 0, 'type': 'run.started', 'bot': 'TestBot'},
        {'seq': 2, 'elapsed': 1, 'type': 'tool.started', 'call_id': 'inv',
         'function': {'name': 'mc_inventory', 'arguments': '{}'}},
        {'seq': 3, 'elapsed': 2, 'type': 'tool.completed', 'call_id': 'inv',
         'result': json.dumps({'ok': True, 'result': inv})},
        {'seq': 4, 'elapsed': 2.1, 'type': 'mc.notable_events', 'call_id': 'inv',
         'text': '[死亡 pos=[1, 64, 1] ]; [已重生 pos=[10, 70, 10]]'},
    ])
    assert inc.inventory['items'] == inv['items']
    assert inc.status['pos'] == [10, 70, 10]
    assert any(m['kind'] == 'death' for m in frames[-1]['state_patch']['markers'])


def test_console_delivers_notable_events_on_completed_tool_card(tmp_path: Path) -> None:
    from mc_agent.console.server import ConsoleConfig, ConsoleController

    ctrl = ConsoleController(ConsoleConfig(runs_dir=tmp_path), settings=Settings(state_dir=tmp_path / '.state'))
    ctrl._turns.append({'turn_id': 'turn1', 'items': [], 'actor_requests': 1, 'phase': 'executing_tools'})
    state = {'tool_items': {}, 'skipped_calls': set()}
    updates = []
    ctrl._emit_ws = updates.append
    ctrl._handle_harness_event('turn1', state, {
        'type': 'tool.started', 'call_id': 's1', 'function': {'name': 'mc_status', 'arguments': '{}'},
    })
    ctrl._handle_harness_event('turn1', state, {
        'type': 'tool.completed', 'call_id': 's1', 'result': '{"ok": true, "result": {}}',
    })
    before = len(ctrl._turns[0]['items'])
    ctrl._handle_harness_event('turn1', state, {
        'type': 'mc.notable_events', 'call_id': 's1', 'text': '[受伤 health=12 by arrow]',
    })
    assert len(ctrl._turns[0]['items']) == before
    item = updates[-1]['item']
    assert updates[-1]['type'] == 'turn_item_upsert'
    assert item['status'] == 'ok'
    assert '[期间新事件] [受伤 health=12 by arrow]' in item['result']
