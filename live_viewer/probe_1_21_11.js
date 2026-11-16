'use strict';

const fs = require('fs');
const path = require('path');
const { Worker } = require('worker_threads');
const mineflayer = require('mineflayer');
const mcDataLoader = require('minecraft-data');
const ChunksLoader = require('prismarine-chunk');
const { getVersion, supportedVersions } = require('prismarine-viewer/viewer/lib/version');
const { World } = require('prismarine-viewer/viewer/lib/world');
const { getSectionGeometry } = require('prismarine-viewer/viewer/lib/models');

const HOST = process.env.MC_HOST || '10.50.249.97';
const PORT = parseInt(process.env.MC_PORT || '25565', 10);
const USERNAME = process.env.CAM_USER || 'ProbeCam_01';

function compareBlockRegistries() {
  const d4 = mcDataLoader('1.21.4');
  const d11 = mcDataLoader('1.21.11');
  let maxState4 = 0;
  for (const b of d4.blocksArray) {
    if (b.maxStateId > maxState4) maxState4 = b.maxStateId;
  }
  let maxState11 = 0;
  let shiftedBlocks = 0;
  let newBlocks = [];
  let firstShifted = null;
  const sampleMismatches = [];

  for (const b11 of d11.blocksArray) {
    if (b11.maxStateId > maxState11) maxState11 = b11.maxStateId;
    const b4 = d4.blocksByName[b11.name];
    if (!b4) {
      newBlocks.push(b11.name);
    } else if (b4.defaultState !== b11.defaultState) {
      shiftedBlocks++;
      if (!firstShifted) {
        firstShifted = {
          name: b11.name,
          state_1_21_4: b4.defaultState,
          state_1_21_11: b11.defaultState,
        };
      }
      if (
        ['crafting_table', 'furnace', 'chest', 'white_bed', 'torch', 'deepslate', 'deepslate_iron_ore', 'powder_snow'].includes(
          b11.name
        )
      ) {
        const wrongIn4 = d4.blocksByStateId[b11.defaultState];
        sampleMismatches.push({
          block: b11.name,
          state_1_21_11: b11.defaultState,
          state_1_21_4: b4.defaultState,
          decodedAsInStockViewer: wrongIn4 ? wrongIn4.name : 'OUT_OF_RANGE_NULL',
        });
      }
    }
  }

  return {
    prismarineViewerSupportedVersions: supportedVersions,
    mappedViewerVersionFor_1_21_11: getVersion('1.21.11'),
    blocksCount_1_21_4: d4.blocksArray.length,
    blocksCount_1_21_11: d11.blocksArray.length,
    maxStateId_1_21_4: maxState4,
    maxStateId_1_21_11: maxState11,
    newBlocksCount: newBlocks.length,
    newBlocksSample: newBlocks.slice(0, 20),
    shiftedBlocksCount: shiftedBlocks,
    firstShiftedBlock: firstShifted,
    sampleMismatches,
  };
}

function testWorkerBundle(chunkJson, chunkX, chunkZ, sectionY) {
  return new Promise((resolve) => {
    const workerPath = path.join(
      __dirname,
      'node_modules/prismarine-viewer/public/worker.js'
    );
    const blocksStatesPath = path.join(
      __dirname,
      'node_modules/prismarine-viewer/public/blocksStates/1.21.4.json'
    );
    const blocksStates = JSON.parse(fs.readFileSync(blocksStatesPath, 'utf8'));
    const worker = new Worker(workerPath);
    let settled = false;
    const finish = (res) => {
      if (settled) return;
      settled = true;
      worker.terminate().catch(() => {});
      resolve(res);
    };

    const timer = setTimeout(() => {
      finish({ ok: false, error: 'worker_timeout_3000ms' });
    }, 3000);

    worker.on('error', (err) => {
      clearTimeout(timer);
      finish({ ok: false, error: `${err.name}: ${err.message}` });
    });

    worker.on('message', (msg) => {
      if (msg && msg.type === 'geometry') {
        clearTimeout(timer);
        finish({
          ok: true,
          key: msg.key,
          vertexFloats: msg.geometry.positions.length,
          indexCount: msg.geometry.indices.length,
        });
      } else if (msg && msg.type === 'sectionFinished') {
        // Wait briefly in case geometry came right before, otherwise empty section
        setTimeout(() => {
          clearTimeout(timer);
          finish({ ok: true, key: msg.key, vertexFloats: 0, indexCount: 0, note: 'sectionFinished_empty' });
        }, 80);
      }
    });

    worker.postMessage({ type: 'version', version: '1.21.4' });
    worker.postMessage({ type: 'blockStates', json: blocksStates });
    worker.postMessage({ type: 'chunk', x: chunkX, z: chunkZ, chunk: chunkJson });
    worker.postMessage({ type: 'dirty', x: chunkX, y: sectionY, z: chunkZ, value: true });
  });
}

async function probeLiveServer() {
  return new Promise((resolve) => {
    const t0 = Date.now();
    let bot;
    try {
      bot = mineflayer.createBot({
        host: HOST,
        port: PORT,
        username: USERNAME,
        auth: 'offline',
        version: '1.21.11',
        hideErrors: false,
      });
    } catch (err) {
      resolve({ ok: false, stage: 'createBot', error: `${err.name}: ${err.message}` });
      return;
    }

    let settled = false;
    const errors = [];
    const finish = (res) => {
      if (settled) return;
      settled = true;
      try {
        bot.quit();
      } catch (_) {}
      resolve({ ...res, elapsedMs: Date.now() - t0, errors });
    };

    const timeout = setTimeout(() => {
      finish({ ok: false, stage: 'timeout_10s' });
    }, 10000);

    bot.on('error', (err) => {
      errors.push(`${err.name}: ${err.message}`);
      if (err && (err.code === 'ENETUNREACH' || err.code === 'ECONNREFUSED' || err.code === 'ETIMEDOUT')) {
        clearTimeout(timeout);
        finish({ ok: false, stage: 'connect_error', code: err.code });
      }
    });
    bot.on('kicked', (reason) => {
      clearTimeout(timeout);
      finish({ ok: false, stage: 'kicked', reason: String(reason).slice(0, 300) });
    });

    bot.once('spawn', async () => {
      // Wait up to 1.5s for surrounding chunks to arrive
      await new Promise((r) => setTimeout(r, 1200));
      clearTimeout(timeout);

      const pos = bot.entity.position.clone();
      const chunkX = Math.floor(pos.x / 16) * 16;
      const chunkZ = Math.floor(pos.z / 16) * 16;
      const sectionY = Math.floor(pos.y / 16) * 16;
      const column = await bot.world.getColumnAt(pos);

      let stockChunkResult = null;
      let highStateCrashTest = null;
      if (column) {
        const rawJson = column.toJson();
        stockChunkResult = await testWorkerBundle(rawJson, chunkX, chunkZ, sectionY);

        // Also test what happens when a chunk contains leaf_litter (stateId 27644 in 1.21.11, very common in expedition logs!)
        try {
          const blocksStates = JSON.parse(
            fs.readFileSync(
              path.join(__dirname, 'node_modules/prismarine-viewer/public/blocksStates/1.21.4.json'),
              'utf8'
            )
          );
          const w = new World('1.21.4');
          const Chunk4 = ChunksLoader('1.21.4');
          const c4 = new Chunk4();
          // Inject 1.21.11 leaf_litter stateId (27644) into section
          const Vec3 = require('vec3').Vec3;
          c4.setBlockStateId(new Vec3(1, 64, 1), 27644);
          w.addColumn(0, 0, c4.toJson());
          getSectionGeometry(0, 64, 0, w, blocksStates);
          highStateCrashTest = { crashed: false };
        } catch (err) {
          highStateCrashTest = { crashed: true, error: `${err.name}: ${err.message}` };
        }
      }

      finish({
        ok: true,
        negotiatedVersion: bot.version,
        protocolVersion: bot._client && bot._client.protocolVersion,
        gameMode: bot.game && bot.game.gameMode,
        dimension: bot.game && bot.game.dimension,
        minY: bot.game && bot.game.minY,
        height: bot.game && bot.game.height,
        spawnPos: [Number(pos.x.toFixed(2)), Number(pos.y.toFixed(2)), Number(pos.z.toFixed(2))],
        hasCenterColumn: Boolean(column),
        columnSectionsCount: column && column.sections ? column.sections.length : 0,
        stockWorkerBundleTest: stockChunkResult,
        leafLitterOutOfRangeStateTest: highStateCrashTest,
      });
    });
  });
}

async function main() {
  const registryComparison = compareBlockRegistries();
  const liveProbe = await probeLiveServer();
  console.log(JSON.stringify({ registryComparison, liveProbe }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
