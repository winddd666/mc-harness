#!/usr/bin/env python3
"""Build a reviewable API overlay from the immutable v1.3.9 source snapshot."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BASE = ROOT / "experiments/20261115-until-1900/backend_snapshot"
OUT = ROOT / "experiments/20261115-until-1900/backend_patched"


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"Expected exactly one source anchor: {before[:90]!r}")
    return text.replace(before, after, 1)


def main():
    src = (BASE / "src/session.js").read_text()
    src = replace_once(src, "    s.bot.loadPlugin(pathfinder)", "    const connectionBot = s.bot\n    s.bot.loadPlugin(pathfinder)")
    src = replace_once(src, "    s.bot.on('end', r => {\n      s.log('END', r)", "    s.bot.on('end', r => {\n      if (s.bot !== connectionBot) return\n      s.log('END', r)")
    src = replace_once(src, "      s.bot = null\n      s.clearPosture()", "      s.bot = null\n      require('./action-lifecycle').releaseDisconnectedAction(s)\n      s.clearPosture()")
    http = (BASE / "src/http.js").read_text()
    http = replace_once(http, "const crypto = require('crypto')", "const crypto = require('crypto')\nconst { beginAction, runAction, finishAction } = require('./action-lifecycle')")
    http = replace_once(http, "  // Action guards\n", "  // Action guards\n  let actionLease = null\n")
    http = replace_once(http, "    if (session.busy) return", "    if (session.actionQuarantined) return send(200, { ok: false, error: { code: 'action_quarantined', message: 'previous action timed out; disconnect and reconnect before resuming', requires_replan: true } })\n    if (session.busy) return")
    http = replace_once(http, "    session.busy = { name, t: Date.now() }", "    actionLease = beginAction(session, name)")
    http = replace_once(http, "    const result = await api[name](session, args)", "    const timeoutMs = Math.max(10, Math.min(120000, Number(config.session.actionTimeoutMs) || 30000))\n    const result = isAction\n      ? await runAction(session, actionLease, scoped => api[name](scoped, args), timeoutMs)\n      : await api[name](session, args)")
    http = replace_once(http, "    if (isAction && session.interruptedBy) {", "    if (isAction && session.busy === actionLease && session.interruptedBy) {")
    http = replace_once(http, "    if (isAction && session.bot && (!session.alive || session.bot.health <= 0))", "    if (isAction && session.busy === actionLease && session.bot && (!session.alive || session.bot.health <= 0))")
    http = replace_once(http, "    if (isAction && session.interruptedBy && !['interrupted', 'dead'].includes(e.code))", "    if (isAction && session.busy === actionLease && session.interruptedBy && !['interrupted', 'dead'].includes(e.code))")
    http = replace_once(http, "    if (['interrupted', 'dead'].includes(e.code) && session.bot)", "    if (['interrupted', 'dead'].includes(e.code) && session.bot && (!isAction || session.bot === actionLease.bot))")
    http = replace_once(http, "    if (!READONLY.has(name)) { session.busy = null; session.activeStopOn = null; session.applyPosture() }", "    if (isAction) finishAction(session, actionLease)")
    contents = {"src/session.js": src, "src/http.js": http, "src/action-lifecycle.js": (HERE / "action-lifecycle.js").read_text()}
    hashes = {}
    for name, content in contents.items():
        dst = OUT / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content)
        original = BASE / name
        hashes[name] = {"before": hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None,
                        "after": hashlib.sha256(content.encode()).hexdigest()}
    (OUT / "manifest.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(json.dumps({"overlay": str(OUT), "files": list(contents)}))


if __name__ == "__main__":
    main()
