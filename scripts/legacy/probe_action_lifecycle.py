#!/usr/bin/env python3
"""Real API regression: disconnect during a motionless timed action, then reuse the same token."""
from concurrent.futures import ThreadPoolExecutor
import atexit
import json
from pathlib import Path
import time
import urllib.request

from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.errors import ApiError


def main():
    root = Path(__file__).resolve().parents[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
        if json.load(response).get("running"):
            raise SystemExit("An agent is running; refusing concurrent controls.")
    settings = Settings.load(dotenv_path=root / ".env.beast")
    record = TokenStore(settings.state_dir / "tokens.json").get("BeastBot_01")
    if record is None:
        raise SystemExit("No cached token. No login will be attempted.")
    rows = []
    path = root / "experiments/20261115-until-1900/action-lifecycle-live.json"
    report = {"verified": False, "rows": rows}
    atexit.register(lambda: path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n"))
    with BotSession(record.token, bot_username="BeastBot_01", base_url=settings.mc_api_base_url, timeout=15) as bot:
        def call(action, body=None, method="POST"):
            started = time.monotonic()
            try:
                row = {"ok": True, "result": bot.call(action, body, method=method, allow_reauth=False)}
            except ApiError as error:
                row = {"ok": False, "error": {"code": error.code, "message": error.message, **error.context}}
            rows.append({"action": action, "time": time.time(), "elapsed": time.monotonic() - started, **row})
            return row

        before = call("status", method="GET")["result"]
        if not before.get("ready") or before.get("busy"):
            raise SystemExit("Bot is not ready and idle.")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, "control", {"sneak": True, "ms": 3000})
            for _ in range(20):
                status = call("status", method="GET").get("result", {})
                if status.get("busy") == "control":
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("The timed action was not observed active.")
            stopped_at = time.monotonic()
            call("disconnect")
            outcome = pending.result(timeout=3)
            response_delay = time.monotonic() - stopped_at
        assert not outcome["ok"] and outcome["error"]["cause"] == "disconnected", outcome
        call("connect")
        for _ in range(40):
            state = call("status", method="GET").get("result", {})
            if state.get("ready"):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("Reconnected bot was not ready within 10s.")
        assert state.get("busy") is None, state.get("busy")
        after_action = call("control", {"sneak": True, "ms": 100})
        assert after_action["ok"], after_action
        inventory = call("inventory", method="GET")
    report.update(verified=True, disconnect_response_seconds=response_delay, same_token_reused=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))
    print(json.dumps({"inventory": inventory, "saved": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
