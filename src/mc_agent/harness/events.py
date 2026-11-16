"""过程事件、即时终端展示和追加写入的 JSONL 日志。（源自 winddd666/nano-agent）"""

import inspect
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .control import RequestCancelled, check_control


class EventSink:
    def __init__(self, on_event=None, *, scope="actor", parent=None):
        self.events = []
        self.scope = scope
        self.parent = parent
        self.listener = ConsoleRenderer() if on_event is None else on_event
        self.started = perf_counter()
        self.observer_error = None

    def emit(self, event_type, **data):
        if self.parent is not None:
            event = self.parent.emit(event_type, scope=self.scope, **data)
            self.events.append(event)
            return event
        event = {
            "seq": len(self.events) + 1,
            "time": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(perf_counter() - self.started, 4),
            "scope": self.scope,
            **deepcopy(data),
            "type": event_type,
        }
        self.events.append(event)
        try:
            self.listener(event)
        except RuntimeError as exc:
            self.observer_error = str(exc)
            raise
        return event


def _supports_kwarg(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == name:
            return True
    return False


def request_model(chat_fn, messages, tools, *, stream, events, request, control=None, enable_thinking: bool | None = None):
    """把一次请求的生命周期和增量输出接到统一事件通道。"""
    req_meta = {"request": request, "stream": stream, "message_count": len(messages), "tool_count": len(tools)}
    if enable_thinking is not None:
        req_meta["thinking"] = bool(enable_thinking)
    events.emit("model.request", **req_meta)
    events.emit("model.started", request=request, stream=stream)
    started = perf_counter()

    def forward(event):
        events.emit(
            "model." + event["type"],
            request=request,
            **{key: value for key, value in event.items() if key != "type"},
        )

    try:
        check_control(control)
        extra = {} if control is None else {"control": control}
        if enable_thinking is not None and _supports_kwarg(chat_fn, "enable_thinking"):
            extra["enable_thinking"] = bool(enable_thinking)
        if stream:
            message = chat_fn(messages, tools=tools, stream=True, on_event=forward, **extra)
        else:
            message = chat_fn(messages, tools=tools, **extra)
            if isinstance(message, dict) and message.get("content"):
                events.emit("model.text", request=request, text=message["content"])
        check_control(control)
        events.emit("model.completed", request=request, message=message, duration=round(perf_counter() - started, 4))
        return message
    except (RuntimeError, KeyboardInterrupt) as exc:
        events.emit(
            "model.failed",
            request=request,
            error="用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc),
            cancelled=isinstance(exc, RequestCancelled),
        )
        raise


class ConsoleRenderer:
    def __init__(self):
        self.open_text = False
        self.seen_tools = set()

    def newline(self):
        if self.open_text:
            print(flush=True)
            self.open_text = False

    def __call__(self, event):
        kind = event["type"]
        if kind == "model.started":
            self.newline()
            wait_info = ""
            print(
                f"\n[Qwen 请求 #{event['request']}，{'流式' if event['stream'] else '非流式'}{wait_info}]",
                flush=True,
            )
        elif kind == "model.rate_wait":
            if event.get("wait_s", 0) >= 0.1:
                self.newline()
                print(f"[RPM 限速等待 {event['wait_s']:.2f}s]", flush=True)
        elif kind in ("model.text.delta", "model.text"):
            print(event["text"], end="", flush=True)
            self.open_text = True
        elif kind == "model.tool.delta":
            key = (event["request"], event["index"])
            if key not in self.seen_tools:
                self.seen_tools.add(key)
                self.newline()
                fn_hint = f" {event['name']}" if event.get("name") else ""
                print(f"[接收工具调用 #{event['index']}{fn_hint}]", flush=True)
        elif kind in ("model.completed", "model.failed"):
            self.newline()
        elif kind == "tool.started":
            self.newline()
            fn = event.get("function") or {}
            print(f"[执行工具] {fn.get('name')}({fn.get('arguments', '')})", flush=True)
        elif kind == "tool.completed":
            preview = event["result"]
            if len(preview) > 500:
                preview = preview[:480] + f"... (共 {len(event['result'])} chars)"
            print(f"[工具返回] {preview}", flush=True)
        elif kind == "tool.skipped":
            print(f"[工具 {event['call_id']} 跳过：{event['reason']}]", flush=True)
        elif kind == "input.received":
            self.newline()
            print(f"[已接收新指令 #{event['id']}] {event['text']}", flush=True)
        elif kind == "input.applied":
            print(f"[指令 #{event['input_id']} 已注入上下文（节点 {event['boundary']}）]", flush=True)
        elif kind == "run.finished":
            self.newline()
            if event["status"] == "answered":
                print("[MC Agent 任务完成]", flush=True)
            else:
                print(f"[运行结束：{event['status']}] {event.get('error') or ''}", flush=True)


class JsonlEvents:
    """每个事件先写入并 flush，再展示。"""

    def __init__(self, path):
        self.path = Path(path)
        self.renderer = ConsoleRenderer()
        self.failed = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("x", encoding="utf-8")
        return self

    def __call__(self, event):
        if not self.failed:
            try:
                self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
                self.file.flush()
            except OSError:
                self.failed = True
                raise RuntimeError("事件日志写入失败，停止后续执行。") from None
        self.renderer(event)

    def __exit__(self, *args):
        self.file.close()
