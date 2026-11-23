#!/usr/bin/env python3
"""One bounded reconnect with the existing token, after respecting death-loop cooldown."""
import json
from pathlib import Path
import time
import urllib.request

import httpx

from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.errors import ApiError


ROOT = Path(__file__).resolve().parents[1]


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8770/api/state", timeout=8) as response:
        state = json.load(response)
    if state.get("running"):
        raise SystemExit("An agent is still running; recovery refused.")
    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    record = TokenStore(settings.state_dir / "tokens.json").get("BeastBot_01")
    if record is None:
        raise SystemExit("No cached token; refusing fresh login.")
    rows = []
    with BotSession(record.token, bot_username="BeastBot_01", base_url=settings.mc_api_base_url, timeout=8) as bot:
        events = bot.events(since=0, limit=500).get("events", [])
        until = max([e.get("cooldown_until", 0) for e in events if e.get("type") == "death_loop"] + [0])
        if time.time() * 1000 < until:
            raise SystemExit(f"Death-loop cooldown still active until {until}; no reconnect attempted.")

        def call(action, body=None, method="POST"):
            try:
                row = {"ok": True, "result": bot.call(action, body, method=method, allow_reauth=False)}
            except ApiError as exc:
                row = {"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.context}}
            except httpx.HTTPError as exc:
                row = {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}
            rows.append({"action": action, "time": time.time(), **row})
            return row.get("result", {})

        call("status", method="GET")
        call("connect")
        deadline = time.monotonic() + 15
        status = {}
        while time.monotonic() < deadline:
            status = call("status", method="GET")
            if status.get("ready"):
                break
            time.sleep(0.5)
        if status.get("busy"):
            call("stop")
            call("status", method="GET")
        if status.get("ready"):
            call("inventory", method="GET")
    folder = ROOT / "experiments/20261115-until-1900/connection-diagnosis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{int(time.time())}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False)[:3500], flush=True)
    print("saved", path)


if __name__ == "__main__":
    main()
