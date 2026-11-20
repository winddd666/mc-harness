import json
import os
import time

import pytest

from scripts import d1_overnight_watch as watcher
from scripts.d1_episode_audit import sig


def _bot(root, wave, bot="bot"):
    path = root / wave / bot
    path.mkdir(parents=True)
    return path


def _summary(bot):
    (bot / "wild-endurance-final.json").write_text(json.dumps({
        "curriculum_summary": {"episodes": [{"status": "goal_satisfied"}]},
    }))


def test_follow_catches_up_discovers_and_reopens(tmp_path, monkeypatch, capsys):
    old = _bot(tmp_path, "historical")
    _summary(old)
    os.utime(old / "wild-endurance-final.json", (1, 1))
    complete = _bot(tmp_path, "r29_newmix")
    _summary(complete)
    active = _bot(tmp_path, "r31_long")
    (active / "progress.events.jsonl").write_text("{}\n")
    writes = iter([
        lambda: _summary(active),
        lambda: (_bot(tmp_path, "r31_long", "late-bot"), _summary(_bot(tmp_path, "unfamiliar-name"))),
    ])

    class StopFollow(Exception):
        pass

    def on_sleep(_seconds):
        action = next(writes, None)
        if action is None:
            raise StopFollow
        action()

    monkeypatch.setattr(watcher.time, "sleep", on_sleep)
    with pytest.raises(StopFollow):
        watcher.follow(tmp_path, [], time.time() - 30, 45, 600)

    output = capsys.readouterr().out
    assert "historical" not in output
    assert output.count("WAVE SETTLED r29_newmix: summaries=1/1") == 1
    assert "WAVE DISCOVERED r31_long: summaries=0/1" in output
    assert "WAVE SETTLED r31_long: summaries=1/1" in output
    assert "WAVE REOPENED r31_long: summaries=1/2" in output
    assert "WAVE SETTLED unfamiliar-name: summaries=1/1" in output


def test_snapshot_prefers_final_summary_and_timeout_is_not_http(tmp_path):
    bot = _bot(tmp_path, "wave")
    _summary(bot)
    (bot / "curriculum-stale.json").write_text(json.dumps({"episodes": [{"status": "timeout"}]}))
    message = "运行总时限已到（555.8027942929766s）"
    (bot / "episode.events.jsonl").write_text("\n".join([
        json.dumps({"type": "model.failed", "error": message}),
        json.dumps({"type": "model.failed", "error": "Qwen API 返回 HTTP 429"}),
    ]))

    state = watcher.snapshot(bot.parent)
    assert state["episodes"] == {"goal_satisfied": 1}
    assert state["failed429"] == 1
    assert sig(message) == "episode_timeout"
