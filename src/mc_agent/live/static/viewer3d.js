/**
 * viewer3d.js — Zero-CDN Windowless 3D Voxel & 2D Tactical Radar Engine for mc-live
 *
 * Exposes `window.McViewer3D.create(containerEl, opts)`:
 *   - setMode('first_person' | 'third_person' | 'free_orbit' | 'radar_2d')
 *   - updateState({ status, entities, blocks, markers, trajectory })
 *   - resize()
 *   - destroy()
 *
 * Coordinate & Angle Convention (Mineflayer / Minecraft Java 1.21.11):
 *   +X = East, -X = West, +Y = Up, -Z = North, +Z = South
 *   yaw (rad): 0 faces -Z (North), -PI/2 faces +X (East)
 *   forward horizontal = (-Math.sin(yaw), -Math.cos(yaw))
 */
(function (global) {
  'use strict';

  const TAU = Math.PI * 2;
  const VALID_MODES = new Set(['first_person', 'third_person', 'free_orbit', 'radar_2d']);

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function lerpAngle(a, b, t) {
    let diff = ((b - a + Math.PI) % TAU + TAU) % TAU - Math.PI;
    return a + diff * t;
  }

  function hash2(x, y, seed) {
    let n = (Math.imul(x, 374761393) + Math.imul(y, 668265263) + Math.imul(seed, 1442695041)) | 0;
    n = (n ^ (n >>> 13)) | 0;
    n = Math.imul(n, 1274126177);
    return ((n ^ (n >>> 16)) >>> 0) / 4294967295;
  }

  function normalizeItemName(item) {
    if (!item) return '';
    if (typeof item === 'string') return item.replace(/^minecraft:/, '');
    if (typeof item === 'object' && typeof item.name === 'string') {
      return item.name.replace(/^minecraft:/, '');
    }
    return '';
  }

  function normalizeBlockName(raw) {
    if (!raw || typeof raw !== 'string') return 'stone';
    return raw.toLowerCase().replace(/^minecraft:/, '').trim();
  }

  function extractPos(obj) {
    if (!obj) return null;
    if (Array.isArray(obj) && obj.length >= 3) {
      const x = Number(obj[0]);
      const y = Number(obj[1]);
      const z = Number(obj[2]);
      if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z)) return [x, y, z];
      return null;
    }
    if (Array.isArray(obj.pos) && obj.pos.length >= 3) {
      return extractPos(obj.pos);
    }
    if (typeof obj.x === 'number' && typeof obj.y === 'number' && typeof obj.z === 'number') {
      if (Number.isFinite(obj.x) && Number.isFinite(obj.y) && Number.isFinite(obj.z)) {
        return [obj.x, obj.y, obj.z];
      }
    }
    return null;
  }

  // ============================================================================
  // Minimal 4x4 Column-Major Matrix Math (Zero Dependencies)
  // ============================================================================
  function mat4Perspective(fovRad, aspect, near, far) {
    const f = 1.0 / Math.tan(fovRad * 0.5);
    const nf = 1.0 / (near - far);
    const out = new Float32Array(16);
    out[0] = f / (aspect || 1);
    out[5] = f;
    out[10] = (far + near) * nf;
    out[11] = -1;
    out[14] = 2 * far * near * nf;
    return out;
  }

  function mat4LookAt(eye, center, up) {
    let zx = eye[0] - center[0];
    let zy = eye[1] - center[1];
    let zz = eye[2] - center[2];
    let len = Math.hypot(zx, zy, zz);
    if (len < 1e-6) {
      zz = 1;
      len = 1;
    }
    zx /= len;
    zy /= len;
    zz /= len;

    let xx = up[1] * zz - up[2] * zy;
    let xy = up[2] * zx - up[0] * zz;
    let xz = up[0] * zy - up[1] * zx;
    len = Math.hypot(xx, xy, xz);
    if (len < 1e-6) {
      xx = 1;
      xy = 0;
      xz = 0;
    } else {
      xx /= len;
      xy /= len;
      xz /= len;
    }

    const yx = zy * xz - zz * xy;
    const yy = zz * xx - zx * xz;
    const yz = zx * xy - zy * xx;

    const out = new Float32Array(16);
    out[0] = xx;
    out[1] = yx;
    out[2] = zx;
    out[3] = 0;
    out[4] = xy;
    out[5] = yy;
    out[6] = zy;
    out[7] = 0;
    out[8] = xz;
    out[9] = yz;
    out[10] = zz;
    out[11] = 0;
    out[12] = -(xx * eye[0] + xy * eye[1] + xz * eye[2]);
    out[13] = -(yx * eye[0] + yy * eye[1] + yz * eye[2]);
    out[14] = -(zx * eye[0] + zy * eye[1] + zz * eye[2]);
    out[15] = 1;
    return out;
  }

  function mat4Mul(a, b) {
    const out = new Float32Array(16);
    for (let col = 0; col < 4; col++) {
      const b0 = b[col * 4];
      const b1 = b[col * 4 + 1];
      const b2 = b[col * 4 + 2];
      const b3 = b[col * 4 + 3];
      out[col * 4] = a[0] * b0 + a[4] * b1 + a[8] * b2 + a[12] * b3;
      out[col * 4 + 1] = a[1] * b0 + a[5] * b1 + a[9] * b2 + a[13] * b3;
      out[col * 4 + 2] = a[2] * b0 + a[6] * b1 + a[10] * b2 + a[14] * b3;
      out[col * 4 + 3] = a[3] * b0 + a[7] * b1 + a[11] * b2 + a[15] * b3;
    }
    return out;
  }

  function projectToScreen(vp, x, y, z, width, height) {
    const cx = vp[0] * x + vp[4] * y + vp[8] * z + vp[12];
    const cy = vp[1] * x + vp[5] * y + vp[9] * z + vp[13];
    const cz = vp[2] * x + vp[6] * y + vp[10] * z + vp[14];
    const cw = vp[3] * x + vp[7] * y + vp[11] * z + vp[15];
    if (cw <= 0.05) return null;
    const ndcX = cx / cw;
    const ndcY = cy / cw;
    const ndcZ = cz / cw;
    if (ndcZ < -1.1 || ndcZ > 1.1) return null;
    return {
      x: (ndcX * 0.5 + 0.5) * width,
      y: (0.5 - ndcY * 0.5) * height,
      w: cw,
      inBounds: ndcX >= -1.15 && ndcX <= 1.15 && ndcY >= -1.15 && ndcY <= 1.15,
    };
  }

  // ============================================================================
  // Procedural 16x16 Pixel-Art Minecraft Atlas (8x8 grid = 128x128 RGBA)
  // ============================================================================
  const ATLAS_COLS = 8;
  const TILE_PX = 16;
  const ATLAS_SIZE = ATLAS_COLS * TILE_PX;

  const TILE = {
    SOLID_WHITE: 0,
    GRASS_TOP: 1,
    GRASS_SIDE: 2,
    DIRT: 3,
    STONE: 4,
    COBBLESTONE: 5,
    DEEPSLATE: 6,
    OAK_LOG_SIDE: 7,
    LOG_TOP: 8,
    BIRCH_LOG_SIDE: 9,
    SPRUCE_LOG_SIDE: 10,
    PLANKS: 11,
    LEAVES: 12,
    WATER: 13,
    LAVA: 14,
    POWDER_SNOW: 15,
    SAND: 16,
    GRAVEL: 17,
    COAL_ORE: 18,
    IRON_ORE: 19,
    COPPER_ORE: 20,
    GOLD_ORE: 21,
    DIAMOND_ORE: 22,
    REDSTONE_ORE: 23,
    CRAFT_TOP: 24,
    CRAFT_SIDE: 25,
    FURNACE_FRONT: 26,
    FURNACE_SIDE: 27,
    CHEST_FRONT: 28,
    CHEST_SIDE: 29,
    BED_TOP: 30,
    NETHERRACK: 31,
    OBSIDIAN: 32,
    END_STONE: 33,
    FARMLAND: 34,
    SNOW: 35,
    DEEPSLATE_IRON: 36,
    TORCH: 37,
  };

  function buildProceduralAtlas() {
    const data = new Uint8Array(ATLAS_SIZE * ATLAS_SIZE * 4);

    function setPx(tileId, u, v, r, g, b, a) {
      const tx = (tileId % ATLAS_COLS) * TILE_PX + u;
      const ty = Math.floor(tileId / ATLAS_COLS) * TILE_PX + v;
      const idx = (ty * ATLAS_SIZE + tx) * 4;
      data[idx] = clamp(Math.round(r), 0, 255);
      data[idx + 1] = clamp(Math.round(g), 0, 255);
      data[idx + 2] = clamp(Math.round(b), 0, 255);
      data[idx + 3] = clamp(Math.round(a === undefined ? 255 : a), 0, 255);
    }

    function fillNoise(tileId, base, spread, seed, alpha) {
      for (let v = 0; v < TILE_PX; v++) {
        for (let u = 0; u < TILE_PX; u++) {
          const n = (hash2(u, v, seed) - 0.5) * 2 * spread;
          setPx(tileId, u, v, base[0] + n, base[1] + n, base[2] + n, alpha);
        }
      }
    }

    function paintOreFlecks(tileId, baseTileSeed, baseRgb, fleckRgb, fleckRgb2) {
      fillNoise(tileId, baseRgb, 14, baseTileSeed, 255);
      const spots = [
        [3, 3], [4, 3], [3, 4],
        [10, 4], [11, 4], [11, 5],
        [6, 8], [7, 8], [7, 9],
        [12, 11], [13, 11], [12, 12],
        [3, 12], [4, 12],
      ];
      spots.forEach(([u, v], idx) => {
        const c = idx % 3 === 0 && fleckRgb2 ? fleckRgb2 : fleckRgb;
        setPx(tileId, u, v, c[0], c[1], c[2], 255);
      });
    }

    // 0: Solid white (for vertex-colored geometry: bot, entities, trajectory, beacons)
    fillNoise(TILE.SOLID_WHITE, [255, 255, 255], 0, 1, 255);

    // 1: Grass top
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const n = (hash2(u, v, 11) - 0.5) * 26;
        setPx(TILE.GRASS_TOP, u, v, 92 + n * 0.6, 168 + n, 62 + n * 0.5, 255);
      }
    }

    // 3: Dirt
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const n = (hash2(u, v, 31) - 0.5) * 28;
        const pebble = hash2(u, v, 37) > 0.9 ? 22 : 0;
        setPx(TILE.DIRT, u, v, 134 + n + pebble, 96 + n * 0.8 + pebble, 66 + n * 0.6 + pebble, 255);
      }
    }

    // 2: Grass side (v=0 is top of side face)
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const overhang = 2 + Math.floor(hash2(u, 0, 23) * 3);
        if (v <= overhang) {
          const n = (hash2(u, v, 29) - 0.5) * 24;
          setPx(TILE.GRASS_SIDE, u, v, 90 + n * 0.6, 164 + n, 58 + n * 0.5, 255);
        } else {
          const n = (hash2(u, v, 31) - 0.5) * 28;
          setPx(TILE.GRASS_SIDE, u, v, 134 + n, 96 + n * 0.8, 66 + n * 0.6, 255);
        }
      }
    }

    // 4: Stone (horizontal strata noise)
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const streak = (hash2(Math.floor(u / 2), v, 41) - 0.5) * 26;
        setPx(TILE.STONE, u, v, 125 + streak, 125 + streak, 128 + streak, 255);
      }
    }

    // 5: Cobblestone
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const cell = ((u + (v % 4 >= 2 ? 2 : 0)) % 4 === 0) || (v % 4 === 0);
        const n = (hash2(u, v, 53) - 0.5) * 24;
        const base = cell ? 76 : 122;
        setPx(TILE.COBBLESTONE, u, v, base + n, base + n, base + 2 + n, 255);
      }
    }

    // 6: Deepslate
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const diag = ((u + v) % 4 === 0) ? -12 : 6;
        const n = (hash2(u, v, 61) - 0.5) * 16;
        setPx(TILE.DEEPSLATE, u, v, 72 + diag + n, 74 + diag + n, 82 + diag + n, 255);
      }
    }

    // 7, 8, 9, 10: Logs
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const bark = Math.sin(u * 1.4) * 12 + (hash2(u, v, 71) - 0.5) * 16;
        setPx(TILE.OAK_LOG_SIDE, u, v, 104 + bark, 82 + bark * 0.8, 49 + bark * 0.6, 255);
        setPx(TILE.SPRUCE_LOG_SIDE, u, v, 64 + bark * 0.7, 45 + bark * 0.6, 28 + bark * 0.4, 255);
        const birchSpot = hash2(Math.floor(u / 2), v, 97) > 0.82 ? -130 : 0;
        const bn = (hash2(u, v, 89) - 0.5) * 14;
        setPx(TILE.BIRCH_LOG_SIDE, u, v, 218 + bn + birchSpot, 214 + bn + birchSpot, 204 + bn + birchSpot, 255);

        const dx = u - 7.5;
        const dy = v - 7.5;
        const dist = Math.max(Math.abs(dx), Math.abs(dy));
        if (dist >= 6.5) {
          setPx(TILE.LOG_TOP, u, v, 102, 80, 48, 255);
        } else {
          const ring = Math.round(dist) % 2 === 0 ? 12 : -8;
          setPx(TILE.LOG_TOP, u, v, 174 + ring, 142 + ring, 88 + ring, 255);
        }
      }
    }

    // 11: Planks
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const seam = (v % 4 === 0) || ((u === ((Math.floor(v / 4) * 7) % 16)) && v % 4 !== 0);
        const n = (hash2(u, v, 113) - 0.5) * 16;
        const b = seam ? -28 : 0;
        setPx(TILE.PLANKS, u, v, 168 + b + n, 135 + b + n * 0.8, 84 + b + n * 0.6, 255);
      }
    }

    // 12: Leaves
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const h = hash2(u, v, 127);
        const shade = h > 0.75 ? -26 : h < 0.25 ? 20 : 0;
        setPx(TILE.LEAVES, u, v, 58 + shade * 0.6, 128 + shade, 44 + shade * 0.5, 255);
      }
    }

    // 13: Water (translucent)
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const wave = ((u + v * 2) % 6 === 0) ? 26 : (hash2(u, v, 131) - 0.5) * 14;
        setPx(TILE.WATER, u, v, 42 + wave * 0.6, 118 + wave, 218 + wave, 185);
      }
    }

    // 14: Lava
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const hot = hash2(Math.floor(u / 2), Math.floor(v / 2), 149);
        setPx(TILE.LAVA, u, v, 235 + hot * 20, 95 + hot * 115, 18 + hot * 35, 245);
      }
    }

    // 15 & 35: Powder snow & Snow
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const frost = hash2(u, v, 157);
        if (frost > 0.78) {
          setPx(TILE.POWDER_SNOW, u, v, 178, 228, 252, 245);
        } else {
          setPx(TILE.POWDER_SNOW, u, v, 236, 246, 255, 245);
        }
        const sn = (frost - 0.5) * 12;
        setPx(TILE.SNOW, u, v, 244 + sn, 248 + sn, 252 + sn, 255);
      }
    }

    // 16 & 17: Sand & Gravel
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const n = (hash2(u, v, 163) - 0.5) * 20;
        setPx(TILE.SAND, u, v, 219 + n, 207 + n, 160 + n, 255);
        const gn = (hash2(u, v, 173) - 0.5) * 42;
        setPx(TILE.GRAVEL, u, v, 136 + gn, 130 + gn, 128 + gn, 255);
      }
    }

    // 18..23, 36: Ores
    paintOreFlecks(TILE.COAL_ORE, 41, [125, 125, 128], [28, 28, 32], [48, 48, 52]);
    paintOreFlecks(TILE.IRON_ORE, 41, [125, 125, 128], [218, 178, 146], [186, 138, 106]);
    paintOreFlecks(TILE.COPPER_ORE, 41, [125, 125, 128], [218, 116, 68], [72, 168, 132]);
    paintOreFlecks(TILE.GOLD_ORE, 41, [125, 125, 128], [250, 214, 62], [224, 168, 28]);
    paintOreFlecks(TILE.DIAMOND_ORE, 41, [125, 125, 128], [92, 242, 235], [188, 255, 252]);
    paintOreFlecks(TILE.REDSTONE_ORE, 41, [125, 125, 128], [228, 36, 36], [255, 92, 92]);
    paintOreFlecks(TILE.DEEPSLATE_IRON, 61, [74, 76, 84], [222, 182, 150], [186, 138, 106]);

    // 24 & 25: Crafting Table
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const border = u < 2 || u > 13 || v < 2 || v > 13;
        const grid = u === 5 || u === 10 || v === 5 || v === 10;
        if (border) setPx(TILE.CRAFT_TOP, u, v, 118, 78, 42, 255);
        else if (grid) setPx(TILE.CRAFT_TOP, u, v, 76, 48, 26, 255);
        else setPx(TILE.CRAFT_TOP, u, v, 176, 132, 82, 255);

        if (v < 3) setPx(TILE.CRAFT_SIDE, u, v, 122, 80, 44, 255);
        else if ((u >= 4 && u <= 6 && v >= 5 && v <= 12) || (u >= 10 && u <= 12 && v >= 6 && v <= 11)) {
          setPx(TILE.CRAFT_SIDE, u, v, 168, 176, 186, 255); // iron tool silhouette
        } else {
          const n = (hash2(u, v, 251) - 0.5) * 16;
          setPx(TILE.CRAFT_SIDE, u, v, 156 + n, 116 + n * 0.8, 70 + n * 0.6, 255);
        }
      }
    }

    // 26 & 27: Furnace
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const border = u === 0 || u === 15 || v === 0 || v === 15;
        const n = (hash2(u, v, 263) - 0.5) * 16;
        setPx(TILE.FURNACE_SIDE, u, v, (border ? 82 : 118) + n, (border ? 82 : 118) + n, (border ? 86 : 122) + n, 255);
        if (u >= 4 && u <= 11 && v >= 4 && v <= 7) {
          setPx(TILE.FURNACE_FRONT, u, v, 36, 36, 40, 255);
        } else if (u >= 4 && u <= 11 && v >= 10 && v <= 13) {
          setPx(TILE.FURNACE_FRONT, u, v, 242, 136, 38, 255);
        } else {
          setPx(TILE.FURNACE_FRONT, u, v, (border ? 82 : 118) + n, (border ? 82 : 118) + n, (border ? 86 : 122) + n, 255);
        }
      }
    }

    // 28 & 29: Chest
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const border = u === 0 || u === 15 || v === 0 || v === 15 || v === 5;
        const c = border ? [82, 54, 24] : [182, 130, 52];
        setPx(TILE.CHEST_SIDE, u, v, c[0], c[1], c[2], 255);
        if (u >= 7 && u <= 8 && v >= 4 && v <= 7) {
          setPx(TILE.CHEST_FRONT, u, v, 218, 224, 230, 255);
        } else {
          setPx(TILE.CHEST_FRONT, u, v, c[0], c[1], c[2], 255);
        }
      }
    }

    // 30: Bed
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        if (v < 5) setPx(TILE.BED_TOP, u, v, 242, 244, 248, 255);
        else setPx(TILE.BED_TOP, u, v, 206, 56, 56, 255);
      }
    }

    // 31, 32, 33, 34, 37
    fillNoise(TILE.NETHERRACK, [118, 46, 46], 24, 311, 255);
    fillNoise(TILE.OBSIDIAN, [28, 20, 44], 14, 317, 255);
    fillNoise(TILE.END_STONE, [222, 224, 168], 18, 331, 255);
    for (let v = 0; v < 16; v++) {
      for (let u = 0; u < 16; u++) {
        const furrow = u % 4 === 0 ? -24 : 0;
        setPx(TILE.FARMLAND, u, v, 108 + furrow, 72 + furrow, 44 + furrow, 255);
        setPx(TILE.TORCH, u, v, v < 5 ? 255 : 152, v < 5 ? 216 : 112, v < 5 ? 78 : 56, 255);
      }
    }

    return data;
  }

  function tileUV(tileId) {
    const col = tileId % ATLAS_COLS;
    const row = Math.floor(tileId / ATLAS_COLS);
    const pad = 0.12 / ATLAS_SIZE;
    return [
      col / ATLAS_COLS + pad,
      row / ATLAS_COLS + pad,
      (col + 1) / ATLAS_COLS - pad,
      (row + 1) / ATLAS_COLS - pad,
    ];
  }

  function classifyBlock(rawName) {
    const n = normalizeBlockName(rawName);
    if (n === 'air' || n === 'cave_air' || n === 'void_air') return null;

    if (n === 'grass_block' || n === 'podzol' || n === 'mycelium') {
      return { top: TILE.GRASS_TOP, side: TILE.GRASS_SIDE, bottom: TILE.DIRT, tint: [1, 1, 1], radar: '#4fa83d', opaque: true, height: 1 };
    }
    if (n === 'farmland' || n === 'dirt_path') {
      return { top: TILE.FARMLAND, side: TILE.DIRT, bottom: TILE.DIRT, tint: [1, 1, 1], radar: '#7c5333', opaque: false, height: 0.94 };
    }
    if (n.includes('dirt') || n === 'mud' || n === 'rooted_dirt') {
      return { top: TILE.DIRT, side: TILE.DIRT, bottom: TILE.DIRT, tint: [1, 1, 1], radar: '#866042', opaque: true, height: 1 };
    }
    if (n === 'powder_snow') {
      return { top: TILE.POWDER_SNOW, side: TILE.POWDER_SNOW, bottom: TILE.POWDER_SNOW, tint: [0.92, 0.98, 1.0], radar: '#aee6ff', opaque: false, height: 1 };
    }
    if (n === 'snow' || n === 'snow_block' || n.includes('ice')) {
      const h = n === 'snow' ? 0.18 : 1;
      return { top: TILE.SNOW, side: TILE.SNOW, bottom: TILE.SNOW, tint: [1, 1, 1], radar: '#eef6ff', opaque: h === 1, height: h };
    }
    if (n.includes('water') || n === 'kelp' || n === 'seagrass' || n === 'bubble_column') {
      return { top: TILE.WATER, side: TILE.WATER, bottom: TILE.WATER, tint: [1, 1, 1], radar: '#2c78dc', opaque: false, translucent: true, height: 0.88 };
    }
    if (n.includes('lava') || n.includes('fire') || n === 'magma_block') {
      return { top: TILE.LAVA, side: TILE.LAVA, bottom: TILE.LAVA, tint: [1.15, 1.05, 1.0], radar: '#f97316', opaque: false, emissive: 1.0, height: n.includes('lava') ? 0.88 : 0.6 };
    }
    if (n.includes('diamond_ore')) {
      return { top: TILE.DIAMOND_ORE, side: TILE.DIAMOND_ORE, bottom: TILE.DIAMOND_ORE, tint: [1, 1, 1], radar: '#38f2e8', opaque: true, height: 1 };
    }
    if (n.includes('iron_ore')) {
      const t = n.includes('deepslate') ? TILE.DEEPSLATE_IRON : TILE.IRON_ORE;
      return { top: t, side: t, bottom: t, tint: [1, 1, 1], radar: '#e2b18c', opaque: true, height: 1 };
    }
    if (n.includes('coal_ore')) {
      return { top: TILE.COAL_ORE, side: TILE.COAL_ORE, bottom: TILE.COAL_ORE, tint: [1, 1, 1], radar: '#3c3f46', opaque: true, height: 1 };
    }
    if (n.includes('copper_ore')) {
      return { top: TILE.COPPER_ORE, side: TILE.COPPER_ORE, bottom: TILE.COPPER_ORE, tint: [1, 1, 1], radar: '#df7844', opaque: true, height: 1 };
    }
    if (n.includes('gold_ore')) {
      return { top: TILE.GOLD_ORE, side: TILE.GOLD_ORE, bottom: TILE.GOLD_ORE, tint: [1, 1, 1], radar: '#facc15', opaque: true, height: 1 };
    }
    if (n.includes('redstone_ore') || n.includes('emerald_ore') || n.includes('lapis_ore')) {
      const tint = n.includes('emerald') ? [0.4, 1.1, 0.5] : n.includes('lapis') ? [0.45, 0.65, 1.2] : [1, 1, 1];
      return { top: TILE.REDSTONE_ORE, side: TILE.REDSTONE_ORE, bottom: TILE.REDSTONE_ORE, tint, radar: '#ef4444', opaque: true, height: 1 };
    }
    if (n === 'crafting_table') {
      return { top: TILE.CRAFT_TOP, side: TILE.CRAFT_SIDE, bottom: TILE.PLANKS, tint: [1, 1, 1], radar: '#c68b45', opaque: true, height: 1 };
    }
    if (n.includes('furnace') || n === 'smoker') {
      return { top: TILE.FURNACE_SIDE, side: TILE.FURNACE_SIDE, front: TILE.FURNACE_FRONT, bottom: TILE.FURNACE_SIDE, tint: [1, 1, 1], radar: '#f59e0b', opaque: true, height: 1 };
    }
    if (n.includes('chest') || n === 'barrel') {
      return { top: TILE.CHEST_SIDE, side: TILE.CHEST_SIDE, front: TILE.CHEST_FRONT, bottom: TILE.CHEST_SIDE, tint: [1, 1, 1], radar: '#d99326', opaque: false, height: 0.88 };
    }
    if (n.endsWith('_bed') || n === 'bed') {
      return { top: TILE.BED_TOP, side: TILE.PLANKS, bottom: TILE.PLANKS, tint: [1, 1, 1], radar: '#ef4444', opaque: false, height: 0.56 };
    }
    if (n.includes('birch_log') || n.includes('pale_oak_log')) {
      return { top: TILE.LOG_TOP, side: TILE.BIRCH_LOG_SIDE, bottom: TILE.LOG_TOP, tint: [1, 1, 1], radar: '#dcd8cc', opaque: true, height: 1 };
    }
    if (n.includes('spruce_log') || n.includes('dark_oak_log')) {
      return { top: TILE.LOG_TOP, side: TILE.SPRUCE_LOG_SIDE, bottom: TILE.LOG_TOP, tint: [1, 1, 1], radar: '#4b341f', opaque: true, height: 1 };
    }
    if (n.includes('log') || n.includes('wood') || n.includes('stem')) {
      return { top: TILE.LOG_TOP, side: TILE.OAK_LOG_SIDE, bottom: TILE.LOG_TOP, tint: [1, 1, 1], radar: '#6e5332', opaque: true, height: 1 };
    }
    if (n.includes('planks') || n.includes('fence') || n.includes('door') || n.includes('slab') || n.includes('stairs') && n.includes('oak')) {
      return { top: TILE.PLANKS, side: TILE.PLANKS, bottom: TILE.PLANKS, tint: [1, 1, 1], radar: '#a88754', opaque: true, height: n.includes('slab') ? 0.5 : 1 };
    }
    if (n === 'leaf_litter') {
      return { top: TILE.LEAVES, side: TILE.LEAVES, bottom: TILE.LEAVES, tint: [0.85, 0.62, 0.36], radar: '#7c5d32', opaque: false, height: 0.12 };
    }
    if (n.includes('leaves') || n.includes('grass') || n.includes('bush') || n === 'wheat' || n.includes('sapling') || n.includes('vine')) {
      const h = (n.includes('grass') || n === 'wheat' || n.includes('bush') || n.includes('sapling')) && n !== 'grass_block' ? 0.55 : 1;
      return { top: TILE.LEAVES, side: TILE.LEAVES, bottom: TILE.LEAVES, tint: [1, 1, 1], radar: '#3d8b2f', opaque: h === 1, height: h };
    }
    if (n.includes('sand')) {
      return { top: TILE.SAND, side: TILE.SAND, bottom: TILE.SAND, tint: [1, 1, 1], radar: '#dbd0a0', opaque: true, height: 1 };
    }
    if (n === 'gravel' || n === 'clay') {
      return { top: TILE.GRAVEL, side: TILE.GRAVEL, bottom: TILE.GRAVEL, tint: [1, 1, 1], radar: '#888280', opaque: true, height: 1 };
    }
    if (n.includes('cobble')) {
      return { top: TILE.COBBLESTONE, side: TILE.COBBLESTONE, bottom: TILE.COBBLESTONE, tint: [1, 1, 1], radar: '#757578', opaque: true, height: 1 };
    }
    if (n.includes('deepslate') || n === 'tuff' || n === 'bedrock') {
      return { top: TILE.DEEPSLATE, side: TILE.DEEPSLATE, bottom: TILE.DEEPSLATE, tint: [1, 1, 1], radar: '#4a4c54', opaque: true, height: 1 };
    }
    if (n.includes('netherrack') || n.includes('crimson') || n.includes('nether_bricks')) {
      return { top: TILE.NETHERRACK, side: TILE.NETHERRACK, bottom: TILE.NETHERRACK, tint: [1, 1, 1], radar: '#762e2e', opaque: true, height: 1 };
    }
    if (n.includes('obsidian') || n.includes('portal')) {
      return { top: TILE.OBSIDIAN, side: TILE.OBSIDIAN, bottom: TILE.OBSIDIAN, tint: [1, 1, 1], radar: '#2c1847', opaque: true, height: 1 };
    }
    if (n.includes('end_stone')) {
      return { top: TILE.END_STONE, side: TILE.END_STONE, bottom: TILE.END_STONE, tint: [1, 1, 1], radar: '#dedfa8', opaque: true, height: 1 };
    }
    if (n.includes('torch') || n.includes('lantern') || n === 'glowstone') {
      const h = n.includes('torch') ? 0.62 : 1;
      const w = n.includes('torch') ? 0.18 : 1;
      return { top: TILE.TORCH, side: TILE.TORCH, bottom: TILE.TORCH, tint: [1.2, 1.1, 0.9], radar: '#fde047', opaque: false, emissive: 1, height: h, width: w };
    }
    return { top: TILE.STONE, side: TILE.STONE, bottom: TILE.STONE, tint: [1, 1, 1], radar: '#7d7d80', opaque: true, height: 1 };
  }

  // ============================================================================
  // Geometry Builder Helpers (Interleaved: x, y, z, u, v, r, g, b, a = 9 floats/vertex)
  // ============================================================================
  class MeshBuilder {
    constructor(initialFloats = 4096) {
      this.buf = new Float32Array(initialFloats);
      this.len = 0;
    }

    ensure(extra) {
      if (this.len + extra <= this.buf.length) return;
      let next = this.buf.length * 2;
      while (next < this.len + extra) next *= 2;
      const grown = new Float32Array(next);
      grown.set(this.buf.subarray(0, this.len));
      this.buf = grown;
    }

    pushVertex(x, y, z, u, v, r, g, b, a) {
      this.ensure(9);
      const o = this.len;
      this.buf[o] = x;
      this.buf[o + 1] = y;
      this.buf[o + 2] = z;
      this.buf[o + 3] = u;
      this.buf[o + 4] = v;
      this.buf[o + 5] = r;
      this.buf[o + 6] = g;
      this.buf[o + 7] = b;
      this.buf[o + 8] = a;
      this.len += 9;
    }

    pushQuad(p0, p1, p2, p3, uv, r, g, b, a) {
      const [u0, v0, u1, v1] = uv;
      this.pushVertex(p0[0], p0[1], p0[2], u0, v1, r, g, b, a);
      this.pushVertex(p1[0], p1[1], p1[2], u1, v1, r, g, b, a);
      this.pushVertex(p2[0], p2[1], p2[2], u1, v0, r, g, b, a);

      this.pushVertex(p0[0], p0[1], p0[2], u0, v1, r, g, b, a);
      this.pushVertex(p2[0], p2[1], p2[2], u1, v0, r, g, b, a);
      this.pushVertex(p3[0], p3[1], p3[2], u0, v0, r, g, b, a);
    }

    addBlockBox(x0, y0, z0, x1, y1, z1, spec, cullMask, dayBrightness) {
      const tint = spec.tint || [1, 1, 1];
      const alpha = spec.translucent ? 0.74 : 1.0;
      const light = spec.emissive ? 1.08 : dayBrightness;
      const tr = tint[0] * light;
      const tg = tint[1] * light;
      const tb = tint[2] * light;

      // Top (+Y)
      if (!(cullMask & 1)) {
        this.pushQuad(
          [x0, y1, z1], [x1, y1, z1], [x1, y1, z0], [x0, y1, z0],
          tileUV(spec.top), tr * 1.0, tg * 1.0, tb * 1.0, alpha
        );
      }
      // Bottom (-Y)
      if (!(cullMask & 2)) {
        this.pushQuad(
          [x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1],
          tileUV(spec.bottom), tr * 0.52, tg * 0.52, tb * 0.52, alpha
        );
      }
      // North (-Z)
      if (!(cullMask & 4)) {
        this.pushQuad(
          [x1, y0, z0], [x0, y0, z0], [x0, y1, z0], [x1, y1, z0],
          tileUV(spec.front || spec.side), tr * 0.84, tg * 0.84, tb * 0.84, alpha
        );
      }
      // South (+Z)
      if (!(cullMask & 8)) {
        this.pushQuad(
          [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
          tileUV(spec.side), tr * 0.80, tg * 0.80, tb * 0.80, alpha
        );
      }
      // East (+X)
      if (!(cullMask & 16)) {
        this.pushQuad(
          [x1, y0, z1], [x1, y0, z0], [x1, y1, z0], [x1, y1, z1],
          tileUV(spec.side), tr * 0.70, tg * 0.70, tb * 0.70, alpha
        );
      }
      // West (-X)
      if (!(cullMask & 32)) {
        this.pushQuad(
          [x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0],
          tileUV(spec.side), tr * 0.66, tg * 0.66, tb * 0.66, alpha
        );
      }
    }

    /**
     * Add an articulated limb/part box rotated around a world pivot by yaw (Y) and local pitch (X).
     * Note: Local -Z is forward so yaw=0 faces -Z directly!
     */
    addOrientedBox(pivot, minLocal, maxLocal, yaw, pitch, roll, rgb, alpha) {
      const cy = Math.cos(yaw);
      const sy = Math.sin(yaw);
      const cp = Math.cos(pitch || 0);
      const sp = Math.sin(pitch || 0);
      const cr = Math.cos(roll || 0);
      const sr = Math.sin(roll || 0);

      const xf = (lx, ly, lz) => {
        // 1. Roll around Z
        const rx = lx * cr - ly * sr;
        const ry = lx * sr + ly * cr;
        const rz = lz;
        // 2. Pitch around X (positive pitch tilts local -Z UP -> +Y)
        const px = rx;
        const py = ry * cp - rz * sp;
        const pz = ry * sp + rz * cp;
        // 3. Yaw around Y (yaw=0 keeps -Z -> -Z; yaw=-PI/2 maps -Z -> +X because wx = px*cy + pz*sy -> (0) + (-1)*(-1) = +1!)
        const wx = px * cy + pz * sy;
        const wy = py;
        const wz = -px * sy + pz * cy;
        return [pivot[0] + wx, pivot[1] + wy, pivot[2] + wz];
      };

      const [x0, y0, z0] = minLocal;
      const [x1, y1, z1] = maxLocal;
      const c000 = xf(x0, y0, z0);
      const c100 = xf(x1, y0, z0);
      const c010 = xf(x0, y1, z0);
      const c110 = xf(x1, y1, z0);
      const c001 = xf(x0, y0, z1);
      const c101 = xf(x1, y0, z1);
      const c011 = xf(x0, y1, z1);
      const c111 = xf(x1, y1, z1);

      const uv = tileUV(TILE.SOLID_WHITE);
      const [r, g, b] = rgb;
      const a = alpha === undefined ? 1.0 : alpha;
      // Top
      this.pushQuad(c011, c111, c110, c010, uv, r * 1.05, g * 1.05, b * 1.05, a);
      // Bottom
      this.pushQuad(c000, c100, c101, c001, uv, r * 0.55, g * 0.55, b * 0.55, a);
      // Front (-Z)
      this.pushQuad(c100, c000, c010, c110, uv, r * 0.96, g * 0.96, b * 0.96, a);
      // Back (+Z)
      this.pushQuad(c001, c101, c111, c011, uv, r * 0.78, g * 0.78, b * 0.78, a);
      // Right (+X)
      this.pushQuad(c101, c100, c110, c111, uv, r * 0.72, g * 0.72, b * 0.72, a);
      // Left (-X)
      this.pushQuad(c000, c001, c011, c010, uv, r * 0.68, g * 0.68, b * 0.68, a);
    }

    addRibbonSegment(a, b, halfWidth, rgbA, rgbB, alpha) {
      const dx = b[0] - a[0];
      const dz = b[2] - a[2];
      const len = Math.hypot(dx, dz);
      if (len < 0.02) return;
      const nx = (-dz / len) * halfWidth;
      const nz = (dx / len) * halfWidth;
      const uv = tileUV(TILE.SOLID_WHITE);

      const p0 = [a[0] - nx, a[1], a[2] - nz];
      const p1 = [a[0] + nx, a[1], a[2] + nz];
      const p2 = [b[0] + nx, b[1], b[2] + nz];
      const p3 = [b[0] - nx, b[1], b[2] - nz];

      const [u0, v0, u1, v1] = uv;
      this.pushVertex(p0[0], p0[1], p0[2], u0, v1, rgbA[0], rgbA[1], rgbA[2], alpha);
      this.pushVertex(p1[0], p1[1], p1[2], u1, v1, rgbA[0], rgbA[1], rgbA[2], alpha);
      this.pushVertex(p2[0], p2[1], p2[2], u1, v0, rgbB[0], rgbB[1], rgbB[2], alpha);

      this.pushVertex(p0[0], p0[1], p0[2], u0, v1, rgbA[0], rgbA[1], rgbA[2], alpha);
      this.pushVertex(p2[0], p2[1], p2[2], u1, v0, rgbB[0], rgbB[1], rgbB[2], alpha);
      this.pushVertex(p3[0], p3[1], p3[2], u0, v0, rgbB[0], rgbB[1], rgbB[2], alpha);
    }

    subarray() {
      return this.buf.subarray(0, this.len);
    }

    get vertexCount() {
      return (this.len / 9) | 0;
    }
  }

  function itemMaterialColor(itemName) {
    if (!itemName) return [0.85, 0.72, 0.56];
    if (itemName.includes('diamond')) return [0.35, 0.95, 0.92];
    if (itemName.includes('iron') || itemName === 'shears' || itemName === 'bucket') return [0.86, 0.88, 0.91];
    if (itemName.includes('gold')) return [0.98, 0.84, 0.25];
    if (itemName.includes('stone') || itemName.includes('cobble') || itemName === 'furnace') return [0.58, 0.60, 0.63];
    if (itemName.includes('copper')) return [0.86, 0.48, 0.28];
    if (itemName.includes('wood') || itemName.includes('oak') || itemName === 'stick' || itemName === 'crafting_table') {
      return [0.68, 0.50, 0.28];
    }
    if (itemName.includes('torch')) return [1.0, 0.82, 0.24];
    return [0.76, 0.82, 0.88];
  }

  // ============================================================================
  // Main Viewer Instance
  // ============================================================================
  function createViewer(containerEl, opts = {}) {
    if (!containerEl || typeof containerEl.appendChild !== 'function') {
      throw new Error('McViewer3D.create requires a valid DOM container element');
    }

    const root = document.createElement('div');
    root.className = 'mc-viewer3d-root';
    root.style.cssText =
      'position:relative;width:100%;height:100%;min-height:180px;overflow:hidden;user-select:none;touch-action:none;background:#0b1220;';

    const glCanvas = document.createElement('canvas');
    glCanvas.className = 'mc-viewer3d-webgl';
    glCanvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;';

    const hudCanvas = document.createElement('canvas');
    hudCanvas.className = 'mc-viewer3d-overlay';
    hudCanvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none;';

    root.appendChild(glCanvas);
    root.appendChild(hudCanvas);
    containerEl.appendChild(root);

    const ctx2d = hudCanvas.getContext('2d');
    let gl = null;
    const glAttrs = { antialias: true, alpha: false, depth: true, preserveDrawingBuffer: true };
    try {
      gl =
        glCanvas.getContext('webgl', glAttrs) ||
        glCanvas.getContext('experimental-webgl', glAttrs);
    } catch (_) {
      gl = null;
    }

    // WebGL pipeline resources
    let glRes = null;
    if (gl) {
      glRes = initWebGL(gl);
    }

    let mode = VALID_MODES.has(opts.mode) ? opts.mode : 'third_person';
    let destroyed = false;
    let rafId = null;
    let paused = Boolean(opts.paused);
    let cssWidth = 640;
    let cssHeight = 400;
    let dpr = 1;

    // Camera interaction state
    const camCtrl = {
      tpYawOffset: 0,
      tpPitch: 0.42,
      tpDist: 7.5,
      orbitYaw: -0.65,
      orbitPitch: 0.82,
      orbitDist: 22.0,
      orbitPan: [0, 0, 0],
      radarZoom: 8.5, // pixels per block
      radarPan: [0, 0], // world XZ offset
      dragging: false,
      dragBtn: 0,
      lastX: 0,
      lastY: 0,
    };

    // World & smoothed Bot state
    const worldState = {
      hasTelemetry: false,
      status: {
        pos: [0.5, 64, 0.5],
        yaw: 0,
        pitch: 0,
        health: 20,
        food: 20,
        oxygen: 20,
        held: null,
        offhand: null,
        in_water: false,
        submerged: false,
        in_lava: false,
        on_fire: false,
        sneaking: false,
        blocking: false,
        busy: null,
        time_of_day: 6000,
        is_day: true,
        dimension: 'overworld',
      },
      entities: [],
      blocks: [],
      markers: [],
      showHurtMarkers: false,
      trajectory: [],
      blocksSig: '',
      blocksOpaqueMesh: new MeshBuilder(16384),
      blocksTransMesh: new MeshBuilder(4096),
      blocksBufferDirty: true,
      radarBlockMap: new Map(),
      walkPhase: 0,
      hurtFlashUntil: 0,
      lastHealth: 20,
    };

    const smoothBot = {
      initialized: false,
      pos: [0.5, 64, 0.5],
      yaw: 0,
      pitch: 0,
      movingAmount: 0,
    };

    const dynOpaque = new MeshBuilder(8192);
    const dynTrans = new MeshBuilder(4096);
    const gridLines = new Float32Array(1024 * 7); // x,y,z, r,g,b,a

    function initWebGL(glCtx) {
      const vsSrc = `
        attribute vec3 aPos;
        attribute vec2 aUV;
        attribute vec4 aColor;
        uniform mat4 uVP;
        varying vec2 vUV;
        varying vec4 vColor;
        varying float vDepth;
        void main() {
          vec4 clip = uVP * vec4(aPos, 1.0);
          gl_Position = clip;
          vUV = aUV;
          vColor = aColor;
          vDepth = clip.w;
        }
      `;
      const fsSrc = `
        precision mediump float;
        varying vec2 vUV;
        varying vec4 vColor;
        varying float vDepth;
        uniform sampler2D uTex;
        uniform vec3 uFogColor;
        uniform float uFogNear;
        uniform float uFogFar;
        void main() {
          vec4 texel = texture2D(uTex, vUV);
          vec4 col = texel * vColor;
          if (col.a < 0.06) discard;
          float fogFactor = clamp((vDepth - uFogNear) / max(1.0, uFogFar - uFogNear), 0.0, 0.88);
          vec3 mixed = mix(col.rgb, uFogColor, fogFactor);
          gl_FragColor = vec4(mixed, col.a);
        }
      `;

      const lineVs = `
        attribute vec3 aPos;
        attribute vec4 aColor;
        uniform mat4 uVP;
        varying vec4 vColor;
        void main() {
          gl_Position = uVP * vec4(aPos, 1.0);
          vColor = aColor;
        }
      `;
      const lineFs = `
        precision mediump float;
        varying vec4 vColor;
        void main() {
          gl_FragColor = vColor;
        }
      `;

      function compile(type, src) {
        const s = glCtx.createShader(type);
        glCtx.shaderSource(s, src);
        glCtx.compileShader(s);
        if (!glCtx.getShaderParameter(s, glCtx.COMPILE_STATUS)) {
          glCtx.deleteShader(s);
          return null;
        }
        return s;
      }

      function link(vs, fs) {
        const vsObj = compile(glCtx.VERTEX_SHADER, vs);
        const fsObj = compile(glCtx.FRAGMENT_SHADER, fs);
        if (!vsObj || !fsObj) return null;
        const p = glCtx.createProgram();
        glCtx.attachShader(p, vsObj);
        glCtx.attachShader(p, fsObj);
        glCtx.linkProgram(p);
        glCtx.deleteShader(vsObj);
        glCtx.deleteShader(fsObj);
        if (!glCtx.getProgramParameter(p, glCtx.LINK_STATUS)) {
          glCtx.deleteProgram(p);
          return null;
        }
        return p;
      }

      const prog = link(vsSrc, fsSrc);
      const lineProg = link(lineVs, lineFs);
      if (!prog || !lineProg) return null;

      const atlasPixels = buildProceduralAtlas();
      const tex = glCtx.createTexture();
      glCtx.bindTexture(glCtx.TEXTURE_2D, tex);
      glCtx.texImage2D(
        glCtx.TEXTURE_2D,
        0,
        glCtx.RGBA,
        ATLAS_SIZE,
        ATLAS_SIZE,
        0,
        glCtx.RGBA,
        glCtx.UNSIGNED_BYTE,
        atlasPixels
      );
      glCtx.texParameteri(glCtx.TEXTURE_2D, glCtx.TEXTURE_MIN_FILTER, glCtx.NEAREST);
      glCtx.texParameteri(glCtx.TEXTURE_2D, glCtx.TEXTURE_MAG_FILTER, glCtx.NEAREST);
      glCtx.texParameteri(glCtx.TEXTURE_2D, glCtx.TEXTURE_WRAP_S, glCtx.CLAMP_TO_EDGE);
      glCtx.texParameteri(glCtx.TEXTURE_2D, glCtx.TEXTURE_WRAP_T, glCtx.CLAMP_TO_EDGE);

      return {
        prog,
        lineProg,
        tex,
        loc: {
          aPos: glCtx.getAttribLocation(prog, 'aPos'),
          aUV: glCtx.getAttribLocation(prog, 'aUV'),
          aColor: glCtx.getAttribLocation(prog, 'aColor'),
          uVP: glCtx.getUniformLocation(prog, 'uVP'),
          uTex: glCtx.getUniformLocation(prog, 'uTex'),
          uFogColor: glCtx.getUniformLocation(prog, 'uFogColor'),
          uFogNear: glCtx.getUniformLocation(prog, 'uFogNear'),
          uFogFar: glCtx.getUniformLocation(prog, 'uFogFar'),
        },
        lineLoc: {
          aPos: glCtx.getAttribLocation(lineProg, 'aPos'),
          aColor: glCtx.getAttribLocation(lineProg, 'aColor'),
          uVP: glCtx.getUniformLocation(lineProg, 'uVP'),
        },
        vboBlocksOpaque: glCtx.createBuffer(),
        vboBlocksTrans: glCtx.createBuffer(),
        vboDynOpaque: glCtx.createBuffer(),
        vboDynTrans: glCtx.createBuffer(),
        vboLines: glCtx.createBuffer(),
        blocksOpaqueVerts: 0,
        blocksTransVerts: 0,
      };
    }

    function computeSkyTheme() {
      const st = worldState.status;
      const dim = String(st.dimension || 'overworld').toLowerCase();
      if (dim.includes('nether')) {
        return {
          skyTop: [0.22, 0.05, 0.05],
          skyHorizon: [0.38, 0.10, 0.08],
          fog: [0.32, 0.09, 0.07],
          dayLight: 0.78,
        };
      }
      if (dim.includes('end')) {
        return {
          skyTop: [0.06, 0.04, 0.12],
          skyHorizon: [0.14, 0.09, 0.24],
          fog: [0.11, 0.07, 0.19],
          dayLight: 0.82,
        };
      }
      let isDay = st.is_day !== false;
      if (typeof st.time_of_day === 'number') {
        const tod = ((st.time_of_day % 24000) + 24000) % 24000;
        isDay = tod < 12500 || tod > 23400;
      }
      if (!isDay) {
        return {
          skyTop: [0.04, 0.07, 0.16],
          skyHorizon: [0.09, 0.14, 0.28],
          fog: [0.08, 0.12, 0.24],
          dayLight: 0.58,
        };
      }
      return {
        skyTop: [0.24, 0.48, 0.84],
        skyHorizon: [0.56, 0.75, 0.96],
        fog: [0.50, 0.70, 0.92],
        dayLight: 1.0,
      };
    }

    function rebuildStaticBlockMeshes() {
      const blocks = worldState.blocks;
      const opaqueMesh = worldState.blocksOpaqueMesh;
      const transMesh = worldState.blocksTransMesh;
      opaqueMesh.len = 0;
      transMesh.len = 0;
      worldState.radarBlockMap.clear();

      const occupiedOpaque = new Set();
      const validBlocks = [];

      for (let i = 0; i < blocks.length; i++) {
        const b = blocks[i];
        const p = extractPos(b);
        if (!p) continue;
        const spec = classifyBlock(b.name);
        if (!spec) continue;
        const bx = Math.floor(p[0]);
        const by = Math.floor(p[1]);
        const bz = Math.floor(p[2]);
        const key3 = `${bx},${by},${bz}`;
        if (spec.opaque) occupiedOpaque.add(key3);
        validBlocks.push({ bx, by, bz, spec, name: normalizeBlockName(b.name) });

        // Topmost block per XZ column for 2D radar shading
        const key2 = `${bx},${bz}`;
        const prev = worldState.radarBlockMap.get(key2);
        if (!prev || by >= prev.by) {
          worldState.radarBlockMap.set(key2, { bx, by, bz, spec, name: normalizeBlockName(b.name) });
        }
      }

      const dayBrightness = computeSkyTheme().dayLight;

      for (let i = 0; i < validBlocks.length; i++) {
        const { bx, by, bz, spec } = validBlocks[i];
        let cull = 0;
        if (spec.opaque) {
          if (occupiedOpaque.has(`${bx},${by + 1},${bz}`)) cull |= 1;
          if (occupiedOpaque.has(`${bx},${by - 1},${bz}`)) cull |= 2;
          if (occupiedOpaque.has(`${bx},${by},${bz - 1}`)) cull |= 4;
          if (occupiedOpaque.has(`${bx},${by},${bz + 1}`)) cull |= 8;
          if (occupiedOpaque.has(`${bx + 1},${by},${bz}`)) cull |= 16;
          if (occupiedOpaque.has(`${bx - 1},${by},${bz}`)) cull |= 32;
          if (cull === 63) continue;
        }
        const inset = spec.width ? (1 - spec.width) * 0.5 : 0;
        const h = spec.height || 1;
        const target = spec.translucent ? transMesh : opaqueMesh;
        target.addBlockBox(
          bx + inset,
          by,
          bz + inset,
          bx + 1 - inset,
          by + h,
          bz + 1 - inset,
          spec,
          cull,
          dayBrightness
        );
      }

      if (gl && glRes) {
        gl.bindBuffer(gl.ARRAY_BUFFER, glRes.vboBlocksOpaque);
        gl.bufferData(gl.ARRAY_BUFFER, opaqueMesh.subarray(), gl.STATIC_DRAW);
        glRes.blocksOpaqueVerts = opaqueMesh.vertexCount;

        gl.bindBuffer(gl.ARRAY_BUFFER, glRes.vboBlocksTrans);
        gl.bufferData(gl.ARRAY_BUFFER, transMesh.subarray(), gl.STATIC_DRAW);
        glRes.blocksTransVerts = transMesh.vertexCount;
      }

      worldState.blocksBufferDirty = false;
    }

    function buildDynamicScene(nowMs) {
      dynOpaque.len = 0;
      dynTrans.len = 0;

      const st = worldState.status;
      const bp = smoothBot.pos;
      const yaw = smoothBot.yaw;
      const pitch = smoothBot.pitch;
      const hurtTint = worldState.showHurtMarkers && nowMs < worldState.hurtFlashUntil ? [1.25, 0.45, 0.45] : [1, 1, 1];

      // 1. Articulated 3D Bot (in third_person and free_orbit)
      if (mode !== 'first_person') {
        const sneak = Boolean(st.sneaking);
        const legSwing = Math.sin(worldState.walkPhase) * 0.65 * smoothBot.movingAmount;
        const busySwing = st.busy ? Math.sin(nowMs * 0.016) * 0.75 : 0;
        const armSwing = -legSwing * 0.8 + busySwing;

        const hipY = bp[1] + (sneak ? 0.68 : 0.75);
        const shoulderY = bp[1] + (sneak ? 1.26 : 1.38);
        const headY = bp[1] + (sneak ? 1.32 : 1.44);

        const pantsCol = [0.22 * hurtTint[0], 0.28 * hurtTint[1], 0.58 * hurtTint[2]];
        const torsoCol = [0.14 * hurtTint[0], 0.68 * hurtTint[1], 0.72 * hurtTint[2]];
        const skinCol = [0.88 * hurtTint[0], 0.69 * hurtTint[1], 0.54 * hurtTint[2]];
        const hairCol = [0.25 * hurtTint[0], 0.17 * hurtTint[1], 0.11 * hurtTint[2]];

        // Left & Right Legs
        const cy = Math.cos(yaw);
        const sy = Math.sin(yaw);
        const sideOffset = (ox) => [bp[0] + ox * cy, hipY, bp[2] - ox * sy];
        dynOpaque.addOrientedBox(sideOffset(-0.13), [-0.12, -0.75, -0.12], [0.12, 0, 0.12], yaw, legSwing, 0, pantsCol, 1);
        dynOpaque.addOrientedBox(sideOffset(0.13), [-0.12, -0.75, -0.12], [0.12, 0, 0.12], yaw, -legSwing, 0, pantsCol, 1);

        // Torso
        dynOpaque.addOrientedBox(
          [bp[0], hipY, bp[2]],
          [-0.25, 0, -0.14],
          [0.25, 0.66, 0.14],
          yaw,
          sneak ? -0.25 : 0,
          0,
          torsoCol,
          1
        );

        // Head + Visor/Eyes on -Z face
        dynOpaque.addOrientedBox(
          [bp[0], headY, bp[2]],
          [-0.24, 0, -0.24],
          [0.24, 0.48, 0.24],
          yaw,
          pitch,
          0,
          skinCol,
          1
        );
        dynOpaque.addOrientedBox(
          [bp[0], headY, bp[2]],
          [-0.25, 0.34, -0.25],
          [0.25, 0.50, 0.25],
          yaw,
          pitch,
          0,
          hairCol,
          1
        );
        dynOpaque.addOrientedBox(
          [bp[0], headY, bp[2]],
          [-0.18, 0.16, -0.255],
          [0.18, 0.26, -0.23],
          yaw,
          pitch,
          0,
          [0.12, 0.95, 0.98],
          1
        );

        // Right Arm (+X in local Steve frame when facing -Z: wait, facing -Z, right hand is +X? Check: East(+X) x Up(+Y) = South(+Z). Facing North(-Z), right hand is East(+X)!)
        const rArmPivot = [bp[0] + 0.36 * cy, shoulderY, bp[2] - 0.36 * sy];
        const heldName = normalizeItemName(st.held);
        const rArmPitch = heldName ? 0.45 + armSwing : armSwing;
        dynOpaque.addOrientedBox(rArmPivot, [-0.11, -0.62, -0.11], [0.11, 0.06, 0.11], yaw, rArmPitch, 0, skinCol, 1);

        if (heldName) {
          const toolCol = itemMaterialColor(heldName);
          // Handle + tool head extending forward (-Z) from hand
          dynOpaque.addOrientedBox(rArmPivot, [-0.04, -0.58, -0.52], [0.04, -0.50, -0.05], yaw, rArmPitch, 0, [0.52, 0.36, 0.20], 1);
          if (heldName.includes('pickaxe') || heldName.includes('axe') || heldName.includes('hoe')) {
            dynOpaque.addOrientedBox(rArmPivot, [-0.22, -0.62, -0.56], [0.22, -0.46, -0.44], yaw, rArmPitch, 0, toolCol, 1);
          } else if (heldName.includes('sword')) {
            dynOpaque.addOrientedBox(rArmPivot, [-0.05, -0.59, -0.82], [0.05, -0.49, -0.28], yaw, rArmPitch, 0, toolCol, 1);
          } else {
            dynOpaque.addOrientedBox(rArmPivot, [-0.14, -0.64, -0.44], [0.14, -0.38, -0.18], yaw, rArmPitch, 0, toolCol, 1);
          }
        }

        // Left Arm (-X) + Shield cue
        const lArmPivot = [bp[0] - 0.36 * cy, shoulderY, bp[2] + 0.36 * sy];
        const offName = normalizeItemName(st.offhand);
        const hasShield = offName.includes('shield') || Boolean(st.blocking);
        const lArmPitch = st.blocking ? 0.95 : -armSwing * 0.6;
        dynOpaque.addOrientedBox(lArmPivot, [-0.11, -0.62, -0.11], [0.11, 0.06, 0.11], yaw, lArmPitch, 0, skinCol, 1);
        if (hasShield) {
          dynOpaque.addOrientedBox(
            lArmPivot,
            [-0.26, -0.68, -0.28],
            [0.14, -0.04, -0.20],
            yaw,
            lArmPitch,
            0,
            st.blocking ? [0.35, 0.85, 0.98] : [0.68, 0.50, 0.28],
            1
          );
        }

        // Fire / Water / Freeze aura cues on Bot
        if (st.on_fire || st.in_lava) {
          const pulse = 0.3 + 0.15 * Math.sin(nowMs * 0.02);
          dynTrans.addOrientedBox([bp[0], bp[1] + 0.1, bp[2]], [-0.42, 0, -0.42], [0.42, 1.7, 0.42], nowMs * 0.003, 0, 0, [1.0, 0.45, 0.08], pulse);
        } else if (st.in_water || st.submerged) {
          dynTrans.addOrientedBox([bp[0], bp[1] + 0.05, bp[2]], [-0.45, 0, -0.45], [0.45, 0.9, 0.45], 0, 0, 0, [0.22, 0.62, 0.98], 0.28);
        }
      }

      // 2. Entities (mobs, animals, items, players)
      const entities = worldState.entities;
      for (let i = 0; i < entities.length; i++) {
        const e = entities[i];
        const ep = extractPos(e);
        if (!ep) continue;
        const etype = String(e.type || '').toLowerCase();
        const ename = String(e.name || '').toLowerCase();

        if (etype === 'item' || ename === 'item') {
          const bob = Math.sin(nowMs * 0.004 + i) * 0.12 + 0.28;
          const spin = nowMs * 0.0025 + i;
          const col = itemMaterialColor(String(e.item || e.name || ''));
          dynOpaque.addOrientedBox([ep[0], ep[1] + bob, ep[2]], [-0.18, -0.18, -0.18], [0.18, 0.18, 0.18], spin, 0.25, 0, col, 1);
        } else if (etype === 'hostile' || ['zombie', 'skeleton', 'creeper', 'spider', 'enderman', 'witch', 'drowned'].includes(ename)) {
          const mobCol =
            ename.includes('skeleton') ? [0.85, 0.85, 0.82]
            : ename.includes('creeper') ? [0.26, 0.78, 0.28]
            : ename.includes('spider') ? [0.26, 0.20, 0.22]
            : [0.32, 0.58, 0.34];
          const tall = ename === 'spider' ? 0.85 : 1.8;
          const wide = ename === 'spider' ? 0.55 : 0.28;
          dynOpaque.addOrientedBox(ep, [-wide, 0, -wide], [wide, tall * 0.72, wide], 0, 0, 0, mobCol, 1);
          dynOpaque.addOrientedBox([ep[0], ep[1] + tall * 0.72, ep[2]], [-0.24, 0, -0.24], [0.24, 0.46, 0.24], 0, 0, 0, [0.95, 0.26, 0.26], 1);
        } else if (etype === 'animal') {
          const animCol =
            ename.includes('pig') ? [0.95, 0.64, 0.68]
            : ename.includes('sheep') ? [0.94, 0.94, 0.92]
            : ename.includes('chicken') ? [0.96, 0.92, 0.82]
            : [0.52, 0.36, 0.24];
          dynOpaque.addOrientedBox(ep, [-0.34, 0, -0.45], [0.34, 0.85, 0.45], 0, 0, 0, animCol, 1);
          dynOpaque.addOrientedBox([ep[0], ep[1] + 0.68, ep[2] - 0.48], [-0.22, 0, -0.22], [0.22, 0.40, 0.22], 0, 0, 0, animCol, 1);
        } else {
          dynOpaque.addOrientedBox(ep, [-0.28, 0, -0.28], [0.28, 1.75, 0.28], 0, 0, 0, [0.35, 0.78, 0.98], 1);
        }
      }

      // 3. Trajectory 3D Breadcrumb Ribbon (active dimension)
      const activeDim = String(st.dimension || 'overworld');
      const traj = worldState.trajectory;
      const dimPts = [];
      for (let i = 0; i < traj.length; i++) {
        const pt = traj[i];
        if (pt && pt.dimension && String(pt.dimension) !== activeDim) continue;
        const p = extractPos(pt);
        if (p) dimPts.push(p);
      }
      if (worldState.hasTelemetry) {
        dimPts.push([bp[0], bp[1], bp[2]]);
      }
      const startIdx = Math.max(0, dimPts.length - 180);
      for (let i = startIdx; i < dimPts.length - 1; i++) {
        const t0 = (i - startIdx) / Math.max(1, dimPts.length - 1 - startIdx);
        const t1 = (i + 1 - startIdx) / Math.max(1, dimPts.length - 1 - startIdx);
        const a = [dimPts[i][0], dimPts[i][1] + 0.08, dimPts[i][2]];
        const b = [dimPts[i + 1][0], dimPts[i + 1][1] + 0.08, dimPts[i + 1][2]];
        const cA = [lerp(0.12, 0.22, t0), lerp(0.58, 0.98, t0), lerp(0.95, 0.48, t0)];
        const cB = [lerp(0.12, 0.22, t1), lerp(0.58, 0.98, t1), lerp(0.95, 0.48, t1)];
        dynTrans.addRibbonSegment(a, b, 0.14, cA, cB, 0.78);
      }

      // 4. Semantic Markers 3D Beacons
      const markers = worldState.markers;
      for (let i = Math.max(0, markers.length - 60); i < markers.length; i++) {
        const m = markers[i];
        if (!m || (m.dimension && String(m.dimension) !== activeDim)) continue;
        const mp = extractPos(m);
        if (!mp) continue;
        const kind = String(m.kind || '');
        if (!worldState.showHurtMarkers && (kind === 'hurt' || kind === 'death')) continue;
        const col =
          kind === 'protected_region' ? [0.98, 0.24, 0.28]
          : kind === 'death' ? [0.92, 0.28, 0.95]
          : kind === 'hurt' ? [0.98, 0.62, 0.16]
          : kind === 'station' ? [0.96, 0.78, 0.22]
          : [0.24, 0.92, 0.58];
        const pulseH = kind === 'death' || kind === 'protected_region' ? 6.5 : 3.6;
        dynTrans.addOrientedBox(
          [mp[0] + 0.5, mp[1], mp[2] + 0.5],
          [-0.14, 0, -0.14],
          [0.14, pulseH, 0.14],
          nowMs * 0.0015,
          0,
          0,
          col,
          0.36
        );
        if (kind === 'protected_region') {
          dynTrans.addOrientedBox(
            [mp[0] + 0.5, mp[1], mp[2] + 0.5],
            [-0.54, -0.02, -0.54],
            [0.54, 1.04, 0.54],
            0,
            0,
            0,
            col,
            0.30
          );
        }
      }
    }

    function buildFootGridLines() {
      const bp = smoothBot.pos;
      const cx = Math.floor(bp[0]);
      const gy = Math.floor(bp[1]) + 0.015;
      const cz = Math.floor(bp[2]);
      const radius = 14;
      let o = 0;

      const pushLine = (x0, y0, z0, x1, y1, z1, r, g, b, a) => {
        if (o + 14 > gridLines.length) return;
        gridLines[o++] = x0; gridLines[o++] = y0; gridLines[o++] = z0;
        gridLines[o++] = r; gridLines[o++] = g; gridLines[o++] = b; gridLines[o++] = a;
        gridLines[o++] = x1; gridLines[o++] = y1; gridLines[o++] = z1;
        gridLines[o++] = r; gridLines[o++] = g; gridLines[o++] = b; gridLines[o++] = a;
      };

      for (let i = -radius; i <= radius; i += 2) {
        const major = (cx + i) % 8 === 0;
        const alpha = (major ? 0.30 : 0.14) * (1 - Math.abs(i) / (radius + 4));
        pushLine(cx + i, gy, cz - radius, cx + i, gy, cz + radius, 0.38, 0.65, 0.92, alpha);
      }
      for (let k = -radius; k <= radius; k += 2) {
        const major = (cz + k) % 8 === 0;
        const alpha = (major ? 0.30 : 0.14) * (1 - Math.abs(k) / (radius + 4));
        pushLine(cx - radius, gy, cz + k, cx + radius, gy, cz + k, 0.38, 0.65, 0.92, alpha);
      }

      // North (-Z) cyan axis cue & East (+X) amber axis cue from Bot foot
      pushLine(bp[0], gy + 0.02, bp[2], bp[0], gy + 0.02, bp[2] - 2.6, 0.20, 0.92, 0.98, 0.65);
      pushLine(bp[0], gy + 0.02, bp[2], bp[0] + 2.6, gy + 0.02, bp[2], 0.98, 0.72, 0.24, 0.65);

      return o / 7;
    }

    function computeCameraMatrices() {
      const bp = smoothBot.pos;
      const sneak = Boolean(worldState.status.sneaking);
      let eye;
      let center;

      if (mode === 'first_person') {
        const eyeY = bp[1] + (sneak ? 1.35 : 1.62);
        eye = [bp[0], eyeY, bp[2]];
        const cp = Math.cos(smoothBot.pitch);
        const fx = -Math.sin(smoothBot.yaw) * cp;
        const fy = Math.sin(smoothBot.pitch);
        const fz = -Math.cos(smoothBot.yaw) * cp;
        center = [eye[0] + fx * 10, eye[1] + fy * 10, eye[2] + fz * 10];
      } else if (mode === 'third_person') {
        const az = smoothBot.yaw + camCtrl.tpYawOffset;
        const el = clamp(camCtrl.tpPitch, -0.45, 1.38);
        const dist = clamp(camCtrl.tpDist, 2.5, 45);
        center = [bp[0], bp[1] + 1.25, bp[2]];
        // Forward is (-sin(az), -cos(az)), so behind Bot is (+sin(az), +cos(az))
        const horiz = Math.cos(el) * dist;
        eye = [
          center[0] + Math.sin(az) * horiz,
          center[1] + Math.sin(el) * dist,
          center[2] + Math.cos(az) * horiz,
        ];
      } else {
        // free_orbit
        const az = camCtrl.orbitYaw;
        const el = clamp(camCtrl.orbitPitch, 0.08, 1.48);
        const dist = clamp(camCtrl.orbitDist, 4, 120);
        center = [
          bp[0] + camCtrl.orbitPan[0],
          bp[1] + 1.0 + camCtrl.orbitPan[1],
          bp[2] + camCtrl.orbitPan[2],
        ];
        const horiz = Math.cos(el) * dist;
        eye = [
          center[0] + Math.sin(az) * horiz,
          center[1] + Math.sin(el) * dist,
          center[2] + Math.cos(az) * horiz,
        ];
      }

      const proj = mat4Perspective((mode === 'first_person' ? 72 : 60) * (Math.PI / 180), cssWidth / Math.max(1, cssHeight), 0.12, 220);
      const view = mat4LookAt(eye, center, [0, 1, 0]);
      return { vp: mat4Mul(proj, view), eye, center };
    }

    function drawMeshBuffer(vbo, vertCount) {
      if (!gl || !glRes || vertCount <= 0) return;
      const stride = 9 * 4;
      gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
      gl.enableVertexAttribArray(glRes.loc.aPos);
      gl.vertexAttribPointer(glRes.loc.aPos, 3, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(glRes.loc.aUV);
      gl.vertexAttribPointer(glRes.loc.aUV, 2, gl.FLOAT, false, stride, 12);
      gl.enableVertexAttribArray(glRes.loc.aColor);
      gl.vertexAttribPointer(glRes.loc.aColor, 4, gl.FLOAT, false, stride, 20);
      gl.drawArrays(gl.TRIANGLES, 0, vertCount);
    }

    function render3DScene(nowMs) {
      if (worldState.blocksBufferDirty) {
        rebuildStaticBlockMeshes();
      }
      buildDynamicScene(nowMs);
      const lineVertCount = buildFootGridLines();
      const { vp } = computeCameraMatrices();
      const sky = computeSkyTheme();

      if (gl && glRes) {
        glCanvas.style.display = 'block';
        gl.viewport(0, 0, glCanvas.width, glCanvas.height);
        gl.clearColor(sky.skyHorizon[0], sky.skyHorizon[1], sky.skyHorizon[2], 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

        gl.enable(gl.DEPTH_TEST);
        gl.depthFunc(gl.LEQUAL);
        gl.enable(gl.CULL_FACE);
        gl.cullFace(gl.BACK);

        // 1. Opaque blocks + Bot + Entities
        gl.disable(gl.BLEND);
        gl.depthMask(true);
        gl.useProgram(glRes.prog);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, glRes.tex);
        gl.uniform1i(glRes.loc.uTex, 0);
        gl.uniformMatrix4fv(glRes.loc.uVP, false, vp);
        gl.uniform3fv(glRes.loc.uFogColor, sky.fog);
        gl.uniform1f(glRes.loc.uFogNear, 32.0);
        gl.uniform1f(glRes.loc.uFogFar, 115.0);

        drawMeshBuffer(glRes.vboBlocksOpaque, glRes.blocksOpaqueVerts);

        if (dynOpaque.vertexCount > 0) {
          gl.bindBuffer(gl.ARRAY_BUFFER, glRes.vboDynOpaque);
          gl.bufferData(gl.ARRAY_BUFFER, dynOpaque.subarray(), gl.STREAM_DRAW);
          drawMeshBuffer(glRes.vboDynOpaque, dynOpaque.vertexCount);
        }

        // 2. Reference ground grid lines
        if (lineVertCount > 0) {
          gl.enable(gl.BLEND);
          gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
          gl.useProgram(glRes.lineProg);
          gl.uniformMatrix4fv(glRes.lineLoc.uVP, false, vp);
          gl.bindBuffer(gl.ARRAY_BUFFER, glRes.vboLines);
          gl.bufferData(gl.ARRAY_BUFFER, gridLines.subarray(0, lineVertCount * 7), gl.STREAM_DRAW);
          gl.enableVertexAttribArray(glRes.lineLoc.aPos);
          gl.vertexAttribPointer(glRes.lineLoc.aPos, 3, gl.FLOAT, false, 28, 0);
          gl.enableVertexAttribArray(glRes.lineLoc.aColor);
          gl.vertexAttribPointer(glRes.lineLoc.aColor, 4, gl.FLOAT, false, 28, 12);
          gl.drawArrays(gl.LINES, 0, lineVertCount);
        }

        // 3. Translucent blocks (water, powder_snow) + ribbons + beacons (two-sided)
        gl.disable(gl.CULL_FACE);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        gl.depthMask(false);
        gl.useProgram(glRes.prog);
        drawMeshBuffer(glRes.vboBlocksTrans, glRes.blocksTransVerts);

        if (dynTrans.vertexCount > 0) {
          gl.bindBuffer(gl.ARRAY_BUFFER, glRes.vboDynTrans);
          gl.bufferData(gl.ARRAY_BUFFER, dynTrans.subarray(), gl.STREAM_DRAW);
          drawMeshBuffer(glRes.vboDynTrans, dynTrans.vertexCount);
        }
        gl.depthMask(true);
      } else {
        // Software 3D wireframe/point fallback if headless browser disabled WebGL completely
        glCanvas.style.display = 'none';
        renderSoftware3DFallback(vp, sky);
      }

      render3DOverlayHUD(vp, nowMs);
    }

    function renderSoftware3DFallback(vp, sky) {
      if (!ctx2d) return;
      const grad = ctx2d.createLinearGradient(0, 0, 0, cssHeight);
      grad.addColorStop(0, `rgb(${Math.round(sky.skyTop[0] * 255)},${Math.round(sky.skyTop[1] * 255)},${Math.round(sky.skyTop[2] * 255)})`);
      grad.addColorStop(1, `rgb(${Math.round(sky.skyHorizon[0] * 255)},${Math.round(sky.skyHorizon[1] * 255)},${Math.round(sky.skyHorizon[2] * 255)})`);
      ctx2d.fillStyle = grad;
      ctx2d.fillRect(0, 0, cssWidth, cssHeight);

      worldState.radarBlockMap.forEach((b) => {
        const sc = projectToScreen(vp, b.bx + 0.5, b.by + 0.5, b.bz + 0.5, cssWidth, cssHeight);
        if (!sc || !sc.inBounds) return;
        const sz = clamp(240 / Math.max(1, sc.w), 3, 28);
        ctx2d.fillStyle = b.spec.radar;
        ctx2d.fillRect(sc.x - sz * 0.5, sc.y - sz * 0.5, sz, sz);
      });
    }

    function render3DOverlayHUD(vp, nowMs) {
      if (!ctx2d) return;
      if (gl && glRes) {
        ctx2d.clearRect(0, 0, cssWidth, cssHeight);
      }
      const st = worldState.status;

      // Subtle immersion vignettes (submerged / lava / low HP)
      if (st.submerged || st.in_water) {
        ctx2d.fillStyle = st.submerged ? 'rgba(14, 96, 210, 0.24)' : 'rgba(14, 96, 210, 0.10)';
        ctx2d.fillRect(0, 0, cssWidth, cssHeight);
      } else if (st.in_lava || st.on_fire) {
        ctx2d.fillStyle = 'rgba(235, 82, 18, 0.22)';
        ctx2d.fillRect(0, 0, cssWidth, cssHeight);
      }

      // Projected Entity Nametags & HP Pills
      ctx2d.font = '600 11px system-ui, -apple-system, sans-serif';
      ctx2d.textAlign = 'center';
      ctx2d.textBaseline = 'middle';

      const entities = worldState.entities;
      for (let i = 0; i < Math.min(entities.length, 24); i++) {
        const e = entities[i];
        const ep = extractPos(e);
        if (!ep) continue;
        const isItem = String(e.type || '').toLowerCase() === 'item' || e.name === 'item';
        const sc = projectToScreen(vp, ep[0], ep[1] + (isItem ? 0.85 : 2.15), ep[2], cssWidth, cssHeight);
        if (!sc || !sc.inBounds || sc.w > 46) continue;

        const label = isItem
          ? `📦 ${e.item || 'item'}`
          : `${e.name || e.type}${typeof e.health === 'number' ? ` ♥${Math.round(e.health)}` : ''}`;
        const tw = ctx2d.measureText(label).width + 12;
        const isHostile = String(e.type || '').toLowerCase() === 'hostile';
        ctx2d.fillStyle = isHostile ? 'rgba(127, 20, 29, 0.82)' : 'rgba(15, 23, 42, 0.76)';
        ctx2d.fillRect(sc.x - tw * 0.5, sc.y - 9, tw, 18);
        ctx2d.strokeStyle = isHostile ? '#f87171' : 'rgba(148, 163, 184, 0.45)';
        ctx2d.lineWidth = 1;
        ctx2d.strokeRect(sc.x - tw * 0.5, sc.y - 9, tw, 18);
        ctx2d.fillStyle = isHostile ? '#fecaca' : isItem ? '#fde68a' : '#e2e8f0';
        ctx2d.fillText(label, sc.x, sc.y);
      }

      // Projected Semantic Markers
      const activeDim = String(st.dimension || 'overworld');
      const markers = worldState.markers;
      for (let i = Math.max(0, markers.length - 30); i < markers.length; i++) {
        const m = markers[i];
        if (!m || (m.dimension && String(m.dimension) !== activeDim)) continue;
        const mp = extractPos(m);
        if (!mp) continue;
        const sc = projectToScreen(vp, mp[0] + 0.5, mp[1] + 2.4, mp[2] + 0.5, cssWidth, cssHeight);
        if (!sc || !sc.inBounds || sc.w > 75) continue;

        const kind = String(m.kind || '');
        if (!worldState.showHurtMarkers && (kind === 'hurt' || kind === 'death')) continue;
        const badge =
          kind === 'protected_region' ? `✖ ${m.label || '保护区'}`
          : kind === 'death' ? `💀 ${m.label || '死亡点'}`
          : kind === 'hurt' ? `⚡ ${m.label || '受伤'}`
          : kind === 'station' ? `🛠 ${m.label || '站点'}`
          : `🏆 ${m.label || '进度'}`;
        const color =
          kind === 'protected_region' ? '#f87171'
          : kind === 'death' ? '#e879f9'
          : kind === 'hurt' ? '#fb923c'
          : kind === 'station' ? '#facc15'
          : '#4ade80';
        const tw = ctx2d.measureText(badge).width + 12;
        ctx2d.fillStyle = 'rgba(9, 13, 24, 0.84)';
        ctx2d.fillRect(sc.x - tw * 0.5, sc.y - 9, tw, 18);
        ctx2d.strokeStyle = color;
        ctx2d.strokeRect(sc.x - tw * 0.5, sc.y - 9, tw, 18);
        ctx2d.fillStyle = color;
        ctx2d.fillText(badge, sc.x, sc.y);
      }

      // First-person Crosshair + Held Tool Hand Cue
      if (mode === 'first_person') {
        const cx = cssWidth * 0.5;
        const cy = cssHeight * 0.5;
        ctx2d.strokeStyle = 'rgba(248, 250, 252, 0.85)';
        ctx2d.lineWidth = 1.6;
        ctx2d.beginPath();
        ctx2d.moveTo(cx - 8, cy);
        ctx2d.lineTo(cx + 8, cy);
        ctx2d.moveTo(cx, cy - 8);
        ctx2d.lineTo(cx, cy + 8);
        ctx2d.stroke();

        // First-person right hand + held item silhouette
        const bobX = Math.sin(worldState.walkPhase * 0.5) * 6 * smoothBot.movingAmount;
        const bobY = Math.abs(Math.cos(worldState.walkPhase * 0.5)) * 5 * smoothBot.movingAmount;
        const swing = st.busy ? Math.sin(nowMs * 0.018) * 14 : 0;
        const hx = cssWidth - 108 + bobX;
        const hy = cssHeight - 54 + bobY + swing;

        const heldName = normalizeItemName(st.held);
        const col = itemMaterialColor(heldName);
        ctx2d.save();
        ctx2d.translate(hx, hy);
        ctx2d.rotate(-0.36 + (st.busy ? Math.sin(nowMs * 0.018) * 0.22 : 0));

        // Forearm
        ctx2d.fillStyle = '#d9a982';
        ctx2d.fillRect(10, 0, 56, 78);
        ctx2d.fillStyle = '#22a6b3';
        ctx2d.fillRect(8, 30, 60, 52);

        if (heldName) {
          const cssCol = `rgb(${Math.round(col[0] * 255)},${Math.round(col[1] * 255)},${Math.round(col[2] * 255)})`;
          ctx2d.fillStyle = '#7c5329';
          ctx2d.fillRect(24, -68, 12, 76);
          ctx2d.fillStyle = cssCol;
          if (heldName.includes('sword')) {
            ctx2d.fillRect(21, -112, 18, 66);
            ctx2d.fillRect(10, -48, 40, 8);
          } else {
            ctx2d.fillRect(4, -78, 52, 20);
          }
          ctx2d.rotate(0.36);
          ctx2d.font = '600 11px monospace';
          ctx2d.textAlign = 'right';
          ctx2d.fillStyle = 'rgba(226, 232, 240, 0.9)';
          ctx2d.fillText(heldName, 74, 46);
        }
        ctx2d.restore();

        // Offhand shield edge on bottom-left
        const offName = normalizeItemName(st.offhand);
        if (offName.includes('shield') || st.blocking) {
          ctx2d.save();
          ctx2d.translate(st.blocking ? 96 : 52, cssHeight - (st.blocking ? 88 : 56));
          ctx2d.rotate(0.22);
          ctx2d.fillStyle = '#8c5d2c';
          ctx2d.fillRect(-36, -42, 72, 98);
          ctx2d.strokeStyle = st.blocking ? '#38bdf8' : '#cbd5e1';
          ctx2d.lineWidth = 4;
          ctx2d.strokeRect(-36, -42, 72, 98);
          ctx2d.restore();
        }
      }
    }

    // ============================================================================
    // 2D Tactical Radar Renderer ('radar_2d')
    // ============================================================================
    function renderRadar2D(nowMs) {
      glCanvas.style.display = 'none';
      if (!ctx2d) return;

      ctx2d.fillStyle = '#070e1a';
      ctx2d.fillRect(0, 0, cssWidth, cssHeight);

      const cx = cssWidth * 0.5;
      const cy = cssHeight * 0.5;
      const scale = clamp(camCtrl.radarZoom, 2.0, 32.0);
      const bp = smoothBot.pos;
      const originX = bp[0] + camCtrl.radarPan[0];
      const originZ = bp[2] + camCtrl.radarPan[1];

      const toScreen = (wx, wz) => [
        cx + (wx - originX) * scale,
        cy + (wz - originZ) * scale, // North (-Z) is UP (smaller screen Y), East (+X) is RIGHT!
      ];

      // 1. Subtle coordinate grid (every 8 blocks)
      const halfSpanX = cssWidth / (2 * scale) + 2;
      const halfSpanZ = cssHeight / (2 * scale) + 2;
      const minGridX = Math.floor((originX - halfSpanX) / 8) * 8;
      const maxGridX = Math.ceil((originX + halfSpanX) / 8) * 8;
      const minGridZ = Math.floor((originZ - halfSpanZ) / 8) * 8;
      const maxGridZ = Math.ceil((originZ + halfSpanZ) / 8) * 8;

      ctx2d.strokeStyle = 'rgba(30, 58, 95, 0.38)';
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      for (let gx = minGridX; gx <= maxGridX; gx += 8) {
        const [sx] = toScreen(gx, originZ);
        ctx2d.moveTo(sx, 0);
        ctx2d.lineTo(sx, cssHeight);
      }
      for (let gz = minGridZ; gz <= maxGridZ; gz += 8) {
        const [, sy] = toScreen(originX, gz);
        ctx2d.moveTo(0, sy);
        ctx2d.lineTo(cssWidth, sy);
      }
      ctx2d.stroke();

      // 2. Height-shaded top-down voxel cache
      const blockPx = Math.max(1.5, scale + 0.4);
      worldState.radarBlockMap.forEach((b) => {
        if (Math.abs(b.bx - originX) > halfSpanX || Math.abs(b.bz - originZ) > halfSpanZ) return;
        const [sx, sy] = toScreen(b.bx, b.bz);
        ctx2d.fillStyle = b.spec.radar;
        ctx2d.fillRect(sx, sy, blockPx, blockPx);
        const dy = b.by - Math.floor(bp[1]);
        if (dy > 1) {
          ctx2d.fillStyle = `rgba(255,255,255,${clamp(dy * 0.03, 0, 0.28)})`;
          ctx2d.fillRect(sx, sy, blockPx, blockPx);
        } else if (dy < -1) {
          ctx2d.fillStyle = `rgba(0,0,0,${clamp(-dy * 0.04, 0, 0.42)})`;
          ctx2d.fillRect(sx, sy, blockPx, blockPx);
        }
      });

      // 3. Concentric tactical range rings around Bot (8m, 16m, 24m hostile perimeter, 32m)
      const [botSx, botSy] = toScreen(bp[0], bp[2]);
      [8, 16, 24, 32].forEach((r) => {
        ctx2d.save();
        ctx2d.beginPath();
        ctx2d.arc(botSx, botSy, r * scale, 0, TAU);
        if (r === 24) {
          ctx2d.setLineDash([5, 4]);
          ctx2d.strokeStyle = worldState.status.nearest_hostile
            ? 'rgba(248, 113, 113, 0.65)'
            : 'rgba(56, 189, 248, 0.34)';
        } else {
          ctx2d.strokeStyle = 'rgba(56, 189, 248, 0.16)';
        }
        ctx2d.lineWidth = 1;
        ctx2d.stroke();
        ctx2d.restore();

        ctx2d.fillStyle = 'rgba(148, 163, 184, 0.55)';
        ctx2d.font = '10px monospace';
        ctx2d.textAlign = 'left';
        ctx2d.fillText(`${r}m`, botSx + 4, botSy - r * scale + 10);
      });

      // Subtle radar sweep line
      const sweepAngle = (nowMs * 0.0014) % TAU;
      const sweepRadius = 32 * scale;
      const sweepGrad = ctx2d.createLinearGradient(
        botSx,
        botSy,
        botSx + Math.cos(sweepAngle) * sweepRadius,
        botSy + Math.sin(sweepAngle) * sweepRadius
      );
      sweepGrad.addColorStop(0, 'rgba(34, 211, 238, 0.25)');
      sweepGrad.addColorStop(1, 'rgba(34, 211, 238, 0.0)');
      ctx2d.strokeStyle = sweepGrad;
      ctx2d.lineWidth = 2;
      ctx2d.beginPath();
      ctx2d.moveTo(botSx, botSy);
      ctx2d.lineTo(botSx + Math.cos(sweepAngle) * sweepRadius, botSy + Math.sin(sweepAngle) * sweepRadius);
      ctx2d.stroke();

      // 4. Trajectory trail
      const activeDim = String(worldState.status.dimension || 'overworld');
      const traj = worldState.trajectory;
      ctx2d.beginPath();
      let started = false;
      for (let i = 0; i < traj.length; i++) {
        const pt = traj[i];
        if (pt && pt.dimension && String(pt.dimension) !== activeDim) continue;
        const p = extractPos(pt);
        if (!p) continue;
        const [sx, sy] = toScreen(p[0], p[2]);
        if (!started) {
          ctx2d.moveTo(sx, sy);
          started = true;
        } else {
          ctx2d.lineTo(sx, sy);
        }
      }
      if (started) {
        ctx2d.lineTo(botSx, botSy);
        ctx2d.strokeStyle = '#22d3ee';
        ctx2d.lineWidth = 2.2;
        ctx2d.stroke();
      }

      // 5. Semantic markers
      ctx2d.font = '600 11px system-ui, sans-serif';
      ctx2d.textAlign = 'center';
      ctx2d.textBaseline = 'middle';
      for (let i = 0; i < worldState.markers.length; i++) {
        const m = worldState.markers[i];
        if (!m || (m.dimension && String(m.dimension) !== activeDim)) continue;
        const mp = extractPos(m);
        if (!mp) continue;
        const [sx, sy] = toScreen(mp[0] + 0.5, mp[2] + 0.5);
        const kind = String(m.kind || '');
        if (!worldState.showHurtMarkers && (kind === 'hurt' || kind === 'death')) continue;
        const sym =
          kind === 'protected_region' ? '✖'
          : kind === 'death' ? '💀'
          : kind === 'hurt' ? '⚡'
          : kind === 'station' ? '🛠'
          : '🏆';
        ctx2d.fillStyle = kind === 'protected_region' ? '#ef4444' : '#f8fafc';
        ctx2d.fillText(sym, sx, sy);
      }

      // 6. Entities
      for (let i = 0; i < worldState.entities.length; i++) {
        const e = worldState.entities[i];
        const ep = extractPos(e);
        if (!ep) continue;
        const [sx, sy] = toScreen(ep[0], ep[2]);
        const etype = String(e.type || '').toLowerCase();
        if (etype === 'hostile') {
          ctx2d.fillStyle = '#ef4444';
          ctx2d.beginPath();
          ctx2d.arc(sx, sy, 4.8, 0, TAU);
          ctx2d.fill();
        } else if (etype === 'item' || e.name === 'item') {
          ctx2d.fillStyle = '#facc15';
          ctx2d.fillRect(sx - 3, sy - 3, 6, 6);
        } else {
          ctx2d.fillStyle = '#4ade80';
          ctx2d.beginPath();
          ctx2d.arc(sx, sy, 3.8, 0, TAU);
          ctx2d.fill();
        }
        ctx2d.fillStyle = 'rgba(226, 232, 240, 0.88)';
        ctx2d.font = '10px system-ui, sans-serif';
        ctx2d.fillText(e.item || e.name || etype, sx, sy - 9);
      }

      // 7. Bot FOV cone + directional arrow (forward = (-sin(yaw), -cos(yaw)))
      const fdx = -Math.sin(smoothBot.yaw);
      const fdz = -Math.cos(smoothBot.yaw);
      const headingCanvasAngle = Math.atan2(fdz, fdx);

      ctx2d.fillStyle = 'rgba(56, 189, 248, 0.16)';
      ctx2d.beginPath();
      ctx2d.moveTo(botSx, botSy);
      ctx2d.arc(botSx, botSy, 12 * scale, headingCanvasAngle - 0.52, headingCanvasAngle + 0.52);
      ctx2d.closePath();
      ctx2d.fill();

      ctx2d.save();
      ctx2d.translate(botSx, botSy);
      ctx2d.rotate(headingCanvasAngle);
      ctx2d.fillStyle = '#38bdf8';
      ctx2d.strokeStyle = '#0f172a';
      ctx2d.lineWidth = 1.6;
      ctx2d.beginPath();
      ctx2d.moveTo(10, 0);
      ctx2d.lineTo(-7, -6.5);
      ctx2d.lineTo(-4, 0);
      ctx2d.lineTo(-7, 6.5);
      ctx2d.closePath();
      ctx2d.fill();
      ctx2d.stroke();
      ctx2d.restore();

      // Cardinal N/E/S/W edge markers (clear of corner DOM overlays)
      ctx2d.font = '700 11px monospace';
      ctx2d.fillStyle = '#38bdf8';
      ctx2d.textAlign = 'center';
      ctx2d.fillText('N (-Z)', cx, 18);
      ctx2d.fillStyle = 'rgba(148, 163, 184, 0.72)';
      ctx2d.fillText('S (+Z)', cx, cssHeight - 12);
      ctx2d.fillText('W (-X)', 30, cy);
      ctx2d.fillText('E (+X)', cssWidth - 30, cy);
    }

    // ============================================================================
    // Animation Loop & Pointer Controls
    // ============================================================================
    let lastFrameMs = 0;
    function tick(nowMs) {
      if (destroyed || paused) {
        rafId = null;
        return;
      }
      if (typeof document !== 'undefined' && document.hidden) {
        lastFrameMs = 0;
        rafId = global.requestAnimationFrame(tick);
        return;
      }
      const dt = lastFrameMs ? clamp((nowMs - lastFrameMs) / 1000, 0.001, 0.1) : 0.016;
      lastFrameMs = nowMs;

      const targetPos = extractPos(worldState.status.pos) || [0.5, 64, 0.5];
      const targetYaw = typeof worldState.status.yaw === 'number' && Number.isFinite(worldState.status.yaw) ? worldState.status.yaw : 0;
      const targetPitch = typeof worldState.status.pitch === 'number' && Number.isFinite(worldState.status.pitch) ? worldState.status.pitch : 0;

      if (!smoothBot.initialized) {
        smoothBot.pos = [targetPos[0], targetPos[1], targetPos[2]];
        smoothBot.yaw = targetYaw;
        smoothBot.pitch = targetPitch;
        smoothBot.initialized = true;
      } else {
        const distJump = Math.hypot(targetPos[0] - smoothBot.pos[0], targetPos[1] - smoothBot.pos[1], targetPos[2] - smoothBot.pos[2]);
        if (distJump > 18) {
          smoothBot.pos = [targetPos[0], targetPos[1], targetPos[2]];
        } else {
          const k = 1 - Math.exp(-12 * dt);
          smoothBot.pos[0] = lerp(smoothBot.pos[0], targetPos[0], k);
          smoothBot.pos[1] = lerp(smoothBot.pos[1], targetPos[1], k);
          smoothBot.pos[2] = lerp(smoothBot.pos[2], targetPos[2], k);
        }
        const ak = 1 - Math.exp(-14 * dt);
        smoothBot.yaw = lerpAngle(smoothBot.yaw, targetYaw, ak);
        smoothBot.pitch = lerp(smoothBot.pitch, clamp(targetPitch, -1.5, 1.5), ak);

        const horizDelta = Math.hypot(targetPos[0] - smoothBot.pos[0], targetPos[2] - smoothBot.pos[2]);
        const reportedSpeed = Number(worldState.status.speed) || 0;
        const moving = horizDelta > 0.03 || reportedSpeed > 0.15 ? 1 : 0;
        smoothBot.movingAmount = lerp(smoothBot.movingAmount, moving, 1 - Math.exp(-10 * dt));
        if (smoothBot.movingAmount > 0.02) {
          worldState.walkPhase += dt * (reportedSpeed > 4.8 ? 13 : 9);
        }
      }

      if (mode === 'radar_2d') {
        renderRadar2D(nowMs);
      } else {
        render3DScene(nowMs);
      }

      rafId = global.requestAnimationFrame(tick);
    }

    function onPointerDown(e) {
      if (mode === 'first_person') return;
      camCtrl.dragging = true;
      camCtrl.dragBtn = e.button || 0;
      camCtrl.lastX = e.clientX;
      camCtrl.lastY = e.clientY;
      if (root.setPointerCapture && e.pointerId !== undefined) {
        try {
          root.setPointerCapture(e.pointerId);
        } catch (_) {}
      }
    }

    function onPointerMove(e) {
      if (!camCtrl.dragging) return;
      const dx = e.clientX - camCtrl.lastX;
      const dy = e.clientY - camCtrl.lastY;
      camCtrl.lastX = e.clientX;
      camCtrl.lastY = e.clientY;

      if (mode === 'radar_2d') {
        camCtrl.radarPan[0] -= dx / Math.max(1, camCtrl.radarZoom);
        camCtrl.radarPan[1] -= dy / Math.max(1, camCtrl.radarZoom);
      } else if (mode === 'third_person') {
        camCtrl.tpYawOffset -= dx * 0.0075;
        camCtrl.tpPitch = clamp(camCtrl.tpPitch + dy * 0.0065, -0.35, 1.38);
      } else if (mode === 'free_orbit') {
        if (camCtrl.dragBtn === 2 || e.shiftKey) {
          const panScale = camCtrl.orbitDist * 0.0022;
          const cy = Math.cos(camCtrl.orbitYaw);
          const sy = Math.sin(camCtrl.orbitYaw);
          camCtrl.orbitPan[0] -= (dx * cy + dy * sy * 0.6) * panScale;
          camCtrl.orbitPan[2] -= (-dx * sy + dy * cy * 0.6) * panScale;
        } else {
          camCtrl.orbitYaw -= dx * 0.0075;
          camCtrl.orbitPitch = clamp(camCtrl.orbitPitch + dy * 0.0065, 0.08, 1.48);
        }
      }
    }

    function onPointerUp() {
      camCtrl.dragging = false;
    }

    function onWheel(e) {
      if (mode === 'first_person') return;
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.12 : 0.89;
      if (mode === 'radar_2d') {
        camCtrl.radarZoom = clamp(camCtrl.radarZoom / factor, 2.0, 32.0);
      } else if (mode === 'third_person') {
        camCtrl.tpDist = clamp(camCtrl.tpDist * factor, 2.5, 45.0);
      } else if (mode === 'free_orbit') {
        camCtrl.orbitDist = clamp(camCtrl.orbitDist * factor, 4.0, 120.0);
      }
    }

    function onDblClick() {
      camCtrl.tpYawOffset = 0;
      camCtrl.tpPitch = 0.42;
      camCtrl.tpDist = 7.5;
      camCtrl.orbitPan = [0, 0, 0];
      camCtrl.radarPan = [0, 0];
    }

    function onContextMenu(e) {
      if (mode === 'free_orbit' || mode === 'radar_2d') e.preventDefault();
    }

    root.addEventListener('pointerdown', onPointerDown);
    root.addEventListener('pointermove', onPointerMove);
    root.addEventListener('pointerup', onPointerUp);
    root.addEventListener('pointercancel', onPointerUp);
    root.addEventListener('wheel', onWheel, { passive: false });
    root.addEventListener('dblclick', onDblClick);
    root.addEventListener('contextmenu', onContextMenu);

    function renderNow() {
      if (destroyed) return;
      const nowMs = global.performance ? global.performance.now() : Date.now();
      const targetPos = extractPos(worldState.status.pos) || [0.5, 64, 0.5];
      if (!smoothBot.initialized) {
        smoothBot.pos = [targetPos[0], targetPos[1], targetPos[2]];
        smoothBot.yaw =
          typeof worldState.status.yaw === 'number' && Number.isFinite(worldState.status.yaw)
            ? worldState.status.yaw
            : 0;
        smoothBot.pitch =
          typeof worldState.status.pitch === 'number' && Number.isFinite(worldState.status.pitch)
            ? worldState.status.pitch
            : 0;
        smoothBot.initialized = true;
      }
      if (mode === 'radar_2d') {
        renderRadar2D(nowMs);
      } else {
        render3DScene(nowMs);
      }
    }

    function resize() {
      if (destroyed || paused) return;
      const rect = containerEl.getBoundingClientRect();
      cssWidth = Math.max(1, Math.floor(rect.width || containerEl.clientWidth || 640));
      cssHeight = Math.max(1, Math.floor(rect.height || containerEl.clientHeight || 400));
      dpr = clamp(global.devicePixelRatio || 1, 1, 2);

      const pxW = Math.max(1, Math.floor(cssWidth * dpr));
      const pxH = Math.max(1, Math.floor(cssHeight * dpr));
      let resized = false;
      if (glCanvas.width !== pxW || glCanvas.height !== pxH) {
        glCanvas.width = pxW;
        glCanvas.height = pxH;
        resized = true;
      }
      if (hudCanvas.width !== pxW || hudCanvas.height !== pxH) {
        hudCanvas.width = pxW;
        hudCanvas.height = pxH;
        if (ctx2d) ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
        resized = true;
      }
      if (resized && !paused) {
        renderNow();
      }
    }

    let resizeObs = null;
    if (typeof global.ResizeObserver === 'function') {
      resizeObs = new global.ResizeObserver(() => resize());
      resizeObs.observe(containerEl);
    }

    function setMode(nextMode) {
      const m = String(nextMode || '').toLowerCase().trim();
      const aliases = {
        first: 'first_person',
        third: 'third_person',
        orbit: 'free_orbit',
        radar: 'radar_2d',
      };
      const resolved = aliases[m] || m;
      if (VALID_MODES.has(resolved) && resolved !== mode) {
        mode = resolved;
        if (!paused) {
          renderNow();
        }
      }
      return mode;
    }

    function setPaused(nextPaused) {
      const target = Boolean(nextPaused);
      if (target === paused || destroyed) return paused;
      paused = target;
      if (paused) {
        if (rafId !== null) {
          global.cancelAnimationFrame(rafId);
          rafId = null;
        }
        lastFrameMs = 0;
      } else {
        resize();
        renderNow();
        if (rafId === null) {
          rafId = global.requestAnimationFrame(tick);
        }
      }
      return paused;
    }

    function updateState(payload) {
      if (destroyed || !payload || typeof payload !== 'object') return;

      if (payload.status && typeof payload.status === 'object') {
        const prevDim = worldState.status.dimension;
        const prevDay = worldState.status.is_day;
        if (typeof payload.status.health === 'number' && payload.status.health < worldState.lastHealth - 0.1) {
          worldState.hurtFlashUntil = (global.performance ? global.performance.now() : Date.now()) + 450;
        }
        if (typeof payload.status.health === 'number') {
          worldState.lastHealth = payload.status.health;
        }
        worldState.status = Object.assign({}, worldState.status, payload.status);
        if (extractPos(payload.status.pos)) {
          worldState.hasTelemetry = true;
        }
        if (worldState.status.dimension !== prevDim || worldState.status.is_day !== prevDay) {
          worldState.blocksBufferDirty = true;
        }
      }

      if (Array.isArray(payload.entities)) {
        worldState.entities = payload.entities;
      }

      if (Array.isArray(payload.blocks)) {
        const arr = payload.blocks;
        let h = (2166136261 ^ arr.length) >>> 0;
        const step = arr.length > 600 ? Math.floor(arr.length / 300) : 1;
        for (let i = 0; i < arr.length; i += step) {
          const b = arr[i];
          const p = extractPos(b);
          if (p) {
            h = Math.imul(h ^ (p[0] | 0), 16777619) >>> 0;
            h = Math.imul(h ^ (p[1] | 0), 16777619) >>> 0;
            h = Math.imul(h ^ (p[2] | 0), 16777619) >>> 0;
          }
          const nm = b && b.name ? String(b.name) : '';
          for (let c = 0; c < nm.length; c += 2) {
            h = Math.imul(h ^ nm.charCodeAt(c), 16777619) >>> 0;
          }
        }
        const sig = `${arr.length}:${h}`;
        if (sig !== worldState.blocksSig) {
          worldState.blocks = arr;
          worldState.blocksSig = sig;
          worldState.blocksBufferDirty = true;
        }
      }

      if (typeof payload.showHurtMarkers === 'boolean') {
        worldState.showHurtMarkers = payload.showHurtMarkers;
      }

      if (Array.isArray(payload.markers)) {
        worldState.markers = payload.markers;
      }

      if (Array.isArray(payload.trajectory)) {
        worldState.trajectory = payload.trajectory;
      }
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      if (rafId !== null) {
        global.cancelAnimationFrame(rafId);
        rafId = null;
      }
      if (resizeObs) {
        resizeObs.disconnect();
        resizeObs = null;
      }
      root.removeEventListener('pointerdown', onPointerDown);
      root.removeEventListener('pointermove', onPointerMove);
      root.removeEventListener('pointerup', onPointerUp);
      root.removeEventListener('pointercancel', onPointerUp);
      root.removeEventListener('wheel', onWheel);
      root.removeEventListener('dblclick', onDblClick);
      root.removeEventListener('contextmenu', onContextMenu);

      if (gl && glRes) {
        gl.deleteBuffer(glRes.vboBlocksOpaque);
        gl.deleteBuffer(glRes.vboBlocksTrans);
        gl.deleteBuffer(glRes.vboDynOpaque);
        gl.deleteBuffer(glRes.vboDynTrans);
        gl.deleteBuffer(glRes.vboLines);
        gl.deleteTexture(glRes.tex);
        gl.deleteProgram(glRes.prog);
        gl.deleteProgram(glRes.lineProg);
      }
      if (root.parentNode) {
        root.parentNode.removeChild(root);
      }
    }

    if (!paused) {
      resize();
      renderNow();
      rafId = global.requestAnimationFrame(tick);
    }

    return {
      setMode,
      getMode: () => mode,
      setPaused,
      isPaused: () => paused,
      updateState,
      resize,
      destroy,
    };
  }

  global.McViewer3D = {
    create: createViewer,
  };
})(typeof window !== 'undefined' ? window : globalThis);
