"""Qwen API 接入层（基于 winddd666/nano-agent model.py 改造）。

支持两类 OpenAI Chat Completions 兼容 Qwen 后端：
1. 南大比赛官方 Qwen 接口（默认）：`https://api.nju-aia.com/v1`，`model="default"`（后端 qwen3.6-35b-a3b），
   内置共享 RPM 滑动窗口限速器（默认 24 RPM，防止触发 30 RPM 硬限流），使用 `chat_template_kwargs: {"enable_thinking": false}` 关闭思考。
2. 阿里云百炼 DashScope 或任意自定义 Qwen 接口：设置 `QWEN_API_KEY`（或 `DASHSCOPE_API_KEY`）、`QWEN_BASE_URL`、`QWEN_MODEL`。
"""

from __future__ import annotations

import collections
from collections.abc import Callable
import dataclasses
import json
import math
import os
import pathlib
import queue
import random
import re
import threading
import time

import httpx

from ..config import DEFAULT_NJU_BASE_URL, parse_dotenv, should_trust_env
from ..errors import ConfigError
from .control import RequestCancelled, check_control

DASHSCOPE_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_DEFAULT_MODEL = "qwen-plus"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
OPENCODE_DEFAULT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
OPENCODE_DEFAULT_MODEL = "glm-5.3"
RETRYABLE_HTTP_STATUSES = (429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526)


class OutputTruncatedError(RuntimeError):
    """模型响应触及 max_tokens 截断且未形成任何完整合法工具调用。"""


@dataclasses.dataclass(frozen=True, slots=True)
class QwenSpec:
    """可序列化模型参数描述。"""

    dotenv_path: str | None = None
    fallback_dotenv_path: str | None = ".env"
    provider: str | None = None
    base_url_override: str | None = None
    model_override: str | None = None
    rpm_budget_override: int | None = None
    enable_thinking: bool = False
    max_tokens: int = 1024
    temperature: float = 0.0
    timeout_s: float = 135.0
    post_cancel_cooldown_s: float = 4.0


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedQwenEndpoint:
    endpoint: str
    base_url: str
    model: str
    api_key: str = dataclasses.field(repr=False)
    rpm_budget: int = 24
    extra_headers: tuple[tuple[str, str], ...] = ()

    def extra_body(self, *, enable_thinking: bool) -> dict:
        if "deepseek.com" in self.base_url:
            return {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}
        if "dashscope.aliyuncs.com" in self.base_url:
            return {"enable_thinking": bool(enable_thinking)}
        if "opencode.ai" in self.base_url:
            return {"reasoning_effort": "low" if enable_thinking else "none"}
        return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}


def _load_opencode_credentials(merged: dict[str, str]) -> tuple[str | None, str | None]:
    """读取 OpenCode Go API Key 与可选 session id（默认 ~/.local/share/opencode/auth.json，绝不打印密钥）。"""
    key = merged.get("OPENCODE_API_KEY", "").strip()
    if not key:
        auth_path = pathlib.Path(
            merged.get("OPENCODE_AUTH_FILE", "").strip()
            or pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json"
        )
        try:
            if auth_path.is_file():
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
                entry = payload.get("opencode-go") or {}
                candidate = entry.get("key")
                if isinstance(candidate, str) and candidate.strip():
                    key = candidate.strip()
        except (OSError, ValueError, TypeError):
            pass
    session = merged.get("OPENCODE_SESSION_ID", "").strip() or None
    return (key or None), session


def _load_local_deepseek_key(merged: dict[str, str]) -> str | None:
    """按环境变量 -> 本机 Claude DeepSeek 配置 -> quick-translate 配置顺序加载密钥（绝不打印或外泄）。"""
    env_key = merged.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key
    claude_settings = pathlib.Path.home() / ".claude" / "settings.json"
    try:
        if claude_settings.is_file():
            env_cfg = json.loads(claude_settings.read_text(encoding="utf-8")).get("env") or {}
            base = str(env_cfg.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
            token = env_cfg.get("ANTHROPIC_AUTH_TOKEN")
            if base.startswith("https://api.deepseek.com") and isinstance(token, str) and token.strip():
                return token.strip()
    except (OSError, ValueError, TypeError):
        pass
    qt_cfg = pathlib.Path.home() / ".config" / "quick-translate" / "config.json"
    try:
        if qt_cfg.is_file():
            token = json.loads(qt_cfg.read_text(encoding="utf-8")).get("api_key")
            if isinstance(token, str) and token.strip():
                return token.strip()
    except (OSError, ValueError, TypeError):
        pass
    return None


def resolve_qwen_endpoint(spec: QwenSpec) -> ResolvedQwenEndpoint:
    """按 环境变量 -> 指定 .env -> 默认 .env 顺序解析 LLM 连接参数；绝不打印密钥。"""
    merged: dict[str, str] = {}
    if spec.fallback_dotenv_path:
        fb = pathlib.Path(spec.fallback_dotenv_path)
        if fb.is_file():
            merged.update(parse_dotenv(fb))
    if spec.dotenv_path:
        dp = pathlib.Path(spec.dotenv_path)
        if dp.is_file():
            merged.update(parse_dotenv(dp))
    for k, v in os.environ.items():
        if v:
            merged[k] = v

    def pick(*names: str) -> str | None:
        for n in names:
            val = merged.get(n, "").strip()
            if val:
                return val
        return None

    provider = (spec.provider or pick("MC_LLM_PROVIDER") or "").strip().lower()
    if not provider and spec.base_url_override and "deepseek.com" in spec.base_url_override:
        provider = "deepseek"
    if not provider and spec.base_url_override and "opencode.ai" in spec.base_url_override:
        provider = "opencode"

    if provider == "opencode":
        api_key, session_id = _load_opencode_credentials(merged)
        if not api_key:
            raise ConfigError(
                "未配置 OpenCode API 密钥：请设置 OPENCODE_API_KEY，"
                "或确认 ~/.local/share/opencode/auth.json 中存在 opencode-go.key。"
            )
        base_url = (
            spec.base_url_override or pick("OPENCODE_BASE_URL") or OPENCODE_DEFAULT_BASE_URL
        ).rstrip("/")
        model = spec.model_override or pick("OPENCODE_MODEL") or OPENCODE_DEFAULT_MODEL
        raw_rpm = pick("OPENCODE_RPM_BUDGET")
        rpm_budget = (
            spec.rpm_budget_override
            if spec.rpm_budget_override is not None
            else (int(raw_rpm) if raw_rpm else 0)
        )
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        if not session_id:
            session_id = f"ses_mc_{os.getpid()}_{int(time.time())}"
        return ResolvedQwenEndpoint(
            endpoint=endpoint,
            base_url=base_url,
            model=model,
            api_key=api_key,
            rpm_budget=rpm_budget,
            extra_headers=(("x-opencode-session", session_id),),
        )

    if provider == "deepseek":
        api_key = _load_local_deepseek_key(merged)
        if not api_key:
            raise ConfigError(
                "未配置 DeepSeek API 密钥：请设置 DEEPSEEK_API_KEY，"
                "或检查本机 ~/.claude/settings.json / ~/.config/quick-translate/config.json。"
            )
        base_url = (spec.base_url_override or pick("DEEPSEEK_BASE_URL") or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")
        model = spec.model_override or pick("DEEPSEEK_MODEL") or DEEPSEEK_DEFAULT_MODEL
        raw_rpm = pick("DEEPSEEK_RPM_BUDGET")
        rpm_budget = (
            spec.rpm_budget_override
            if spec.rpm_budget_override is not None
            else (int(raw_rpm) if raw_rpm else 0)
        )
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        return ResolvedQwenEndpoint(
            endpoint=endpoint,
            base_url=base_url,
            model=model,
            api_key=api_key,
            rpm_budget=rpm_budget,
        )

    qwen_key = pick("QWEN_API_KEY")
    nju_key = pick("NJU_API_KEY")
    dash_key = pick("DASHSCOPE_API_KEY")

    if provider == "dashscope":
        api_key = dash_key or qwen_key
    elif provider == "nju":
        api_key = nju_key or qwen_key
    else:
        api_key = qwen_key or nju_key or dash_key

    if not api_key:
        raise ConfigError(
            "未配置 LLM API 密钥：请在 .env / .env.local 或环境变量中设置 "
            "NJU_API_KEY（比赛接口）、DEEPSEEK_API_KEY（--provider deepseek）或 QWEN_API_KEY / DASHSCOPE_API_KEY。"
        )

    if spec.base_url_override:
        base_url = spec.base_url_override.rstrip("/")
    elif provider == "dashscope":
        base_url = (pick("DASHSCOPE_BASE_URL", "QWEN_BASE_URL") or DASHSCOPE_DEFAULT_BASE_URL).rstrip("/")
    elif provider == "nju":
        base_url = (pick("NJU_BASE_URL", "QWEN_BASE_URL") or DEFAULT_NJU_BASE_URL).rstrip("/")
    elif qwen_key:
        base_url = (pick("QWEN_BASE_URL", "NJU_BASE_URL") or DEFAULT_NJU_BASE_URL).rstrip("/")
    elif nju_key:
        base_url = (pick("NJU_BASE_URL", "QWEN_BASE_URL") or DEFAULT_NJU_BASE_URL).rstrip("/")
    else:
        base_url = (pick("QWEN_BASE_URL") or DASHSCOPE_DEFAULT_BASE_URL).rstrip("/")

    if spec.model_override:
        model = spec.model_override
    elif "dashscope.aliyuncs.com" in base_url and not pick("QWEN_MODEL"):
        model = DASHSCOPE_DEFAULT_MODEL
    else:
        model = pick("QWEN_MODEL", "NJU_MODEL") or "default"

    # 比赛网关防坑：网关仅向 token 授权别名 "default"，直传底层权重名 "qwen3.6-35b-a3b" 会返 HTTP 403
    if "nju-aia.com" in base_url and model.lower() == "qwen3.6-35b-a3b":
        model = "default"

    if spec.rpm_budget_override is not None:
        rpm_budget = spec.rpm_budget_override
    else:
        raw_rpm = pick("QWEN_RPM_BUDGET", "NJU_RPM_BUDGET")
        rpm_budget = int(raw_rpm) if raw_rpm else (24 if "nju-aia.com" in base_url else 0)

    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    return ResolvedQwenEndpoint(
        endpoint=endpoint,
        base_url=base_url,
        model=model,
        api_key=api_key,
        rpm_budget=rpm_budget,
    )


class RpmLimiter:
    """进程内线程安全滑动窗口限速器（60.5s 窗口，适配比赛 Qwen 30 RPM 硬限流）。"""

    def __init__(self, rpm_budget: int = 24, window_s: float = 60.5) -> None:
        self.rpm_budget = rpm_budget
        self.window_s = window_s
        self._lock = threading.Lock()
        self._times: collections.deque[float] = collections.deque()

    def acquire(self, *, control: Callable[[], bool] | None = None) -> float:
        if self.rpm_budget <= 0:
            return 0.0
        started = time.monotonic()
        while True:
            check_control(control)
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window_s:
                    self._times.popleft()
                if len(self._times) < self.rpm_budget:
                    self._times.append(now)
                    return time.monotonic() - started
                wait_s = max(0.0, self._times[0] + self.window_s - now)
            if wait_s > 0:
                time.sleep(min(0.15, wait_s))


def _sse_data(response):
    parts = []
    for raw in response:
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise RuntimeError("流式响应不是有效 UTF-8。") from None
        else:
            line = str(raw).rstrip("\r\n")
        if not line:
            if parts:
                yield "\n".join(parts)
                parts = []
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
    if parts:
        yield "\n".join(parts)


def _stream_message(response, on_event):
    text, reasoning, calls = [], [], {}
    finish_reason, done = None, False

    def emit(kind, **data):
        if on_event is not None:
            on_event({"type": kind, **data})

    for data in _sse_data(response):
        if data.strip() == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            raise RuntimeError("流式事件不是有效 JSON。") from None
        if not isinstance(chunk, dict) or chunk.get("error"):
            err_msg = chunk.get("error") if isinstance(chunk, dict) else "unknown"
            raise RuntimeError(f"流式 API 返回错误：{err_msg}")
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise RuntimeError("流式事件缺少 choices。")
        if chunk.get("usage") is not None:
            emit("usage", usage=chunk["usage"])
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise RuntimeError("当前只支持单个流式 choice。")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise RuntimeError("流式事件缺少 delta。")
            if delta.get("role") not in (None, "assistant"):
                raise RuntimeError("流式响应角色不是 assistant。")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise RuntimeError("流式正文必须是字符串。")
            rc = delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                reasoning.append(rc)
            fragments = delta.get("tool_calls") or []
            if not isinstance(fragments, list):
                raise RuntimeError("流式工具片段必须是列表。")
            if content:
                text.append(content)
                emit("text.delta", text=content)
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise RuntimeError("工具片段不是对象。")
                index = fragment.get("index", 0)
                if type(index) is not int or index < 0:
                    raise RuntimeError("工具片段缺少有效 index。")
                if fragment.get("type") not in (None, "function"):
                    raise RuntimeError("不支持此工具片段类型。")
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise RuntimeError("工具片段缺少 function 对象。")
                pieces = (
                    fragment.get("id") or "",
                    function.get("name") or "",
                    function.get("arguments") or "",
                )
                if any(not isinstance(piece, str) for piece in pieces):
                    raise RuntimeError("工具片段字段必须是字符串。")
                call = calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call["id"] += pieces[0]
                call["function"]["name"] += pieces[1]
                call["function"]["arguments"] += pieces[2]
                emit("tool.delta", index=index, id=pieces[0], name=pieces[1], arguments=pieces[2])
            reason = choice.get("finish_reason")
            if reason is not None:
                finish_reason = reason

    if not done or finish_reason is None:
        raise RuntimeError("模型流中断：缺少完整结束标记 [DONE] 或 finish_reason。")

    ordered = [calls[index] for index in sorted(calls)] if calls else []
    for idx, call in enumerate(ordered):
        if not call["id"]:
            call["id"] = f"call_{idx}"

    if finish_reason == "length":
        valid_calls = _filter_complete_prefix_calls(ordered)
        if valid_calls:
            emit("length_recovered", kept_calls=len(valid_calls), dropped_calls=len(ordered) - len(valid_calls))
            content_str: str | None = "".join(text) or None
            message = {"role": "assistant", "content": content_str, "tool_calls": valid_calls}
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            return message
        content_str = "".join(text) or None
        if content_str:
            cleaned, extracted = _extract_text_tool_calls(content_str)
            if extracted:
                emit("length_recovered", kept_calls=len(extracted), dropped_calls=0)
                message = {"role": "assistant", "content": cleaned, "tool_calls": extracted}
                if reasoning:
                    message["reasoning_content"] = "".join(reasoning)
                return message
        raise OutputTruncatedError("模型响应达到 max_tokens 被截断（finish_reason=length），未生成完整工具调用。")

    if finish_reason not in ("stop", "tool_calls"):
        raise RuntimeError(f"模型响应未正常完成（finish_reason={finish_reason}，可能 max_tokens 不足）。")
    if calls and finish_reason not in ("tool_calls", "stop"):
        raise RuntimeError("工具请求与流式结束原因不一致。")
    if finish_reason == "tool_calls" and not calls:
        raise RuntimeError("模型声明了工具请求但未返回工具。")
    if calls and set(calls) != set(range(len(calls))):
        raise RuntimeError("流式工具请求缺少部分 index。")
    ids = [call["id"] for call in ordered]
    if any(not call["function"]["name"] for call in ordered) or len(set(ids)) != len(ids):
        raise RuntimeError("流式工具请求缺少函数名称或 id 重复。")
    content_str = "".join(text) or None
    if not ordered and content_str:
        content_str, ordered = _extract_text_tool_calls(content_str)
    message = {"role": "assistant", "content": content_str}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if ordered:
        message["tool_calls"] = ordered
    return message


def _filter_complete_prefix_calls(ordered_calls: list[dict]) -> list[dict]:
    """当输出被 max_tokens 截断时，保留前缀中 JSON 参数完整且可解析的合法 tool_calls。"""
    kept: list[dict] = []
    seen_ids: set[str] = set()
    for idx, call in enumerate(ordered_calls):
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or ""
        if not name:
            break
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except ValueError:
            break
        if not isinstance(parsed, dict):
            break
        cid = call.get("id") or f"call_{idx}"
        if cid in seen_ids:
            cid = f"call_{idx}"
        seen_ids.add(cid)
        kept.append(
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(parsed, ensure_ascii=False)},
            }
        )
    return kept


_TEXT_TOOL_FENCE_RE = re.compile(
    r"(?:^|\n)\s*(?:tool_calls\s*:?\s*)?```(?:json)?\s*\n?([\s\S]*?)\n?```\s*",
    re.IGNORECASE,
)


def _normalize_text_tool_entry(item: object, index: int) -> dict | None:
    if not isinstance(item, dict):
        return None
    fn = item.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        raw_args = fn.get("arguments", {})
    else:
        name = item.get("name")
        raw_args = item.get("arguments", item.get("args", {}))
    if not isinstance(name, str) or not name.startswith("mc_"):
        return None
    if isinstance(raw_args, dict):
        args_str = json.dumps(raw_args, ensure_ascii=False)
    elif isinstance(raw_args, str):
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed_args, dict):
            return None
        args_str = json.dumps(parsed_args, ensure_ascii=False)
    else:
        return None
    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = f"text_call_{index}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args_str,
        },
    }


def _extract_text_tool_calls(raw_text: str) -> tuple[str | None, list[dict]]:
    """Fallback parser for endpoints/models that emit fenced JSON tool_calls in text."""
    if "tool_calls" not in raw_text and "mc_" not in raw_text:
        return raw_text, []
    for match in _TEXT_TOOL_FENCE_RE.finditer(raw_text):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
            items = payload["tool_calls"]
        elif isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            continue
        if not items:
            continue
        parsed: list[dict] = []
        seen_ids: set[str] = set()
        valid = True
        for idx, item in enumerate(items, start=1):
            entry = _normalize_text_tool_entry(item, idx)
            if entry is None:
                valid = False
                break
            cid = entry["id"]
            if cid in seen_ids:
                entry["id"] = f"text_call_{idx}"
            seen_ids.add(entry["id"])
            parsed.append(entry)
        if valid and parsed:
            cleaned = (raw_text[: match.start()] + "\n" + raw_text[match.end() :]).strip()
            return (cleaned or None), parsed
    return raw_text, []


def _make_http_client(spec: QwenSpec, base_url: str) -> httpx.Client:
    trust_env = should_trust_env(base_url)
    transport = (
        httpx.HTTPTransport(local_address="0.0.0.0")
        if ("nju-aia.com" in base_url and not trust_env)
        else None
    )
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(spec.timeout_s, connect=8.0),
        trust_env=trust_env,
    )


def _retry_delay_s(attempt: int, retry_after_header: str | None = None) -> float:
    jitter = random.uniform(0.3, 2.0)
    if retry_after_header:
        try:
            parsed = float(retry_after_header.strip())
            if parsed > 0:
                return min(20.0, parsed + jitter)
        except ValueError:
            pass
    return 2.0 * (attempt + 1) + jitter


def _is_concurrency_limit_429(status_code: int, detail: str) -> bool:
    if status_code != 429:
        return False
    low = (detail or "").lower()
    markers = (
        "concurrency",
        "simultaneous",
        "rate limit",
        "rate_limit",
        "overloaded",
        "try again later",
        "server_error",
    )
    return any(m in low for m in markers) or not low


def _concurrency_retry_delay_s(attempt: int, retry_after_header: str | None = None) -> float:
    schedule = (6.0, 10.0, 15.0, 20.0, 22.0, 25.0, 28.0)
    base = schedule[min(max(0, attempt), len(schedule) - 1)]
    jitter = random.uniform(0.5, 3.0)
    if retry_after_header:
        try:
            parsed = float(retry_after_header.strip())
            if parsed > 0:
                return min(35.0, max(base, parsed) + jitter)
        except ValueError:
            pass
    return min(35.0, base + jitter)


def _sleep_with_cancel(wait_s: float, cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelled("立即处理新输入，已取消本次模型请求。")
    if wait_s <= 0:
        return
    if cancel_event is None:
        time.sleep(wait_s)
        return
    steps = max(1, int(math.ceil(wait_s / 0.1)))
    step_s = wait_s / steps
    for _ in range(steps):
        if cancel_event.is_set():
            raise RequestCancelled("立即处理新输入，已取消本次模型请求。")
        time.sleep(step_s)
    if cancel_event.is_set():
        raise RequestCancelled("立即处理新输入，已取消本次模型请求。")


def _chat_direct(
    spec: QwenSpec,
    messages,
    tools=None,
    *,
    stream=False,
    on_event=None,
    client: httpx.Client | None = None,
    cancel_event: threading.Event | None = None,
):
    resolved = resolve_qwen_endpoint(spec)
    body = {
        "model": resolved.model,
        "messages": messages,
        "temperature": spec.temperature,
        "max_tokens": spec.max_tokens,
        "stream": stream,
        **resolved.extra_body(enable_thinking=spec.enable_thinking),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream_options"] = {"include_usage": True}

    headers = {
        "Authorization": f"Bearer {resolved.api_key}",
        "User-Agent": "mc-agent-harness/0.1",
    }
    for h_name, h_val in resolved.extra_headers:
        headers[h_name] = h_val
    owns_client = client is None
    http_client = _make_http_client(spec, resolved.base_url) if owns_client else client
    emitted_any = False

    def tracked_on_event(evt):
        nonlocal emitted_any
        emitted_any = True
        if on_event is not None:
            on_event(evt)

    standard_max_attempts = 3
    concurrency_max_attempts = 8
    try:
        for attempt in range(concurrency_max_attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelled("立即处理新输入，已取消本次模型请求。")
            emitted_any = False
            try:
                if stream:
                    retry_wait: tuple[int, float, bool] | None = None
                    with http_client.stream("POST", resolved.endpoint, headers=headers, json=body) as response:
                        if response.status_code != 200:
                            detail = response.read().decode("utf-8", errors="replace")[:300].strip()
                            is_conc_429 = _is_concurrency_limit_429(response.status_code, detail) or response.status_code == 429
                            allowed = concurrency_max_attempts if is_conc_429 else standard_max_attempts
                            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < allowed - 1:
                                wait_s = (
                                    _concurrency_retry_delay_s(attempt, response.headers.get("Retry-After"))
                                    if is_conc_429
                                    else _retry_delay_s(attempt, response.headers.get("Retry-After"))
                                )
                                retry_wait = (response.status_code, wait_s, is_conc_429)
                            else:
                                suffix = f"：{detail}" if detail else ""
                                raise RuntimeError(f"Qwen API 返回 HTTP {response.status_code}{suffix}")
                        else:
                            return _stream_message(response.iter_lines(), tracked_on_event)
                    if retry_wait is not None:
                        status_code, wait_s, is_conc_429 = retry_wait
                        if on_event is not None:
                            on_event(
                                {
                                    "type": "http_retry",
                                    "status_code": status_code,
                                    "attempt": attempt + 1,
                                    "wait_s": wait_s,
                                    "concurrency_limit": is_conc_429,
                                }
                            )
                        _sleep_with_cancel(wait_s, cancel_event)
                        continue
                response = http_client.post(resolved.endpoint, headers=headers, json=body)
                if response.status_code != 200:
                    detail = response.text[:300].strip()
                    is_conc_429 = _is_concurrency_limit_429(response.status_code, detail) or response.status_code == 429
                    allowed = concurrency_max_attempts if is_conc_429 else standard_max_attempts
                    if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < allowed - 1:
                        wait_s = (
                            _concurrency_retry_delay_s(attempt, response.headers.get("Retry-After"))
                            if is_conc_429
                            else _retry_delay_s(attempt, response.headers.get("Retry-After"))
                        )
                        if on_event is not None:
                            on_event(
                                {
                                    "type": "http_retry",
                                    "status_code": response.status_code,
                                    "attempt": attempt + 1,
                                    "wait_s": wait_s,
                                    "concurrency_limit": is_conc_429,
                                }
                            )
                        _sleep_with_cancel(wait_s, cancel_event)
                        continue
                    suffix = f"：{detail}" if detail else ""
                    raise RuntimeError(f"Qwen API 返回 HTTP {response.status_code}{suffix}")
                result = response.json()
                break
            except httpx.HTTPError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise RequestCancelled("立即处理新输入，已取消本次模型请求。") from None
                if attempt < standard_max_attempts - 1 and not emitted_any:
                    wait_s = _retry_delay_s(attempt)
                    if on_event is not None:
                        on_event({"type": "http_retry", "error": type(exc).__name__, "attempt": attempt + 1, "wait_s": wait_s})
                    _sleep_with_cancel(wait_s, cancel_event)
                    continue
                raise RuntimeError(f"Qwen API 连接或读取失败（{type(exc).__name__}: {exc}）") from None
    finally:
        if owns_client:
            http_client.close()

    choice = result["choices"][0]
    reason = choice.get("finish_reason")
    msg = choice["message"]
    if reason == "length":
        raw_calls = msg.get("tool_calls") or []
        valid_calls = _filter_complete_prefix_calls(raw_calls)
        if valid_calls:
            if on_event is not None:
                on_event({"type": "length_recovered", "kept_calls": len(valid_calls), "dropped_calls": len(raw_calls) - len(valid_calls)})
            out = {"role": "assistant", "content": msg.get("content") or None, "tool_calls": valid_calls}
            if msg.get("reasoning_content"):
                out["reasoning_content"] = msg["reasoning_content"]
            return out
        raise OutputTruncatedError("模型响应达到 max_tokens 被截断（finish_reason=length），未生成完整工具调用。")
    if reason not in ("stop", "tool_calls"):
        raise RuntimeError(f"模型响应未正常完成：{reason}")
    out = {"role": "assistant", "content": msg.get("content") or None}
    if msg.get("reasoning_content"):
        out["reasoning_content"] = msg["reasoning_content"]
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    return out


def _process_request(
    spec: QwenSpec,
    messages,
    tools,
    stream,
    *,
    on_event,
    control,
    client: httpx.Client | None = None,
    on_cancel=None,
):
    """在守护线程中复用持久连接执行请求，主线程每 50ms 轮询 control 实现零进程孵化开销的可取消调用。"""
    check_control(control)
    inbox: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
    cancel_event = threading.Event()

    def _run_worker() -> None:
        try:
            message = _chat_direct(
                spec,
                messages,
                tools,
                stream=stream,
                on_event=lambda evt: inbox.put(("event", evt)),
                client=client,
                cancel_event=cancel_event,
            )
            inbox.put(("result", message))
        except BaseException as exc:
            inbox.put(("error_exc", exc))

    worker = threading.Thread(target=_run_worker, daemon=True)
    worker.start()
    finished = False
    try:
        while True:
            check_control(control)
            try:
                kind, value = inbox.get(timeout=0.05)
            except queue.Empty:
                if worker.is_alive():
                    continue
                worker.join(timeout=0.05)
                try:
                    kind, value = inbox.get_nowait()
                except queue.Empty:
                    raise RuntimeError("模型请求线程意外退出，未取得完整响应。") from None
            if kind == "event":
                if on_event is not None:
                    on_event(value)
            elif kind == "result":
                finished = True
                check_control(control)
                return value
            elif kind == "error_exc":
                finished = True
                exc = value
                if isinstance(exc, RequestCancelled):
                    raise exc
                if isinstance(exc, ConfigError):
                    raise exc
                if isinstance(exc, OutputTruncatedError):
                    raise exc
                if isinstance(exc, RuntimeError):
                    raise exc
                raise RuntimeError(f"{type(exc).__name__}: {exc}") from None
            else:
                raise RuntimeError("模型请求线程返回了未知事件。")
    finally:
        if not finished:
            cancel_event.set()
            if on_cancel is not None:
                try:
                    on_cancel()
                except Exception:
                    pass


class QwenModel:
    """可复用的 Qwen 客户端封装（自带进程内 RPM 滑动窗口限速与持久 HTTP 连接池，签名兼容 nano-agent `chat`）。"""

    def __init__(self, spec: QwenSpec | None = None) -> None:
        self.spec = spec or QwenSpec()
        resolved = resolve_qwen_endpoint(self.spec)
        self.base_url = resolved.base_url
        self.model = resolved.model
        self.rpm_budget = resolved.rpm_budget
        self._limiter = RpmLimiter(resolved.rpm_budget)
        self._client_lock = threading.Lock()
        self._client = _make_http_client(self.spec, self.base_url)
        self._cancel_cooldown_until: float = 0.0
        self._last_rotation_error: str | None = None

    def _current_client(self) -> httpx.Client:
        with self._client_lock:
            return self._client

    def _abort_and_rotate_client(self) -> None:
        old: httpx.Client | None = None
        with self._client_lock:
            if self.spec.post_cancel_cooldown_s > 0:
                self._cancel_cooldown_until = max(
                    self._cancel_cooldown_until,
                    time.monotonic() + float(self.spec.post_cancel_cooldown_s),
                )
            try:
                new_client = _make_http_client(self.spec, self.base_url)
            except Exception as exc:
                # BUG-086: preserve RequestCancelled and keep existing client usable when pool rotation fails (e.g. ssl.SSLError)
                self._last_rotation_error = f"{type(exc).__name__}: {exc}"
                return
            old = self._client
            self._client = new_client
            self._last_rotation_error = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._client_lock:
            client = self._client
        client.close()

    def __enter__(self) -> QwenModel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def chat(
        self,
        messages,
        tools=None,
        *,
        stream=False,
        on_event=None,
        control=None,
        enable_thinking: bool | None = None,
        max_tokens: int | None = None,
    ):
        check_control(control)
        with self._client_lock:
            cd_until = self._cancel_cooldown_until
        rem_cd = cd_until - time.monotonic()
        if rem_cd > 0:
            if on_event is not None:
                on_event({"type": "cancel_cooldown_wait", "wait_s": round(rem_cd, 3)})
            steps = max(1, int(math.ceil(rem_cd / 0.1)))
            step_s = rem_cd / steps
            for _ in range(steps):
                check_control(control)
                time.sleep(step_s)
            check_control(control)
        wait_s = self._limiter.acquire(control=control)
        if wait_s > 0 and on_event is not None:
            on_event({"type": "rate_wait", "wait_s": round(wait_s, 3)})
        req_spec = self.spec
        if enable_thinking is not None or max_tokens is not None:
            resolved_thinking = req_spec.enable_thinking if enable_thinking is None else bool(enable_thinking)
            resolved_max_tokens = (
                int(max_tokens)
                if max_tokens is not None
                else (max(req_spec.max_tokens, 4096) if resolved_thinking else req_spec.max_tokens)
            )
            req_spec = dataclasses.replace(
                req_spec,
                enable_thinking=resolved_thinking,
                max_tokens=resolved_max_tokens,
            )
        client = self._current_client()
        if control is None:
            return _chat_direct(
                req_spec,
                messages,
                tools,
                stream=stream,
                on_event=on_event,
                client=client,
            )
        return _process_request(
            req_spec,
            messages,
            tools,
            stream,
            on_event=on_event,
            control=control,
            client=client,
            on_cancel=self._abort_and_rotate_client,
        )
