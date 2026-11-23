from tests.harness._common import *  # noqa: F401,F403

def test_guardian_silent_eat_does_not_cancel_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from mc_agent.harness.control import check_control

    clock = [10.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {"food": 14, "ate": False}
    events: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        if action == "events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20.0, "food": state["food"], "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "bread", "count": 3}]}})
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "use_item":
            state["ate"] = True
            state["food"] = 19
            return httpx.Response(200, json={"ok": True, "result": {"used": "bread"}})
        raise AssertionError(action)

    def chat(messages, tools=None, *, control=None, **kwargs):
        clock[0] += 1.5
        # 轮询仅触发纯进食，requires_replan=False，check_control 不应抛 RequestCancelled！
        check_control(control)
        return {"role": "assistant", "content": "未被打断，正常完成。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        rep = run_mc_agent(
            "测试静默进食",
            bot,
            chat_fn=chat,
            max_calls=3,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=events.append,
        )
    assert rep["status"] == "answered"
    assert state["ate"] is True
    assert rep["guardian"]["incidents"] == 1
    assert rep["guardian"]["cancelled_generations"] == 0
    assert any(e["type"] == "guardian.incident" and e["requires_replan"] is False for e in events)


def test_cancel_aborts_zombie_retries_and_enforces_post_cancel_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mc_agent.harness.model.time.sleep", lambda _: None)
    env_p = tmp_path / ".env.v2"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None, post_cancel_cooldown_s=1.0)

    # 1. cancel_event 已置位时，HTTPError 立即抛 RequestCancelled，绝不再发出第 2 次僵尸重试
    zombie_calls = 0
    cancel_ev = threading.Event()

    def abort_transport(req: httpx.Request) -> httpx.Response:
        nonlocal zombie_calls
        zombie_calls += 1
        cancel_ev.set()
        raise httpx.ReadError("client closed")

    abort_client = httpx.Client(transport=httpx.MockTransport(abort_transport))
    try:
        with pytest.raises(RequestCancelled):
            _chat_direct(
                spec,
                [{"role": "user", "content": "hi"}],
                stream=False,
                client=abort_client,
                cancel_event=cancel_ev,
            )
    finally:
        abort_client.close()
    assert zombie_calls == 1

    # 2. Harness concurrency-limit 429（哪怕带 Retry-After: 2）升级为 >=6.0s 阶梯退避且最多 5 次成功
    attempts = 0
    retry_events: list[dict[str, Any]] = []

    def conc_transport(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 5:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"error": {"message": "Harness group concurrency limit: 2 simultaneous requests"}},
            )
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "recovered"}}]})

    conc_client = httpx.Client(transport=httpx.MockTransport(conc_transport))
    try:
        out = _chat_direct(
            spec,
            [{"role": "user", "content": "hi"}],
            stream=False,
            on_event=retry_events.append,
            client=conc_client,
        )
    finally:
        conc_client.close()
    assert out["content"] == "recovered"
    assert attempts == 5
    waits = [e["wait_s"] for e in retry_events if e["type"] == "http_retry"]
    assert len(waits) == 4
    for actual, base in zip(waits, [6.0, 10.0, 15.0, 20.0]):
        assert base <= actual <= base + 3.0
    assert all(e["concurrency_limit"] is True for e in retry_events if e["type"] == "http_retry")

    # 3. QwenModel 在一次取消后会进入 post_cancel_cooldown_s，并于下次请求触发 cancel_cooldown_wait
    # 首客户端用事件阻塞促使 _process_request 真正进入 worker 等待后再被 control 取消，旋转后的客户端立刻返回 200
    call_phase = {"first": True}
    started_ev = threading.Event()
    release_ev = threading.Event()

    def factory_seq(s: QwenSpec, base_url: str) -> httpx.Client:
        if call_phase["first"]:
            call_phase["first"] = False

            def slow_h(req: httpx.Request) -> httpx.Response:
                started_ev.set()
                release_ev.wait(timeout=1.0)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "late"}}]})

            return httpx.Client(transport=httpx.MockTransport(slow_h))

        def fast_h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok2"}}]})

        return httpx.Client(transport=httpx.MockTransport(fast_h))

    monkeypatch.setattr("mc_agent.harness.model._make_http_client", factory_seq)
    qwen = QwenModel(spec)
    try:
        with pytest.raises(RequestCancelled):
            qwen.chat([{"role": "user", "content": "1"}], stream=False, control=started_ev.is_set)

        cd_events: list[dict[str, Any]] = []
        res2 = qwen.chat(
            [{"role": "user", "content": "2"}],
            stream=False,
            on_event=cd_events.append,
            control=lambda: False,
        )
        assert res2["content"] == "ok2"
        assert any(e["type"] == "cancel_cooldown_wait" and e["wait_s"] > 0 for e in cd_events)
    finally:
        release_ev.set()
        qwen.close()


def test_process_request_exit_race_drains_inbox_and_transient_llm_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue as _stdlib_queue
    import time as _stdlib_time
    import mc_agent.harness.model as model_mod

    RealSimpleQueue = _stdlib_queue.SimpleQueue

    class _RaceQueue:
        def __init__(self, expected_puts: int) -> None:
            self._inner = RealSimpleQueue()
            self._puts = 0
            self._expected_puts = expected_puts
            self._injected_empty = False

        def put(self, item: Any) -> None:
            self._inner.put(item)
            self._puts += 1

        def get(self, timeout: float | None = None) -> Any:
            if not self._injected_empty:
                self._injected_empty = True
                deadline = _stdlib_time.monotonic() + 1.0
                while self._puts < self._expected_puts and _stdlib_time.monotonic() < deadline:
                    _stdlib_time.sleep(0.002)
                _stdlib_time.sleep(0.02)
                raise _stdlib_queue.Empty
            return self._inner.get(timeout=timeout)

        def get_nowait(self) -> Any:
            return self._inner.get_nowait()

    # 子测试 A1：worker 投递末尾 event 与 result 并退出，首次 get(timeout) 恰好抛 Empty 时仍完整排空并返回 result
    race_mode = {"step": "ok"}

    def fake_chat_direct(spec, messages, tools=None, *, stream=False, on_event=None, client=None, cancel_event=None):
        if on_event is not None:
            on_event({"type": "tool.delta", "index": 0, "id": "c1", "name": "mc_status", "arguments": "{}"})
        if race_mode["step"] == "err":
            raise RuntimeError("模型流中断：缺少完整结束标记 [DONE] 或 finish_reason。")
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(model_mod, "_chat_direct", fake_chat_direct)
    monkeypatch.setattr(model_mod.queue, "SimpleQueue", lambda: _RaceQueue(expected_puts=2))

    seen_events: list[dict[str, Any]] = []
    res_msg = _process_request(
        QwenSpec(),
        [{"role": "user", "content": "hi"}],
        [],
        True,
        on_event=seen_events.append,
        control=lambda: False,
    )
    assert res_msg == {"role": "assistant", "content": "done"}
    assert any(e.get("type") == "tool.delta" and e.get("name") == "mc_status" for e in seen_events)

    # 子测试 A2：worker 投递真实流中断 error_exc 并退出，首次 get(timeout) 抛 Empty 时仍保留真实异常而非“线程意外退出”
    race_mode["step"] = "err"
    with pytest.raises(RuntimeError, match="模型流中断"):
        _process_request(
            QwenSpec(),
            [{"role": "user", "content": "hi"}],
            [],
            True,
            on_event=None,
            control=lambda: False,
        )

    # 子测试 B：局内瞬时 SSE 断流自动重连（退避且退还消耗额度），非瞬时错误直接暴露 runtime_error
    monkeypatch.setattr("mc_agent.harness.agent.time.sleep", lambda _s: None)

    def handle_bot(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        raise AssertionError(path)

    class _DummyRetryChat:
        def __init__(self) -> None:
            self.calls = 0
            self.rotated = 0

        def _abort_and_rotate_client(self) -> None:
            self.rotated += 1

        def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "查状态",
                    "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
                }
            if self.calls == 2:
                raise RuntimeError("模型流中断：缺少完整结束标记 [DONE] 或 finish_reason。")
            return {"role": "assistant", "content": "恢复成功"}

    dummy = _DummyRetryChat()
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot_ok:
        rep_ok = run_mc_agent(
            "测试局内流断恢复",
            bot_ok,
            chat_fn=dummy.chat,
            max_calls=2,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_ok["status"] == "answered"
    assert rep_ok["answer"] == "恢复成功"
    assert dummy.calls == 3
    assert dummy.rotated == 1
    assert any(e.get("type") == "model.transient_retry" for e in rep_ok["events"])

    class _DummyFatalChat:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            raise RuntimeError("致命参数结构损坏")

    fatal = _DummyFatalChat()
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot_fatal:
        rep_fatal = run_mc_agent(
            "测试非瞬时错误直透",
            bot_fatal,
            chat_fn=fatal.chat,
            max_calls=3,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_fatal["status"] == "runtime_error"
    assert "致命参数结构损坏" in str(rep_fatal.get("error"))
    assert fatal.calls == 1

