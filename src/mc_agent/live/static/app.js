/* ==========================================================================
   MC-Live Frontend Application (app.js)
   Zero External Dependencies · Works 100% Offline over LAN / Radmin VPN
   ========================================================================== */
(function () {
  "use strict";

  // -------------------- Constants & Tool Metadata --------------------
  const SHARED_TOOLS = new Set([
    "mc_status",
    "mc_inventory",
    "mc_find_blocks",
    "mc_get_block",
    "mc_get_entities",
    "mc_events",
    "mc_advancements",
    "mc_advancement_frontier",
    "mc_kb_lookup",
    "mc_look",
    "mc_posture",
    "read_result",
  ]);

  const STATION_BLOCKS = new Set([
    "crafting_table",
    "furnace",
    "blast_furnace",
    "smoker",
    "chest",
    "trapped_chest",
    "barrel",
    "anvil",
    "enchanting_table",
    "stonecutter",
    "white_bed",
    "red_bed",
    "black_bed",
    "blue_bed",
    "brown_bed",
    "cyan_bed",
    "gray_bed",
    "green_bed",
    "light_blue_bed",
    "light_gray_bed",
    "lime_bed",
    "magenta_bed",
    "orange_bed",
    "pink_bed",
    "purple_bed",
    "yellow_bed",
  ]);

  const DIM_LABELS = {
    overworld: "主世界",
    the_nether: "下界",
    the_end: "末地",
  };

  const PHASE_LABELS = {
    idle: "待命",
    preparing: "准备上下文 (preparing)",
    generating: "模型推理中 (generating)",
    executing_tools: "执行工具中 (executing_tools)",
    finished: "已完成 (finished)",
    answered: "已完成 (answered)",
    failed: "执行异常 (failed)",
    timed_out: "运行超时 (timed_out)",
    cancelled: "已取消 (cancelled)",
    max_calls_exceeded: "达步数上限",
  };

  const MAX_LIVE_BLOCKS = 2800;

  // -------------------- Application State --------------------
  const state = {
    mode: "live", // 'live' | 'replay'
    leftView: "local_capture", // 'local_capture' | 'third_person' | 'first_person' | 'free_orbit' | 'radar_2d' | 'prismarine'
    savedLiveView: "local_capture",
    overlaysVisible: true,
    alertsVisible: false,
    openDrawer: null, // null | 'inventory' | 'advancements'
    taskExpanded: false,
    autoScroll: true,

    ws: null,
    wsConnected: false,
    wsReconnectTimer: null,
    wsReconnectAttempts: 0,

    info: {
      bot_username: null,
      mc_api_base_url: "",
      has_cached_token: false,
      online: false,
      ready: false,
      active_run_id: null,
      prismarine_viewer: { available: false, embed_url: null, note: "" },
      capture_stream: {
        active: false,
        stream_url: null,
        snapshot_url: null,
        width: 0,
        height: 0,
        fps: 0,
        source: null,
        seq: 0,
        camera_mode: "third_person",
        director_available: false,
      },
    },

    runs: [],

    // Live telemetry & active run stream
    live: {
      status: {},
      prevHealth: null,
      envHurtUntilMs: 0,
      envHurtReason: "",
      inventory: {},
      advancements: {},
      bestFrontier: [],
      entities: [],
      blocksMap: new Map(), // "x,y,z" -> { pos: [x,y,z], name }
      trajectory: [],
      markers: [],
      runId: null,
      runStream: createEmptyStreamModel(),
      rateWait: { waitS: 0, startedWallMs: 0 },
      usage: { promptTokens: null, cachedTokens: null, completionTokens: null },
    },

    // Historical replay state
    replay: {
      loading: false,
      id: null,
      bundle: null, // prepared bundle
      runSummary: null,
      playing: false,
      speed: 1,
      currentElapsed: 0,
      durationS: 0,
      lastTickWallMs: 0,
      keyframeTimes: [],
      reconstructed: null,
      cachedBundleId: null,
      cachedFrameIdx: 0,
      cachedStreamModel: null,
      cachedAcc: null,
      cachedEnvHurtElapsed: -1,
      cachedPrevHp: undefined,
    },

    // UI expansion sets so toggling chips/raw JSON survives updates
    expandedSharedCallIds: new Set(),
    expandedRawCallIds: new Set(),
    waterfallMaxEntries: 30,
    lastWaterfallStructureSig: "",
    tickingToolEls: [],
    lastRpmRenderedText: "",
    liveRenderRaf: 0,

    // Trajectory viewport camera (XZ & Y)
    traj: {
      selectedDim: "overworld",
      dimAutoFollow: true,
      followBot: true,
      camX: 0,
      camZ: 0,
      scale: 5.5, // pixels per block
      dragging: false,
      dragStartX: 0,
      dragStartY: 0,
      dragCamX: 0,
      dragCamZ: 0,
      hoverPoint: null, // { pos, elapsed, health, food, label, kind, screenX, screenY }
      hoverRaf: 0,
      lastHoverKey: "",
      yScrubbing: false,
    },

    // Left 3D viewer instance & capture media stream + LAN relay
    viewer3d: null,
    viewerInitTried: 0,
    captureStream: null,
    captureRelayEnabled: true,
    captureRelayWs: null,
    captureRelayTimer: null,
    captureEncoding: false,
    captureMetaKey: "",
    captureErrorMsg: "",
    captureCameraPending: false,
    captureCameraError: "",
    captureCameraErrorTimer: null,
    captureReconnectTimer: null,
    captureStandbyPollTimer: null,
    prismarineMode: "third_person",
    prismarinePending: false,
    prismarineFeedback: "",
    prismarineFeedbackTimer: null,
    prismarineHealthProbedUrl: "",
  };

  // -------------------- DOM Element Cache --------------------
  const dom = {};
  function cacheDom() {
    const ids = [
      "app-shell",
      "btn-mode-live",
      "btn-mode-replay",
      "run-select",
      "btn-refresh-runs",
      "replay-controls",
      "btn-step-prev",
      "btn-play-pause",
      "btn-step-next",
      "replay-scrubber",
      "replay-time-label",
      "conn-pill",
      "conn-pill-text",
      "bot-name-badge",
      "bot-name-text",
      "server-tag-badge",
      "server-tag-text",
      "capture-dot",
      "prismarine-dot",
      "viewport-hint",
      "btn-toggle-alerts",
      "btn-toggle-overlays",
      "viewport-stage",
      "viewer3d-container",
      "prismarine-panel",
      "prismarine-live-bar",
      "prismarine-live-status",
      "btn-prismarine-first",
      "btn-prismarine-third",
      "btn-prismarine-reload",
      "prismarine-unavailable",
      "prismarine-note",
      "btn-recheck-prismarine",
      "prismarine-iframe",
      "capture-panel",
      "capture-placeholder",
      "capture-note",
      "capture-wait-badge",
      "manual-capture-fallback",
      "chk-relay-lan",
      "btn-start-capture",
      "local-capture-video",
      "remote-capture-img",
      "capture-live-bar",
      "capture-live-status",
      "capture-camera-toggle",
      "btn-capture-first",
      "btn-capture-third",
      "btn-switch-capture",
      "btn-stop-capture",
      "ov-pos",
      "ov-dim",
      "ov-biome",
      "ov-time",
      "ov-compass",
      "ov-speed",
      "overlay-top-right",
      "ov-busy-badge",
      "ov-busy-text",
      "ov-posture-tags",
      "btn-ov-hurt-toggle",
      "ov-scene-stats",
      "hud-hp-val",
      "hud-hp-bar",
      "hud-food-val",
      "hud-food-bar",
      "hud-oxy-val",
      "hud-oxy-bar",
      "hud-armor-val",
      "hud-armor-bar",
      "hud-held-box",
      "hud-held-name",
      "hud-held-dur-wrap",
      "hud-held-dur",
      "hud-offhand-box",
      "hud-offhand-name",
      "hud-offhand-dur-wrap",
      "hud-offhand-dur",
      "hud-rpm-box",
      "hud-rpm-val",
      "hud-rpm-bar",
      "hud-cache-badge",
      "hud-kv-pct",
      "btn-adv-pill",
      "hud-adv-count",
      "btn-drawer-inv",
      "btn-drawer-adv",
      "drawer-inventory",
      "inv-summary-meta",
      "inv-equipment-grid",
      "inv-slots-grid",
      "drawer-advancements",
      "adv-summary-meta",
      "adv-total-bar",
      "adv-cat-pills",
      "adv-frontier-list",
      "adv-completed-list",
      "wf-phase-badge",
      "wf-turn-progress",
      "wf-model-tag",
      "chk-autoscroll",
      "btn-toggle-task",
      "wf-task-text",
      "waterfall-list",
      "dim-cnt-overworld",
      "dim-cnt-nether",
      "dim-cnt-end",
      "traj-readout",
      "btn-traj-hurt",
      "btn-traj-follow",
      "btn-traj-fit",
      "xz-stage",
      "xz-canvas",
      "traj-tooltip",
      "y-stage",
      "y-canvas",
    ];
    for (const id of ids) {
      dom[id] = document.getElementById(id);
    }
  }

  // -------------------- Utility Helpers --------------------
  function escapeHtml(val) {
    if (val === null || val === undefined) return "";
    return String(val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normDim(dim) {
    if (!dim) return "overworld";
    const s = String(dim).toLowerCase().replace(/^minecraft:/, "");
    if (s.includes("nether")) return "the_nether";
    if (s.includes("end")) return "the_end";
    return "overworld";
  }

  function validPos(pos) {
    return (
      Array.isArray(pos) &&
      pos.length >= 3 &&
      Number.isFinite(Number(pos[0])) &&
      Number.isFinite(Number(pos[1])) &&
      Number.isFinite(Number(pos[2]))
    );
  }

  function toPos(pos) {
    return [Number(pos[0]), Number(pos[1]), Number(pos[2])];
  }

  function blockKey(pos) {
    return `${Math.floor(Number(pos[0]))},${Math.floor(Number(pos[1]))},${Math.floor(Number(pos[2]))}`;
  }

  function fmtPos(pos, digits = 1) {
    if (!validPos(pos)) return "[--, --, --]";
    const fmt = (n) => {
      const v = Number(n);
      return Number.isInteger(v) ? String(v) : v.toFixed(digits);
    };
    return `[${fmt(pos[0])}, ${fmt(pos[1])}, ${fmt(pos[2])}]`;
  }

  function fmtDurationMs(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "--";
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  function fmtSeconds(s) {
    if (!Number.isFinite(s) || s < 0) return "0.0s";
    return `${Number(s).toFixed(1)}s`;
  }

  function fmtTokens(n) {
    if (!Number.isFinite(n) || n === null) return "--";
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(Math.round(n));
  }

  function safeJsonParse(raw) {
    if (raw === null || raw === undefined) return null;
    if (typeof raw === "object") return raw;
    if (typeof raw !== "string") return null;
    const trimmed = raw.trim();
    if (!trimmed) return null;
    try {
      return JSON.parse(trimmed);
    } catch (_) {
      // Handle tool outputs that have trailing "\n[期间新事件] ..."
      const nlIdx = trimmed.indexOf("\n[期间新事件]");
      if (nlIdx > 0) {
        try {
          return JSON.parse(trimmed.slice(0, nlIdx));
        } catch (_2) {
          return null;
        }
      }
      return null;
    }
  }

  function inferConcurrency(toolName, explicitConcurrency) {
    if (explicitConcurrency === "shared" || explicitConcurrency === "exclusive") {
      return explicitConcurrency;
    }
    if (!toolName) return "exclusive";
    if (SHARED_TOOLS.has(toolName)) return "shared";
    return "exclusive";
  }

  function formatCompass(yaw) {
    if (!Number.isFinite(Number(yaw))) return "🧭 --";
    const y = Number(yaw);
    // Mineflayer convention: yaw=0 -> -Z (North), yaw=-1.57 -> +X (East)
    // Forward vector: dx = -sin(y), dz = -cos(y)
    const dx = -Math.sin(y);
    const dz = -Math.cos(y);
    // Clockwise bearing from North (-Z): 0=N, 90=E, 180=S, 270=W
    let deg = (Math.atan2(dx, -dz) * 180) / Math.PI;
    if (deg < 0) deg += 360;
    const dirs = ["N 北", "NE 东北", "E 东", "SE 东南", "S 南", "SW 西南", "W 西", "NW 西北"];
    const idx = Math.round(deg / 45) % 8;
    return `🧭 ${dirs[idx]} · ${Math.round(deg)}°`;
  }

  function formatMcTime(timeOfDay, isDay, raining, thundering) {
    let weather = "";
    if (thundering) weather = " · ⛈️雷暴";
    else if (raining) weather = " · 🌧️雨";

    if (!Number.isFinite(Number(timeOfDay))) {
      if (isDay === true) return `☀️ 白昼${weather}`;
      if (isDay === false) return `🌙 夜晚${weather}`;
      return "☀️ --";
    }
    const ticks = ((Number(timeOfDay) % 24000) + 24000) % 24000;
    const dayFlag = isDay !== undefined ? Boolean(isDay) : ticks < 12542 || ticks > 23460;
    const totalHours = (ticks / 1000 + 6) % 24;
    const hh = String(Math.floor(totalHours)).padStart(2, "0");
    const mm = String(Math.floor((totalHours % 1) * 60)).padStart(2, "0");
    return `${dayFlag ? "☀️ 白昼" : "🌙 夜晚"} ${hh}:${mm}${weather}`;
  }

  function inferServerInfo(baseUrl) {
    if (!baseUrl) {
      return { cls: "server-unknown", label: "离线回放 / 未配置", host: "" };
    }
    let host = baseUrl;
    try {
      const u = new URL(baseUrl);
      host = u.host;
    } catch (_) {
      host = String(baseUrl).replace(/^https?:\/\//, "").split("/")[0];
    }
    const lower = host.toLowerCase();
    if (
      lower.startsWith("10.") ||
      lower.startsWith("26.") ||
      lower.startsWith("127.") ||
      lower.startsWith("192.168.") ||
      lower.startsWith("172.") ||
      lower.includes("localhost") ||
      lower.includes(":18900")
    ) {
      return { cls: "server-local", label: "🏠 本地测试服", host };
    }
    if (lower.includes("nmo.net.cn") || lower.includes("windywindie.top")) {
      return { cls: "server-comp", label: "🏟️ 线上比赛服", host };
    }
    return { cls: "server-local", label: `🌐 ${host}`, host };
  }

  // -------------------- Stream / Waterfall Model Builder --------------------
  function createEmptyStreamModel() {
    return {
      meta: {
        task: "",
        bot: "",
        model: "",
        max_calls: null,
        duration_s: 0,
        status: "idle",
        actor_requests: 0,
        phase: "idle",
      },
      entries: [], // Ordered items: { entryType: 'turn' | 'event', ... }
      turnsByNum: new Map(), // requestNum -> turnObj
      toolsById: new Map(), // call_id -> toolObj
      currentTurnNum: 0,
      lastSeq: 0,
      latestUsage: { promptTokens: null, cachedTokens: null, completionTokens: null },
      latestRateWait: { waitS: 0, elapsed: 0 },
    };
  }

  function ensureTurn(streamModel, reqNum, elapsed) {
    const turnNum = reqNum && reqNum > 0 ? reqNum : Math.max(1, streamModel.currentTurnNum || 1);
    streamModel.currentTurnNum = Math.max(streamModel.currentTurnNum, turnNum);
    streamModel.meta.actor_requests = Math.max(streamModel.meta.actor_requests || 0, turnNum);
    let turn = streamModel.turnsByNum.get(turnNum);
    if (!turn) {
      turn = {
        entryType: "turn",
        turnNum,
        startElapsed: elapsed ?? 0,
        endElapsed: elapsed ?? 0,
        text: "",
        textStreaming: false,
        textFinalized: false,
        rateWaitS: 0,
        durationS: null,
        usage: null,
        items: [], // { itemType: 'shared_group', tools: [] } | { itemType: 'exclusive_tool', tool } | { itemType: 'event', ev }
      };
      streamModel.turnsByNum.set(turnNum, turn);
      streamModel.entries.push(turn);
    }
    return turn;
  }

  function appendToolToTurn(turn, tool) {
    if (tool.addedToTurn) return;
    tool.addedToTurn = true;
    if (tool.concurrency === "shared") {
      const last = turn.items[turn.items.length - 1];
      if (last && last.itemType === "shared_group") {
        last.tools.push(tool);
        return;
      }
      turn.items.push({ itemType: "shared_group", tools: [tool] });
    } else {
      turn.items.push({ itemType: "exclusive_tool", tool });
    }
  }

  function ensureToolRecord(streamModel, callId, hintTurnNum, elapsed) {
    const id = callId || `anon_${streamModel.toolsById.size + 1}`;
    let tool = streamModel.toolsById.get(id);
    if (!tool) {
      const turn = ensureTurn(streamModel, hintTurnNum || streamModel.currentTurnNum || 1, elapsed);
      tool = {
        callId: id,
        turnNum: turn.turnNum,
        name: "",
        rawArgs: "",
        parsedArgs: null,
        concurrency: null, // assigned once known
        status: "ready", // 'ready' | 'running' | 'completed' | 'skipped'
        ok: null,
        errorCode: "",
        kbHint: "",
        skipReason: "",
        rawResult: "",
        parsedResult: null,
        startElapsed: elapsed ?? null,
        endElapsed: null,
        startedWallMs: performance.now(),
        durationMs: null,
        addedToTurn: false,
      };
      streamModel.toolsById.set(id, tool);
    }
    return tool;
  }

  // Normalize a frame from either backend normalized replay/WS OR raw .events.jsonl
  function normalizeRawFrame(raw) {
    if (!raw || typeof raw !== "object") return null;
    const seq = Number(raw.seq || 0);
    const elapsed = Number(raw.elapsed ?? raw.ts_elapsed ?? 0);
    const d = raw.data && typeof raw.data === "object" ? raw.data : raw;
    const rawKind = String(raw.kind || raw.type || "");
    return { seq, elapsed, rawKind, d, statePatch: raw.state_patch || null, raw };
  }

  function extractUsage(uObj) {
    if (!uObj || typeof uObj !== "object") return null;
    const u = uObj.usage && typeof uObj.usage === "object" ? uObj.usage : uObj;
    const promptTokens = Number(u.prompt_tokens ?? u.input_tokens ?? NaN);
    const completionTokens = Number(u.completion_tokens ?? u.output_tokens ?? NaN);
    const cachedRaw =
      u.prompt_tokens_details?.cached_tokens ??
      u.prompt_cache_hit_tokens ??
      u.cached_tokens ??
      null;
    const cachedTokens = cachedRaw !== null && cachedRaw !== undefined ? Number(cachedRaw) : null;
    if (!Number.isFinite(promptTokens)) return null;
    return {
      promptTokens,
      cachedTokens: Number.isFinite(cachedTokens) ? cachedTokens : 0,
      completionTokens: Number.isFinite(completionTokens) ? completionTokens : 0,
    };
  }

  function inspectToolResultPayload(tool, rawResult) {
    tool.rawResult = typeof rawResult === "string" ? rawResult : JSON.stringify(rawResult ?? "");
    const parsed = safeJsonParse(rawResult);
    tool.parsedResult = parsed;

    if (typeof rawResult === "string" && rawResult.startsWith("错误：")) {
      tool.ok = false;
      tool.errorCode = "error";
    } else if (parsed && typeof parsed === "object") {
      if (parsed.ok === false) {
        tool.ok = false;
      } else if (parsed.ok === true) {
        tool.ok = true;
      } else {
        tool.ok = true;
      }
      const errObj = parsed.error || parsed.partial_error || null;
      if (errObj && typeof errObj === "object") {
        if (errObj.code) tool.errorCode = String(errObj.code);
        if (errObj.kb_hint) tool.kbHint = String(errObj.kb_hint);
      }
      if (!tool.kbHint && parsed.kb_hint) {
        tool.kbHint = String(parsed.kb_hint);
      }
      if (!tool.errorCode && parsed.protected_blacklisted) {
        tool.errorCode = `protected×${parsed.protected_blacklisted}`;
      }
    } else {
      tool.ok = true;
    }
  }

  function applyFrameToStreamModel(streamModel, rawFrame, options = {}) {
    const nf = normalizeRawFrame(rawFrame);
    if (!nf) return null;
    const { seq, elapsed, rawKind, d } = nf;
    const isCoalescedThought =
      (rawKind === "thought_delta" || rawKind === "model.text.delta") &&
      seq > 0 &&
      seq === streamModel.lastSeq;
    if (seq > 0 && seq <= streamModel.lastSeq && !isCoalescedThought && !options.allowReplayRebuild) {
      return nf;
    }
    if (seq > 0) streamModel.lastSeq = Math.max(streamModel.lastSeq, seq);
    streamModel.meta.duration_s = Math.max(streamModel.meta.duration_s || 0, elapsed);

    const reqNum = Number(d.request || d.turn || nf.raw.request || 0);

    // 1. Run started / metadata
    if (rawKind === "run.started" || rawKind === "started" || rawKind === "run_started") {
      if (d.task) streamModel.meta.task = d.task;
      if (d.bot) streamModel.meta.bot = d.bot;
      if (d.model) streamModel.meta.model = d.model;
      if (d.max_calls) streamModel.meta.max_calls = d.max_calls;
      streamModel.meta.status = "running";
    }
    // 2. Phase transition (also carries run.started metadata in backend ReplayIncrementer)
    else if (rawKind === "phase" || rawKind === "run.phase") {
      if (d.task) streamModel.meta.task = d.task;
      if (d.bot) streamModel.meta.bot = d.bot;
      if (d.model) streamModel.meta.model = d.model;
      if (d.max_calls) streamModel.meta.max_calls = d.max_calls;
      const ph = d.phase || nf.raw.phase;
      if (ph) streamModel.meta.phase = String(ph);
      if (ph === "failed" && d.error) {
        const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
        turn.textStreaming = false;
        turn.items.push({
          itemType: "event",
          ev: { kind: "hurt", elapsed, text: `⚠️ 模型异常: ${d.error}` },
        });
      }
    }
    // 3. Model request / started
    else if (
      rawKind === "model.request" ||
      rawKind === "model.started" ||
      rawKind === "model_request" ||
      rawKind === "model_started"
    ) {
      const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum + 1, elapsed);
      turn.textStreaming = true;
      streamModel.meta.phase = "generating";
    }
    // 4. Rate limit wait
    else if (rawKind === "rate_wait" || rawKind === "model.rate_wait") {
      const waitS = Number(d.wait_s ?? nf.raw.wait_s ?? 0);
      const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
      if (waitS > 0) {
        turn.rateWaitS = Math.max(turn.rateWaitS, waitS);
      }
      streamModel.latestRateWait = { waitS, elapsed };
    }
    // 5. Streaming assistant thought / planning text
    else if (
      rawKind === "thought_delta" ||
      rawKind === "model.text.delta" ||
      rawKind === "thought" ||
      rawKind === "model.text"
    ) {
      const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
      const txt = String(d.text ?? d.delta ?? d.content ?? "");
      if (typeof d.accumulated === "string" && d.accumulated.length > 0) {
        turn.text = d.accumulated;
        turn.textStreaming = rawKind !== "thought" && rawKind !== "model.text";
      } else if (rawKind === "thought" || rawKind === "model.text") {
        if (!turn.text || d.replace) {
          turn.text = txt;
        } else if (!turn.textFinalized) {
          turn.text += txt;
        }
        turn.textStreaming = false;
      } else if (!turn.textFinalized) {
        turn.text += txt;
        turn.textStreaming = true;
      }
      turn.endElapsed = Math.max(turn.endElapsed, elapsed);
    }
    // 6. Streaming tool delta (preview arguments while model generates)
    else if (rawKind === "model.tool.delta" || rawKind === "tool_delta") {
      // Optional early preview; actual card finalized at tool_ready/tool_started
    }
    // 7. Token & KV Cache usage
    else if (rawKind === "usage" || rawKind === "model.usage") {
      const u = extractUsage(d);
      if (u) {
        streamModel.latestUsage = u;
        const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
        turn.usage = u;
      }
    }
    // 8. Model completed / failed
    else if (rawKind === "model.completed" || rawKind === "model_completed") {
      const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
      turn.textStreaming = false;
      turn.textFinalized = true;
      if (d.duration !== undefined) turn.durationS = Number(d.duration);
      const msg = d.message;
      if (msg && typeof msg === "object") {
        const reasoning = typeof msg.reasoning_content === "string" ? msg.reasoning_content.trim() : "";
        const content = typeof msg.content === "string" ? msg.content.trim() : "";
        if (reasoning) {
          const baseText = (turn.text || content || "").trim();
          if (!baseText.startsWith("[思考]")) {
            turn.text = baseText ? `[思考]\n${reasoning}\n\n${baseText}` : `[思考]\n${reasoning}`;
          }
        } else if (!turn.text && typeof msg.content === "string" && msg.content.trim()) {
          turn.text = msg.content;
        }
        if (Array.isArray(msg.tool_calls)) {
          for (const tc of msg.tool_calls) {
            if (!tc || !tc.id) continue;
            const tool = ensureToolRecord(streamModel, tc.id, turn.turnNum, elapsed);
            const fn = tc.function || {};
            if (fn.name) tool.name = fn.name;
            if (fn.arguments !== undefined) {
              tool.rawArgs = typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments);
              tool.parsedArgs = safeJsonParse(tool.rawArgs);
            }
          }
        }
      }
    } else if (rawKind === "model.failed" || rawKind === "model_failed") {
      const turn = ensureTurn(streamModel, reqNum || streamModel.currentTurnNum || 1, elapsed);
      turn.textStreaming = false;
      const errMsg = d.error || "模型请求失败";
      turn.items.push({
        itemType: "event",
        ev: { kind: "hurt", elapsed, text: `⚠️ 模型异常: ${errMsg}` },
      });
    }
    // 9. Tool lifecycle: ready -> started -> completed / skipped
    else if (rawKind === "tool_ready" || rawKind === "tool.ready") {
      const callId = d.call_id || d.id || nf.raw.call_id;
      const tool = ensureToolRecord(streamModel, callId, reqNum, elapsed);
      if (d.name || d.tool) tool.name = d.name || d.tool;
      const fn = d.function || {};
      if (fn.name) tool.name = fn.name;
      if (fn.arguments !== undefined && !tool.rawArgs) {
        tool.rawArgs = typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments);
        tool.parsedArgs = safeJsonParse(tool.rawArgs);
      }
      tool.concurrency = inferConcurrency(tool.name, d.concurrency || nf.raw.concurrency);
    } else if (rawKind === "tool_started" || rawKind === "tool.started") {
      streamModel.meta.phase = "executing_tools";
      const callId = d.call_id || d.id || nf.raw.call_id;
      const tool = ensureToolRecord(streamModel, callId, reqNum, elapsed);
      const fn = d.function || nf.raw.function || {};
      const name = fn.name || d.name || d.tool || tool.name || "tool";
      tool.name = name;
      const argsVal =
        fn.arguments !== undefined
          ? fn.arguments
          : d.arguments !== undefined
          ? d.arguments
          : d.args;
      if (argsVal !== undefined) {
        tool.rawArgs = typeof argsVal === "string" ? argsVal : JSON.stringify(argsVal);
        tool.parsedArgs = safeJsonParse(tool.rawArgs);
      }
      tool.concurrency = inferConcurrency(tool.name, tool.concurrency || d.concurrency);
      tool.status = "running";
      tool.startElapsed = elapsed;
      tool.startedWallMs = performance.now();
      const turn = ensureTurn(streamModel, tool.turnNum, elapsed);
      appendToolToTurn(turn, tool);
    } else if (rawKind === "tool_skipped" || rawKind === "tool.skipped") {
      const callId = d.call_id || d.id || nf.raw.call_id;
      const tool = ensureToolRecord(streamModel, callId, reqNum, elapsed);
      const fn = d.function || {};
      if (fn.name && !tool.name) tool.name = fn.name;
      if (d.name && !tool.name) tool.name = d.name;
      if (!tool.name) tool.name = `tool_${String(callId).slice(0, 8)}`;
      const argsVal = d.arguments !== undefined ? d.arguments : d.args;
      if (argsVal !== undefined && !tool.rawArgs) {
        tool.rawArgs = typeof argsVal === "string" ? argsVal : JSON.stringify(argsVal);
        tool.parsedArgs = safeJsonParse(tool.rawArgs);
      }
      tool.concurrency = inferConcurrency(tool.name, tool.concurrency || d.concurrency);
      tool.status = "skipped";
      tool.skipReason = String(d.reason || nf.raw.reason || "previous_action_failed");
      tool.endElapsed = elapsed;
      const turn = ensureTurn(streamModel, tool.turnNum, elapsed);
      appendToolToTurn(turn, tool);
    } else if (rawKind === "tool_completed" || rawKind === "tool.completed") {
      const callId = d.call_id || d.id || nf.raw.call_id;
      const tool = ensureToolRecord(streamModel, callId, reqNum, elapsed);
      if (d.name && !tool.name) tool.name = d.name;
      const fn = d.function || {};
      if (fn.name && !tool.name) tool.name = fn.name;
      const argsVal = d.arguments !== undefined ? d.arguments : d.args;
      if (argsVal !== undefined && !tool.rawArgs) {
        tool.rawArgs = typeof argsVal === "string" ? argsVal : JSON.stringify(argsVal);
        tool.parsedArgs = safeJsonParse(tool.rawArgs);
      }
      tool.concurrency = inferConcurrency(tool.name, tool.concurrency || d.concurrency);

      // If this tool was previously marked skipped by ToolScheduler._skip (which emits tool.skipped then tool.completed), preserve skipped status!
      const rawRes = d.result !== undefined ? d.result : nf.raw.result;
      if (tool.status !== "skipped") {
        tool.status = "completed";
        inspectToolResultPayload(tool, rawRes);
        if (d.ok === false) tool.ok = false;
        if (d.kb_hint && !tool.kbHint) tool.kbHint = String(d.kb_hint);
        if (d.error_code && !tool.errorCode) tool.errorCode = String(d.error_code);
      } else {
        tool.rawResult = typeof rawRes === "string" ? rawRes : JSON.stringify(rawRes ?? "");
      }

      tool.endElapsed = elapsed;
      if (d.duration_ms !== undefined) {
        tool.durationMs = Number(d.duration_ms);
      } else if (d.duration_s !== undefined) {
        tool.durationMs = Number(d.duration_s) * 1000;
      } else if (tool.startElapsed !== null && elapsed >= tool.startElapsed) {
        tool.durationMs = (elapsed - tool.startElapsed) * 1000;
      }

      const turn = ensureTurn(streamModel, tool.turnNum, elapsed);
      appendToolToTurn(turn, tool);

      // Extract inline game events from d.game_events (coalescing bursts of identical hurt sources) or statePatch.marker
      if (Array.isArray(d.game_events) && d.game_events.length > 0) {
        const coalesced = [];
        for (const gev of d.game_events) {
          if (!gev || typeof gev !== "object") continue;
          const prev = coalesced[coalesced.length - 1];
          if (
            gev.type === "hurt" &&
            prev &&
            prev.type === "hurt" &&
            (prev.source || "") === (gev.source || "")
          ) {
            prev._burst = (prev._burst || 1) + 1;
            if (gev.health !== undefined) prev.health = gev.health;
            prev.label = `⚡ 受伤 (${gev.source || "env"} ×${prev._burst})${
              prev.health !== undefined ? ` HP→${prev.health}` : ""
            }`;
          } else {
            coalesced.push({ ...gev });
          }
        }
        for (const gev of coalesced) {
          appendInlineMcEvent(streamModel, { ...gev, elapsed });
        }
      } else if (nf.statePatch?.marker) {
        const mk = nf.statePatch.marker;
        if (mk.kind === "death" || mk.kind === "hurt" || mk.kind === "advancement") {
          appendInlineMcEvent(streamModel, {
            type: mk.kind,
            elapsed,
            pos: mk.pos,
            label: mk.label,
            dropped_items_at: mk.dropped_items_at,
          });
        }
      }
    }
    // 10. Runtime user input injection
    else if (rawKind === "input" || rawKind === "input.received" || rawKind === "input.applied") {
      const inText = String(d.text || nf.raw.text || "");
      if (
        rawKind !== "input.applied" &&
        d.event_type !== "input.applied" &&
        inText &&
        !inText.includes("[开局实时状态快照]")
      ) {
        appendInlineMcEvent(streamModel, {
          type: "input",
          elapsed,
          text: inText,
        });
      }
    }
    // 11. Interleaved MC game event frame
    else if (rawKind === "game_events") {
      for (const event of d.game_events || []) {
        appendInlineMcEvent(streamModel, { ...event, elapsed });
      }
    } else if (rawKind === "mc_event" || rawKind === "game_event") {
      appendInlineMcEvent(streamModel, { ...d, elapsed });
    }
    // 12. Run finished
    else if (rawKind === "finished" || rawKind === "run.finished") {
      const st = d.status || nf.raw.status || "finished";
      streamModel.meta.status = st;
      streamModel.meta.phase = st === "answered" || st === "finished" ? "finished" : st;
      if (d.actor_requests !== undefined) {
        streamModel.meta.actor_requests = Number(d.actor_requests);
      }
      // Close any streaming cursor
      for (const t of streamModel.turnsByNum.values()) {
        t.textStreaming = false;
      }
      if (d.answer && streamModel.currentTurnNum > 0) {
        const lastTurn = streamModel.turnsByNum.get(streamModel.currentTurnNum);
        if (lastTurn && !lastTurn.text) {
          lastTurn.text = String(d.answer);
        }
      }
      if (d.error) {
        appendInlineMcEvent(streamModel, {
          type: "hurt",
          elapsed,
          text: `🛑 运行终止 (${st}): ${d.error}`,
        });
      }
    }

    return nf;
  }

  function appendInlineMcEvent(streamModel, rawEv) {
    if (!rawEv || typeof rawEv !== "object") return;
    const evType = String(rawEv.type || rawEv.kind || "info").toLowerCase();
    const elapsed = Number(rawEv.elapsed ?? streamModel.meta.duration_s ?? 0);
    let chipKind = "chat";
    let text = "";
    let dropPos = null;

    if (evType === "hurt") {
      chipKind = "hurt";
      const hp = rawEv.health !== undefined ? `HP ${Number(rawEv.health).toFixed(1)}` : "";
      const dmg = rawEv.damage !== undefined ? `-${Number(rawEv.damage).toFixed(1)}` : "";
      const attacker =
        rawEv.likely_attacker?.name ||
        rawEv.source_entity?.name ||
        rawEv.source ||
        rawEv.by ||
        "";
      text = rawEv.text || rawEv.label || `⚡ 受伤 ${dmg} ${hp}${attacker ? ` · 来源: ${attacker}` : ""}`.trim();
    } else if (evType === "death") {
      chipKind = "death";
      const cause = rawEv.cause || rawEv.message || rawEv.label || "角色死亡";
      const posStr = validPos(rawEv.pos) ? ` 死亡点 ${fmtPos(rawEv.pos, 1)}` : "";
      dropPos = validPos(rawEv.dropped_items_at) ? rawEv.dropped_items_at : validPos(rawEv.pos) ? rawEv.pos : null;
      text = `💀 ${cause}${posStr}`;
    } else if (evType === "pickup") {
      chipKind = "pickup";
      const item = rawEv.item || "物品";
      const cnt = rawEv.count !== undefined ? ` ×${rawEv.count}` : "";
      text = `📦 拾取掉落物: ${item}${cnt}`;
    } else if (evType === "chat") {
      chipKind = "chat";
      const sender = rawEv.username || rawEv.sender || rawEv.from || "";
      const msg = rawEv.text || rawEv.message || "";
      if (!msg) return;
      text = `💬 ${sender ? `${sender}: ` : ""}${msg}`;
    } else if (evType === "advancement" || evType === "advancements") {
      chipKind = "advancement";
      const title = rawEv.title || rawEv.label || rawEv.advancement || rawEv.id || "新进度";
      text = `🏆 解锁进度: ${title}`;
    } else if (evType === "input") {
      chipKind = "input";
      text = `⌨️ 实时指令注入: ${rawEv.text || ""}`;
    } else {
      return;
    }

    const evObj = { kind: chipKind, elapsed, text, dropPos };
    if (streamModel.currentTurnNum > 0) {
      const turn = ensureTurn(streamModel, streamModel.currentTurnNum, elapsed);
      // Deduplicate identical consecutive event in same turn
      const prev = turn.items[turn.items.length - 1];
      if (prev && prev.itemType === "event" && prev.ev.text === evObj.text) return;
      turn.items.push({ itemType: "event", ev: evObj });
    } else {
      streamModel.entries.push({ entryType: "event", ev: evObj });
    }
  }

  // -------------------- State Extraction From Patch & Tool Payloads --------------------
  function applyStatePatchAndToolEffects(acc, nf) {
    const patch = nf.statePatch;
    if (patch && typeof patch === "object") {
      if (patch.status && typeof patch.status === "object") {
        Object.assign(acc.status, patch.status);
      }
      const nullableKeys = new Set([
        "held",
        "offhand",
        "nearest_hostile",
        "busy",
        "in_vehicle",
        "using_item",
      ]);
      for (const k of [
        "pos",
        "yaw",
        "pitch",
        "dimension",
        "health",
        "food",
        "oxygen",
        "armor_points",
        "held",
        "offhand",
        "biome",
        "time_of_day",
        "is_day",
        "in_water",
        "submerged",
        "in_lava",
        "on_fire",
        "sneaking",
        "blocking",
        "busy",
        "speed",
        "nearest_hostile",
        "last_death",
      ]) {
        if (patch[k] !== undefined && (patch[k] !== null || nullableKeys.has(k))) {
          acc.status[k] = patch[k];
        }
      }
      if (patch.inventory && typeof patch.inventory === "object") {
        acc.inventory = patch.inventory;
      }
      if (patch.advancements && typeof patch.advancements === "object") {
        acc.advancements = mergeAdvancementData(acc.advancements, patch.advancements);
      }
      if (Array.isArray(patch.entities)) {
        acc.entities = patch.entities;
      }
      if (Array.isArray(patch.blocks_removed)) {
        for (const p of patch.blocks_removed) {
          const pos = Array.isArray(p) ? p : p?.pos;
          if (validPos(pos)) acc.blocksMap.delete(blockKey(pos));
        }
      }
      if (Array.isArray(patch.blocks_added)) {
        for (const b of patch.blocks_added) {
          if (b && validPos(b.pos) && b.name) {
            acc.blocksMap.set(blockKey(b.pos), { pos: toPos(b.pos), name: String(b.name) });
          }
        }
      }
    }

    // Also harvest directly from tool.completed JSON payloads for extra resilience
    if (nf.rawKind === "tool_completed" || nf.rawKind === "tool.completed") {
      const rawRes = nf.d.result !== undefined ? nf.d.result : nf.raw.result;
      const parsed = safeJsonParse(rawRes);
      if (!parsed || typeof parsed !== "object") return;

      const inner = parsed.result && typeof parsed.result === "object" ? parsed.result : parsed;

      // Status payload
      if (validPos(inner.pos) && inner.health !== undefined && inner.food !== undefined) {
        Object.assign(acc.status, inner);
      } else {
        if (validPos(inner.pos_after)) acc.status.pos = toPos(inner.pos_after);
        else if (validPos(inner.pos)) acc.status.pos = toPos(inner.pos);
        else if (inner.control && validPos(inner.control.pos)) acc.status.pos = toPos(inner.control.pos);

        if (inner.control && Number.isFinite(inner.control.yaw)) acc.status.yaw = Number(inner.control.yaw);
        if (Number.isFinite(inner.health)) acc.status.health = Number(inner.health);
        if (Number.isFinite(inner.food)) acc.status.food = Number(inner.food);
        if (typeof inner.in_water === "boolean") acc.status.in_water = inner.in_water;
      }

      // Inventory payload
      if (Array.isArray(inner.items) && inner.empty_slots !== undefined) {
        acc.inventory = inner;
        if (inner.held !== undefined) acc.status.held = inner.held;
        if (inner.offhand !== undefined) acc.status.offhand = inner.offhand;
      }

      // Advancements frontier or catalog
      if (inner.total_catalog !== undefined || Array.isArray(inner.frontier) || Array.isArray(inner.advancements)) {
        acc.advancements = mergeAdvancementData(acc.advancements, inner);
      }

      // Entities payload
      if (Array.isArray(inner.entities)) {
        acc.entities = inner.entities;
      }

      // Blocks from find_blocks or skill dug/placed
      if (Array.isArray(inner.blocks)) {
        for (const b of inner.blocks) {
          if (b && validPos(b.pos) && b.name) {
            acc.blocksMap.set(blockKey(b.pos), { pos: toPos(b.pos), name: String(b.name) });
          }
        }
      } else if (inner.blocks && typeof inner.blocks === "object") {
        for (const [bName, info] of Object.entries(inner.blocks)) {
          if (info && validPos(info.nearest)) {
            acc.blocksMap.set(blockKey(info.nearest), { pos: toPos(info.nearest), name: bName });
          }
        }
      }

      if (Array.isArray(inner.dug)) {
        for (const dItem of inner.dug) {
          if (dItem && validPos(dItem.pos)) acc.blocksMap.delete(blockKey(dItem.pos));
        }
      }
      if (Array.isArray(inner.placed)) {
        for (const pItem of inner.placed) {
          if (pItem && validPos(pItem.pos) && (pItem.block || pItem.item || pItem.name)) {
            acc.blocksMap.set(blockKey(pItem.pos), {
              pos: toPos(pItem.pos),
              name: String(pItem.block || pItem.item || pItem.name),
            });
          }
        }
      }
      for (const [stationKey, blockName] of [
        ["table_pos", "crafting_table"],
        ["furnace_pos", "furnace"],
        ["bed_pos", "white_bed"],
      ]) {
        if (validPos(inner[stationKey])) {
          acc.blocksMap.set(blockKey(inner[stationKey]), { pos: toPos(inner[stationKey]), name: blockName });
        }
      }
    }
  }

  function mergeAdvancementData(prev, next) {
    const out = { ...(prev || {}) };
    if (!next || typeof next !== "object") return out;
    const src = next.result && typeof next.result === "object" ? next.result : next;

    if (Number.isFinite(src.total_catalog)) out.total_catalog = src.total_catalog;
    if (Number.isFinite(src.completed_count)) {
      out.completed_count = Math.max(out.completed_count || 0, src.completed_count);
    } else if (Number.isFinite(src.summary?.completed)) {
      out.completed_count = Math.max(out.completed_count || 0, src.summary.completed);
    }

    if (Array.isArray(src.completed_ids) && src.completed_ids.length >= (out.completed_ids?.length || 0)) {
      out.completed_ids = src.completed_ids;
    }
    if (Array.isArray(src.completed) && src.completed.length >= (out.completed?.length || 0)) {
      out.completed = src.completed;
    }
    if (Array.isArray(src.advancements)) {
      out.advancements = src.advancements;
      const doneIds = src.advancements
        .filter((a) => a && a.status === "completed" && a.id)
        .map((a) => String(a.id));
      if (doneIds.length > 0) {
        const mergedSet = new Set([...(out.completed_ids || []), ...doneIds]);
        out.completed_ids = Array.from(mergedSet);
        out.completed_count = Math.max(out.completed_count || 0, out.completed_ids.length);
      }
    }
    if (Array.isArray(src.frontier) && src.frontier.length > 0) {
      out.frontier = src.frontier;
      out.frontier_count = src.frontier_count ?? src.frontier.length;
    } else if (Array.isArray(src.frontier_top10) && src.frontier_top10.length > 0 && !out.frontier) {
      out.frontier = src.frontier_top10;
    }
    return out;
  }

  // -------------------- Replay Preparation & Fast Time-Scrubbing --------------------
  function prepareReplayBundle(rawBundle, runMeta) {
    const frames = Array.isArray(rawBundle.frames) ? rawBundle.frames : [];
    const normalizedFrames = frames.map(normalizeRawFrame).filter(Boolean);
    const trajectory = Array.isArray(rawBundle.trajectory)
      ? rawBundle.trajectory
          .filter((p) => p && validPos(p.pos))
          .map((p) => ({
            elapsed: Number(p.elapsed ?? 0),
            pos: toPos(p.pos),
            yaw: Number.isFinite(Number(p.yaw)) ? Number(p.yaw) : 0,
            dimension: normDim(p.dimension),
            health: Number.isFinite(Number(p.health)) ? Number(p.health) : undefined,
            food: Number.isFinite(Number(p.food)) ? Number(p.food) : undefined,
          }))
          .sort((a, b) => a.elapsed - b.elapsed)
      : [];

    const markers = Array.isArray(rawBundle.markers)
      ? rawBundle.markers
          .filter((m) => m && validPos(m.pos))
          .map((m) => ({
            elapsed: Number(m.elapsed ?? 0),
            kind: String(m.kind || "station"),
            label: String(m.label || m.kind || ""),
            pos: toPos(m.pos),
            dimension: normDim(m.dimension),
          }))
          .sort((a, b) => a.elapsed - b.elapsed)
      : [];

    const baseBlocks = Array.isArray(rawBundle.blocks)
      ? rawBundle.blocks
          .filter((b) => b && validPos(b.pos) && b.name)
          .map((b) => ({ pos: toPos(b.pos), name: String(b.name) }))
      : [];

    let maxElapsed = Number(rawBundle.meta?.duration_s || runMeta?.summary?.elapsed_s || 0);
    for (const f of normalizedFrames) maxElapsed = Math.max(maxElapsed, f.elapsed);
    for (const t of trajectory) maxElapsed = Math.max(maxElapsed, t.elapsed);
    if (maxElapsed <= 0) maxElapsed = 1;

    // Keyframe timestamps for crisp ⏮ / ⏭ stepping (ceil to ms + exact maxElapsed so frames never miss by sub-ms)
    const kfSet = new Set([0]);
    for (const f of normalizedFrames) {
      if (
        f.rawKind === "tool_started" ||
        f.rawKind === "tool.started" ||
        f.rawKind === "tool_completed" ||
        f.rawKind === "tool.completed" ||
        f.rawKind === "tool_skipped" ||
        f.rawKind === "tool.skipped" ||
        f.rawKind === "model.completed" ||
        f.rawKind === "model_completed" ||
        f.rawKind === "finished" ||
        f.rawKind === "run.finished"
      ) {
        kfSet.add(Math.min(maxElapsed, Math.ceil(f.elapsed * 1000) / 1000));
      }
    }
    for (const m of markers) {
      kfSet.add(Math.min(maxElapsed, Math.ceil(m.elapsed * 1000) / 1000));
    }
    kfSet.add(maxElapsed);
    const keyframeTimes = Array.from(kfSet).sort((a, b) => a - b);

    return {
      id: rawBundle.id || runMeta?.id || "",
      meta: rawBundle.meta || {},
      normalizedFrames,
      trajectory,
      markers,
      baseBlocks,
      finalState: rawBundle.final_state || {},
      summary: runMeta?.summary || null,
      durationS: maxElapsed,
      keyframeTimes,
    };
  }

  function resetReplayCache() {
    state.replay.cachedBundleId = null;
    state.replay.cachedFrameIdx = 0;
    state.replay.cachedStreamModel = null;
    state.replay.cachedAcc = null;
    state.replay.cachedEnvHurtElapsed = -1;
    state.replay.cachedPrevHp = undefined;
  }

  function reconstructReplayAt(prepared, targetElapsed) {
    const t = Math.max(0, Math.min(prepared.durationS, Number(targetElapsed) || 0));
    const bundleKey = `${prepared.id || ""}:${prepared.normalizedFrames.length}:${prepared.durationS}`;

    // Determine target frame index slice [0, endIdx)
    let endIdx = prepared.normalizedFrames.length;
    for (let i = 0; i < prepared.normalizedFrames.length; i++) {
      if (prepared.normalizedFrames[i].elapsed > t + 1e-6) {
        endIdx = i;
        break;
      }
    }

    const canReuse =
      state.replay.cachedBundleId === bundleKey &&
      state.replay.cachedStreamModel &&
      state.replay.cachedAcc &&
      endIdx >= state.replay.cachedFrameIdx;

    let streamModel;
    let acc;
    let startFrameIdx;
    let envHurtElapsed;
    let prevHpTrack;

    if (canReuse) {
      streamModel = state.replay.cachedStreamModel;
      acc = state.replay.cachedAcc;
      startFrameIdx = state.replay.cachedFrameIdx;
      envHurtElapsed = state.replay.cachedEnvHurtElapsed;
      prevHpTrack = state.replay.cachedPrevHp;
    } else {
      streamModel = createEmptyStreamModel();
      streamModel.meta = {
        task: prepared.meta.task || "",
        bot: prepared.meta.bot || "",
        model: prepared.meta.model || prepared.summary?.provider_model || "",
        max_calls: prepared.meta.max_calls ?? prepared.summary?.max_calls ?? null,
        duration_s: prepared.durationS,
        status: prepared.meta.status || "answered",
        actor_requests: 0,
        phase: "preparing",
      };

      acc = {
        status: {},
        inventory: {},
        advancements: {},
        entities: [],
        blocksMap: new Map(),
      };

      // Seed initial position from summary or first trajectory point so t=0 has valid coordinates
      if (prepared.trajectory.length > 0) {
        const firstPt = prepared.trajectory[0];
        acc.status.pos = [...firstPt.pos];
        acc.status.yaw = firstPt.yaw;
        acc.status.dimension = firstPt.dimension;
        if (firstPt.health !== undefined) acc.status.health = firstPt.health;
        if (firstPt.food !== undefined) acc.status.food = firstPt.food;
      } else if (validPos(prepared.summary?.pos_before)) {
        acc.status.pos = toPos(prepared.summary.pos_before);
        acc.status.dimension = "overworld";
        acc.status.health = 20;
        acc.status.food = 20;
      }

      if (prepared.summary?.frontier_top10) {
        acc.advancements = mergeAdvancementData(acc.advancements, {
          completed_count: prepared.summary.completed_count,
          frontier_top10: prepared.summary.frontier_top10,
        });
      }

      startFrameIdx = 0;
      envHurtElapsed = -1;
      prevHpTrack = acc.status.health;
    }

    // Incrementally apply only frames [startFrameIdx, endIdx)
    for (let i = startFrameIdx; i < endIdx; i++) {
      const nf = prepared.normalizedFrames[i];
      applyFrameToStreamModel(streamModel, nf.raw, { allowReplayRebuild: true });
      applyStatePatchAndToolEffects(acc, nf);

      if (acc.status.health !== undefined && prevHpTrack !== undefined) {
        if (
          acc.status.health < prevHpTrack - 0.1 &&
          !acc.status.in_water &&
          !acc.status.submerged &&
          !acc.status.in_lava &&
          !acc.status.on_fire
        ) {
          envHurtElapsed = nf.elapsed;
        }
      }
      if (acc.status.health !== undefined) prevHpTrack = acc.status.health;
    }

    state.replay.cachedBundleId = bundleKey;
    state.replay.cachedFrameIdx = endIdx;
    state.replay.cachedStreamModel = streamModel;
    state.replay.cachedAcc = acc;
    state.replay.cachedEnvHurtElapsed = envHurtElapsed;
    state.replay.cachedPrevHp = prevHpTrack;

    let envFreezeAlert = envHurtElapsed >= 0 && t - envHurtElapsed <= 20;
    const viewStatus = { ...acc.status };
    let viewInventory = acc.inventory;
    let viewAdvancements = acc.advancements;

    // Interpolate smooth trajectory position & speed at time t
    const activeTraj = [];
    let interpSpeed = 0;
    for (let i = 0; i < prepared.trajectory.length; i++) {
      const pt = prepared.trajectory[i];
      if (pt.elapsed <= t + 1e-6) {
        activeTraj.push(pt);
      } else {
        break;
      }
    }

    if (activeTraj.length > 0) {
      const lastPt = activeTraj[activeTraj.length - 1];
      const nextIdx = activeTraj.length;
      viewStatus.dimension = lastPt.dimension || viewStatus.dimension || "overworld";
      if (lastPt.health !== undefined) viewStatus.health = lastPt.health;
      if (lastPt.food !== undefined) viewStatus.food = lastPt.food;

      if (nextIdx < prepared.trajectory.length) {
        const nextPt = prepared.trajectory[nextIdx];
        const dt = nextPt.elapsed - lastPt.elapsed;
        const dx = nextPt.pos[0] - lastPt.pos[0];
        const dy = nextPt.pos[1] - lastPt.pos[1];
        const dz = nextPt.pos[2] - lastPt.pos[2];
        const dist3d = Math.hypot(dx, dy, dz);
        if (dt > 0.01 && nextPt.dimension === lastPt.dimension && dist3d < 36) {
          const alpha = Math.max(0, Math.min(1, (t - lastPt.elapsed) / dt));
          viewStatus.pos = [
            lastPt.pos[0] + dx * alpha,
            lastPt.pos[1] + dy * alpha,
            lastPt.pos[2] + dz * alpha,
          ];
          // Shortest angular interpolation for yaw
          let dYaw = nextPt.yaw - lastPt.yaw;
          while (dYaw > Math.PI) dYaw -= Math.PI * 2;
          while (dYaw < -Math.PI) dYaw += Math.PI * 2;
          viewStatus.yaw = lastPt.yaw + dYaw * alpha;
          interpSpeed = Math.hypot(dx, dz) / dt;
        } else {
          viewStatus.pos = [...lastPt.pos];
          viewStatus.yaw = lastPt.yaw;
        }
      } else {
        viewStatus.pos = [...lastPt.pos];
        viewStatus.yaw = lastPt.yaw;
      }
    }
    if (viewStatus.speed === undefined) {
      viewStatus.speed = Math.min(12, interpSpeed);
    }

    // If at end of replay or no intermediate inventory/advancements captured yet, merge final_state fallbacks cleanly
    if (t >= prepared.durationS - 0.05 && prepared.finalState) {
      if (prepared.finalState.status && Object.keys(viewStatus).length === 0) {
        Object.assign(viewStatus, prepared.finalState.status);
      }
      if (prepared.finalState.inventory && Object.keys(viewInventory).length === 0) {
        viewInventory = prepared.finalState.inventory;
      }
      if (prepared.finalState.advancements) {
        viewAdvancements = mergeAdvancementData(viewAdvancements, prepared.finalState.advancements);
      }
    } else if (
      prepared.finalState?.advancements &&
      (Object.keys(viewAdvancements).length === 0 || !viewAdvancements.completed_count)
    ) {
      viewAdvancements = mergeAdvancementData(viewAdvancements, prepared.finalState.advancements);
    }

    // Blocks: combine accumulated per-frame blocks; if empty (or to enrich terrain context), merge baseBlocks
    if (acc.blocksMap.size === 0 && prepared.baseBlocks.length > 0) {
      for (const b of prepared.baseBlocks) {
        acc.blocksMap.set(blockKey(b.pos), b);
      }
    }
    const blocks = Array.from(acc.blocksMap.values());

    // Markers active up to t (also check freeze/hurt markers near t)
    const activeMarkers = prepared.markers.filter((m) => m.elapsed <= t + 0.05);
    for (const m of activeMarkers) {
      if (
        (m.kind === "hurt" || m.kind === "death") &&
        t - m.elapsed <= 18 &&
        !viewStatus.in_water &&
        !viewStatus.submerged
      ) {
        if (String(m.label).includes("冻") || String(m.label).includes("snow") || (viewStatus.pos && viewStatus.pos[1] >= 118)) {
          envFreezeAlert = true;
        }
      }
    }

    // Infer underlying busy lock if an exclusive tool is currently running at time t
    let runningTool = null;
    for (const tool of streamModel.toolsById.values()) {
      if (tool.status === "running" && tool.concurrency === "exclusive") {
        runningTool = tool;
      }
    }
    if (runningTool && !viewStatus.busy) {
      viewStatus.busy = runningTool.name;
    }

    const displayStatus = t >= prepared.durationS - 0.05 ? prepared.meta.status || "answered" : "running";
    const viewStreamModel = {
      ...streamModel,
      meta: {
        ...streamModel.meta,
        status: displayStatus,
      },
    };

    return {
      status: viewStatus,
      inventory: viewInventory,
      advancements: viewAdvancements,
      entities: acc.entities,
      blocks,
      trajectory: activeTraj,
      fullTrajectory: prepared.trajectory,
      markers: activeMarkers,
      fullMarkers: prepared.markers,
      streamModel: viewStreamModel,
      envFreezeAlert,
    };
  }

  // -------------------- Active View Snapshot Selector --------------------
  function getCurrentSceneView() {
    if (state.mode === "replay") {
      const rec = state.replay.reconstructed;
      if (!rec) {
        return {
          status: {},
          inventory: {},
          advancements: {},
          entities: [],
          blocks: [],
          trajectory: [],
          fullTrajectory: [],
          markers: [],
          fullMarkers: [],
          streamModel: createEmptyStreamModel(),
          envFreezeAlert: false,
          currentElapsed: 0,
        };
      }
      return {
        ...rec,
        currentElapsed: state.replay.currentElapsed,
      };
    }

    // Live mode: combine live telemetry with active_run fallback if Bot is currently offline
    const liveStatus = state.live.status || {};
    const hasLivePos = validPos(liveStatus.pos);
    const blocks = Array.from(state.live.blocksMap.values());
    const envFreezeAlert = Date.now() < state.live.envHurtUntilMs;

    return {
      status: liveStatus,
      inventory: state.live.inventory || {},
      advancements: state.live.advancements || {},
      entities: state.live.entities || [],
      blocks,
      trajectory: state.live.trajectory || [],
      fullTrajectory: state.live.trajectory || [],
      markers: state.live.markers || [],
      fullMarkers: state.live.markers || [],
      streamModel: state.live.runStream,
      envFreezeAlert,
      currentElapsed: hasLivePos
        ? state.live.trajectory[state.live.trajectory.length - 1]?.elapsed ?? 0
        : state.live.runStream.meta.duration_s,
    };
  }

  // -------------------- Top Header, Mode & API Fetching --------------------
  async function fetchInfoAndRuns() {
    try {
      const [infoRes, runsRes] = await Promise.all([
        fetch("/api/info").then((r) => (r.ok ? r.json() : null)).catch(() => null),
        fetch("/api/runs").then((r) => (r.ok ? r.json() : null)).catch(() => null),
      ]);
      if (infoRes && typeof infoRes === "object") {
        applyServerInfo(infoRes);
      }
      if (runsRes && Array.isArray(runsRes.runs)) {
        state.runs = runsRes.runs;
        renderRunSelector();
      }
    } catch (_) {
      // Handled gracefully offline
    }
  }

  function applyServerInfo(info) {
    const prevCaptureActive = Boolean(state.info.capture_stream?.active);
    state.info = {
      ...state.info,
      ...info,
      prismarine_viewer: {
        available: false,
        embed_url: null,
        note: "",
        ...(info.prismarine_viewer || {}),
      },
      capture_stream: {
        active: false,
        stream_url: null,
        snapshot_url: null,
        width: 0,
        height: 0,
        fps: 0,
        source: null,
        seq: 0,
        camera_mode: "third_person",
        director_available: false,
        ...(info.capture_stream || {}),
      },
    };
    if (!prevCaptureActive && state.info.capture_stream.active) {
      state.captureCameraError = "";
    }
    renderHeaderStatus();
    renderPrismarineState();
    renderCaptureState();
  }

  function renderHeaderStatus() {
    // Mode buttons & replay bar visibility
    dom["btn-mode-live"].classList.toggle("active", state.mode === "live");
    dom["btn-mode-replay"].classList.toggle("active", state.mode === "replay");
    dom["replay-controls"].classList.toggle("hidden", state.mode !== "replay");

    // Connection pill
    const pill = dom["conn-pill"];
    const pillText = dom["conn-pill-text"];
    pill.className = "status-pill";
    if (state.mode === "replay") {
      pill.classList.add("replay");
      pillText.textContent = state.replay.loading
        ? "加载回放中..."
        : `离线回放${state.wsConnected ? " · WS在线" : ""}`;
    } else if (!state.wsConnected) {
      pill.classList.add("connecting");
      pillText.textContent = state.wsReconnectAttempts > 0 ? "WS 重连中..." : "连接中...";
    } else if (state.info.online && state.info.ready) {
      pill.classList.add("ready");
      pillText.textContent = "🟢 Bot 在线就绪";
    } else if (state.info.online) {
      pill.classList.add("standby");
      pillText.textContent = "🟡 已连接 (未进服)";
    } else {
      pill.classList.add("standby");
      pillText.textContent = "🟡 旁路日志监听";
    }

    // Bot username
    const view = getCurrentSceneView();
    const botName =
      (state.mode === "replay" && view.streamModel.meta.bot) ||
      view.status?.username ||
      state.info.bot_username ||
      state.live.runStream.meta.bot ||
      "未指定 Bot";
    dom["bot-name-text"].textContent = botName;

    // Server tag inferred from mc_api_base_url
    const srv = inferServerInfo(state.info.mc_api_base_url);
    dom["server-tag-badge"].className = `meta-badge ${srv.cls}`;
    dom["server-tag-text"].textContent = srv.label;
    dom["server-tag-badge"].title = srv.host ? `API 节点: ${srv.host}` : "MC Agent API";
  }

  function renderRunSelector() {
    const sel = dom["run-select"];
    const prevVal = sel.value || state.replay.id || state.info.active_run_id || "";
    sel.innerHTML = "";

    if (!state.runs.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "暂无 runs/*.events.jsonl 日志";
      sel.appendChild(opt);
      return;
    }

    for (const r of state.runs) {
      const opt = document.createElement("option");
      opt.value = r.id;
      const sum = r.summary || {};
      const stageName = sum.stage || r.label || r.id;
      const statusTag = sum.status === "answered" ? "✓" : sum.status ? `[${sum.status}]` : "";
      const durTag = Number.isFinite(Number(sum.elapsed_s)) ? ` · ${Number(sum.elapsed_s).toFixed(0)}s` : "";
      const liveMark = r.id === state.info.active_run_id ? "🔴 " : "";
      opt.textContent = `${liveMark}${stageName} ${statusTag}${durTag}`.trim();
      sel.appendChild(opt);
    }

    if (prevVal && state.runs.some((r) => r.id === prevVal)) {
      sel.value = prevVal;
    } else {
      sel.value = state.runs[0].id;
    }
  }

  async function loadReplayById(runId, autoPlay = false) {
    if (!runId) return;
    const prevMode = state.mode;
    if (prevMode !== "replay") {
      state.savedLiveView = state.leftView;
    }
    state.mode = "replay";
    if (prevMode !== "replay") {
      if (state.leftView === "local_capture" || state.leftView === "prismarine") {
        setLeftView("third_person");
      } else {
        renderCaptureState();
      }
    }
    state.replay.loading = true;
    state.replay.playing = false;
    state.replay.id = runId;
    if (dom["run-select"].value !== runId) {
      dom["run-select"].value = runId;
    }
    renderHeaderStatus();

    try {
      const res = await fetch(`/api/replay?id=${encodeURIComponent(runId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const rawBundle = await res.json();
      const runMeta = state.runs.find((r) => r.id === runId) || null;
      const prepared = prepareReplayBundle(rawBundle, runMeta);

      state.replay.bundle = prepared;
      state.replay.runSummary = runMeta;
      state.replay.durationS = prepared.durationS;
      state.replay.keyframeTimes = prepared.keyframeTimes;
      // Start at full duration so user immediately sees complete expedition trajectory & waterfall, ready to scrub or play from 0!
      state.replay.currentElapsed = autoPlay ? 0 : prepared.durationS;
      state.replay.playing = Boolean(autoPlay);
      state.replay.lastTickWallMs = performance.now();

      dom["replay-scrubber"].max = String(prepared.durationS);
      dom["replay-scrubber"].value = String(state.replay.currentElapsed);

      state.expandedSharedCallIds.clear();
      state.expandedRawCallIds.clear();
      state.waterfallMaxEntries = 30;
      state.lastWaterfallStructureSig = "";
      resetReplayCache();

      updateReplayFrame();
      fitTrajectoryToAll();
    } catch (err) {
      state.replay.reconstructed = null;
      dom["waterfall-list"].innerHTML = `<div class="wf-empty">加载回放失败: ${escapeHtml(err.message || err)}</div>`;
    } finally {
      state.replay.loading = false;
      renderHeaderStatus();
      renderReplayTransport();
    }
  }

  function setMode(newMode) {
    if (newMode === state.mode) return;
    const prevMode = state.mode;
    state.mode = newMode;
    state.lastWaterfallStructureSig = "";

    if (newMode === "replay") {
      if (prevMode === "live") {
        state.savedLiveView = state.leftView;
        if (state.leftView === "local_capture" || state.leftView === "prismarine") {
          setLeftView("third_person");
        } else {
          renderCaptureState();
        }
      }
      const targetId = dom["run-select"].value || state.runs[0]?.id;
      if (!state.replay.bundle || (targetId && state.replay.id !== targetId)) {
        if (targetId) loadReplayById(targetId, false);
      } else {
        updateReplayFrame();
      }
    } else {
      state.replay.playing = false;
      let restoreView = state.savedLiveView || "local_capture";
      if (
        restoreView === "prismarine" &&
        !state.info.prismarine_viewer?.available &&
        state.info.capture_stream?.active
      ) {
        restoreView = "local_capture";
      }
      if (state.leftView !== restoreView) {
        setLeftView(restoreView);
      } else {
        renderPrismarineState();
        renderCaptureState();
      }
      renderReplayTransport();
      renderAllViews();
      if (state.traj.followBot) centerCameraOnBot();
    }
    renderHeaderStatus();
  }

  function updateReplayFrame() {
    if (!state.replay.bundle) return;
    state.replay.reconstructed = reconstructReplayAt(
      state.replay.bundle,
      state.replay.currentElapsed
    );
    dom["replay-scrubber"].value = String(state.replay.currentElapsed);
    dom["replay-time-label"].textContent = `${fmtSeconds(state.replay.currentElapsed)} / ${fmtSeconds(
      state.replay.durationS
    )}`;
    renderReplayTransport();
    renderAllViews();
  }

  function renderReplayTransport() {
    dom["btn-play-pause"].textContent = state.replay.playing ? "⏸ 暂停" : "▶ 播放";
    document.querySelectorAll(".speed-btn").forEach((btn) => {
      btn.classList.toggle("active", Number(btn.dataset.speed) === state.replay.speed);
    });
  }

  function stepReplayKeyframe(dir) {
    if (!state.replay.bundle) return;
    state.replay.playing = false;
    const kfs = state.replay.keyframeTimes;
    const cur = state.replay.currentElapsed;
    if (dir > 0) {
      const next = kfs.find((t) => t > cur + 0.06);
      state.replay.currentElapsed = next !== undefined ? next : state.replay.durationS;
    } else {
      let prev = 0;
      for (const t of kfs) {
        if (t < cur - 0.06) prev = t;
        else break;
      }
      state.replay.currentElapsed = prev;
    }
    updateReplayFrame();
  }

  // -------------------- WebSocket Live Connection --------------------
  function connectWebSocket() {
    if (state.wsReconnectTimer) {
      clearTimeout(state.wsReconnectTimer);
      state.wsReconnectTimer = null;
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws`;
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (_) {
      scheduleWsReconnect();
      return;
    }
    state.ws = ws;

    ws.onopen = () => {
      state.wsConnected = true;
      state.wsReconnectAttempts = 0;
      renderHeaderStatus();
    };

    ws.onmessage = (ev) => {
      const msg = safeJsonParse(ev.data);
      if (!msg || typeof msg !== "object") return;
      handleWsMessage(msg);
    };

    ws.onerror = () => {
      // Handled in onclose
    };

    ws.onclose = () => {
      state.wsConnected = false;
      state.ws = null;
      renderHeaderStatus();
      scheduleWsReconnect();
    };
  }

  function scheduleWsReconnect() {
    if (state.wsReconnectTimer) return;
    state.wsReconnectAttempts += 1;
    const delay = Math.min(5000, 800 * Math.pow(1.6, Math.min(4, state.wsReconnectAttempts - 1)));
    state.wsReconnectTimer = setTimeout(() => {
      state.wsReconnectTimer = null;
      connectWebSocket();
    }, delay);
  }

  function ingestActiveRunBundle(bundle) {
    state.live.runStream = createEmptyStreamModel();
    if (!bundle || typeof bundle !== "object") return;
    state.live.runId = bundle.id || state.info.active_run_id || null;
    if (bundle.meta && typeof bundle.meta === "object") {
      Object.assign(state.live.runStream.meta, bundle.meta);
    }
    const acc = {
      status: {},
      inventory: {},
      advancements: state.live.advancements || {},
      entities: [],
      blocksMap: state.live.blocksMap,
    };
    if (Array.isArray(bundle.frames)) {
      for (const f of bundle.frames) {
        const nf = applyFrameToStreamModel(state.live.runStream, f, { allowReplayRebuild: true });
        if (nf) applyStatePatchAndToolEffects(acc, nf);
      }
    }
    if (Array.isArray(bundle.blocks)) {
      for (const b of bundle.blocks) {
        if (b && validPos(b.pos) && b.name) {
          state.live.blocksMap.set(blockKey(b.pos), { pos: toPos(b.pos), name: String(b.name) });
        }
      }
    }
    if (!validPos(state.live.status?.pos) && validPos(acc.status.pos)) {
      state.live.status = { ...acc.status, ...state.live.status };
    }
    if (Object.keys(state.live.inventory || {}).length === 0) {
      if (bundle.final_state?.inventory) state.live.inventory = bundle.final_state.inventory;
      else if (Object.keys(acc.inventory).length > 0) state.live.inventory = acc.inventory;
    }
    if (bundle.final_state?.advancements) {
      state.live.advancements = mergeAdvancementData(state.live.advancements, bundle.final_state.advancements);
    } else {
      state.live.advancements = acc.advancements;
    }
    if (state.live.trajectory.length === 0 && Array.isArray(bundle.trajectory)) {
      state.live.trajectory = bundle.trajectory
        .filter((p) => p && validPos(p.pos))
        .map((p) => ({
          elapsed: Number(p.elapsed ?? 0),
          pos: toPos(p.pos),
          yaw: Number(p.yaw || 0),
          dimension: normDim(p.dimension),
          health: p.health,
          food: p.food,
        }));
    }
    if (state.live.markers.length === 0 && Array.isArray(bundle.markers)) {
      state.live.markers = bundle.markers
        .filter((m) => m && validPos(m.pos))
        .map((m) => ({
          elapsed: Number(m.elapsed ?? 0),
          kind: String(m.kind || "station"),
          label: String(m.label || ""),
          pos: toPos(m.pos),
          dimension: normDim(m.dimension),
        }));
    }
    if (state.live.runStream.latestUsage.promptTokens !== null) {
      state.live.usage = state.live.runStream.latestUsage;
    }
  }

  function updateLiveStatusAndCheckHazards(newStatus, { isLiveTelemetry = false } = {}) {
    if (!newStatus || typeof newStatus !== "object") return;
    const nextHp = Number.isFinite(Number(newStatus.health)) ? Number(newStatus.health) : null;
    if (nextHp !== null) state.live.prevHealth = nextHp;

    state.live.status = { ...state.live.status, ...newStatus };
    if ((isLiveTelemetry || state.info.online) && newStatus.ready !== undefined) {
      state.info.ready = Boolean(newStatus.ready);
    }
    if (newStatus.username) state.info.bot_username = newStatus.username;
  }

  function capLiveBlocksMap() {
    if (state.live.blocksMap.size <= MAX_LIVE_BLOCKS) return;
    const excess = state.live.blocksMap.size - MAX_LIVE_BLOCKS;
    let removed = 0;
    for (const k of state.live.blocksMap.keys()) {
      state.live.blocksMap.delete(k);
      removed += 1;
      if (removed >= excess) break;
    }
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case "snapshot": {
        if (msg.info) applyServerInfo(msg.info);
        const lv = msg.live || {};
        state.live.blocksMap.clear();
        if (Array.isArray(lv.blocks)) {
          for (const b of lv.blocks) {
            if (b && validPos(b.pos) && b.name) {
              state.live.blocksMap.set(blockKey(b.pos), { pos: toPos(b.pos), name: String(b.name) });
            }
          }
        }
        if (Array.isArray(lv.trajectory)) {
          state.live.trajectory = lv.trajectory
            .filter((p) => p && validPos(p.pos))
            .map((p) => ({
              elapsed: Number(p.elapsed ?? 0),
              pos: toPos(p.pos),
              yaw: Number(p.yaw || 0),
              dimension: normDim(p.dimension),
              health: p.health,
              food: p.food,
            }));
        }
        if (Array.isArray(lv.markers)) {
          state.live.markers = lv.markers
            .filter((m) => m && validPos(m.pos))
            .map((m) => ({
              elapsed: Number(m.elapsed ?? 0),
              kind: String(m.kind || "station"),
              label: String(m.label || ""),
              pos: toPos(m.pos),
              dimension: normDim(m.dimension),
            }));
        }
        if (lv.inventory && typeof lv.inventory === "object") {
          state.live.inventory = lv.inventory;
        }
        if (lv.advancements && typeof lv.advancements === "object") {
          state.live.advancements = mergeAdvancementData(state.live.advancements, lv.advancements);
        }
        if (Array.isArray(lv.entities)) {
          state.live.entities = lv.entities;
        }
        if (lv.status && typeof lv.status === "object") {
          updateLiveStatusAndCheckHazards(lv.status);
        }
        if (msg.active_run) {
          ingestActiveRunBundle(msg.active_run);
        }
        capLiveBlocksMap();
        if (state.mode === "live") {
          renderHeaderStatus();
          renderAllViews();
          fitTrajectoryToAll();
        }
        break;
      }

      case "run_switched": {
        if (msg.run) {
          state.info.active_run_id = msg.run.id || state.info.active_run_id;
          ingestActiveRunBundle(msg.run);
          fetchInfoAndRuns();
          if (state.mode === "live") {
            state.waterfallMaxEntries = 30;
            state.lastWaterfallStructureSig = "";
            renderAllViews();
          }
        }
        break;
      }

      case "harness_frame": {
        const frame = msg.frame;
        if (!frame) break;
        const nf = applyFrameToStreamModel(state.live.runStream, frame);
        if (nf) {
          const acc = {
            status: state.live.status,
            inventory: state.live.inventory,
            advancements: state.live.advancements,
            entities: state.live.entities,
            blocksMap: state.live.blocksMap,
          };
          applyStatePatchAndToolEffects(acc, nf);
          state.live.inventory = acc.inventory;
          state.live.advancements = acc.advancements;
          state.live.entities = acc.entities;

          if (nf.rawKind === "rate_wait" || nf.rawKind === "model.rate_wait") {
            const ws = Number(nf.d.wait_s ?? 0);
            if (ws > 0) {
              state.live.rateWait = { waitS: ws, startedWallMs: performance.now() };
            }
          }
          if (state.live.runStream.latestUsage.promptTokens !== null) {
            state.live.usage = state.live.runStream.latestUsage;
          }
          const mks = Array.isArray(nf.statePatch?.markers)
            ? nf.statePatch.markers
            : nf.statePatch?.marker
            ? [nf.statePatch.marker]
            : [];
          for (const mk of mks) {
            if (mk && validPos(mk.pos)) {
              state.live.markers.push({
                elapsed: nf.elapsed,
                kind: String(mk.kind || "station"),
                label: String(mk.label || ""),
                pos: toPos(mk.pos),
                dimension: normDim(mk.dimension || state.live.status?.dimension),
              });
            }
          }
          capLiveBlocksMap();
        }
        if (state.mode === "live") {
          const rawKind = nf ? nf.rawKind : "";
          const isDeltaOnly =
            rawKind === "thought_delta" ||
            rawKind === "model.text.delta" ||
            rawKind === "model.tool.delta" ||
            rawKind === "tool_delta";
          if (isDeltaOnly) {
            scheduleLiveDeltaRender();
          } else {
            if (state.liveRenderRaf) {
              cancelAnimationFrame(state.liveRenderRaf);
              state.liveRenderRaf = 0;
            }
            renderAllViews();
          }
        }
        break;
      }

      case "telemetry": {
        if (msg.status) {
          state.info.online = true;
          updateLiveStatusAndCheckHazards(msg.status, { isLiveTelemetry: true });
        }
        if (Array.isArray(msg.entities)) {
          state.live.entities = msg.entities;
        }
        const pt = msg.pos_point;
        if (pt && validPos(pt.pos)) {
          const last = state.live.trajectory[state.live.trajectory.length - 1];
          const p = toPos(pt.pos);
          if (
            !last ||
            Math.hypot(p[0] - last.pos[0], p[1] - last.pos[1], p[2] - last.pos[2]) >= 0.25
          ) {
            state.live.trajectory.push({
              elapsed: Number(pt.elapsed ?? state.live.runStream.meta.duration_s ?? 0),
              pos: p,
              yaw: Number(pt.yaw ?? msg.status?.yaw ?? 0),
              dimension: normDim(pt.dimension ?? msg.status?.dimension),
              health: pt.health ?? msg.status?.health,
              food: pt.food ?? msg.status?.food,
            });
            if (state.live.trajectory.length > 4000) {
              state.live.trajectory.splice(0, state.live.trajectory.length - 4000);
            }
          }
        }
        if (state.mode === "live") {
          renderHeaderStatus();
          renderLeftOverlaysAnd3D();
          renderTopHud();
          renderTrajectoryCanvases();
        }
        break;
      }

      case "mc_events": {
        if (Array.isArray(msg.events)) {
          for (const ev of msg.events) {
            appendInlineMcEvent(state.live.runStream, ev);
            if (ev && ev.type === "hurt" && !state.live.status?.in_water && !state.live.status?.submerged) {
              const src = String(ev.source || "").toLowerCase();
              if (
                src.includes("freeze") ||
                src.includes("powder_snow") ||
                src.includes("berry") ||
                src.includes("sweet_berry")
              ) {
                state.live.envHurtUntilMs = Date.now() + 10000;
              }
            }
          }
        }
        if (Array.isArray(msg.markers)) {
          for (const m of msg.markers) {
            if (m && validPos(m.pos)) {
              state.live.markers.push({
                elapsed: Number(m.elapsed ?? state.live.runStream.meta.duration_s ?? 0),
                kind: String(m.kind || "hurt"),
                label: String(m.label || ""),
                pos: toPos(m.pos),
                dimension: normDim(m.dimension || state.live.status?.dimension),
              });
            }
          }
        }
        if (state.mode === "live") {
          renderAllViews();
        }
        break;
      }

      case "blocks_patch": {
        if (Array.isArray(msg.removed)) {
          for (const r of msg.removed) {
            const p = Array.isArray(r) ? r : r?.pos;
            if (validPos(p)) state.live.blocksMap.delete(blockKey(p));
          }
        }
        if (Array.isArray(msg.blocks)) {
          for (const b of msg.blocks) {
            if (b && validPos(b.pos) && b.name) {
              state.live.blocksMap.set(blockKey(b.pos), { pos: toPos(b.pos), name: String(b.name) });
              if (STATION_BLOCKS.has(b.name)) {
                const exists = state.live.markers.some(
                  (m) =>
                    m.kind === "station" &&
                    Math.abs(m.pos[0] - b.pos[0]) < 1 &&
                    Math.abs(m.pos[2] - b.pos[2]) < 1
                );
                if (!exists) {
                  state.live.markers.push({
                    elapsed: state.live.runStream.meta.duration_s || 0,
                    kind: "station",
                    label: b.name,
                    pos: toPos(b.pos),
                    dimension: normDim(state.live.status?.dimension),
                  });
                }
              }
            }
          }
        }
        capLiveBlocksMap();
        if (state.mode === "live") {
          renderLeftOverlaysAnd3D();
          renderTrajectoryCanvases();
        }
        break;
      }

      case "inventory_advancements": {
        if (msg.inventory && typeof msg.inventory === "object") {
          state.live.inventory = msg.inventory;
        }
        if (msg.advancements && typeof msg.advancements === "object") {
          state.live.advancements = mergeAdvancementData(state.live.advancements, msg.advancements);
        }
        if (state.mode === "live") {
          renderTopHud();
          renderOpenDrawer();
        }
        break;
      }

      case "capture_status": {
        if (msg.capture_stream && typeof msg.capture_stream === "object") {
          const prevCam = state.info.capture_stream?.camera_mode;
          state.info.capture_stream = {
            active: false,
            stream_url: null,
            snapshot_url: null,
            width: 0,
            height: 0,
            fps: 0,
            source: null,
            seq: 0,
            camera_mode: "third_person",
            director_available: false,
            ...msg.capture_stream,
          };
          if (state.info.capture_stream.camera_mode !== prevCam) {
            state.captureCameraError = "";
          }
          renderCaptureState();
        }
        break;
      }
    }
  }

  // -------------------- Left Pane: 3D Viewer, Prismarine & Overlays --------------------
  function isEngineViewMode(viewMode) {
    return viewMode !== "prismarine" && viewMode !== "local_capture";
  }

  function ensureViewer3D() {
    if (state.viewer3d) return state.viewer3d;
    const container = dom["viewer3d-container"];
    if (!container) return null;
    if (window.McViewer3D && typeof window.McViewer3D.create === "function") {
      try {
        const shouldPause = state.mode === "live" && !isEngineViewMode(state.leftView);
        state.viewer3d = window.McViewer3D.create(container, { paused: shouldPause });
        if (
          isEngineViewMode(state.leftView) &&
          typeof state.viewer3d.setMode === "function"
        ) {
          state.viewer3d.setMode(state.leftView);
        }
        return state.viewer3d;
      } catch (err) {
        console.warn("[MC-Live] McViewer3D init fallback:", err);
      }
    }
    return null;
  }

  function syncStageLiveBarClass() {
    const stage = dom["viewport-stage"];
    if (!stage) return;
    const captureBarShown =
      state.leftView === "local_capture" &&
      dom["capture-live-bar"] &&
      !dom["capture-live-bar"].classList.contains("hidden");
    const prismarineBarShown =
      state.leftView === "prismarine" &&
      dom["prismarine-live-bar"] &&
      !dom["prismarine-live-bar"].classList.contains("hidden");
    stage.classList.toggle("has-live-bar", Boolean(captureBarShown || prismarineBarShown));
  }

  function setLeftView(viewMode) {
    state.leftView = viewMode;
    document.querySelectorAll(".view-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === viewMode);
    });

    const isPrismarine = viewMode === "prismarine";
    const isCapture = viewMode === "local_capture";
    const isEngine = !isPrismarine && !isCapture;

    dom["viewer3d-container"].classList.toggle("hidden", !isEngine);
    dom["prismarine-panel"].classList.toggle("hidden", !isPrismarine);
    dom["capture-panel"].classList.toggle("hidden", !isCapture);

    const hints = {
      local_capture: "MC 客户端实景直播 · 局域网自动同步",
      prismarine: "Prismarine 备选 3D 旁观预览流",
      third_person: "AI 记忆体素 · 拖拽旋转 · 滚轮缩放",
      first_person: "AI 记忆体素 · 主视角准星与手持工具朝向",
      free_orbit: "AI 记忆体素 · 自由环绕鸟瞰周边地形",
      radar_2d: "2D 顶视实体与方块战术扫描",
    };
    dom["viewport-hint"].textContent = hints[viewMode] || "";

    // Always sync capture & prismarine states so background streams/iframes tear down immediately when hidden
    renderCaptureState();
    renderPrismarineState();
    if (!isEngine && state.viewer3d && typeof state.viewer3d.setPaused === "function") {
      try {
        state.viewer3d.setPaused(true);
      } catch (_) {}
    } else if (isEngine) {
      const v = ensureViewer3D();
      if (v) {
        try {
          if (typeof v.setMode === "function") v.setMode(viewMode);
          if (typeof v.setPaused === "function") v.setPaused(false);
          else if (typeof v.resize === "function") v.resize();
        } catch (_) {}
      }
      renderLeftOverlaysAnd3D();
    }
    syncStageLiveBarClass();
  }

  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"]);

  function resolveViewerEmbedUrl(rawUrl) {
    if (!rawUrl || typeof rawUrl !== "string") return "";
    const trimmed = rawUrl.trim();
    if (!trimmed) return "";
    try {
      const parsed = new URL(trimmed, window.location.origin);
      const targetHost = (parsed.hostname || "").toLowerCase();
      const pageHost = (window.location.hostname || "").toLowerCase();
      if (LOOPBACK_HOSTS.has(targetHost) && pageHost && !LOOPBACK_HOSTS.has(pageHost)) {
        parsed.hostname = window.location.hostname;
      }
      return parsed.toString();
    } catch (_) {
      return trimmed;
    }
  }

  function renderPrismarineControls() {
    const mode = state.prismarineMode === "first_person" ? "first_person" : "third_person";
    const modeLabel = mode === "first_person" ? "第一视角" : "第三视角";
    if (dom["btn-prismarine-first"]) {
      dom["btn-prismarine-first"].classList.toggle("active", mode === "first_person");
      dom["btn-prismarine-first"].disabled = state.prismarinePending;
    }
    if (dom["btn-prismarine-third"]) {
      dom["btn-prismarine-third"].classList.toggle("active", mode === "third_person");
      dom["btn-prismarine-third"].disabled = state.prismarinePending;
    }
    if (dom["prismarine-live-status"]) {
      if (state.prismarinePending) {
        dom["prismarine-live-status"].textContent = "⏳ 正在切换 Prismarine 视角...";
      } else if (state.prismarineFeedback) {
        dom["prismarine-live-status"].textContent = `⚠️ ${state.prismarineFeedback} · ${modeLabel}`;
      } else {
        dom["prismarine-live-status"].textContent = `🟢 Prismarine 3D · ${modeLabel}`;
      }
    }
  }

  async function probePrismarineHealth(resolvedUrl) {
    if (!resolvedUrl) return;
    try {
      const u = new URL(resolvedUrl);
      u.pathname = `${u.pathname.replace(/\/+$/, "")}/health`;
      u.search = "";
      const res = await fetch(u.toString(), { method: "GET" });
      if (!res.ok) return;
      const data = await res.json().catch(() => null);
      if (data && (data.follow_mode === "first_person" || data.follow_mode === "third_person")) {
        state.prismarineMode = data.follow_mode;
        renderPrismarineControls();
      }
    } catch (_) {
      // Optional health probe error ignored cleanly
    }
  }

  async function setPrismarineMode(mode) {
    if (mode !== "first_person" && mode !== "third_person") return;
    const pv = state.info.prismarine_viewer || {};
    const resolvedUrl = resolveViewerEmbedUrl(pv.embed_url);
    if (!pv.available || !resolvedUrl || state.prismarinePending) return;

    state.prismarinePending = true;
    state.prismarineFeedback = "";
    if (state.prismarineFeedbackTimer) {
      clearTimeout(state.prismarineFeedbackTimer);
      state.prismarineFeedbackTimer = null;
    }
    renderPrismarineControls();

    try {
      const u = new URL(resolvedUrl);
      u.pathname = `${u.pathname.replace(/\/+$/, "")}/api/mode`;
      u.search = `?mode=${encodeURIComponent(mode)}`;
      const res = await fetch(u.toString(), { method: "GET" });
      const data = await res.json().catch(() => null);
      if (!res.ok || (data && data.ok === false)) {
        throw new Error((data && data.error) || `HTTP ${res.status}`);
      }
      const nextMode =
        data && (data.follow_mode === "first_person" || data.follow_mode === "third_person")
          ? data.follow_mode
          : data && (data.mode === "first_person" || data.mode === "third_person")
          ? data.mode
          : mode;
      state.prismarineMode = nextMode;
    } catch (err) {
      state.prismarineFeedback = `视角切换失败 (${err?.message || "请求失败"})`;
      state.prismarineFeedbackTimer = setTimeout(() => {
        state.prismarineFeedback = "";
        state.prismarineFeedbackTimer = null;
        renderPrismarineControls();
      }, 5000);
    } finally {
      state.prismarinePending = false;
      renderPrismarineControls();
    }
  }

  function reloadPrismarineIframe() {
    const pv = state.info.prismarine_viewer || {};
    const resolvedUrl = resolveViewerEmbedUrl(pv.embed_url);
    if (!pv.available || !resolvedUrl || !dom["prismarine-iframe"]) return;
    try {
      const u = new URL(resolvedUrl);
      u.searchParams.set("_t", String(Date.now()));
      dom["prismarine-iframe"].setAttribute("src", u.toString());
    } catch (_) {
      dom["prismarine-iframe"].setAttribute("src", resolvedUrl);
    }
  }

  function renderPrismarineState() {
    const pv = state.info.prismarine_viewer || {};
    const resolvedUrl = resolveViewerEmbedUrl(pv.embed_url);
    const avail = Boolean(pv.available && resolvedUrl);
    const isActivePrismarine = avail && state.mode === "live" && state.leftView === "prismarine";
    dom["prismarine-dot"].className = `tab-dot ${avail ? "online" : "offline"}`;
    dom["prismarine-panel"].classList.toggle("has-bar", isActivePrismarine);
    if (dom["prismarine-live-bar"]) {
      dom["prismarine-live-bar"].classList.toggle("hidden", !isActivePrismarine);
    }

    if (isActivePrismarine) {
      dom["prismarine-unavailable"].classList.add("hidden");
      dom["prismarine-iframe"].classList.remove("hidden");
      if (dom["prismarine-iframe"].dataset.resolvedSrc !== resolvedUrl) {
        dom["prismarine-iframe"].dataset.resolvedSrc = resolvedUrl;
        dom["prismarine-iframe"].setAttribute("src", resolvedUrl);
      }
      if (state.prismarineHealthProbedUrl !== resolvedUrl) {
        state.prismarineHealthProbedUrl = resolvedUrl;
        probePrismarineHealth(resolvedUrl);
      }
      renderPrismarineControls();
    } else {
      dom["prismarine-iframe"].classList.add("hidden");
      if (dom["prismarine-iframe"].hasAttribute("src")) {
        delete dom["prismarine-iframe"].dataset.resolvedSrc;
        dom["prismarine-iframe"].removeAttribute("src");
      }
      dom["prismarine-unavailable"].classList.toggle("hidden", avail);
      dom["prismarine-note"].textContent =
        pv.note || "当前未检测到本地 Prismarine 旁观服务 (端口 8766)，左侧可使用实景游戏画面或 3D 体素引擎。";
    }
    syncStageLiveBarClass();
  }

  const DEFAULT_CAPTURE_NOTE_HTML =
    "服务端实时画面流（<code>beast_director</code> / 上游代理流）启动后将在此自动播放，局域网纯 HTTP 打开本页面即可免操作观看。";

  function stopCaptureStandbyPoll() {
    if (state.captureStandbyPollTimer) {
      clearInterval(state.captureStandbyPollTimer);
      state.captureStandbyPollTimer = null;
    }
  }

  function ensureCaptureStandbyPoll() {
    const shouldPoll =
      state.mode === "live" &&
      state.leftView === "local_capture" &&
      !state.captureStream &&
      !state.info.capture_stream?.active;
    if (!shouldPoll) {
      stopCaptureStandbyPoll();
      return;
    }
    if (!state.captureStandbyPollTimer) {
      state.captureStandbyPollTimer = setInterval(() => {
        if (
          state.mode === "live" &&
          state.leftView === "local_capture" &&
          !state.captureStream &&
          !state.info.capture_stream?.active
        ) {
          fetchInfoAndRuns();
        } else {
          stopCaptureStandbyPoll();
        }
      }, 3000);
    }
  }

  async function setCaptureCameraMode(mode) {
    if (mode !== "first_person" && mode !== "third_person") return;
    if (state.captureCameraPending) return;
    state.captureCameraPending = true;
    state.captureCameraError = "";
    if (state.captureCameraErrorTimer) {
      clearTimeout(state.captureCameraErrorTimer);
      state.captureCameraErrorTimer = null;
    }
    renderCaptureState();

    try {
      const res = await fetch("/api/capture/camera-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok || (data && data.ok === false)) {
        const reason = (data && data.error) || `HTTP ${res.status}`;
        throw new Error(reason);
      }
      const nextMode =
        data && (data.mode === "first_person" || data.mode === "third_person")
          ? data.mode
          : mode;
      state.info.capture_stream = {
        ...(state.info.capture_stream || {}),
        camera_mode: nextMode,
        ...(data && data.director_available !== undefined
          ? { director_available: Boolean(data.director_available) }
          : {}),
      };
    } catch (err) {
      state.captureCameraError = `视角切换失败 (${err?.message || "请求失败"})`;
      state.captureCameraErrorTimer = setTimeout(() => {
        state.captureCameraError = "";
        state.captureCameraErrorTimer = null;
        renderCaptureState();
      }, 5000);
    } finally {
      state.captureCameraPending = false;
      renderCaptureState();
    }
  }

  function renderCaptureState() {
    const hasLocal = Boolean(state.captureStream);
    const cs = state.info.capture_stream || {};
    const serverActive = Boolean(cs.active);
    const isLiveCaptureTab = state.mode === "live" && state.leftView === "local_capture";
    const hasRemote = !hasLocal && serverActive && isLiveCaptureTab;
    const activeOnScreen = hasLocal || hasRemote;

    dom["capture-dot"].className = `tab-dot ${hasLocal || serverActive ? "online" : "offline"}`;
    dom["capture-panel"].classList.toggle("has-bar", activeOnScreen);
    dom["local-capture-video"].classList.toggle("hidden", !hasLocal);
    dom["remote-capture-img"].classList.toggle("hidden", !hasRemote);
    dom["capture-placeholder"].classList.toggle("hidden", activeOnScreen);
    dom["capture-live-bar"].classList.toggle("hidden", !activeOnScreen);

    const camMode = cs.camera_mode === "first_person" ? "first_person" : "third_person";
    const camModeLabel = camMode === "first_person" ? "第一视角" : "第三视角";
    const showCamToggle = hasRemote || Boolean(cs.director_available);
    if (dom["capture-camera-toggle"]) {
      dom["capture-camera-toggle"].classList.toggle("hidden", !showCamToggle);
    }
    if (dom["btn-capture-first"]) {
      dom["btn-capture-first"].classList.toggle("active", camMode === "first_person");
      dom["btn-capture-first"].disabled = state.captureCameraPending;
    }
    if (dom["btn-capture-third"]) {
      dom["btn-capture-third"].classList.toggle("active", camMode === "third_person");
      dom["btn-capture-third"].disabled = state.captureCameraPending;
    }

    if (hasRemote) {
      const img = dom["remote-capture-img"];
      const streamUrl = cs.stream_url || "/api/capture/stream.mjpg";
      if (img.dataset.streaming !== "1" || img.dataset.streamBase !== streamUrl) {
        const sep = streamUrl.includes("?") ? "&" : "?";
        img.src = `${streamUrl}${sep}t=${Date.now()}`;
        img.dataset.streaming = "1";
        img.dataset.streamBase = streamUrl;
      }
      dom["btn-switch-capture"].classList.add("hidden");
      dom["btn-stop-capture"].classList.add("hidden");

      const metaParts = [];
      if (cs.width > 0 && cs.height > 0) metaParts.push(`${cs.width}×${cs.height}`);
      if (Number(cs.fps) > 0) metaParts.push(`${Number(cs.fps)}fps`);
      metaParts.push(camModeLabel);
      const metaText = metaParts.join(" · ");

      if (state.captureCameraPending) {
        dom["capture-live-status"].textContent = `⏳ 正在切换视角... (${metaText})`;
      } else if (state.captureCameraError) {
        dom["capture-live-status"].textContent = `⚠️ ${state.captureCameraError} · ${metaText}`;
      } else {
        dom["capture-live-status"].textContent = `🟢 实景直播 · ${metaText}`;
      }
    } else {
      const img = dom["remote-capture-img"];
      if (img.dataset.streaming === "1" || img.hasAttribute("src")) {
        delete img.dataset.streaming;
        delete img.dataset.streamBase;
        img.removeAttribute("src");
      }
    }

    if (hasLocal) {
      dom["btn-switch-capture"].classList.remove("hidden");
      dom["btn-stop-capture"].classList.remove("hidden");
      dom["capture-live-status"].textContent = state.captureRelayEnabled
        ? "🔴 本机 60fps 实景采集中 · 局域网同步直播已开启"
        : "🟢 本机 60fps 实景采集中 (仅本机预览)";
    }

    if (!activeOnScreen) {
      const canCapture = Boolean(
        navigator.mediaDevices && typeof navigator.mediaDevices.getDisplayMedia === "function"
      );
      if (dom["manual-capture-fallback"]) {
        dom["manual-capture-fallback"].classList.toggle("hidden", !canCapture || state.mode !== "live");
      }

      if (state.mode !== "live") {
        dom["capture-note"].textContent =
          "当前处于「历史回放」模式，实景直播已暂停。请切换上方「🔴 实时监看」观看实景游戏画面，或使用「🧊 AI 体素」回放历史过程。";
        if (dom["capture-wait-badge"]) {
          dom["capture-wait-badge"].textContent = "⏸ 历史回放中 · 实景自动播放已暂停";
        }
      } else {
        if (state.captureErrorMsg) {
          dom["capture-note"].textContent = state.captureErrorMsg;
        } else if (dom["capture-note"].innerHTML !== DEFAULT_CAPTURE_NOTE_HTML) {
          dom["capture-note"].innerHTML = DEFAULT_CAPTURE_NOTE_HTML;
        }
        if (dom["capture-wait-badge"]) {
          dom["capture-wait-badge"].textContent =
            "⏳ 正在等待服务端实景推流上线（就绪后自动播放，无需手动开启；也可切换「🟢 Prismarine 备选 3D」或「🧊 AI 体素」）";
        }
        if (canCapture && dom["btn-start-capture"]) {
          dom["btn-start-capture"].disabled = false;
          dom["btn-start-capture"].textContent = "🎥 选择本机 Minecraft 窗口采集";
        }
      }
    }

    ensureCaptureStandbyPoll();
    syncStageLiveBarClass();
  }

  function stopCaptureRelayPublisher() {
    if (state.captureRelayTimer) {
      clearInterval(state.captureRelayTimer);
      state.captureRelayTimer = null;
    }
    state.captureEncoding = false;
    state.captureMetaKey = "";
    const ws = state.captureRelayWs;
    state.captureRelayWs = null;
    if (ws) {
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "stop" }));
        }
        ws.close();
      } catch (_) {}
    }
  }

  function startCaptureRelayPublisher() {
    if (!state.captureStream || !state.captureRelayEnabled) {
      stopCaptureRelayPublisher();
      return;
    }
    stopCaptureRelayPublisher();

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(`${proto}//${window.location.host}/ws-capture`);
    } catch (_) {
      return;
    }
    state.captureRelayWs = ws;
    state.captureEncoding = false;
    state.captureMetaKey = "";

    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");

    state.captureRelayTimer = setInterval(() => {
      if (!state.captureStream || !state.captureRelayEnabled || state.captureRelayWs !== ws) {
        stopCaptureRelayPublisher();
        return;
      }
      const video = dom["local-capture-video"];
      if (
        ws.readyState !== WebSocket.OPEN ||
        state.captureEncoding ||
        ws.bufferedAmount >= 262144 ||
        !video ||
        video.readyState < 2 ||
        video.videoWidth <= 0 ||
        !ctx
      ) {
        return;
      }

      const vw = video.videoWidth;
      const vh = video.videoHeight;
      const ratio = Math.min(1, 1280 / vw, 720 / vh);
      const cw = Math.max(2, Math.floor((vw * ratio) / 2) * 2);
      const ch = Math.max(2, Math.floor((vh * ratio) / 2) * 2);
      if (canvas.width !== cw || canvas.height !== ch) {
        canvas.width = cw;
        canvas.height = ch;
      }

      const metaKey = `${cw}x${ch}`;
      if (state.captureMetaKey !== metaKey) {
        state.captureMetaKey = metaKey;
        try {
          ws.send(JSON.stringify({ type: "meta", width: cw, height: ch, fps: 15, source: "browser" }));
        } catch (_) {}
      }

      state.captureEncoding = true;
      try {
        ctx.drawImage(video, 0, 0, cw, ch);
        canvas.toBlob(
          (blob) => {
            state.captureEncoding = false;
            if (blob && state.captureRelayWs === ws && ws.readyState === WebSocket.OPEN) {
              try {
                ws.send(blob);
              } catch (_) {}
            }
          },
          "image/jpeg",
          0.72
        );
      } catch (_) {
        state.captureEncoding = false;
      }
    }, 66);

    ws.onclose = () => {
      if (state.captureRelayWs === ws) {
        stopCaptureRelayPublisher();
      }
    };
  }

  async function startLocalCapture() {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getDisplayMedia !== "function") {
      renderCaptureState();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 60, max: 60 } },
        audio: false,
      });
      stopLocalCapture();
      state.captureErrorMsg = "";
      state.captureStream = stream;
      dom["local-capture-video"].srcObject = stream;
      dom["local-capture-video"].play().catch(() => {});
      const [track] = stream.getVideoTracks();
      if (track) {
        track.addEventListener("ended", () => stopLocalCapture());
      }
      startCaptureRelayPublisher();
      renderCaptureState();
    } catch (err) {
      state.captureErrorMsg = `未开启窗口采集: ${err.message || "已取消选择"}`;
      renderCaptureState();
    }
  }

  function stopLocalCapture() {
    stopCaptureRelayPublisher();
    if (state.captureStream) {
      for (const t of state.captureStream.getTracks()) t.stop();
      state.captureStream = null;
    }
    dom["local-capture-video"].srcObject = null;
    renderCaptureState();
  }

  function filterVisibleMarkers(markers) {
    if (!Array.isArray(markers)) return [];
    if (state.alertsVisible) return markers;
    return markers.filter((m) => m && m.kind !== "hurt" && m.kind !== "death");
  }

  function renderLeftOverlaysAnd3D() {
    const view = getCurrentSceneView();
    const st = view.status || {};

    // Top-left overlays
    dom["ov-pos"].textContent = fmtPos(st.pos, 1);
    const dimKey = normDim(st.dimension);
    dom["ov-dim"].textContent = DIM_LABELS[dimKey] || dimKey;
    dom["ov-dim"].className = `dim-pill ${
      dimKey === "the_nether" ? "nether" : dimKey === "the_end" ? "end" : "overworld"
    }`;

    dom["ov-biome"].textContent = `🌲 ${st.biome || "--"}`;
    dom["ov-time"].textContent = formatMcTime(st.time_of_day, st.is_day, st.raining, st.thundering);
    dom["ov-compass"].textContent = formatCompass(st.yaw);
    const spd = Number.isFinite(Number(st.speed)) ? Number(st.speed) : 0;
    dom["ov-speed"].textContent = `⚡ ${spd.toFixed(2)} m/s`;

    // Top-right Danger Alerts
    const alerts = [];
    const skipOfflineAlerts = state.mode === "live" && !state.info.online;
    if (!skipOfflineAlerts) {
      if (st.alive === false || (st.health !== undefined && Number(st.health) <= 0)) {
        alerts.push({ cls: "", text: "💀 角色已阵亡 (Health = 0)" });
      }
      if (st.in_lava) {
        alerts.push({ cls: "", text: "🔥 岩浆灼烧危急 (IN LAVA)!" });
      }
      if (st.on_fire && !st.in_lava) {
        alerts.push({ cls: "", text: "🔥 身体着火燃烧 (ON FIRE)!" });
      }
      const inWater = Boolean(st.in_water || st.submerged);
      if (inWater && st.oxygen !== undefined && Number(st.oxygen) < 15) {
        alerts.push({
          cls: "freeze",
          text: `🫧 水下缺氧警报 (O₂ ${Number(st.oxygen).toFixed(0)}/20)`,
        });
      }

      // Check nearest hostile <= 16m
      let hostile = st.nearest_hostile;
      if (!hostile && Array.isArray(view.entities)) {
        hostile = view.entities
          .filter((e) => e && e.type === "hostile" && Number.isFinite(Number(e.dist)))
          .sort((a, b) => Number(a.dist) - Number(b.dist))[0];
      }
      if (hostile && Number.isFinite(Number(hostile.dist)) && Number(hostile.dist) <= 16) {
        alerts.push({
          cls: Number(hostile.dist) <= 8 ? "" : "warn",
          text: `⚔️ 敌对逼近: ${hostile.name || "mob"} (${Number(hostile.dist).toFixed(1)}m)`,
        });
      }

      // Freeze / powder snow / berry environmental hurt
      const nearPowderSnow =
        validPos(st.pos) &&
        view.blocks.some(
          (b) =>
            b.name === "powder_snow" &&
            Math.hypot(b.pos[0] - st.pos[0], b.pos[1] - st.pos[1], b.pos[2] - st.pos[2]) <= 3.5
        );
      if (view.envFreezeAlert || nearPowderSnow) {
        alerts.push({
          cls: "freeze",
          text: "⚠️ 疑似细雪冻伤/环境扣血",
        });
      } else if (
        st.health !== undefined &&
        Number(st.health) > 0 &&
        Number(st.health) <= 8 &&
        !inWater &&
        !st.in_lava
      ) {
        alerts.push({
          cls: "warn",
          text: `💔 低血量危急 (${Number(st.health).toFixed(1)} / 20)`,
        });
      }
    }

    dom["overlay-top-right"].innerHTML = alerts
      .map(
        (a) =>
          `<div class="danger-badge ${a.cls}" title="点击隐藏受伤/危急弹窗">${escapeHtml(a.text)}<span class="alert-close">✕</span></div>`
      )
      .join("");

    // Bottom-left active underlying lock badge + macro skill indicator
    let activeExTool = null;
    for (const tool of view.streamModel.toolsById.values()) {
      if (tool.status === "running" && tool.concurrency === "exclusive") {
        activeExTool = tool;
      }
    }
    const apiBusy = st.busy ? String(st.busy) : null;
    if (apiBusy || activeExTool) {
      dom["ov-busy-badge"].className = "busy-pill active";
      if (apiBusy && activeExTool && apiBusy !== activeExTool.name) {
        dom["ov-busy-text"].textContent = `busy: ${apiBusy} · 技能 ${activeExTool.name}`;
      } else {
        dom["ov-busy-text"].textContent = `busy: ${apiBusy || activeExTool.name}`;
      }
    } else {
      dom["ov-busy-badge"].className = "busy-pill idle";
      dom["ov-busy-text"].textContent = "busy: idle (空闲)";
    }

    // Posture chips
    const postures = [];
    if (st.sneaking) postures.push("潜行 Sneak");
    if (st.blocking) postures.push("举盾 Shield");
    if (st.is_sleeping) postures.push("睡眠中");
    if (st.in_vehicle) postures.push(`骑乘 ${st.in_vehicle.name || "载具"}`);
    dom["ov-posture-tags"].innerHTML = postures
      .map((p) => `<span class="posture-chip">${escapeHtml(p)}</span>`)
      .join("");

    dom["ov-scene-stats"].textContent = `方块 ${view.blocks.length} · 实体 ${view.entities.length}`;

    // Push state to window.McViewer3D
    if (state.leftView !== "prismarine" && state.leftView !== "local_capture") {
      const v = ensureViewer3D();
      if (v && typeof v.updateState === "function") {
        try {
          v.updateState({
            status: st,
            entities: view.entities,
            blocks: view.blocks,
            markers: filterVisibleMarkers(view.markers),
            showHurtMarkers: Boolean(state.alertsVisible),
            trajectory: view.trajectory,
          });
        } catch (err) {
          console.warn("[MC-Live] viewer.updateState error:", err);
        }
      }
    }
  }

  // -------------------- Right Pane Section A: Compact HUD & Drawers --------------------
  function findItemDurability(itemObj, inv) {
    if (!itemObj) return null;
    if (Array.isArray(itemObj.durability) && itemObj.durability.length >= 2) {
      const [cur, max] = itemObj.durability.map(Number);
      if (max > 0) return { cur, max, pct: Math.max(0, Math.min(100, (cur / max) * 100)) };
    }
    if (inv && Array.isArray(inv.items) && itemObj.slot !== undefined) {
      const match = inv.items.find((it) => it && it.slot === itemObj.slot);
      if (match && Array.isArray(match.durability) && match.durability.length >= 2) {
        const [cur, max] = match.durability.map(Number);
        if (max > 0) return { cur, max, pct: Math.max(0, Math.min(100, (cur / max) * 100)) };
      }
    }
    return null;
  }

  function renderEquipPill(itemObj, inv, nameEl, durWrapEl, durFillEl, emptyLabel) {
    if (!itemObj || (typeof itemObj === "object" && !itemObj.name)) {
      nameEl.textContent = emptyLabel;
      durWrapEl.classList.add("hidden");
      return;
    }
    const name = typeof itemObj === "string" ? itemObj : itemObj.name;
    const cnt = typeof itemObj === "object" && itemObj.count > 1 ? ` ×${itemObj.count}` : "";
    nameEl.textContent = `${name}${cnt}`;

    const dur = typeof itemObj === "object" ? findItemDurability(itemObj, inv) : null;
    if (dur) {
      durWrapEl.classList.remove("hidden");
      durFillEl.style.width = `${dur.pct}%`;
      durFillEl.style.background =
        dur.pct > 50 ? "#22c55e" : dur.pct > 20 ? "#f59e0b" : "#ef4444";
    } else {
      durWrapEl.classList.add("hidden");
    }
  }

  function renderTopHud() {
    const view = getCurrentSceneView();
    const st = view.status || {};
    const inv = view.inventory || {};

    // HP
    const hp = Number.isFinite(Number(st.health)) ? Math.max(0, Math.min(20, Number(st.health))) : null;
    dom["hud-hp-val"].textContent = hp !== null ? `${hp % 1 === 0 ? hp : hp.toFixed(1)} / 20` : "-- / 20";
    dom["hud-hp-bar"].style.width = `${hp !== null ? (hp / 20) * 100 : 0}%`;
    dom["hud-hp-bar"].classList.toggle("low", hp !== null && hp <= 8);

    // Food
    const food = Number.isFinite(Number(st.food)) ? Math.max(0, Math.min(20, Number(st.food))) : null;
    dom["hud-food-val"].textContent = food !== null ? `${Math.round(food)} / 20` : "-- / 20";
    dom["hud-food-bar"].style.width = `${food !== null ? (food / 20) * 100 : 0}%`;

    // Oxygen
    const oxyRaw = st.oxygen !== undefined ? Number(st.oxygen) : 20;
    const oxy = Number.isFinite(oxyRaw) ? Math.max(0, Math.min(20, oxyRaw)) : 20;
    dom["hud-oxy-val"].textContent = `${Math.round(oxy)} / 20`;
    dom["hud-oxy-bar"].style.width = `${(oxy / 20) * 100}%`;

    // Armor
    const armor = Number.isFinite(Number(st.armor_points))
      ? Math.max(0, Math.min(20, Number(st.armor_points)))
      : 0;
    dom["hud-armor-val"].textContent = `${Math.round(armor)} / 20`;
    dom["hud-armor-bar"].style.width = `${(armor / 20) * 100}%`;

    // Held & Offhand
    const held = st.held !== undefined ? st.held : inv.held;
    const offhand = st.offhand !== undefined ? st.offhand : inv.offhand;
    renderEquipPill(
      held,
      inv,
      dom["hud-held-name"],
      dom["hud-held-dur-wrap"],
      dom["hud-held-dur"],
      "空手"
    );
    renderEquipPill(
      offhand,
      inv,
      dom["hud-offhand-name"],
      dom["hud-offhand-dur-wrap"],
      dom["hud-offhand-dur"],
      "无"
    );

    // Tokens & KV Cache hit %
    const usage =
      view.streamModel.latestUsage?.promptTokens !== null
        ? view.streamModel.latestUsage
        : state.live.usage;
    if (usage && Number.isFinite(usage.promptTokens) && usage.promptTokens > 0) {
      const hitPct = Math.round(((usage.cachedTokens || 0) / usage.promptTokens) * 100);
      dom["hud-cache-badge"].innerHTML = `<span>🧠 ${fmtTokens(
        usage.promptTokens
      )}</span><span class="sep">·</span><span id="hud-kv-pct">KV ${hitPct}%</span>`;
    } else {
      dom["hud-cache-badge"].innerHTML = `<span>🧠 Tokens --</span><span class="sep">·</span><span id="hud-kv-pct">KV --%</span>`;
    }

    // Advancements count pill
    const advNorm = normalizeAdvancementsView(view.advancements);
    dom["hud-adv-count"].textContent = String(advNorm.completedCount);
  }

  function normalizeAdvancementsView(adv) {
    const src = adv && typeof adv === "object" ? adv : {};
    const total = Number(src.total_catalog || 125);
    const completedIds = Array.isArray(src.completed_ids) ? src.completed_ids : [];
    const completedDetails = Array.isArray(src.completed) ? src.completed : [];
    const advRows = Array.isArray(src.advancements) ? src.advancements : [];
    const completedRows = advRows.filter((r) => r && r.status === "completed");

    const idMap = new Map();
    for (const id of completedIds) {
      idMap.set(String(id), { id: String(id), title: String(id).replace(/^minecraft:/, "") });
    }
    for (const d of completedDetails) {
      if (!d || !d.id) continue;
      const id = String(d.id);
      idMap.set(id, {
        id,
        title: d.title_zh || d.title || id.replace(/^minecraft:/, ""),
        category: d.category,
      });
    }
    for (const r of completedRows) {
      const id = String(r.id || "");
      if (id && !idMap.has(id)) {
        idMap.set(id, {
          id,
          title: r.title_zh || r.title || id.replace(/^minecraft:/, ""),
          category: r.category,
        });
      }
    }

    const completedCount = Math.max(
      Number(src.completed_count || 0),
      Number(src.summary?.completed || 0),
      idMap.size
    );
    const frontier = Array.isArray(src.frontier)
      ? src.frontier
      : Array.isArray(src.frontier_top10)
      ? src.frontier_top10
      : [];

    return {
      total,
      completedCount,
      completedList: Array.from(idMap.values()),
      frontier,
    };
  }

  function toggleDrawer(name) {
    state.openDrawer = state.openDrawer === name ? null : name;
    dom["drawer-inventory"].classList.toggle("hidden", state.openDrawer !== "inventory");
    dom["drawer-advancements"].classList.toggle("hidden", state.openDrawer !== "advancements");
    dom["btn-drawer-inv"].classList.toggle("active", state.openDrawer === "inventory");
    dom["btn-drawer-adv"].classList.toggle("active", state.openDrawer === "advancements");
    renderOpenDrawer();
  }

  function renderOpenDrawer() {
    if (!state.openDrawer) return;
    const view = getCurrentSceneView();

    if (state.openDrawer === "inventory") {
      const inv = view.inventory || {};
      const st = view.status || {};
      const armor = inv.armor || {};
      const equipSlots = [
        { label: "头盔 Head", item: armor.head },
        { label: "胸甲 Chest", item: armor.chest },
        { label: "护腿 Legs", item: armor.legs },
        { label: "靴子 Feet", item: armor.feet },
        { label: "主手 Held", item: inv.held || st.held },
        { label: "副手 Offhand", item: inv.offhand || st.offhand },
      ];

      dom["inv-equipment-grid"].innerHTML = equipSlots
        .map((s) => renderSlotCardHtml(s.item, s.label, inv))
        .join("");

      let itemsList = [];
      if (Array.isArray(inv.items)) {
        itemsList = [...inv.items].sort((a, b) => Number(a.slot ?? 99) - Number(b.slot ?? 99));
      } else if (inv && typeof inv === "object") {
        // Handle summary flat dict { oak_log: 3, ... }
        for (const [k, v] of Object.entries(inv)) {
          if (["items", "armor", "held", "offhand", "empty_slots"].includes(k)) continue;
          if (typeof v === "number" && v > 0) {
            itemsList.push({ name: k, count: v });
          }
        }
      }

      const emptySlots = inv.empty_slots !== undefined ? inv.empty_slots : Math.max(0, 36 - itemsList.length);
      dom["inv-summary-meta"].textContent = `物品堆 ${itemsList.length} · 空余槽位 ${emptySlots}`;

      if (!itemsList.length) {
        dom["inv-slots-grid"].innerHTML = `<div class="wf-empty">背包当前为空或尚未采集快照</div>`;
      } else {
        dom["inv-slots-grid"].innerHTML = itemsList
          .map((it) => renderSlotCardHtml(it, it.slot !== undefined ? `槽位 #${it.slot}` : "背包", inv))
          .join("");
      }
    } else if (state.openDrawer === "advancements") {
      const advNorm = normalizeAdvancementsView(view.advancements);
      const pct = Math.min(100, Math.round((advNorm.completedCount / advNorm.total) * 100));
      dom["adv-summary-meta"].textContent = `${advNorm.completedCount} / ${advNorm.total} (${pct}%)`;
      dom["adv-total-bar"].style.width = `${pct}%`;

      const catCounts = { story: 0, husbandry: 0, adventure: 0, nether: 0, end: 0 };
      for (const c of advNorm.completedList) {
        const clean = c.id.replace(/^minecraft:/, "");
        const cat = c.category || clean.split("/")[0];
        if (catCounts[cat] !== undefined) catCounts[cat] += 1;
      }
      dom["adv-cat-pills"].innerHTML = [
        `📖 故事 ${catCounts.story}/16`,
        `🌾 农牧 ${catCounts.husbandry}/30`,
        `🗡️ 冒险 ${catCounts.adventure}/47`,
        `🔥 下界 ${catCounts.nether}/23`,
        `🌌 末地 ${catCounts.end}/9`,
      ]
        .map((txt) => `<span class="adv-cat-pill mono-num">${txt}</span>`)
        .join("");

      if (!advNorm.frontier.length) {
        dom["adv-frontier-list"].innerHTML = `<div class="wf-empty">等待 mc_advancement_frontier 或服务端进度同步...</div>`;
      } else {
        dom["adv-frontier-list"].innerHTML = advNorm.frontier
          .map((f) => {
            const diff = f.difficulty || "medium";
            const prog =
              f.progress && f.progress.criteria_total > 1
                ? ` · 进度 ${f.progress.criteria_done || 0}/${f.progress.criteria_total}`
                : "";
            return `<div class="frontier-card">
              <div class="frontier-head">
                <div>
                  <span class="frontier-title">${escapeHtml(f.title_zh || f.id)}</span>
                  <span class="frontier-id">${escapeHtml(String(f.id || "").replace(/^minecraft:/, ""))}${escapeHtml(prog)}</span>
                </div>
                <span class="diff-badge ${escapeHtml(diff)}">${escapeHtml(diff)}</span>
              </div>
              ${f.description_zh ? `<div class="frontier-desc">${escapeHtml(f.description_zh)}</div>` : ""}
              ${f.how_to ? `<div class="frontier-howto">💡 攻略: ${escapeHtml(f.how_to)}</div>` : ""}
            </div>`;
          })
          .join("");
      }

      if (!advNorm.completedList.length) {
        dom["adv-completed-list"].innerHTML = `<div class="wf-empty">暂无已解锁进度记录</div>`;
      } else {
        dom["adv-completed-list"].innerHTML = advNorm.completedList
          .map((c) => `<span class="adv-done-chip">🏆 ${escapeHtml(c.title)}</span>`)
          .join("");
      }
    }
  }

  function renderSlotCardHtml(item, slotLabel, inv) {
    if (!item || (typeof item === "object" && !item.name)) {
      return `<div class="inv-slot-card empty">
        <div class="inv-slot-top"><span class="inv-item-name">空</span></div>
        <div class="inv-slot-sub"><span>${escapeHtml(slotLabel)}</span></div>
      </div>`;
    }
    const name = typeof item === "string" ? item : item.name;
    const count = typeof item === "object" && item.count !== undefined ? item.count : 1;
    const dur = typeof item === "object" ? findItemDurability(item, inv) : null;
    const ench =
      typeof item === "object" && Array.isArray(item.enchantments) && item.enchantments.length
        ? item.enchantments.map((e) => `${e.name} ${e.level || ""}`).join(", ")
        : "";

    return `<div class="inv-slot-card">
      <div class="inv-slot-top">
        <span class="inv-item-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="inv-item-cnt">×${escapeHtml(count)}</span>
      </div>
      <div class="inv-slot-sub">
        <span>${escapeHtml(slotLabel)}</span>
        ${dur ? `<span class="mono-num">耐久 ${dur.cur}/${dur.max}</span>` : ""}
      </div>
      ${
        dur
          ? `<div class="bar-track"><div class="bar-fill" style="width:${dur.pct}%;background:${
              dur.pct > 50 ? "#22c55e" : dur.pct > 20 ? "#f59e0b" : "#ef4444"
            }"></div></div>`
          : ""
      }
      ${ench ? `<div class="inv-slot-sub" style="color:#c4b5fd">✨ ${escapeHtml(ench)}</div>` : ""}
    </div>`;
  }

  // -------------------- Right Pane Section B: AI Thought & Tool Waterfall --------------------
  function summarizeToolArgs(name, parsedArgs, rawArgs) {
    if (!parsedArgs || typeof parsedArgs !== "object") {
      return rawArgs && rawArgs !== "{}" ? rawArgs : "";
    }
    const entries = Object.entries(parsedArgs).filter(([, v]) => v !== null && v !== undefined);
    if (!entries.length) return "";
    return entries
      .map(([k, v]) => {
        if (Array.isArray(v)) {
          return `${k}=[${v.slice(0, 4).join(",")}${v.length > 4 ? "…" : ""}]`;
        }
        if (typeof v === "object") return `${k}=${JSON.stringify(v).slice(0, 48)}`;
        return `${k}=${JSON.stringify(v)}`;
      })
      .join(", ");
  }

  function summarizeToolResult(tool) {
    if (tool.status === "running") return "正在执行动作并等待服务器回执...";
    if (tool.status === "skipped") return `已触发连招熔断跳过 (${tool.skipReason || "previous_action_failed"})`;
    const p = tool.parsedResult;
    if (!p || typeof p !== "object") {
      return (tool.rawResult || "").slice(0, 110);
    }

    if (tool.ok === false) {
      const err = p.error || p.partial_error || {};
      const msg = err.message || p.reason || p.message || "";
      const extra = p.protected_blacklisted ? ` (拉黑保护区方块 ×${p.protected_blacklisted})` : "";
      return `${err.code ? `[${err.code}] ` : ""}${msg || "ok: false"}${extra}`.trim();
    }

    const r = p.result && typeof p.result === "object" ? p.result : p;
    if (tool.name === "mc_status") {
      return `HP ${Number(r.health ?? 0).toFixed(1)} · Food ${r.food ?? "--"} · ${fmtPos(r.pos, 1)}`;
    }
    if (tool.name === "mc_inventory") {
      const n = Array.isArray(r.items) ? r.items.length : 0;
      return `${n} 格物品 · 空槽 ${r.empty_slots ?? "--"}`;
    }
    if (tool.name === "mc_find_blocks") {
      if (Array.isArray(r.blocks)) {
        const first = r.blocks[0];
        return `共 ${r.total ?? r.blocks.length} 处${
          first ? ` · 最近 ${first.name} ${fmtPos(first.pos, 0)} (${first.dist}m)` : ""
        }`;
      }
      if (r.blocks && typeof r.blocks === "object") {
        const keys = Object.keys(r.blocks);
        return `扫描 ${r.scanned ?? "--"} 块 · ${keys.slice(0, 4).join(", ")}`;
      }
    }
    if (tool.name === "mc_get_entities") {
      const ents = Array.isArray(r.entities) ? r.entities : [];
      return `${ents.length} 个实体${ents[0] ? ` · 最近 ${ents[0].name}(${ents[0].dist}m)` : ""}`;
    }
    if (tool.name === "mc_kb_lookup") {
      return `"${r.query || ""}" 命中 ${r.count ?? (r.results?.length || 0)} 条知识`;
    }
    if (tool.name === "mc_advancement_frontier") {
      return `已解锁 ${r.completed_count ?? 0}/${r.total_catalog ?? 125} · 前沿 ${r.frontier_count ?? 0} 项`;
    }

    // Macro & exclusive actions
    const parts = [];
    if (r.dug_count !== undefined) parts.push(`挖掘 ${r.dug_count} 块`);
    else if (Array.isArray(r.dug) && r.dug.length) parts.push(`挖掘 ${r.dug.length} 块`);
    if (r.gained && typeof r.gained === "object" && Object.keys(r.gained).length) {
      parts.push(
        `收获 ${Object.entries(r.gained)
          .map(([k, v]) => `${k}×${v}`)
          .join(", ")}`
      );
    }
    if (r.crafted) parts.push(`合成 ${JSON.stringify(r.crafted)}`);
    if (r.smelted || r.output) parts.push(`产物 ${JSON.stringify(r.smelted || r.output)}`);
    if (validPos(r.pos_after)) parts.push(`到达 ${fmtPos(r.pos_after, 1)}`);
    else if (validPos(r.pos)) parts.push(`坐标 ${fmtPos(r.pos, 1)}`);
    if (parts.length) return parts.join(" · ");

    return (tool.rawResult || "ok: true").slice(0, 120);
  }

  function renderWaterfall() {
    const view = getCurrentSceneView();
    const sm = view.streamModel;
    const meta = sm.meta || {};

    // Update Task Banner
    const phaseKey = meta.phase || "idle";
    dom["wf-phase-badge"].className = `phase-badge ${escapeHtml(phaseKey)}`;
    dom["wf-phase-badge"].textContent = PHASE_LABELS[phaseKey] || phaseKey;

    const maxCallsStr = meta.max_calls ? String(meta.max_calls) : "--";
    dom["wf-turn-progress"].textContent = `轮次 ${meta.actor_requests || 0} / ${maxCallsStr}`;
    dom["wf-model-tag"].textContent = meta.model || "--";
    dom["wf-model-tag"].title = meta.model || "";

    dom["wf-task-text"].textContent =
      meta.task || "暂无活动任务（可等待 mc-harness 运行或切换至「⏪ 历史回放」选择远征记录）";
    dom["wf-task-text"].classList.toggle("collapsed", !state.taskExpanded);
    dom["btn-toggle-task"].textContent = state.taskExpanded ? "收起任务" : "展开任务";

    const maxEntries = Math.max(20, state.waterfallMaxEntries || 30);
    const startIdx = Math.max(0, sm.entries.length - maxEntries);
    const visibleEntries = startIdx > 0 ? sm.entries.slice(startIdx) : sm.entries;

    // Structure-only signature excludes streaming text length so text deltas update in O(1) without DOM rebuilds
    const sigParts = [
      state.mode,
      sm.entries.length,
      startIdx,
      state.expandedSharedCallIds.size,
      state.expandedRawCallIds.size,
    ];
    for (const entry of visibleEntries) {
      if (entry.entryType === "turn") {
        const hasThought = entry.text || entry.textStreaming ? 1 : 0;
        sigParts.push(
          `T${entry.turnNum}:${hasThought}:${entry.textStreaming ? 1 : 0}:${entry.items.length}`
        );
        for (const it of entry.items) {
          if (it.itemType === "exclusive_tool") {
            sigParts.push(`${it.tool.callId}:${it.tool.status}:${it.tool.ok}`);
          } else if (it.itemType === "shared_group") {
            sigParts.push(it.tools.map((t) => `${t.callId}:${t.status}`).join(","));
          }
        }
      }
    }
    const sig = sigParts.join("|");
    const listEl = dom["waterfall-list"];
    if (sig === state.lastWaterfallStructureSig) {
      let thoughtUpdated = false;
      for (let i = visibleEntries.length - 1; i >= 0; i--) {
        const entry = visibleEntries[i];
        if (entry.entryType === "turn" && (entry.text || entry.textStreaming)) {
          const thoughtEl = listEl.querySelector(`[data-turn-thought="${entry.turnNum}"]`);
          const nextThought = (entry.text || "").trim() || "思考规划中...";
          if (thoughtEl && thoughtEl.textContent !== nextThought) {
            thoughtEl.textContent = nextThought;
            thoughtUpdated = true;
          }
          break;
        }
      }
      if (thoughtUpdated && state.autoScroll) {
        listEl.scrollTop = listEl.scrollHeight;
      }
      updateTickingToolTimers(view);
      return;
    }
    state.lastWaterfallStructureSig = sig;

    if (!sm.entries.length) {
      state.tickingToolEls = [];
      listEl.innerHTML = `<div class="wf-empty">等待实时事件流或历史回放帧...</div>`;
      return;
    }

    const htmlChunks = [];
    if (startIdx > 0) {
      htmlChunks.push(
        `<button class="wf-load-older-btn" data-load-older="1" type="button">▲ 展开更早记录（已折叠前 ${startIdx} 条，点击加载 +40 条）</button>`
      );
    }
    for (const entry of visibleEntries) {
      if (entry.entryType === "event") {
        htmlChunks.push(renderMcEventChipHtml(entry.ev));
        continue;
      }

      const turn = entry;
      const waitTag = turn.rateWaitS >= 0.1 ? `<span>⏱限速 ${turn.rateWaitS.toFixed(1)}s</span>` : "";
      const durTag = turn.durationS !== null ? `<span>推理 ${turn.durationS.toFixed(1)}s</span>` : "";
      const cacheTag =
        turn.usage && turn.usage.promptTokens > 0
          ? `<span>KV ${Math.round(((turn.usage.cachedTokens || 0) / turn.usage.promptTokens) * 100)}%</span>`
          : "";

      const itemsHtml = [];
      if (turn.text || turn.textStreaming) {
        itemsHtml.push(
          `<div class="wf-thought-bubble ${turn.textStreaming ? "streaming" : ""}" data-turn-thought="${turn.turnNum}">${escapeHtml(
            turn.text.trim() || "思考规划中..."
          )}</div>`
        );
      }

      for (const item of turn.items) {
        if (item.itemType === "shared_group") {
          itemsHtml.push(renderSharedGroupHtml(item.tools));
        } else if (item.itemType === "exclusive_tool") {
          itemsHtml.push(renderExclusiveToolCardHtml(item.tool));
        } else if (item.itemType === "event") {
          itemsHtml.push(renderMcEventChipHtml(item.ev));
        }
      }

      htmlChunks.push(`<div class="wf-turn">
        <div class="wf-turn-head">
          <span class="turn-tag">Turn #${turn.turnNum}</span>
          <div class="turn-submeta">
            ${waitTag}${durTag}${cacheTag}
            <span>+${fmtSeconds(turn.startElapsed)}</span>
          </div>
        </div>
        ${itemsHtml.join("")}
      </div>`);
    }

    listEl.innerHTML = htmlChunks.join("");
    state.tickingToolEls = Array.from(listEl.querySelectorAll("[data-tick-call]"));
    updateTickingToolTimers(view);

    if (state.autoScroll) {
      listEl.scrollTop = listEl.scrollHeight;
    }
  }

  function renderSharedGroupHtml(tools) {
    const chips = tools
      .map((t) => {
        const cls =
          t.status === "running"
            ? "running"
            : t.status === "skipped"
            ? "is-skipped"
            : t.ok === false
            ? "is-error"
            : "";
        const icon =
          t.status === "running"
            ? "⏳"
            : t.status === "skipped"
            ? "⏭"
            : t.ok === false
            ? "✖"
            : "✓";
        const shortSum = summarizeToolResult(t);
        return `<button class="tool-chip-shared ${cls}" data-shared-id="${escapeHtml(
          t.callId
        )}" type="button" title="点击展开/收起只读查询详情">
          <span>${icon} ${escapeHtml(t.name || "obs")}</span>
          ${
            t.status === "completed" && shortSum
              ? `<span class="chip-summary">· ${escapeHtml(shortSum)}</span>`
              : ""
          }
        </button>`;
      })
      .join("");

    const expandedDetails = tools
      .filter((t) => state.expandedSharedCallIds.has(t.callId))
      .map((t) => {
        const argsStr = summarizeToolArgs(t.name, t.parsedArgs, t.rawArgs);
        return `<div class="shared-detail-box"><strong>${escapeHtml(t.name)}(${escapeHtml(
          argsStr
        )})</strong>\n${escapeHtml(t.rawResult || "运行中...")}</div>`;
      })
      .join("");

    return `<div class="tool-shared-group">
      <div class="shared-chips-row">
        <span class="shared-group-label">⚡并发观察</span>
        ${chips}
      </div>
      ${expandedDetails}
    </div>`;
  }

  function renderExclusiveToolCardHtml(t) {
    const isMacro = String(t.name || "").startsWith("mc_skill_");
    const statusCls =
      t.status === "running"
        ? "is-running"
        : t.status === "skipped"
        ? "is-skipped"
        : t.ok === false
        ? "is-error"
        : "";
    const argsSummary = summarizeToolArgs(t.name, t.parsedArgs, t.rawArgs);
    const resSummary = summarizeToolResult(t);
    const showRaw = state.expandedRawCallIds.has(t.callId);

    let rightBadge = "";
    if (t.status === "running") {
      rightBadge = `<span class="dur-badge ticking" data-tick-call="${escapeHtml(
        t.callId
      )}">⏱ 0.0s 运行中</span>`;
    } else if (t.status === "skipped") {
      rightBadge = `<span class="skipped-badge">⏭ 熔断跳过 (${escapeHtml(
        t.skipReason || "chain_break"
      )})</span>`;
    } else {
      const errBadge =
        t.ok === false
          ? `<span class="err-code-badge">${escapeHtml(t.errorCode || "ok: false")}</span>`
          : "";
      rightBadge = `${errBadge}<span class="dur-badge">${fmtDurationMs(t.durationMs)}</span>`;
    }

    return `<div class="tool-card-exclusive ${isMacro ? "is-macro" : ""} ${statusCls}">
      <div class="tool-card-head">
        <div class="tool-title-wrap">
          <span class="tool-kind-badge ${isMacro ? "macro" : ""}">${
      isMacro ? "⚡ 宏技能" : "🔧 动作"
    }</span>
          <span class="tool-name">${escapeHtml(t.name || "action")}</span>
        </div>
        <div class="tool-status-right">
          ${rightBadge}
          ${
            t.rawResult
              ? `<button class="text-link-btn" data-raw-id="${escapeHtml(
                  t.callId
                )}" type="button">${showRaw ? "收起" : "回执"}</button>`
              : ""
          }
        </div>
      </div>
      ${argsSummary ? `<div class="tool-args-line">参数: ${escapeHtml(argsSummary)}</div>` : ""}
      <div class="tool-result-line">${escapeHtml(resSummary)}</div>
      ${
        t.kbHint
          ? `<div class="kb-hint-box"><span class="kb-hint-title">💡 KB 纠偏提示:</span>${escapeHtml(
              t.kbHint
            )}</div>`
          : ""
      }
      ${showRaw ? `<pre class="raw-json-pre">${escapeHtml(t.rawResult)}</pre>` : ""}
    </div>`;
  }

  function renderMcEventChipHtml(ev) {
    const dropHtml = validPos(ev.dropPos)
      ? `<span class="drop-coord-pill">掉落物 dropped_items_at: ${fmtPos(ev.dropPos, 1)}</span>`
      : "";
    return `<div class="mc-event-chip ${escapeHtml(ev.kind)}">
      <span>${escapeHtml(ev.text)}</span>
      ${dropHtml}
      <span class="mono-num" style="opacity:0.65;font-size:10px">+${fmtSeconds(ev.elapsed)}</span>
    </div>`;
  }

  function updateTickingToolTimers(view) {
    if (!state.tickingToolEls || state.tickingToolEls.length === 0) return;
    const sm = view.streamModel;
    for (const el of state.tickingToolEls) {
      const id = el.getAttribute("data-tick-call");
      const tool = sm.toolsById.get(id);
      if (!tool) continue;
      let sec = 0;
      if (state.mode === "replay") {
        sec = Math.max(0, (view.currentElapsed || 0) - (tool.startElapsed || 0));
      } else {
        sec = Math.max(0, (performance.now() - (tool.startedWallMs || performance.now())) / 1000);
      }
      el.textContent = `⏱ ${sec.toFixed(1)}s 运行中`;
    }
  }

  // -------------------- Right Pane Section C: XZ + Y Linked Trajectory Canvases --------------------
  function getFilteredTrajectoryAndMarkers(view) {
    // Update dimension tab counts
    const fullTraj = view.fullTrajectory || view.trajectory || [];
    const counts = { overworld: 0, the_nether: 0, the_end: 0 };
    for (const p of fullTraj) {
      const d = normDim(p.dimension);
      if (counts[d] !== undefined) counts[d] += 1;
    }
    dom["dim-cnt-overworld"].textContent = String(counts.overworld);
    dom["dim-cnt-nether"].textContent = String(counts.the_nether);
    dom["dim-cnt-end"].textContent = String(counts.the_end);

    // Auto-select active dimension if enabled
    const activeBotDim = normDim(view.status?.dimension);
    if (state.traj.dimAutoFollow && activeBotDim && counts[activeBotDim] > 0) {
      state.traj.selectedDim = activeBotDim;
    }
    document.querySelectorAll(".dim-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.dim === state.traj.selectedDim);
    });

    const dim = state.traj.selectedDim;
    const dimActiveTraj = (view.trajectory || []).filter((p) => normDim(p.dimension) === dim);
    const dimFullTraj = fullTraj.filter((p) => normDim(p.dimension) === dim);
    const dimMarkers = filterVisibleMarkers(view.markers).filter((m) => normDim(m.dimension) === dim);

    return { dim, dimActiveTraj, dimFullTraj, dimMarkers };
  }

  function centerCameraOnBot() {
    const view = getCurrentSceneView();
    if (validPos(view.status?.pos)) {
      state.traj.camX = Number(view.status.pos[0]);
      state.traj.camZ = Number(view.status.pos[2]);
      return;
    }
    const { dimActiveTraj } = getFilteredTrajectoryAndMarkers(view);
    if (dimActiveTraj.length > 0) {
      const last = dimActiveTraj[dimActiveTraj.length - 1];
      state.traj.camX = last.pos[0];
      state.traj.camZ = last.pos[2];
    }
  }

  function fitTrajectoryToAll() {
    const view = getCurrentSceneView();
    const { dimFullTraj, dimMarkers } = getFilteredTrajectoryAndMarkers(view);
    const pts = [];
    for (const p of dimFullTraj) pts.push(p.pos);
    for (const m of dimMarkers) pts.push(m.pos);
    if (validPos(view.status?.pos)) pts.push(toPos(view.status.pos));

    if (!pts.length) return;
    let minX = Infinity,
      maxX = -Infinity,
      minZ = Infinity,
      maxZ = -Infinity;
    for (const p of pts) {
      minX = Math.min(minX, p[0]);
      maxX = Math.max(maxX, p[0]);
      minZ = Math.min(minZ, p[2]);
      maxZ = Math.max(maxZ, p[2]);
    }
    state.traj.camX = (minX + maxX) / 2;
    state.traj.camZ = (minZ + maxZ) / 2;

    const rect = dom["xz-stage"].getBoundingClientRect();
    const w = Math.max(160, rect.width - 48);
    const h = Math.max(90, rect.height - 36);
    const spanX = Math.max(18, maxX - minX);
    const spanZ = Math.max(18, maxZ - minZ);
    state.traj.scale = Math.max(0.4, Math.min(22, Math.min(w / spanX, h / spanZ)));
    renderTrajectoryCanvases();
  }

  function prepareHiDpiCanvas(canvas) {
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(10, Math.floor(rect.width));
    const h = Math.max(10, Math.floor(rect.height));
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function renderTrajectoryCanvases() {
    const view = getCurrentSceneView();
    const { dim, dimActiveTraj, dimFullTraj, dimMarkers } = getFilteredTrajectoryAndMarkers(view);

    if (state.traj.followBot && !state.traj.dragging) {
      if (validPos(view.status?.pos) && normDim(view.status.dimension) === dim) {
        state.traj.camX = Number(view.status.pos[0]);
        state.traj.camZ = Number(view.status.pos[2]);
      } else if (dimActiveTraj.length > 0) {
        const last = dimActiveTraj[dimActiveTraj.length - 1];
        state.traj.camX = last.pos[0];
        state.traj.camZ = last.pos[2];
      }
    }

    renderXzCanvas(view, dim, dimActiveTraj, dimFullTraj, dimMarkers);
    renderYAltitudeCanvas(view, dimActiveTraj, dimFullTraj, dimMarkers);
  }

  function renderXzCanvas(view, dim, activeTraj, fullTraj, markers) {
    const { ctx, w, h } = prepareHiDpiCanvas(dom["xz-canvas"]);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#080f18";
    ctx.fillRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h / 2;
    const scale = state.traj.scale;
    const camX = state.traj.camX;
    const camZ = state.traj.camZ;

    // North (-Z) is UP -> smaller world Z gives smaller screen Y!
    // East (+X) is RIGHT -> larger world X gives larger screen X!
    const toScreen = (wx, wz) => [cx + (wx - camX) * scale, cy + (wz - camZ) * scale];

    // Adaptive chunk grid
    let stepBlocks = 16;
    if (scale < 1.2) stepBlocks = 128;
    else if (scale < 3) stepBlocks = 64;
    else if (scale > 12) stepBlocks = 8;

    const minWorldX = camX - cx / scale;
    const maxWorldX = camX + cx / scale;
    const minWorldZ = camZ - cy / scale;
    const maxWorldZ = camZ + cy / scale;

    ctx.lineWidth = 1;
    for (
      let gx = Math.floor(minWorldX / stepBlocks) * stepBlocks;
      gx <= maxWorldX;
      gx += stepBlocks
    ) {
      const [sx] = toScreen(gx, 0);
      ctx.strokeStyle = gx === 0 ? "rgba(56,189,248,0.28)" : "rgba(30,45,66,0.65)";
      ctx.beginPath();
      ctx.moveTo(sx, 0);
      ctx.lineTo(sx, h);
      ctx.stroke();
    }
    for (
      let gz = Math.floor(minWorldZ / stepBlocks) * stepBlocks;
      gz <= maxWorldZ;
      gz += stepBlocks
    ) {
      const [, sy] = toScreen(0, gz);
      ctx.strokeStyle = gz === 0 ? "rgba(56,189,248,0.28)" : "rgba(30,45,66,0.65)";
      ctx.beginPath();
      ctx.moveTo(0, sy);
      ctx.lineTo(w, sy);
      ctx.stroke();
    }

    // In Replay Mode, draw full future trajectory faintly as tactical route context
    if (state.mode === "replay" && fullTraj.length > 1) {
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(100, 116, 139, 0.38)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      fullTraj.forEach((pt, idx) => {
        const [sx, sy] = toScreen(pt.pos[0], pt.pos[2]);
        if (idx === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      });
      ctx.stroke();
      ctx.restore();
    }

    // Smooth gradient trajectory polyline up to current point
    if (activeTraj.length > 1) {
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (let i = 1; i < activeTraj.length; i++) {
        const p0 = activeTraj[i - 1];
        const p1 = activeTraj[i];
        // Skip huge teleport jumps > 96 blocks (e.g. death respawn) by drawing dashed jump
        const distJump = Math.hypot(p1.pos[0] - p0.pos[0], p1.pos[2] - p0.pos[2]);
        const [x0, y0] = toScreen(p0.pos[0], p0.pos[2]);
        const [x1, y1] = toScreen(p1.pos[0], p1.pos[2]);
        const ratio = i / (activeTraj.length - 1);

        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        if (distJump > 80) {
          ctx.save();
          ctx.setLineDash([3, 4]);
          ctx.strokeStyle = "rgba(248, 113, 113, 0.45)";
          ctx.lineWidth = 1.2;
          ctx.stroke();
          ctx.restore();
        } else {
          // Cyan (#38bdf8) -> Emerald (#22c55e) gradient by progress
          const r = Math.round(56 + (34 - 56) * ratio);
          const g = Math.round(189 + (197 - 189) * ratio);
          const b = Math.round(248 + (94 - 248) * ratio);
          ctx.strokeStyle = `rgb(${r},${g},${b})`;
          ctx.lineWidth = 2.4;
          ctx.stroke();
        }
      }
    }

    // Plot nearby entities subtly
    if (Array.isArray(view.entities) && normDim(view.status?.dimension) === dim) {
      for (const ent of view.entities) {
        if (!ent || !validPos(ent.pos)) continue;
        const [ex, ey] = toScreen(ent.pos[0], ent.pos[2]);
        ctx.fillStyle =
          ent.type === "hostile"
            ? "#ef4444"
            : ent.type === "animal"
            ? "#4ade80"
            : ent.type === "item"
            ? "#facc15"
            : "#38bdf8";
        ctx.beginPath();
        ctx.arc(ex, ey, ent.type === "hostile" ? 3.2 : 2.3, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // Semantic Markers (protected_region, station, hurt, death, advancement)
    const markerGlyphs = {
      protected_region: { icon: "✖", color: "#f87171", bg: "rgba(127,29,29,0.85)" },
      station: { icon: "🛠", color: "#fbbf24", bg: "rgba(30,41,59,0.88)" },
      hurt: { icon: "⚡", color: "#fb923c", bg: "rgba(120,53,15,0.82)" },
      death: { icon: "💀", color: "#fecaca", bg: "rgba(153,27,27,0.92)" },
      advancement: { icon: "🏆", color: "#fde047", bg: "rgba(113,63,18,0.88)" },
    };

    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const m of markers) {
      const [mx, my] = toScreen(m.pos[0], m.pos[2]);
      if (mx < -16 || mx > w + 16 || my < -16 || my > h + 16) continue;
      const spec = markerGlyphs[m.kind] || markerGlyphs.station;
      ctx.fillStyle = spec.bg;
      ctx.strokeStyle = spec.color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(mx, my, 8.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = spec.color;
      ctx.fillText(spec.icon, mx, my + 0.5);
    }

    // Current Bot position, 24m nearest_hostile alert ring & Yaw directional arrow
    const botPos =
      validPos(view.status?.pos) && normDim(view.status?.dimension) === dim
        ? toPos(view.status.pos)
        : activeTraj.length > 0
        ? activeTraj[activeTraj.length - 1].pos
        : null;

    if (botPos) {
      const [bx, by] = toScreen(botPos[0], botPos[2]);

      // 24m nearest_hostile dashed alert ring
      const hasHostile =
        Boolean(view.status?.nearest_hostile) ||
        (Array.isArray(view.entities) && view.entities.some((e) => e && e.type === "hostile" && Number(e.dist) <= 24));
      if (hasHostile) {
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = "rgba(239, 68, 68, 0.72)";
        ctx.fillStyle = "rgba(239, 68, 68, 0.06)";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.arc(bx, by, 24 * scale, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }

      // Bot directional arrow (yaw=0 -> (0, -1) North UP; yaw=-1.57 -> (+1, 0) East RIGHT)
      const yaw = Number.isFinite(Number(view.status?.yaw))
        ? Number(view.status.yaw)
        : activeTraj[activeTraj.length - 1]?.yaw || 0;
      const dx = -Math.sin(yaw);
      const dz = -Math.cos(yaw);
      const angle = Math.atan2(dz, dx);

      ctx.save();
      ctx.translate(bx, by);
      ctx.rotate(angle);
      ctx.fillStyle = "#38bdf8";
      ctx.strokeStyle = "#f8fafc";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(9, 0);
      ctx.lineTo(-6, -5.5);
      ctx.lineTo(-3.5, 0);
      ctx.lineTo(-6, 5.5);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }

    // Synced hover crosshair
    if (state.traj.hoverPoint && validPos(state.traj.hoverPoint.pos)) {
      const [hx, hy] = toScreen(state.traj.hoverPoint.pos[0], state.traj.hoverPoint.pos[2]);
      ctx.save();
      ctx.strokeStyle = "#facc15";
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.arc(hx, hy, 6.5, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    // North Compass badge in top-right corner of XZ canvas
    ctx.fillStyle = "rgba(13, 21, 32, 0.82)";
    ctx.fillRect(w - 58, 6, 52, 18);
    ctx.fillStyle = "#9bb0ca";
    ctx.font = "600 10px monospace";
    ctx.textAlign = "right";
    ctx.fillText("N(-Z)↑", w - 10, 15);
  }

  function renderYAltitudeCanvas(view, activeTraj, fullTraj, markers) {
    const { ctx, w, h } = prepareHiDpiCanvas(dom["y-canvas"]);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#070c14";
    ctx.fillRect(0, 0, w, h);

    const series = state.mode === "replay" && fullTraj.length ? fullTraj : activeTraj;
    const padL = 38;
    const padR = 12;
    const padT = 8;
    const padB = 15;
    const plotW = Math.max(20, w - padL - padR);
    const plotH = Math.max(20, h - padT - padB);

    // Y range covers reference lines Y=0, Y=63, Y=128 + data range
    let minY = -8;
    let maxY = 138;
    for (const pt of series) {
      minY = Math.min(minY, pt.pos[1] - 6);
      maxY = Math.max(maxY, pt.pos[1] + 6);
    }

    const maxTime =
      state.mode === "replay"
        ? Math.max(1, state.replay.durationS)
        : series.length > 1
        ? Math.max(1, series[series.length - 1].elapsed)
        : 1;
    const minTime =
      state.mode === "replay" ? 0 : series.length > 0 ? Math.min(0, series[0].elapsed) : 0;

    const xOf = (t, idx) => {
      if (maxTime > minTime + 0.01) {
        return padL + ((t - minTime) / (maxTime - minTime)) * plotW;
      }
      return padL + (idx / Math.max(1, series.length - 1)) * plotW;
    };
    const yOf = (alt) => padT + (1 - (alt - minY) / (maxY - minY)) * plotH;

    // Reference dashed lines: Y=128 高寒细雪线, Y=63 海平面, Y=0 深板岩层
    const refLines = [
      { y: 128, label: "Y=128 细雪线", color: "rgba(56, 189, 248, 0.55)" },
      { y: 63, label: "Y=63 海平面", color: "rgba(59, 130, 246, 0.52)" },
      { y: 0, label: "Y=0 深板岩", color: "rgba(148, 163, 184, 0.45)" },
    ];

    ctx.font = "10px monospace";
    for (const ref of refLines) {
      const sy = yOf(ref.y);
      if (sy < padT - 2 || sy > padT + plotH + 2) continue;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = ref.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, sy);
      ctx.lineTo(w - padR, sy);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = ref.color;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(ref.label, padL + 4, sy - 1);
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(String(ref.y), padL - 4, sy);
    }

    // Plot Y curve
    if (series.length > 0) {
      // Dim full curve in replay mode
      if (state.mode === "replay" && series.length > 1) {
        ctx.strokeStyle = "rgba(100, 116, 139, 0.45)";
        ctx.lineWidth = 1.3;
        ctx.beginPath();
        series.forEach((pt, idx) => {
          const sx = xOf(pt.elapsed, idx);
          const sy = yOf(pt.pos[1]);
          if (idx === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.stroke();
      }

      // Active Y curve + subtle area fill
      if (activeTraj.length > 0) {
        const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, "rgba(56, 189, 248, 0.28)");
        grad.addColorStop(1, "rgba(34, 197, 94, 0.03)");

        ctx.beginPath();
        activeTraj.forEach((pt, idx) => {
          const sx = xOf(pt.elapsed, idx);
          const sy = yOf(pt.pos[1]);
          if (idx === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        const lastX = xOf(activeTraj[activeTraj.length - 1].elapsed, activeTraj.length - 1);
        const firstX = xOf(activeTraj[0].elapsed, 0);
        ctx.lineTo(lastX, padT + plotH);
        ctx.lineTo(firstX, padT + plotH);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();

        ctx.strokeStyle = "#22c55e";
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        activeTraj.forEach((pt, idx) => {
          const sx = xOf(pt.elapsed, idx);
          const sy = yOf(pt.pos[1]);
          if (idx === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.stroke();
      }
    }

    // Plot death/hurt ticks on Y timeline
    for (const m of markers) {
      if (m.kind !== "death" && m.kind !== "hurt") continue;
      const sx = xOf(m.elapsed, 0);
      const sy = yOf(m.pos[1]);
      ctx.fillStyle = m.kind === "death" ? "#ef4444" : "#f59e0b";
      ctx.beginPath();
      ctx.arc(sx, sy, m.kind === "death" ? 3.5 : 2.2, 0, Math.PI * 2);
      ctx.fill();
    }

    // Replay current playhead vertical line
    if (state.mode === "replay") {
      const px = xOf(state.replay.currentElapsed, 0);
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(px, padT);
      ctx.lineTo(px, padT + plotH);
      ctx.stroke();
    }

    // Synced hover cursor on Y strip
    if (state.traj.hoverPoint && state.traj.hoverPoint.elapsed !== undefined) {
      const hx = xOf(state.traj.hoverPoint.elapsed, 0);
      const hy = yOf(state.traj.hoverPoint.pos[1]);
      ctx.save();
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = "#facc15";
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + plotH);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = "#facc15";
      ctx.beginPath();
      ctx.arc(hx, hy, 4, 0, Math.PI * 2);
      ctx.fill();
    }

    // Bottom axis caption
    ctx.fillStyle = "#647b99";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText(`海拔 Y 轴 · ${fmtSeconds(maxTime)}`, w - padR, h - 2);
  }

  function showTrajTooltip(hpObj, stageX, stageY) {
    const tip = dom["traj-tooltip"];
    if (!hpObj) {
      tip.classList.add("hidden");
      return;
    }
    const kindNames = {
      protected_region: "✖ 保护区拉黑坐标",
      station: "🛠 工作站方块",
      hurt: "⚡ 受伤位置",
      death: "💀 死亡 / 掉落点",
      advancement: "🏆 解锁进度",
      path: "📍 轨迹采样点",
    };
    const title = kindNames[hpObj.kind] || "📍 坐标点";
    const extraLabel = hpObj.label ? `<div>${escapeHtml(hpObj.label)}</div>` : "";
    const vitals =
      hpObj.health !== undefined
        ? `<div class="mono-num">HP ${Number(hpObj.health).toFixed(1)} · Food ${
            hpObj.food ?? "--"
          }</div>`
        : "";
    tip.innerHTML = `<div style="font-weight:700;color:#7dd3fc">${title} · +${fmtSeconds(
      hpObj.elapsed
    )}</div>
      <div class="mono-num">XYZ: ${fmtPos(hpObj.pos, 1)}</div>
      ${extraLabel}${vitals}`;
    tip.classList.remove("hidden");

    const rect = dom["xz-stage"].getBoundingClientRect();
    const left = Math.min(rect.width - 190, Math.max(6, stageX + 12));
    const top = Math.min(rect.height - 72, Math.max(6, stageY - 12));
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  }

  // -------------------- Animation Loop (Replay Playback + RPM Countdown + Timers) --------------------
  function scheduleLiveDeltaRender() {
    if (state.liveRenderRaf) return;
    state.liveRenderRaf = requestAnimationFrame(() => {
      state.liveRenderRaf = 0;
      if (state.mode === "live") {
        renderTopHud();
        renderWaterfall();
      }
    });
  }

  function animationFrameLoop(nowMs) {
    if (typeof document !== "undefined" && document.hidden) {
      state.replay.lastTickWallMs = nowMs;
      requestAnimationFrame(animationFrameLoop);
      return;
    }

    // 1. Advance replay clock if playing
    if (state.mode === "replay" && state.replay.playing && state.replay.bundle) {
      const dtS = Math.min(0.25, Math.max(0, (nowMs - state.replay.lastTickWallMs) / 1000));
      state.replay.lastTickWallMs = nowMs;
      state.replay.currentElapsed += dtS * state.replay.speed;
      if (state.replay.currentElapsed >= state.replay.durationS) {
        state.replay.currentElapsed = state.replay.durationS;
        state.replay.playing = false;
      }
      updateReplayFrame();
    } else {
      state.replay.lastTickWallMs = nowMs;
    }

    // 2. Update 24 RPM rate-limit wait bar smoothly
    updateRpmWaitBar(nowMs);

    // 3. Update live running exclusive tool timers
    if (state.mode === "live" && state.tickingToolEls.length > 0) {
      updateTickingToolTimers(getCurrentSceneView());
    }

    requestAnimationFrame(animationFrameLoop);
  }

  function updateRpmWaitBar(nowMs) {
    let remS = 0;
    let totalS = 2.5;

    if (state.mode === "replay") {
      const view = getCurrentSceneView();
      const rw = view.streamModel.latestRateWait;
      if (rw && rw.waitS > 0) {
        totalS = Math.max(0.5, rw.waitS);
        remS = Math.max(0, rw.elapsed + rw.waitS - state.replay.currentElapsed);
      }
    } else {
      const rw = state.live.rateWait;
      if (rw && rw.waitS > 0) {
        totalS = Math.max(0.5, rw.waitS);
        const elapsedWall = (nowMs - rw.startedWallMs) / 1000;
        remS = Math.max(0, rw.waitS - elapsedWall);
      }
    }

    const nextText = remS > 0.04 ? `${remS.toFixed(1)}s` : "0.0s";
    const nextPct = remS > 0.04 ? `${Math.min(100, (remS / totalS) * 100).toFixed(1)}%` : "0%";
    const cacheKey = `${nextText}|${nextPct}`;
    if (cacheKey === state.lastRpmRenderedText) return;
    state.lastRpmRenderedText = cacheKey;
    dom["hud-rpm-val"].textContent = nextText;
    dom["hud-rpm-bar"].style.width = nextPct;
  }

  function renderAllViews() {
    renderLeftOverlaysAnd3D();
    renderTopHud();
    renderOpenDrawer();
    renderWaterfall();
    renderTrajectoryCanvases();
  }

  // -------------------- Event Bindings --------------------
  function bindEvents() {
    // Mode switches
    dom["btn-mode-live"].addEventListener("click", () => setMode("live"));
    dom["btn-mode-replay"].addEventListener("click", () => setMode("replay"));

    dom["run-select"].addEventListener("change", (e) => {
      const id = e.target.value;
      if (id) loadReplayById(id, false);
    });
    dom["btn-refresh-runs"].addEventListener("click", () => fetchInfoAndRuns());

    // Replay transport controls
    dom["btn-play-pause"].addEventListener("click", () => {
      if (!state.replay.bundle) return;
      if (state.replay.currentElapsed >= state.replay.durationS - 0.05) {
        state.replay.currentElapsed = 0;
      }
      state.replay.playing = !state.replay.playing;
      state.replay.lastTickWallMs = performance.now();
      renderReplayTransport();
    });
    dom["btn-step-prev"].addEventListener("click", () => stepReplayKeyframe(-1));
    dom["btn-step-next"].addEventListener("click", () => stepReplayKeyframe(1));

    document.querySelectorAll(".speed-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.replay.speed = Number(btn.dataset.speed) || 1;
        renderReplayTransport();
      });
    });

    dom["replay-scrubber"].addEventListener("input", (e) => {
      if (!state.replay.bundle) return;
      state.replay.playing = false;
      const val = Number(e.target.value) || 0;
      state.replay.currentElapsed =
        val >= state.replay.durationS - 0.06 ? state.replay.durationS : val;
      updateReplayFrame();
    });

    // Left viewport tabs & capture / prismarine / overlay controls
    document.querySelectorAll(".view-tab").forEach((btn) => {
      btn.addEventListener("click", () => setLeftView(btn.dataset.view));
    });
    dom["btn-recheck-prismarine"].addEventListener("click", () => fetchInfoAndRuns());
    if (dom["btn-prismarine-first"]) {
      dom["btn-prismarine-first"].addEventListener("click", () => setPrismarineMode("first_person"));
    }
    if (dom["btn-prismarine-third"]) {
      dom["btn-prismarine-third"].addEventListener("click", () => setPrismarineMode("third_person"));
    }
    if (dom["btn-prismarine-reload"]) {
      dom["btn-prismarine-reload"].addEventListener("click", () => reloadPrismarineIframe());
    }
    if (dom["btn-capture-first"]) {
      dom["btn-capture-first"].addEventListener("click", () => setCaptureCameraMode("first_person"));
    }
    if (dom["btn-capture-third"]) {
      dom["btn-capture-third"].addEventListener("click", () => setCaptureCameraMode("third_person"));
    }
    dom["btn-start-capture"].addEventListener("click", () => startLocalCapture());
    dom["btn-switch-capture"].addEventListener("click", () => startLocalCapture());
    dom["btn-stop-capture"].addEventListener("click", () => stopLocalCapture());
    dom["chk-relay-lan"].addEventListener("change", (e) => {
      state.captureRelayEnabled = Boolean(e.target.checked);
      if (state.captureStream) {
        if (state.captureRelayEnabled) startCaptureRelayPublisher();
        else stopCaptureRelayPublisher();
      }
      renderCaptureState();
    });
    if (dom["btn-toggle-alerts"]) {
      dom["btn-toggle-alerts"].addEventListener("click", () => toggleAlertsVisibility());
    }
    if (dom["btn-ov-hurt-toggle"]) {
      dom["btn-ov-hurt-toggle"].addEventListener("click", () => toggleAlertsVisibility());
    }
    if (dom["overlay-top-right"]) {
      dom["overlay-top-right"].addEventListener("click", (e) => {
        if (e.target && e.target.closest(".danger-badge")) {
          toggleAlertsVisibility(false);
        }
      });
    }
    dom["btn-toggle-overlays"].addEventListener("click", () => {
      state.overlaysVisible = !state.overlaysVisible;
      dom["btn-toggle-overlays"].classList.toggle("active", state.overlaysVisible);
      dom["viewport-stage"].classList.toggle("hide-overlays", !state.overlaysVisible);
    });
    dom["remote-capture-img"].addEventListener("error", () => {
      const img = dom["remote-capture-img"];
      if (img.dataset.streaming === "1") {
        delete img.dataset.streaming;
        delete img.dataset.streamBase;
        img.removeAttribute("src");
        if (state.captureReconnectTimer) clearTimeout(state.captureReconnectTimer);
        state.captureReconnectTimer = setTimeout(() => {
          state.captureReconnectTimer = null;
          if (state.mode === "live" && state.leftView === "local_capture") {
            fetchInfoAndRuns();
          }
        }, 1000);
      }
    });

    // Drawers
    dom["btn-drawer-inv"].addEventListener("click", () => toggleDrawer("inventory"));
    dom["btn-drawer-adv"].addEventListener("click", () => toggleDrawer("advancements"));
    dom["btn-adv-pill"].addEventListener("click", () => toggleDrawer("advancements"));
    document.querySelectorAll("[data-close-drawer]").forEach((btn) => {
      btn.addEventListener("click", () => toggleDrawer(null));
    });

    // Task banner & waterfall delegation
    dom["btn-toggle-task"].addEventListener("click", () => {
      state.taskExpanded = !state.taskExpanded;
      dom["wf-task-text"].classList.toggle("collapsed", !state.taskExpanded);
      dom["btn-toggle-task"].textContent = state.taskExpanded ? "收起任务" : "展开任务";
    });
    dom["chk-autoscroll"].addEventListener("change", (e) => {
      state.autoScroll = Boolean(e.target.checked);
    });

    dom["waterfall-list"].addEventListener("click", (e) => {
      const loadOlderBtn = e.target.closest("[data-load-older]");
      if (loadOlderBtn) {
        state.waterfallMaxEntries += 40;
        state.lastWaterfallStructureSig = "";
        const prevTop = dom["waterfall-list"].scrollTop;
        renderWaterfall();
        dom["waterfall-list"].scrollTop = prevTop;
        return;
      }
      const sharedBtn = e.target.closest("[data-shared-id]");
      if (sharedBtn) {
        const id = sharedBtn.getAttribute("data-shared-id");
        if (state.expandedSharedCallIds.has(id)) state.expandedSharedCallIds.delete(id);
        else state.expandedSharedCallIds.add(id);
        state.lastWaterfallStructureSig = "";
        renderWaterfall();
        return;
      }
      const rawBtn = e.target.closest("[data-raw-id]");
      if (rawBtn) {
        const id = rawBtn.getAttribute("data-raw-id");
        if (state.expandedRawCallIds.has(id)) state.expandedRawCallIds.delete(id);
        else state.expandedRawCallIds.add(id);
        state.lastWaterfallStructureSig = "";
        renderWaterfall();
      }
    });

    // Section C: Dimension filter & Follow / Fit buttons
    document.querySelectorAll(".dim-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.traj.selectedDim = btn.dataset.dim;
        state.traj.dimAutoFollow = false;
        fitTrajectoryToAll();
      });
    });

    if (dom["btn-traj-hurt"]) {
      dom["btn-traj-hurt"].addEventListener("click", () => toggleAlertsVisibility());
    }

    dom["btn-traj-follow"].addEventListener("click", () => {
      state.traj.followBot = !state.traj.followBot;
      dom["btn-traj-follow"].classList.toggle("active", state.traj.followBot);
      if (state.traj.followBot) centerCameraOnBot();
      renderTrajectoryCanvases();
    });

    dom["btn-traj-fit"].addEventListener("click", () => {
      state.traj.followBot = false;
      dom["btn-traj-follow"].classList.remove("active");
      fitTrajectoryToAll();
    });

    // XZ Canvas pan, wheel zoom & hover tooltip
    const xzCanvas = dom["xz-canvas"];
    xzCanvas.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = xzCanvas.getBoundingClientRect();
        const mx = e.clientX - rect.left - rect.width / 2;
        const my = e.clientY - rect.top - rect.height / 2;
        const worldBeforeX = state.traj.camX + mx / state.traj.scale;
        const worldBeforeZ = state.traj.camZ + my / state.traj.scale;

        const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
        state.traj.scale = Math.max(0.25, Math.min(35, state.traj.scale * factor));
        if (!state.traj.followBot) {
          state.traj.camX = worldBeforeX - mx / state.traj.scale;
          state.traj.camZ = worldBeforeZ - my / state.traj.scale;
        }
        renderTrajectoryCanvases();
      },
      { passive: false }
    );

    xzCanvas.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      state.traj.dragging = true;
      state.traj.dragStartX = e.clientX;
      state.traj.dragStartY = e.clientY;
      state.traj.dragCamX = state.traj.camX;
      state.traj.dragCamZ = state.traj.camZ;
    });

    window.addEventListener("mousemove", (e) => {
      if (!state.traj.dragging) return;
      const dx = e.clientX - state.traj.dragStartX;
      const dy = e.clientY - state.traj.dragStartY;
      if (Math.hypot(dx, dy) > 3 && state.traj.followBot) {
        state.traj.followBot = false;
        dom["btn-traj-follow"].classList.remove("active");
      }
      state.traj.camX = state.traj.dragCamX - dx / state.traj.scale;
      state.traj.camZ = state.traj.dragCamZ - dy / state.traj.scale;
      renderTrajectoryCanvases();
    });

    window.addEventListener("mouseup", () => {
      state.traj.dragging = false;
      state.traj.yScrubbing = false;
    });

    let pendingXzMoveEvt = null;
    xzCanvas.addEventListener("mousemove", (e) => {
      if (state.traj.dragging) return;
      pendingXzMoveEvt = { clientX: e.clientX, clientY: e.clientY };
      if (state.traj.hoverRaf) return;
      state.traj.hoverRaf = requestAnimationFrame(() => {
        state.traj.hoverRaf = 0;
        const ev = pendingXzMoveEvt;
        if (!ev || state.traj.dragging) return;
        const rect = xzCanvas.getBoundingClientRect();
        const sx = ev.clientX - rect.left;
        const sy = ev.clientY - rect.top;
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        const wx = state.traj.camX + (sx - cx) / state.traj.scale;
        const wz = state.traj.camZ + (sy - cy) / state.traj.scale;
        dom["traj-readout"].textContent = `X: ${wx.toFixed(1)}, Z: ${wz.toFixed(1)}`;

        const view = getCurrentSceneView();
        const { dimFullTraj, dimActiveTraj, dimMarkers } = getFilteredTrajectoryAndMarkers(view);
        const pts = state.mode === "replay" ? dimFullTraj : dimActiveTraj;

        let best = null;
        let bestDistPx = 14;

        for (const m of dimMarkers) {
          const mx = cx + (m.pos[0] - state.traj.camX) * state.traj.scale;
          const my = cy + (m.pos[2] - state.traj.camZ) * state.traj.scale;
          const d = Math.hypot(sx - mx, sy - my);
          if (d < bestDistPx) {
            bestDistPx = d;
            best = { ...m };
          }
        }
        if (!best) {
          for (const p of pts) {
            const px = cx + (p.pos[0] - state.traj.camX) * state.traj.scale;
            const py = cy + (p.pos[2] - state.traj.camZ) * state.traj.scale;
            const d = Math.hypot(sx - px, sy - py);
            if (d < bestDistPx) {
              bestDistPx = d;
              best = { ...p, kind: "path" };
            }
          }
        }

        const nextKey = best
          ? `${best.kind || "pt"}:${Number(best.elapsed || 0).toFixed(2)}:${Number(best.pos[0]).toFixed(2)}:${Number(best.pos[2]).toFixed(2)}`
          : "";
        state.traj.hoverPoint = best;
        if (nextKey !== state.traj.lastHoverKey) {
          state.traj.lastHoverKey = nextKey;
          showTrajTooltip(best, sx, sy);
          renderTrajectoryCanvases();
        } else if (best) {
          showTrajTooltip(best, sx, sy);
        }
      });
    });

    xzCanvas.addEventListener("mouseleave", () => {
      pendingXzMoveEvt = null;
      state.traj.hoverPoint = null;
      if (state.traj.lastHoverKey) {
        state.traj.lastHoverKey = "";
        showTrajTooltip(null, 0, 0);
        renderTrajectoryCanvases();
      } else {
        showTrajTooltip(null, 0, 0);
      }
      dom["traj-readout"].textContent = "北(-Z)上 · 东(+X)右";
    });

    xzCanvas.addEventListener("click", () => {
      if (state.mode === "replay" && state.traj.hoverPoint && state.replay.bundle) {
        state.replay.playing = false;
        state.replay.currentElapsed = state.traj.hoverPoint.elapsed || 0;
        updateReplayFrame();
      }
    });

    // Linked Y-Altitude Strip hover & click-to-scrub
    const yCanvas = dom["y-canvas"];
    const handleYPointer = (e, doScrub) => {
      const rect = yCanvas.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const padL = 38;
      const padR = 12;
      const plotW = Math.max(20, rect.width - padL - padR);
      const ratio = Math.max(0, Math.min(1, (sx - padL) / plotW));

      const view = getCurrentSceneView();
      const { dimFullTraj, dimActiveTraj } = getFilteredTrajectoryAndMarkers(view);
      const series = state.mode === "replay" && dimFullTraj.length ? dimFullTraj : dimActiveTraj;

      if (state.mode === "replay" && state.replay.bundle && doScrub) {
        state.replay.playing = false;
        state.replay.currentElapsed = ratio * state.replay.durationS;
        updateReplayFrame();
      }

      if (series.length > 0) {
        const maxT =
          state.mode === "replay"
            ? Math.max(1, state.replay.durationS)
            : Math.max(1, series[series.length - 1].elapsed);
        const targetT = ratio * maxT;
        let nearest = series[0];
        let minDiff = Infinity;
        for (const p of series) {
          const df = Math.abs(p.elapsed - targetT);
          if (df < minDiff) {
            minDiff = df;
            nearest = p;
          }
        }
        state.traj.hoverPoint = { ...nearest, kind: "path" };
        dom["traj-readout"].textContent = `t=${fmtSeconds(nearest.elapsed)} · Y=${nearest.pos[1].toFixed(
          1
        )}`;
        renderTrajectoryCanvases();
      }
    };

    yCanvas.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      state.traj.yScrubbing = true;
      handleYPointer(e, true);
    });
    let yMoveRaf = 0;
    let pendingYMoveEvt = null;
    yCanvas.addEventListener("mousemove", (e) => {
      pendingYMoveEvt = { clientX: e.clientX, clientY: e.clientY };
      if (yMoveRaf) return;
      yMoveRaf = requestAnimationFrame(() => {
        yMoveRaf = 0;
        if (pendingYMoveEvt) {
          handleYPointer(pendingYMoveEvt, state.traj.yScrubbing);
        }
      });
    });
    yCanvas.addEventListener("mouseleave", () => {
      pendingYMoveEvt = null;
      state.traj.hoverPoint = null;
      if (state.traj.lastHoverKey) {
        state.traj.lastHoverKey = "";
        renderTrajectoryCanvases();
      } else {
        renderTrajectoryCanvases();
      }
      dom["traj-readout"].textContent = "北(-Z)上 · 东(+X)右";
    });

    // ResizeObserver for crisp WebGL & Canvas resizing
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => {
        if (state.viewer3d && typeof state.viewer3d.resize === "function") {
          try {
            state.viewer3d.resize();
          } catch (_) {}
        }
        renderTrajectoryCanvases();
      });
      ro.observe(dom["viewport-stage"]);
      ro.observe(dom["xz-stage"]);
      ro.observe(dom["y-stage"]);
    } else {
      window.addEventListener("resize", () => {
        if (state.viewer3d && typeof state.viewer3d.resize === "function") {
          try {
            state.viewer3d.resize();
          } catch (_) {}
        }
        renderTrajectoryCanvases();
      });
    }
  }

  // -------------------- Initialization --------------------
  function toggleAlertsVisibility(forceVal) {
    state.alertsVisible = typeof forceVal === "boolean" ? forceVal : !state.alertsVisible;
    try {
      localStorage.setItem("mc_live_hurt_alerts_v2", state.alertsVisible ? "1" : "0");
    } catch (_) {}
    syncAlertsVisibility();
    renderLeftOverlaysAnd3D();
    renderTrajectoryCanvases();
  }

  function syncAlertsVisibility() {
    const hidden = !state.alertsVisible;
    if (dom["app-shell"]) {
      dom["app-shell"].classList.toggle("hide-alerts", hidden);
    }
    if (dom["viewport-stage"]) {
      dom["viewport-stage"].classList.toggle("hide-alerts", hidden);
    }
    if (dom["btn-toggle-alerts"]) {
      dom["btn-toggle-alerts"].classList.toggle("active", state.alertsVisible);
      dom["btn-toggle-alerts"].textContent = state.alertsVisible ? "🔔 伤害提示: 开" : "🔕 伤害提示: 关";
    }
    if (dom["btn-ov-hurt-toggle"]) {
      dom["btn-ov-hurt-toggle"].classList.toggle("active", state.alertsVisible);
      dom["btn-ov-hurt-toggle"].textContent = state.alertsVisible ? "🔔 伤害标记显示中" : "🔕 伤害标记已隐";
    }
    if (dom["btn-traj-hurt"]) {
      dom["btn-traj-hurt"].classList.toggle("active", state.alertsVisible);
      dom["btn-traj-hurt"].textContent = state.alertsVisible ? "🔔 显示伤害" : "🔕 伤害已隐";
    }
  }

  function init() {
    cacheDom();
    try {
      state.alertsVisible = localStorage.getItem("mc_live_hurt_alerts_v2") === "1";
    } catch (_) {
      state.alertsVisible = false;
    }
    syncAlertsVisibility();
    bindEvents();
    if (isEngineViewMode(state.leftView)) {
      ensureViewer3D();
    }

    renderHeaderStatus();
    renderCaptureState();
    renderAllViews();
    fetchInfoAndRuns();
    connectWebSocket();
    requestAnimationFrame(animationFrameLoop);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
