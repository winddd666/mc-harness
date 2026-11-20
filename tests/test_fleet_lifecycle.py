import json
from pathlib import Path

from scripts.fleet_lifecycle import LifecycleCounter
from scripts.v2_fleet import triage_bot_dir


FIXTURE = Path(__file__).parent / "fixtures" / "fleet_lifecycle_bug063.events.jsonl"


def test_fleet_distinguishes_lifecycle_records(tmp_path):
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    (tmp_path / "episode-01.events.jsonl").write_text("".join(lines[:1]), encoding="utf-8")
    (tmp_path / "episode-02.events.jsonl").write_text("".join(lines[1:]), encoding="utf-8")

    row = triage_bot_dir(tmp_path)

    assert row.deaths == 5  # One identified death and four ID-less receipts.
    assert row.death_receipts == 4
    assert row.respawns == 2
    assert row.connection_changes == 1
    assert row.mixed_guardian_incidents == 3


def test_deduplication_uses_bot_and_source_id():
    previous = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
    event = {"type": "death", "event_id": "101"}
    first = LifecycleCounter("bot-a")
    later_episode = LifecycleCounter("bot-a", first.seen_ids)
    other_bot = LifecycleCounter("bot-b", first.seen_ids)

    first.observe(previous)
    later_episode.observe(event)
    other_bot.observe(event)

    assert (first.deaths, later_episode.deaths, other_bot.deaths) == (1, 0, 1)


def test_episode_local_sequence_does_not_deduplicate_receipts():
    receipt = {"type": "mc.notable_events", "seq": 7, "text": "[死亡 pos=None lava]"}
    first = LifecycleCounter("bot-a")
    second = LifecycleCounter("bot-a", first.seen_ids)
    first.observe(receipt)
    second.observe(receipt)

    assert first.death_receipts == second.death_receipts == 1
