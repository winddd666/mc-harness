(() => {
  const $ = (id) => document.getElementById(id);

  const state = {
    botUsername: "BeastBot_01",
    running: false,
    activeTurnId: null,
    telemetry: null,
    turns: [],
    turnMap: new Map(),
    expandedTools: new Set(),
    stickToBottom: true,
    ws: null,
    reconnectTimer: null,
  };

  const STATUS_LABELS = {
    running: "执行中",
    answered: "已完成",
    interrupted: "已停止",
    timeout: "已超时",
    limit_reached: "达步数上限",
    runtime_error: "运行异常",
    empty_response: "空回复",
  };

  const STEER_STATUS_LABELS = {
    queued: "排队中...",
    received: "已送达安全节点",
    applied: "已注入上下文",
    pending: "未及执行（任务已结束）",
  };

  function compactArgs(raw) {
    if (!raw) return "{}";
    const s = String(raw).trim();
    if (!s || s === "{}") return "{}";
    try {
      const obj = JSON.parse(s);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        const parts = Object.entries(obj).map(([k, v]) => {
          const val = typeof v === "string" ? (v.length > 24 ? `${v.slice(0, 21)}...` : v) : JSON.stringify(v);
          return `${k}=${val}`;
        });
        return parts.join(", ") || "{}";
      }
    } catch (_) {
      // ignore parse fallback
    }
    return s.length > 80 ? `${s.slice(0, 77)}...` : s;
  }

  function formatDuration(ms) {
    if (typeof ms !== "number" || Number.isNaN(ms)) return "";
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }

  function syncComposerUi() {
    const running = state.running;
    $("send-btn").classList.toggle("hidden", running);
    $("queue-btn").classList.toggle("hidden", !running);
    $("urgent-btn").classList.toggle("hidden", !running);
    $("stop-btn").classList.toggle("hidden", !running);
    $("clear-btn").disabled = running;
    $("run-banner").classList.toggle("hidden", !running);

    const active = state.activeTurnId ? state.turnMap.get(state.activeTurnId) : null;
    if (active) {
      $("run-phase-text").textContent = active.phase_detail || active.phase || "执行中...";
      $("run-step-text").textContent = `${active.actor_requests || 0} / ${active.max_calls || 24} 轮`;
    }
  }

  function renderTelemetry(t) {
    state.telemetry = t || {};
    const dot = $("bot-dot");
    const txt = $("bot-state-text");
    const recBtn = $("reconnect-btn");
    const name = state.telemetry.username || state.botUsername || "Bot";
    $("bot-name").textContent = name;
    $("hero-bot-name").textContent = name;

    dot.classList.remove("ready", "offline");
    if (state.telemetry.ready) {
      dot.classList.add("ready");
      txt.textContent = "已就绪";
      recBtn.classList.add("hidden");
    } else if (state.telemetry.connected) {
      txt.textContent = "切服/重生中";
      recBtn.classList.remove("hidden");
    } else {
      dot.classList.add("offline");
      txt.textContent = state.telemetry.error ? "连接异常" : "离线";
      recBtn.classList.remove("hidden");
    }

    const pos = state.telemetry.pos;
    $("vital-pos").textContent = pos ? `XYZ ${pos.x}, ${pos.y}, ${pos.z}` : "XYZ —";
    $("vital-hp").textContent =
      typeof state.telemetry.health === "number" ? `❤️ ${Math.round(state.telemetry.health)}/20` : "❤️ —";
    $("vital-food").textContent =
      typeof state.telemetry.food === "number" ? `🍖 ${Math.round(state.telemetry.food)}/20` : "🍖 —";

    const held = state.telemetry.held;
    $("vital-held").textContent = held && held.name ? `🗡 ${held.name}${held.count > 1 ? ` x${held.count}` : ""}` : state.telemetry.ready ? "🗡 空手" : "🗡 —";
    $("vital-inv-count").textContent = typeof state.telemetry.inventory_total === "number" ? String(state.telemetry.inventory_total) : "—";
    $("inv-dim").textContent = [state.telemetry.dimension, state.telemetry.biome].filter(Boolean).join(" · ") || "—";

    const grid = $("inv-items-grid");
    grid.replaceChildren();
    const items = Array.isArray(state.telemetry.top_items) ? state.telemetry.top_items : [];
    if (items.length === 0) {
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = state.telemetry.inventory_available ? "背包为空" : "背包状态暂不可用";
      grid.appendChild(empty);
    } else {
      for (const it of items) {
        const tag = document.createElement("span");
        tag.className = "inv-tag";
        tag.textContent = `${it.name} x${it.count}`;
        grid.appendChild(tag);
      }
    }
  }

  function buildItemElement(item, turn) {
    if (item.kind === "assistant_text") {
      const wrap = document.createElement("div");
      wrap.className = "assistant-text-block";
      wrap.dataset.itemId = item.id;
      updateAssistantTextElement(wrap, item, turn);
      return wrap;
    }

    if (item.kind === "tool_call") {
      const details = document.createElement("details");
      details.className = "tool-card";
      details.dataset.itemId = item.id;
      details._toolItem = item;
      if (state.expandedTools.has(item.id)) {
        details.open = true;
      }
      details.addEventListener("toggle", () => {
        if (details.open) {
          state.expandedTools.add(item.id);
        } else {
          state.expandedTools.delete(item.id);
        }
        populateToolPreIfOpen(details);
      });

      const summary = document.createElement("summary");
      summary.className = "tool-summary";
      const icon = document.createElement("span");
      icon.className = "tool-icon";
      const nameEl = document.createElement("span");
      nameEl.className = "tool-name";
      const argsPreview = document.createElement("span");
      argsPreview.className = "tool-args-preview";
      const durEl = document.createElement("span");
      durEl.className = "tool-duration";
      const chev = document.createElement("span");
      chev.className = "tool-chevron";
      chev.textContent = "▶";
      summary.append(icon, nameEl, argsPreview, durEl, chev);

      const body = document.createElement("div");
      body.className = "tool-body";
      const argsSec = document.createElement("div");
      const argsLabel = document.createElement("div");
      argsLabel.className = "tool-section-label";
      argsLabel.textContent = "Arguments";
      const argsPre = document.createElement("pre");
      argsPre.className = "tool-pre";
      argsSec.append(argsLabel, argsPre);

      const resSec = document.createElement("div");
      const resLabel = document.createElement("div");
      resLabel.className = "tool-section-label";
      resLabel.textContent = "Result";
      const resPre = document.createElement("pre");
      resPre.className = "tool-pre";
      resSec.append(resLabel, resPre);

      body.append(argsSec, resSec);
      details.append(summary, body);
      updateToolElement(details, item);
      return details;
    }

    if (item.kind === "user_steer") {
      const card = document.createElement("div");
      card.className = `steer-card ${item.priority === "urgent" ? "urgent" : ""}`;
      card.dataset.itemId = item.id;
      const left = document.createElement("div");
      left.className = "steer-left";
      const tag = document.createElement("span");
      tag.className = "steer-tag";
      const txt = document.createElement("span");
      left.append(tag, txt);
      const st = document.createElement("span");
      st.className = "steer-state";
      card.append(left, st);
      updateSteerElement(card, item);
      return card;
    }

    const notice = document.createElement("div");
    notice.className = `notice-card ${item.level || "info"}`;
    notice.dataset.itemId = item.id;
    notice.textContent = item.text || "";
    return notice;
  }

  function updateAssistantTextElement(el, item, turn) {
    el.replaceChildren();
    if (item.reasoning) {
      const rDetails = document.createElement("details");
      rDetails.className = "reasoning-details";
      rDetails.open = true;
      const rSum = document.createElement("summary");
      rSum.textContent = "💭 模型思考规划";
      const rBody = document.createElement("div");
      rBody.className = "reasoning-body";
      rBody.textContent = item.reasoning;
      rDetails.append(rSum, rBody);
      el.appendChild(rDetails);
    }

    const isFinalDuplicate =
      Boolean(turn && turn.answer) &&
      !item.streaming &&
      item.text &&
      item.text.trim() === String(turn.answer).trim() &&
      turn.items[turn.items.length - 1]?.id === item.id;

    if (!isFinalDuplicate && item.text) {
      const textSpan = document.createElement("div");
      textSpan.textContent = item.text;
      el.appendChild(textSpan);
    }
    el.classList.toggle("streaming", Boolean(item.streaming));
    el.classList.toggle("hidden", isFinalDuplicate && !item.reasoning);
  }

  function populateToolPreIfOpen(details) {
    const item = details._toolItem;
    const pres = details.querySelectorAll(".tool-pre");
    if (!details.open || !item) {
      if (pres[0] && pres[0].textContent) pres[0].textContent = "";
      if (pres[1] && pres[1].textContent) pres[1].textContent = "";
      return;
    }
    if (pres[0]) pres[0].textContent = item.arguments_pretty || item.arguments || "{}";
    if (pres[1]) {
      pres[1].textContent =
        item.result !== null && item.result !== undefined
          ? item.result
          : item.status === "running"
            ? "(等待执行返回...)"
            : "";
    }
  }

  function updateToolElement(details, item) {
    details._toolItem = item;
    details.classList.toggle("failed", item.status === "failed");
    const icon = details.querySelector(".tool-icon");
    const nameEl = details.querySelector(".tool-name");
    const argsPreview = details.querySelector(".tool-args-preview");
    const durEl = details.querySelector(".tool-duration");

    icon.className = `tool-icon ${item.status || "running"}`;
    if (item.status === "ok") icon.textContent = "✓";
    else if (item.status === "failed") icon.textContent = "✕";
    else if (item.status === "skipped") icon.textContent = "⏭";
    else icon.textContent = "◌";

    nameEl.textContent = item.name || "tool";
    argsPreview.textContent = compactArgs(item.arguments);
    durEl.textContent = item.status === "running" ? "执行中" : formatDuration(item.duration_ms);
    populateToolPreIfOpen(details);
  }

  function updateSteerElement(card, item) {
    const tag = card.querySelector(".steer-tag");
    const txt = card.querySelector(".steer-left span:last-child");
    const st = card.querySelector(".steer-state");
    tag.textContent = item.priority === "urgent" ? "⚡ 立即插队" : "⏳ 排队追加";
    txt.textContent = item.text || "";
    const statusText = STEER_STATUS_LABELS[item.status] || item.status || "";
    st.textContent = item.boundary ? `${statusText} (${item.boundary})` : statusText;
  }

  function updateTurnFinalCard(section, turn) {
    const finalWrap = section.querySelector(".turn-final-slot");
    finalWrap.replaceChildren();
    if (turn.status === "running") return;

    if (turn.answer) {
      const card = document.createElement("div");
      card.className = "final-card";
      const h = document.createElement("div");
      h.className = "final-header";
      h.textContent = "✓ 任务交付";
      const b = document.createElement("div");
      b.className = "final-body";
      b.textContent = turn.answer;
      card.append(h, b);
      finalWrap.appendChild(card);
    } else if (turn.error) {
      const card = document.createElement("div");
      card.className = "final-card error";
      const h = document.createElement("div");
      h.className = "final-header";
      h.textContent = `✕ ${STATUS_LABELS[turn.status] || turn.status}`;
      const b = document.createElement("div");
      b.className = "final-body";
      b.textContent = turn.error;
      card.append(h, b);
      finalWrap.appendChild(card);
    } else if (turn.status === "interrupted") {
      const card = document.createElement("div");
      card.className = "final-card interrupted";
      const h = document.createElement("div");
      h.className = "final-header";
      h.textContent = "⏹ 已按用户指令手动停止";
      const b = document.createElement("div");
      b.className = "final-body";
      b.textContent = "当前任务已中断，你可以随时在底部输入下一条指令。";
      card.append(h, b);
      finalWrap.appendChild(card);
    }
  }

  function updateTurnHeader(section, turn) {
    const pill = section.querySelector(".status-pill");
    const meta = section.querySelector(".turn-meta-chip");
    pill.className = `status-pill ${turn.status || "running"}`;
    pill.textContent =
      turn.status === "running"
        ? `执行中 · ${turn.actor_requests || 0}/${turn.max_calls || 24}`
        : STATUS_LABELS[turn.status] || turn.status;
    meta.textContent = `${turn.provider || "nju"} · ${turn.actor_requests || 0} 轮`;
  }

  function ensureTurnElement(turn) {
    let section = document.querySelector(`.turn-block[data-turn-id="${CSS.escape(turn.turn_id)}"]`);
    if (section) return section;

    section = document.createElement("section");
    section.className = "turn-block";
    section.dataset.turnId = turn.turn_id;

    const header = document.createElement("div");
    header.className = "user-prompt-card";
    const left = document.createElement("div");
    left.className = "user-prompt-left";
    const caret = document.createElement("span");
    caret.className = "prompt-caret";
    caret.textContent = "›";
    const promptEl = document.createElement("div");
    promptEl.className = "prompt-text";
    promptEl.textContent = turn.prompt || "";
    left.append(caret, promptEl);

    const badges = document.createElement("div");
    badges.className = "turn-badge-group";
    const meta = document.createElement("span");
    meta.className = "turn-meta-chip";
    const pill = document.createElement("span");
    pill.className = "status-pill";
    badges.append(meta, pill);
    header.append(left, badges);

    const itemsBox = document.createElement("div");
    itemsBox.className = "turn-items";

    const finalSlot = document.createElement("div");
    finalSlot.className = "turn-final-slot";

    section.append(header, itemsBox, finalSlot);
    $("turns-list").appendChild(section);
    updateTurnHeader(section, turn);

    for (const item of turn.items || []) {
      itemsBox.appendChild(buildItemElement(item, turn));
    }
    updateTurnFinalCard(section, turn);
    return section;
  }

  function renderAllTurns(turns) {
    state.turns = Array.isArray(turns) ? turns : [];
    state.turnMap.clear();
    const list = $("turns-list");
    list.replaceChildren();
    $("empty-state").classList.toggle("hidden", state.turns.length > 0);
    for (const t of state.turns) {
      state.turnMap.set(t.turn_id, t);
      ensureTurnElement(t);
    }
    maybeScrollToBottom(true);
  }

  function upsertTurnItem(turnId, item) {
    const turn = state.turnMap.get(turnId);
    if (!turn) return;
    if (!Array.isArray(turn.items)) turn.items = [];
    const idx = turn.items.findIndex((x) => x.id === item.id);
    if (idx >= 0) {
      turn.items[idx] = item;
    } else {
      turn.items.push(item);
    }
    const section = ensureTurnElement(turn);
    const itemsBox = section.querySelector(".turn-items");
    const existingEl = itemsBox.querySelector(`[data-item-id="${CSS.escape(item.id)}"]`);
    if (!existingEl) {
      itemsBox.appendChild(buildItemElement(item, turn));
    } else if (item.kind === "assistant_text") {
      updateAssistantTextElement(existingEl, item, turn);
    } else if (item.kind === "tool_call") {
      updateToolElement(existingEl, item);
    } else if (item.kind === "user_steer") {
      updateSteerElement(existingEl, item);
    } else {
      existingEl.textContent = item.text || "";
    }
    maybeScrollToBottom();
  }

  function appendItemDelta(turnId, itemId, delta) {
    const turn = state.turnMap.get(turnId);
    if (!turn) return;
    const item = (turn.items || []).find((x) => x.id === itemId);
    if (!item) return;
    item.text = (item.text || "") + delta;
    const section = ensureTurnElement(turn);
    const existingEl = section.querySelector(`[data-item-id="${CSS.escape(itemId)}"]`);
    if (existingEl) {
      updateAssistantTextElement(existingEl, item, turn);
    }
    maybeScrollToBottom();
  }

  function maybeScrollToBottom(force = false) {
    const wrap = $("stream-wrap");
    if (force || state.stickToBottom) {
      wrap.scrollTop = wrap.scrollHeight;
      $("jump-bottom-btn").classList.add("hidden");
    }
  }

  function applySnapshot(snap) {
    state.botUsername = snap.bot_username || state.botUsername;
    state.running = Boolean(snap.running);
    state.activeTurnId = snap.active_turn_id || null;
    if (snap.live_url) {
      $("live-link").href = snap.live_url;
    }
    if (snap.config) {
      if (snap.config.provider) $("cfg-provider").value = snap.config.provider;
      if (snap.config.max_calls) $("cfg-max-calls").value = snap.config.max_calls;
      $("cfg-thinking").checked = Boolean(snap.config.enable_thinking);
    }
    renderTelemetry(snap.telemetry);
    renderAllTurns(snap.turns || []);
    syncComposerUi();
  }

  function handleWsMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "snapshot") {
      applySnapshot(msg);
      return;
    }
    if (msg.type === "telemetry") {
      renderTelemetry(msg.telemetry);
      return;
    }
    if (msg.type === "session_cleared") {
      state.running = false;
      state.activeTurnId = null;
      renderAllTurns([]);
      syncComposerUi();
      return;
    }
    if (msg.type === "turn_started" && msg.turn) {
      state.running = true;
      state.activeTurnId = msg.turn.turn_id;
      state.turns.push(msg.turn);
      state.turnMap.set(msg.turn.turn_id, msg.turn);
      $("empty-state").classList.add("hidden");
      ensureTurnElement(msg.turn);
      syncComposerUi();
      maybeScrollToBottom(true);
      return;
    }
    if (msg.type === "turn_item_upsert" && msg.turn_id && msg.item) {
      upsertTurnItem(msg.turn_id, msg.item);
      return;
    }
    if (msg.type === "turn_item_delta" && msg.turn_id && msg.item_id) {
      appendItemDelta(msg.turn_id, msg.item_id, msg.delta || "");
      return;
    }
    if (msg.type === "turn_meta" && msg.turn_id) {
      const turn = state.turnMap.get(msg.turn_id);
      if (turn) {
        if (msg.phase !== undefined) turn.phase = msg.phase;
        if (msg.phase_detail !== undefined) turn.phase_detail = msg.phase_detail;
        if (msg.actor_requests !== undefined) turn.actor_requests = msg.actor_requests;
        if (msg.model !== undefined) turn.model = msg.model;
        if (msg.answer !== undefined) turn.answer = msg.answer;
        const section = ensureTurnElement(turn);
        updateTurnHeader(section, turn);
      }
      syncComposerUi();
      return;
    }
    if (msg.type === "turn_finished" && msg.turn) {
      const t = msg.turn;
      state.turnMap.set(t.turn_id, t);
      const idx = state.turns.findIndex((x) => x.turn_id === t.turn_id);
      if (idx >= 0) state.turns[idx] = t;
      if (state.activeTurnId === t.turn_id) {
        state.running = false;
        state.activeTurnId = null;
      }
      const section = ensureTurnElement(t);
      updateTurnHeader(section, t);
      for (const item of t.items || []) {
        upsertTurnItem(t.turn_id, item);
      }
      updateTurnFinalCard(section, t);
      syncComposerUi();
      maybeScrollToBottom();
    }
  }

  function connectWs() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    state.ws = ws;
    ws.addEventListener("message", (ev) => {
      try {
        handleWsMessage(JSON.parse(ev.data));
      } catch (err) {
        console.error("Bad WS message", err);
      }
    });
    ws.addEventListener("close", () => {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connectWs, 1500);
    });
  }

  async function postJson(url, payload = {}) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return resp.json();
  }

  async function submitPrompt({ immediate = false, overrideText = null } = {}) {
    const input = $("prompt-input");
    const text = (overrideText !== null ? overrideText : input.value).trim();
    if (!text) return;

    const payload = {
      text,
      immediate,
      provider: $("cfg-provider").value,
      max_calls: Number($("cfg-max-calls").value) || 24,
      enable_thinking: $("cfg-thinking").checked,
    };

    if (overrideText === null) {
      input.value = "";
      autoResizeInput();
    }

    try {
      const res = await postJson("/api/command", payload);
      if (!res.ok) {
        alert(res.message || res.error || "指令提交失败");
      }
    } catch (err) {
      alert(`发送失败: ${err}`);
    }
  }

  function autoResizeInput() {
    const el = $("prompt-input");
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }

  // ---- DOM Events ----
  $("composer-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitPrompt({ immediate: false });
  });

  $("queue-btn").addEventListener("click", () => submitPrompt({ immediate: false }));
  $("urgent-btn").addEventListener("click", () => submitPrompt({ immediate: true }));
  $("stop-btn").addEventListener("click", async () => {
    $("stop-btn").disabled = true;
    try {
      await postJson("/api/stop", {});
    } finally {
      setTimeout(() => {
        $("stop-btn").disabled = false;
      }, 400);
    }
  });

  $("clear-btn").addEventListener("click", async () => {
    const res = await postJson("/api/clear", {});
    if (!res.ok && res.message) {
      alert(res.message);
    }
  });

  $("reconnect-btn").addEventListener("click", async () => {
    $("reconnect-btn").disabled = true;
    try {
      const res = await postJson("/api/reconnect", {});
      if (!res.ok) {
        alert(res.message || "重连失败");
      }
    } finally {
      $("reconnect-btn").disabled = false;
    }
  });

  $("inv-toggle-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    $("inv-popover").classList.toggle("hidden");
  });

  document.addEventListener("click", (e) => {
    const pop = $("inv-popover");
    if (!pop.classList.contains("hidden") && !pop.contains(e.target) && e.target !== $("inv-toggle-btn")) {
      pop.classList.add("hidden");
    }
  });

  $("starter-grid").addEventListener("click", (e) => {
    const card = e.target.closest(".starter-card");
    if (!card) return;
    const prompt = card.getAttribute("data-prompt") || "";
    if (prompt) {
      submitPrompt({ immediate: false, overrideText: prompt });
    }
  });

  const inputEl = $("prompt-input");
  inputEl.addEventListener("input", autoResizeInput);
  inputEl.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const urgent = state.running && (e.ctrlKey || e.metaKey);
      submitPrompt({ immediate: urgent });
    }
  });

  const streamWrap = $("stream-wrap");
  streamWrap.addEventListener("scroll", () => {
    const distFromBottom = streamWrap.scrollHeight - streamWrap.scrollTop - streamWrap.clientHeight;
    state.stickToBottom = distFromBottom < 48;
    $("jump-bottom-btn").classList.toggle("hidden", state.stickToBottom);
  });

  $("jump-bottom-btn").addEventListener("click", () => {
    state.stickToBottom = true;
    maybeScrollToBottom(true);
  });

  // Initial load + WS connect
  fetch("/api/state")
    .then((r) => r.json())
    .then((snap) => applySnapshot(snap))
    .catch(() => {})
    .finally(() => connectWs());
})();
