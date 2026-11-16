#!/usr/bin/env node
'use strict';

/**
 * cam_server.js — Optional Prismarine-Viewer WebGL camera companion for 5ease (Leaves 1.21.11).
 *
 * NOTE: Designed ONLY for local offline test servers (e.g. 5ease 10.50.249.97:25565).
 * Do NOT attach a secondary survival camera bot to the online competition server.
 */

const http = require('http');
const express = require('express');
const socketio = require('socket.io');
const mineflayer = require('mineflayer');
const mcDataLoader = require('minecraft-data');
const { Vec3 } = require('vec3');
const { WorldView } = require('prismarine-viewer/viewer/lib/worldView');
const { setupRoutes } = require('prismarine-viewer/lib/common');

const mcData1164 = mcDataLoader('1.16.4');
const viewerEntities1164 = require('prismarine-viewer/viewer/lib/entity/entities.json');
const BLOCKED_VIEWER_ENTITY_NAMES = new Set(['item', 'experience_orb']);

function isSupportedViewerEntityName(name) {
  if (!name || typeof name !== 'string') return false;
  if (BLOCKED_VIEWER_ENTITY_NAMES.has(name)) return false;
  if (!mcData1164 || !mcData1164.entitiesByName || !mcData1164.entitiesByName[name]) return false;
  if (!Object.prototype.hasOwnProperty.call(viewerEntities1164, name)) return false;
  return true;
}

function parseArgs(argv) {
  const cfg = {
    mcHost: process.env.MC_HOST || '10.50.249.97',
    mcPort: parseInt(process.env.MC_PORT || '25565', 10),
    camUser: process.env.CAM_USER || 'LiveCam_01',
    viewerHost: process.env.VIEWER_HOST || '0.0.0.0',
    viewerPort: parseInt(process.env.VIEWER_PORT || '8766', 10),
    followApi: process.env.FOLLOW_API || 'http://127.0.0.1:8765/api/telemetry-pos',
    viewDistance: parseInt(process.env.VIEW_DISTANCE || '6', 10),
    followMode: process.env.FOLLOW_MODE || 'third_person', // 'first_person' | 'third_person'
    tpFollow: process.env.CAM_TP_FOLLOW !== '0',
    mcVersion: process.env.MC_VERSION || '1.21.11',
  };

  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => argv[++i];
    if (arg === '--mc-host') cfg.mcHost = next();
    else if (arg === '--mc-port') cfg.mcPort = parseInt(next(), 10);
    else if (arg === '--cam-user') cfg.camUser = next();
    else if (arg === '--viewer-host') cfg.viewerHost = next();
    else if (arg === '--viewer-port') cfg.viewerPort = parseInt(next(), 10);
    else if (arg === '--follow-api') cfg.followApi = next();
    else if (arg === '--view-distance') cfg.viewDistance = parseInt(next(), 10);
    else if (arg === '--first-person') cfg.followMode = 'first_person';
    else if (arg === '--third-person') cfg.followMode = 'third_person';
    else if (arg === '--mode') cfg.followMode = next();
    else if (arg === '--no-tp') cfg.tpFollow = false;
    else if (arg === '--mc-version') cfg.mcVersion = next();
    else if (arg === '--help' || arg === '-h') {
      console.log(
        [
          'Usage: node live_viewer/cam_server.js [options]',
          '  --mc-host <host>        Minecraft server host (default: 10.50.249.97)',
          '  --mc-port <port>        Minecraft server port (default: 25565)',
          '  --cam-user <name>       Offline camera bot username (default: LiveCam_01)',
          '  --viewer-port <port>    Prismarine WebGL HTTP port (default: 8766)',
          '  --follow-api <url>      mc-live telemetry URL (default: http://127.0.0.1:8765/api/telemetry-pos)',
          '  --first-person          Lock viewer camera to first-person pose',
          '  --third-person          Lock viewer camera to chase third-person pose (default)',
          '  --view-distance <chunks> Chunk radius streamed to browser (default: 6)',
        ].join('\n')
      );
      process.exit(0);
    }
  }
  return cfg;
}

/**
 * Build exact stateId lookup table from 1.21.11 -> 1.21.4 because prismarine-viewer@1.33.0
 * ships prebuilt browser worker/textures capped at 1.21.4 while 1.21.5–1.21.11 added 72 blocks
 * and shifted 961 block state ranges.
 */
function buildStateRemapper(srcVersion = '1.21.11', dstVersion = '1.21.4') {
  const src = mcDataLoader(srcVersion);
  const dst = mcDataLoader(dstVersion);
  if (!src || !dst) return null;

  let maxSrcState = 0;
  for (const b of src.blocksArray) {
    if (b.maxStateId > maxSrcState) maxSrcState = b.maxStateId;
  }

  const proxyByName = (name) => {
    if (name === 'leaf_litter') return 'brown_carpet';
    if (name === 'bush' || name === 'short_dry_grass') return 'short_grass';
    if (name === 'tall_dry_grass' || name === 'firefly_bush') return 'dead_bush';
    if (name === 'wildflowers') return 'dandelion';
    if (name === 'cactus_flower') return 'poppy';
    if (name === 'test_block' || name === 'test_instance_block') return 'structure_block';
    if (name === 'dried_ghast') return 'bone_block';
    if (name.endsWith('_shelf')) {
      const wood = name.slice(0, -'_shelf'.length);
      return dst.blocksByName[`${wood}_planks`] ? `${wood}_planks` : 'oak_planks';
    }
    if (name.includes('copper_wall_torch')) return 'wall_torch';
    if (name.includes('copper_torch')) return 'torch';
    if (name.includes('copper_lantern')) return 'lantern';
    if (name.includes('copper_chain')) return 'chain';
    if (name.includes('copper_bars')) return 'iron_bars';
    if (name.includes('copper_chest')) return 'chest';
    if (name.includes('copper_golem_statue')) return 'cut_copper';
    if (name.includes('lightning_rod')) return 'lightning_rod';
    return 'stone';
  };

  const table = new Uint16Array(maxSrcState + 1);
  for (const bSrc of src.blocksArray) {
    const bDst = dst.blocksByName[bSrc.name];
    const srcSpan = bSrc.maxStateId - bSrc.minStateId;
    if (bDst) {
      const dstSpan = bDst.maxStateId - bDst.minStateId;
      for (let offset = 0; offset <= srcSpan; offset++) {
        table[bSrc.minStateId + offset] =
          dstSpan === srcSpan ? bDst.minStateId + offset : bDst.defaultState;
      }
    } else {
      const fallbackBlock = dst.blocksByName[proxyByName(bSrc.name)] || dst.blocksByName.stone;
      const fallbackState = fallbackBlock ? fallbackBlock.defaultState : 1;
      for (let offset = 0; offset <= srcSpan; offset++) {
        table[bSrc.minStateId + offset] = fallbackState;
      }
    }
  }

  function remapStateId(sid) {
    if (sid >= 0 && sid < table.length) return table[sid];
    return 0;
  }

  function remapChunkJson(rawChunkJson) {
    try {
      const parsed = JSON.parse(rawChunkJson);
      if (!Array.isArray(parsed.sections)) return rawChunkJson;
      for (let i = 0; i < parsed.sections.length; i++) {
        const secStr = parsed.sections[i];
        if (!secStr) continue;
        const sec = JSON.parse(secStr);
        if (!sec || typeof sec.data !== 'string') continue;
        const container = JSON.parse(sec.data);
        let changed = false;
        if (container.type === 'single' && typeof container.value === 'number') {
          const mapped = remapStateId(container.value);
          if (mapped !== container.value) {
            container.value = mapped;
            changed = true;
          }
        } else if (container.type === 'indirect' && Array.isArray(container.palette)) {
          for (let p = 0; p < container.palette.length; p++) {
            const orig = container.palette[p];
            const mapped = remapStateId(orig);
            if (mapped !== orig) {
              container.palette[p] = mapped;
              changed = true;
            }
          }
        }
        if (changed) {
          sec.data = JSON.stringify(container);
          parsed.sections[i] = JSON.stringify(sec);
        }
      }
      return JSON.stringify(parsed);
    } catch (_) {
      return rawChunkJson;
    }
  }

  return { remapStateId, remapChunkJson, maxSrcState };
}

function fetchJson(urlStr, timeoutMs = 1200) {
  return new Promise((resolve) => {
    let u;
    try {
      u = new URL(urlStr);
    } catch (_) {
      resolve(null);
      return;
    }
    const req = http.get(
      {
        hostname: u.hostname,
        port: u.port || 80,
        path: u.pathname + u.search,
        timeout: timeoutMs,
      },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          resolve(null);
          return;
        }
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c) => {
          if (body.length < 16384) body += c;
        });
        res.on('end', () => {
          try {
            resolve(JSON.parse(body));
          } catch (_) {
            resolve(null);
          }
        });
      }
    );
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
    req.on('error', () => resolve(null));
  });
}

function createRemappedSocketProxy(socket, remapper) {
  const allowedEntityIds = new Set();
  const blockedEntityIds = new Set();
  return {
    on: socket.on.bind(socket),
    emit(event, payload) {
      if (remapper && event === 'loadChunk' && payload && typeof payload.chunk === 'string') {
        return socket.emit('loadChunk', {
          x: payload.x,
          z: payload.z,
          chunk: remapper.remapChunkJson(payload.chunk),
        });
      }
      if (remapper && event === 'blockUpdate' && payload && typeof payload.stateId === 'number') {
        return socket.emit('blockUpdate', {
          pos: payload.pos,
          stateId: remapper.remapStateId(payload.stateId),
        });
      }
      if (event === 'entity' && payload && typeof payload === 'object') {
        const eid = payload.id;
        if (payload.delete) {
          allowedEntityIds.delete(eid);
          if (blockedEntityIds.delete(eid)) {
            return;
          }
          return socket.emit('entity', payload);
        }
        if (typeof payload.name === 'string') {
          if (!isSupportedViewerEntityName(payload.name)) {
            if (eid !== undefined && eid !== null) {
              blockedEntityIds.add(eid);
              allowedEntityIds.delete(eid);
            }
            return;
          }
          if (eid !== undefined && eid !== null) {
            blockedEntityIds.delete(eid);
            allowedEntityIds.add(eid);
          }
          return socket.emit('entity', payload);
        }
        if (eid !== undefined && eid !== null && !allowedEntityIds.has(eid)) {
          return;
        }
        return socket.emit('entity', payload);
      }
      return socket.emit(event, payload);
    },
  };
}

function computeCameraPose(targetPos, targetYaw, mode) {
  const yaw = typeof targetYaw === 'number' && Number.isFinite(targetYaw) ? targetYaw : 0;
  if (mode === 'first_person') {
    return {
      pos: new Vec3(targetPos[0], targetPos[1], targetPos[2]),
      yaw,
      pitch: -0.05,
      addMesh: false,
    };
  }
  // Mineflayer forward vector is (-sin(yaw), -cos(yaw)); chase camera sits behind (+sin(yaw), +cos(yaw))
  const dist = 4.2;
  const camX = targetPos[0] + Math.sin(yaw) * dist;
  const camY = targetPos[1] + 2.1;
  const camZ = targetPos[2] + Math.cos(yaw) * dist;
  return {
    pos: new Vec3(camX, camY, camZ),
    yaw,
    pitch: -0.32,
    addMesh: true,
  };
}

function startServer(cfg) {
  const remapper = buildStateRemapper(cfg.mcVersion, '1.21.4');
  const app = express();
  const server = http.createServer(app);
  const io = socketio(server, {
    path: '/socket.io',
    cors: { origin: '*' },
  });

  const state = {
    connected: false,
    spawned: false,
    botVersion: null,
    botPos: null,
    lastTarget: null,
    lastTpAt: 0,
    followMode: cfg.followMode,
    lastError: null,
  };

  app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    next();
  });

  let activeBot = null;
  const clientSessions = new Set();

  function getActiveTargetPose() {
    if (state.lastTarget && Array.isArray(state.lastTarget.pos) && state.lastTarget.pos.length >= 3) {
      return { pos: state.lastTarget.pos, yaw: state.lastTarget.yaw };
    }
    if (activeBot && activeBot.entity && activeBot.entity.position) {
      return {
        pos: [activeBot.entity.position.x, activeBot.entity.position.y, activeBot.entity.position.z],
        yaw: activeBot.entity.yaw,
      };
    }
    return null;
  }

  function pushPoseToSession(session, target) {
    if (!target) return;
    const pose = computeCameraPose(target.pos, target.yaw, session.mode || state.followMode);
    session.lastFocusPos = pose.pos;
    session.socket.emit('position', pose);
    if (session.worldView) {
      session.worldView.updatePosition(pose.pos).catch(() => {});
    }
  }

  app.get('/health', (req, res) => {
    res.json({
      ok: true,
      connected: state.connected,
      spawned: state.spawned,
      mc_host: cfg.mcHost,
      mc_port: cfg.mcPort,
      cam_user: cfg.camUser,
      version: state.botVersion || cfg.mcVersion,
      viewer_schema_version: '1.21.4-remapped',
      follow_mode: state.followMode,
      last_target: state.lastTarget,
      bot_pos: state.botPos,
      last_error: state.lastError,
    });
  });

  app.options('/api/mode', (req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.sendStatus(204);
  });

  app.get('/api/mode', (req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    const m = req.query.mode;
    if (m !== undefined) {
      if (m !== 'first_person' && m !== 'third_person') {
        return res.status(400).json({
          ok: false,
          error: "Invalid mode; expected 'first_person' or 'third_person'",
          follow_mode: state.followMode,
        });
      }
      state.followMode = m;
      const target = getActiveTargetPose();
      for (const session of clientSessions) {
        session.mode = m;
        pushPoseToSession(session, target);
      }
    }
    return res.json({ ok: true, follow_mode: state.followMode });
  });

  setupRoutes(app, '');

  function attachSocketToBot(session, bot) {
    if (!bot || !bot.entity || session.worldView) return;
    const proxy = createRemappedSocketProxy(session.socket, remapper);
    session.socket.emit('version', '1.21.4');
    const center = session.lastFocusPos || bot.entity.position;
    const wv = new WorldView(bot.world, cfg.viewDistance, center, proxy);
    session.worldView = wv;
    wv.listenToBot(bot);
    wv.init(center).catch(() => {});

    const onBotMove = () => {
      if (!bot.entity) return;
      state.botPos = [
        Number(bot.entity.position.x.toFixed(2)),
        Number(bot.entity.position.y.toFixed(2)),
        Number(bot.entity.position.z.toFixed(2)),
      ];
      if (!state.lastTarget) {
        pushPoseToSession(session, {
          pos: [bot.entity.position.x, bot.entity.position.y, bot.entity.position.z],
          yaw: bot.entity.yaw,
        });
      }
    };
    session.onBotMove = onBotMove;
    bot.on('move', onBotMove);
    onBotMove();
    if (state.lastTarget) {
      pushPoseToSession(session, getActiveTargetPose());
    }
  }

  function detachSocketFromBot(session, bot) {
    if (session.onBotMove && bot) {
      bot.removeListener('move', session.onBotMove);
    }
    session.onBotMove = null;
    if (session.worldView && bot) {
      try {
        session.worldView.removeListenersFromBot(bot);
      } catch (_) {}
    }
    session.worldView = null;
  }

  io.on('connection', (socket) => {
    const reqUrl = (socket.request && socket.request.url) || '';
    const initialMode = reqUrl.includes('firstPerson=1') ? 'first_person' : null;
    const session = { socket, worldView: null, onBotMove: null, mode: initialMode, lastFocusPos: null };
    clientSessions.add(session);

    if (activeBot && state.spawned) {
      attachSocketToBot(session, activeBot);
    } else if (state.lastTarget) {
      pushPoseToSession(session, getActiveTargetPose());
    }

    socket.on('disconnect', () => {
      detachSocketFromBot(session, activeBot);
      clientSessions.delete(session);
    });
  });

  let reconnectTimer = null;
  function connectBot() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    let bot;
    try {
      bot = mineflayer.createBot({
        host: cfg.mcHost,
        port: cfg.mcPort,
        username: cfg.camUser,
        auth: 'offline',
        version: cfg.mcVersion,
      });
    } catch (err) {
      state.lastError = err.message;
      reconnectTimer = setTimeout(connectBot, 5000);
      return;
    }

    activeBot = bot;
    bot.on('login', () => {
      state.connected = true;
      state.botVersion = bot.version;
      state.lastError = null;
    });

    bot.once('spawn', () => {
      state.spawned = true;
      state.botPos = [
        Number(bot.entity.position.x.toFixed(2)),
        Number(bot.entity.position.y.toFixed(2)),
        Number(bot.entity.position.z.toFixed(2)),
      ];
      console.log(
        `[cam_server] ${cfg.camUser} spawned on ${cfg.mcHost}:${cfg.mcPort} (${bot.version}) at ${state.botPos.join(', ')}`
      );
      for (const session of clientSessions) {
        attachSocketToBot(session, bot);
      }
    });

    bot.on('error', (err) => {
      state.lastError = `${err.code || err.name}: ${err.message}`;
    });

    bot.on('end', () => {
      for (const session of clientSessions) {
        detachSocketFromBot(session, bot);
      }
      state.connected = false;
      state.spawned = false;
      activeBot = null;
      reconnectTimer = setTimeout(connectBot, 5000);
    });
  }

  // Follow target Bot position from mc-live /api/telemetry-pos
  const followTimer = setInterval(async () => {
    if (!cfg.followApi) return;
    const data = await fetchJson(cfg.followApi, 900);
    if (!data || !data.ok || !Array.isArray(data.pos) || data.pos.length < 3) return;

    state.lastTarget = {
      pos: data.pos,
      yaw: typeof data.yaw === 'number' ? data.yaw : 0,
      dimension: data.dimension || 'overworld',
      ts: Date.now(),
    };

    for (const session of clientSessions) {
      pushPoseToSession(session, state.lastTarget);
    }

    // Optional server-side spectator /tp keep-close if LiveCam_01 has OP spectator privileges on 5ease
    if (
      cfg.tpFollow &&
      activeBot &&
      state.spawned &&
      activeBot.entity &&
      activeBot.game &&
      activeBot.game.gameMode === 'spectator' &&
      Date.now() - state.lastTpAt > 2500
    ) {
      const dx = activeBot.entity.position.x - data.pos[0];
      const dy = activeBot.entity.position.y - data.pos[1];
      const dz = activeBot.entity.position.z - data.pos[2];
      if (dx * dx + dy * dy + dz * dz > 24 * 24) {
        state.lastTpAt = Date.now();
        activeBot.chat(
          `/tp @s ${data.pos[0].toFixed(1)} ${(data.pos[1] + 2).toFixed(1)} ${data.pos[2].toFixed(1)}`
        );
      }
    }
  }, 600);

  server.listen(cfg.viewerPort, cfg.viewerHost, () => {
    console.log(
      `[cam_server] Prismarine 1.21.11->1.21.4 remapped WebGL server listening on http://${cfg.viewerHost}:${cfg.viewerPort}`
    );
    connectBot();
  });

  const shutdown = () => {
    clearInterval(followTimer);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (activeBot) {
      try {
        activeBot.quit();
      } catch (_) {}
    }
    server.close(() => process.exit(0));
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  return { app, server, remapper };
}

if (require.main === module) {
  startServer(parseArgs(process.argv));
}

module.exports = {
  buildStateRemapper,
  computeCameraPose,
  createRemappedSocketProxy,
  isSupportedViewerEntityName,
  parseArgs,
  startServer,
};
