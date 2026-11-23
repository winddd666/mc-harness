#!/usr/bin/env python3
"""Lightweight read-only 'Right-Half Only' live thinking & tool stream viewer for OB bots (NJU_OB_1..3).

Reads live `.events.jsonl` and `curriculum-*.json` files directly from the runs directory — zero MC API
logins, zero video/3D overhead, supports instant switching between NJU_OB_1 / NJU_OB_2 / NJU_OB_3.
"""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import re
import socketserver
import urllib.parse
from typing import Any


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>MC Live — OB 实时思维流</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --card: #1c222b;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --amber: #d29922;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .brand {
    font-weight: 700;
    font-size: 15px;
    color: var(--accent);
    display: flex;
    align-items: center;
    gap: 8px;
    margin-right: 4px;
  }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .45; } }
  .tabs { display: flex; gap: 6px; flex-wrap: wrap; }
  .tab {
    background: var(--card);
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    transition: all .15s;
  }
  .tab:hover { color: var(--text); border-color: var(--accent); }
  .tab.active {
    background: rgba(88,166,255,0.16);
    color: #fff;
    border-color: var(--accent);
  }
  .tab .pts {
    margin-left: 6px;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(63,185,80,0.18);
    color: var(--green);
  }
  .spacer { flex: 1; }
  .ctrl {
    display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--muted);
  }
  .ctrl label { cursor: pointer; display: flex; align-items: center; gap: 4px; }
  .hud {
    background: #12171f;
    border-bottom: 1px solid var(--border);
    padding: 8px 16px;
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    font-size: 12px;
    color: var(--muted);
  }
  .hud b { color: var(--text); font-weight: 600; }
  .hud .badge {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 3px 8px;
  }
  #stream {
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px 40px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 980px;
    width: 100%;
    margin: 0 auto;
  }
  .item {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    line-height: 1.55;
    font-size: 13.5px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .item.thought {
    border-left: 3px solid var(--purple);
    background: #171826;
  }
  .item.text {
    border-left: 3px solid var(--accent);
  }
  .item.tool {
    border-left: 3px solid var(--amber);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px;
  }
  .item.tool.ok { border-left-color: var(--green); }
  .item.tool.fail { border-left-color: var(--red); }
  .item.meta {
    border-left: 3px solid var(--muted);
    color: var(--muted);
    font-size: 12px;
    padding: 6px 12px;
  }
  .hdr {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 5px;
    font-size: 11.5px;
    color: var(--muted);
  }
  .tag {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .3px;
  }
  .tag.thought { color: var(--purple); }
  .tag.text { color: var(--accent); }
  .tag.tool { color: var(--amber); }
  .tag.ok { color: var(--green); }
  .tag.fail { color: var(--red); }
  details summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 12px;
    margin-top: 4px;
  }
  pre {
    margin: 6px 0 0;
    padding: 8px;
    background: #0d1117;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 11.5px;
    color: #c9d1d9;
  }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>MC Live · 实时思维流</div>
  <div class="tabs" id="tabs"></div>
  <div class="spacer"></div>
  <div class="ctrl">
    <label><input type="checkbox" id="autoScroll" checked /> 自动滚到底部</label>
    <label><input type="checkbox" id="showRaw" /> 显示工具详情</label>
  </div>
</header>
<div class="hud" id="hud">加载中...</div>
<main id="stream"></main>

<script>
let currentBot = null;
let lastSig = "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

async function loadBots() {
  const res = await fetch("/api/bots");
  const data = await res.json();
  const bots = data.bots || [];
  if (!currentBot && bots.length) currentBot = bots[0].id;
  const tabs = document.getElementById("tabs");
  tabs.innerHTML = bots.map(b => `
    <button class="tab ${b.id === currentBot ? "active" : ""}" onclick="selectBot('${esc(b.id)}')">
      ${esc(b.label)} <span class="pts">${b.score}分</span>
    </button>
  `).join("");
}

function selectBot(id) {
  currentBot = id;
  lastSig = "";
  loadBots();
  pollFeed();
}

async function pollFeed() {
  if (!currentBot) return;
  try {
    const res = await fetch(`/api/feed?bot=${encodeURIComponent(currentBot)}`);
    const data = await res.json();
    const hud = document.getElementById("hud");
    hud.innerHTML = `
      <span class="badge">Bot: <b>${esc(data.label)}</b></span>
      <span class="badge">阶段: <b>${esc(data.stage)}</b></span>
      <span class="badge">当前集: <b>EP${data.episode} · ${esc(data.target)}</b></span>
      <span class="badge">得分: <b style="color:var(--green)">${data.score}</b></span>
      <span class="badge">状态: <b>${esc(data.heartbeat || "运行中")}</b></span>
    `;
    const sig = `${currentBot}:${data.items.length}:${data.last_seq}:${document.getElementById("showRaw").checked}`;
    if (sig === lastSig) return;
    lastSig = sig;
    const showRaw = document.getElementById("showRaw").checked;
    const el = document.getElementById("stream");
    el.innerHTML = data.items.map(it => {
      if (it.kind === "thought") {
        return `<div class="item thought"><div class="hdr"><span class="tag thought">🧠 思维推理 #${it.req || ""}</span><span>${esc(it.time)}</span></div>${esc(it.text)}</div>`;
      }
      if (it.kind === "text") {
        return `<div class="item text"><div class="hdr"><span class="tag text">💬 决策说明 #${it.req || ""}</span><span>${esc(it.time)}</span></div>${esc(it.text)}</div>`;
      }
      if (it.kind === "tool") {
        const cls = it.status === "ok" ? "ok" : (it.status === "fail" ? "fail" : "");
        const badge = it.status === "ok" ? "✅ 成功" : (it.status === "fail" ? "❌ 失败" : "⏳ 执行中");
        const argsStr = it.args ? esc(it.args) : "";
        const resHtml = it.result ? `<details ${showRaw ? "open" : ""}><summary>返回摘要 (${it.dur || ""})</summary><pre>${esc(it.result)}</pre></details>` : "";
        return `<div class="item tool ${cls}"><div class="hdr"><span class="tag ${cls || "tool"}">🔧 ${esc(it.name)} · ${badge}</span><span>${esc(it.time)}</span></div><div>${argsStr}</div>${resHtml}</div>`;
      }
      return `<div class="item meta">${esc(it.text)}</div>`;
    }).join("");
    if (document.getElementById("autoScroll").checked) {
      el.scrollTop = el.scrollHeight;
    }
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("showRaw").addEventListener("change", () => { lastSig = ""; pollFeed(); });
loadBots().then(pollFeed);
setInterval(loadBots, 4000);
setInterval(pollFeed, 1000);
</script>
</body>
</html>
"""


def discover_bots(runs_dir: pathlib.Path) -> list[dict[str, Any]]:
    bots: list[dict[str, Any]] = []
    if not runs_dir.exists():
        return bots
    for sub in sorted(runs_dir.iterdir()):
        if not sub.is_dir():
            continue
        m = re.match(r"^(nju_ob_\d+)_(.+)$", sub.name, re.I)
        if m:
            label = m.group(1).upper()
            stage = m.group(2)
        else:
            label = sub.name
            stage = ""
        score = 0
        cur_files = sorted(sub.glob("curriculum-*.json"))
        if cur_files:
            try:
                j = json.loads(cur_files[-1].read_text(encoding="utf-8"))
                eps = j.get("episodes") or []
                score = int(j.get("final_score") or (eps[-1].get("score_after") if eps else 0) or 0)
            except Exception:
                pass
        bots.append({"id": sub.name, "label": label, "stage": stage, "score": score, "dir": sub})
    return bots


def build_feed(bot_info: dict[str, Any], max_items: int = 80) -> dict[str, Any]:
    sub: pathlib.Path = bot_info["dir"]
    cur_files = sorted(sub.glob("curriculum-*.json"))
    episode = 1
    target = "运行中"
    score = bot_info["score"]
    if cur_files:
        try:
            j = json.loads(cur_files[-1].read_text(encoding="utf-8"))
            eps = j.get("episodes") or []
            if eps:
                episode = len(eps) + 1
                target = str(eps[-1].get("target") or target)
                score = int(j.get("final_score") or eps[-1].get("score_after") or score)
        except Exception:
            pass

    hb = ""
    logf = sub / "stdout.log"
    if logf.exists():
        try:
            lines = logf.read_text(encoding="utf-8", errors="ignore").splitlines()
            for l in reversed(lines[-80:]):
                if "heartbeat" in l:
                    m = re.search(r"(dim=\S+\s+pos=\[[^\]]+\]\s+hp=\S+)", l)
                    hb = m.group(1) if m else l.strip()[-90:]
                    break
        except Exception:
            pass

    ev_files = sorted(sub.glob("*.events.jsonl"))
    items: list[dict[str, Any]] = []
    last_seq = 0
    # Parse last 2 event files so recent episode transition is visible
    for ef in ev_files[-2:]:
        req_num = 0
        cur_thought: list[str] = []
        cur_text: list[str] = []
        cur_time = ""
        tool_map: dict[str, dict[str, Any]] = {}
        delta_by_slot: dict[tuple[int, int], dict[str, Any]] = {}
        meta_by_cid: dict[str, dict[str, Any]] = {}

        def _flush() -> None:
            if cur_thought:
                t = "".join(cur_thought).strip()
                if t:
                    items.append({"kind": "thought", "req": req_num, "time": cur_time, "text": t})
                cur_thought.clear()
            if cur_text:
                t = "".join(cur_text).strip()
                if t:
                    items.append({"kind": "text", "req": req_num, "time": cur_time, "text": t})
                cur_text.clear()

        try:
            raw_lines = ef.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            seq = int(e.get("seq") or 0)
            if seq > last_seq:
                last_seq = seq
            tp = e.get("type", "")
            el = e.get("elapsed")
            ts_str = f"+{float(el):.0f}s" if isinstance(el, (int, float)) else ""
            if tp == "model.started":
                _flush()
                req_num += 1
                cur_time = ts_str
            elif tp in ("model.reasoning.delta", "model.thought.delta"):
                cur_thought.append(str(e.get("delta") or e.get("text") or ""))
            elif tp == "model.text.delta":
                cur_text.append(str(e.get("delta") or e.get("text") or ""))
            elif tp == "model.tool.delta":
                slot = (int(e.get("request") or 0), int(e.get("index") or 0))
                rec = delta_by_slot.setdefault(slot, {"id": "", "name": "", "args": []})
                if e.get("id"):
                    rec["id"] = str(e["id"])
                    meta_by_cid[rec["id"]] = rec
                if e.get("name"):
                    rec["name"] = str(e["name"])
                if e.get("arguments"):
                    rec["args"].append(str(e["arguments"]))
            elif tp == "model.completed":
                _flush()
            elif tp == "tool.ready":
                _flush()
                cid = str(e.get("call_id") or f"c{seq}")
                mrec = meta_by_cid.get(cid) or {}
                nm = str(mrec.get("name") or e.get("name") or e.get("tool") or "tool")
                args = "".join(mrec.get("args") or []) or e.get("arguments") or e.get("args") or ""
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                entry = {
                    "kind": "tool",
                    "name": nm,
                    "args": str(args)[:320],
                    "status": "running",
                    "time": ts_str,
                    "t0": float(el or 0.0),
                    "result": "",
                    "dur": "",
                }
                tool_map[cid] = entry
                items.append(entry)
            elif tp == "tool.started":
                cid = str(e.get("call_id") or "")
                if cid and cid not in tool_map:
                    _flush()
                    nm = str(e.get("name") or e.get("tool") or "tool")
                    entry = {
                        "kind": "tool",
                        "name": nm,
                        "args": "",
                        "status": "running",
                        "time": ts_str,
                        "t0": float(el or 0.0),
                        "result": "",
                        "dur": "",
                    }
                    tool_map[cid] = entry
                    items.append(entry)
            elif tp == "tool.completed":
                cid = str(e.get("call_id") or "")
                res_raw = e.get("result")
                ok_val = None
                if isinstance(res_raw, str):
                    try:
                        parsed = json.loads(res_raw)
                        if isinstance(parsed, dict):
                            ok_val = parsed.get("ok")
                    except Exception:
                        pass
                    res_str = res_raw
                elif isinstance(res_raw, dict):
                    ok_val = res_raw.get("ok")
                    res_str = json.dumps(res_raw, ensure_ascii=False)
                else:
                    res_str = str(res_raw or "")
                entry = tool_map.get(cid)
                if entry is not None:
                    entry["status"] = "ok" if ok_val is not False else "fail"
                    dt = max(0.0, float(el or 0.0) - float(entry.get("t0") or 0.0))
                    entry["dur"] = f"{dt:.1f}s"
                    entry["result"] = res_str[:1200]
            elif tp == "guard.preempt":
                _flush()
                items.append({"kind": "meta", "text": f"⚡ 小脑抢占：{e.get('trigger')} ({ts_str})"})
        _flush()

    # Drop internal t0 before JSON response
    clean_items = [{k: v for k, v in it.items() if k != "t0"} for it in items[-max_items:]]
    return {
        "id": bot_info["id"],
        "label": bot_info["label"],
        "stage": bot_info["stage"],
        "episode": episode,
        "target": target,
        "score": score,
        "heartbeat": hb,
        "last_seq": last_seq,
        "items": clean_items,
    }


def make_handler(runs_dir: pathlib.Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/bots":
                bots = [{k: v for k, v in b.items() if k != "dir"} for b in discover_bots(runs_dir)]
                self._send_json({"bots": bots})
                return
            if parsed.path == "/api/feed":
                qs = urllib.parse.parse_qs(parsed.query)
                want = (qs.get("bot") or [""])[0]
                bots = discover_bots(runs_dir)
                chosen = next((b for b in bots if b["id"] == want or b["label"] == want), bots[0] if bots else None)
                if not chosen:
                    self._send_json({"error": "no bots found"}, status=404)
                    return
                self._send_json(build_feed(chosen))
                return
            self._send_json({"error": "not found"}, status=404)

    return Handler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--runs-dir", required=True)
    args = ap.parse_args()
    runs_dir = pathlib.Path(args.runs_dir).resolve()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(runs_dir))
    print(f"OB Thought Live listening on http://{args.host}:{args.port} (runs_dir={runs_dir})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
