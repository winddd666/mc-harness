# Phase 0 兼容性实测报告：`mineflayer` + `prismarine-viewer` vs Leaves `1.21.11` (`5ease`)

- **探测时间**：`2026-11-14T16:06:53Z`
- **本机运行环境**：Arch Linux, Node.js `v26.13.1`, npm `12.2.2`
- **依赖版本**：`mineflayer@4.39.0`, `minecraft-data@3.119.0`, `prismarine-viewer@1.33.0`, `prismarine-chunk@1.39.0`

---

## 1. `5ease` 本地测试服连通性实测 (`reachability_check.js`)

| 目标端点 | 地址 | 协议 | 结果 | 延迟 / 说明 |
|---|---|---|---|---|
| 校园网游戏服 (`5ease-lan-mc`) | `10.50.249.97:25565` | TCP | **在线 (`ok: true`)** | `12 ms`，TCP 三次握手立即成功 |
| 校园网 Agent API (`5ease-lan-api`) | `10.50.249.97:18900/api/v1/health` | HTTP | **在线 (`ok: true`)** | `51 ms`，返回 `401 Bearer token required`（符合鉴权预期） |
| Radmin VPN 游戏服 (`5ease-radmin-mc`) | `26.14.9.42:25565` | TCP | 不可达 (`ETIMEDOUT`) | 当前主机直连校园网段，未走 Radmin 虚拟网卡路由 |
| Radmin VPN Agent API (`5ease-radmin-api`) | `26.14.9.42:18900/api/v1/health` | HTTP | 不可达 (`ETIMEDOUT`) | 同上，优先使用 `10.50.249.97` |

---

## 2. `mineflayer` 对 Minecraft Java `1.21.11` 协议握手的实测结论

- **结论：完全原生兼容（`ok: true`，零报错）。**
- `mineflayer@4.39.0`（配套 `minecraft-data@3.119.0`）的 `testedVersions` 已原生包含 `'1.21.11'`（并已覆盖至 `'26.1'`）。
- 使用离线测试账号 `ProbeCam_01` 连接 `10.50.249.97:25565`（Leaves `1.21.11`, `offline` 模式）：
  - 成功完成 `1.21.11` Configuration + Play 阶段握手，`bot.version === "1.21.11"`；
  - `3.71s` 内完成进服 `spawn`（出生点 `[-128.5, 63, 57.5]`, `dimension: "overworld"`, `minY: -64`, `height: 384`）并完整接收周围 `24` 个垂直 Section（`-64..320`）的区块列数据。

---

## 3. 原版 `prismarine-viewer@1.33.0` 在 `1.21.11` 下的精确问题剖析

实测发现原版 `prismarine-viewer@1.33.0` **不会直接崩溃白屏**（中心区块在浏览器 Web Worker `public/worker.js` 中仍能网格化出 `20,688` 个顶点坐标浮点数 / `11,418` 个索引），但直接裸跑存在 **3 个必须修复的工程缺陷**：

1. **Node `v26` 下原版入口强绑原生 C++ `canvas` 导致启动报错**：
   - `prismarine-viewer/lib/mineflayer.js` 调用 `require('../viewer')` 时会连带加载 `viewer/lib/entities.js:7` 的 `require('canvas')`。在 Node `v26.13.1`（`npm install --omit=optional`，无预编译 `node-canvas` 二进制）下直接抛 `MODULE_NOT_FOUND: Cannot find module 'canvas'`。
   - **修复**：服务端旁观流仅需 `WorldView`（区块与实体 Socket.IO 转发），改为按需加载 `require('prismarine-viewer/viewer/lib/worldView')` 即可彻底摆脱 `canvas` / `gl` 原生编译依赖。

2. **方块状态表（`stateId`）停留在 `1.21.4`，导致 `961` 种方块贴图错位 + `72` 种新方块紫黑块**：
   - `prismarine-viewer@1.33.0` 的 `viewer/lib/version.js` 与预打包 `public/blocksStates/`、`public/textures/`、`public/worker.js` 最高仅收录到 `'1.21.4'`（`getVersion('1.21.11') -> '1.21.4'`，`maxStateId = 27865`）。
   - 而 `1.21.5 ~ 1.21.11` 新增了 `72` 种方块（`bush`、`short_dry_grass`、`leaf_litter`、`cactus_flower`、全套 `*_shelf` 书架槽、`copper_torch`、`copper_chest` 等），使 `1.21.11` 的 `maxStateId` 增至 `29670`。
   - 从 `seagrass`（`1.21.4: 2051` vs `1.21.11: 2054`）起的 **`961` 种已有方块发生全局 `stateId` 偏移**。若不转换直接送入原版 `prismarine-viewer`：
     - `torch (3169)` 被误渲染为 `redstone_wire`
     - `chest (3787)` 被误渲染为 `redstone_wire`
     - `crafting_table (5109)` 被误渲染为 `birch_hanging_sign`
     - `furnace (5127)` 被误渲染为 `acacia_hanging_sign`
     - `powder_snow (24487)` 被误渲染为 `waxed_exposed_cut_copper_stairs`
     - `deepslate (27722)` 被误渲染为 `pale_moss_carpet`
     - `stateId > 27865` 的新方块退化为紫黑棋盘 `missing_texture` 方块。
   - **修复（已在 `cam_server.js` 内置实现并验证）**：在 `cam_server.js` 启动时基于 `minecraft-data('1.21.11') -> minecraft-data('1.21.4')` 构建 `Uint16Array(29671)` 精确查找表，在发送 `loadChunk`（仅改写 Section 调色板 `palette` 数组，单区块耗时 `< 0.1 ms`）与 `blockUpdate` 时实时重映射 `stateId`，并将 `72` 种新方块优雅降级到语义一致的 `1.21.4` 贴图（如 `leaf_litter -> brown_carpet`、`*_shelf -> *_planks`、`copper_chest -> chest`），使工作台、熔炉、箱子、火把、细雪、深板岩等关键方块 100% 准确渲染。

3. **负高度深板岩层（`Y < 0`）与线上比赛服旁观限制**：
   - `prismarine-viewer` 前端打包的 `worldrenderer.js` 脏区块循环写死为 `0 <= y < 256`，下深矿至 `Y < 0` 时区块网格不会自动刷新；
   - **线上比赛服安全红线**：`cam_server.js` 需要额外启动一个游戏内 Bot（默认 `LiveCam_01`）。在 `5ease` 本地离线服不仅零成本，还可通过 `ssh 5ease "D:\mc-server\runtime\node\node.exe D:\mc-server\rcon.js \"gamemode spectator LiveCam_01\""` 设为旁观穿墙模式；但在**线上比赛服（`api.nju-aia.com`）**额外登录生存模式小号会消耗积分并引发引怪、抢拾掉落物、卡位碰撞等严重副作用。因此线上比赛监看与历史日志回放统一以内置零侵入 WebGL 引擎 `src/mc_agent/live/static/viewer3d.js`（`window.McViewer3D`）为主力视界，`cam_server.js` 作为 `5ease` 本地测试服的可选实景增强旁路。

---

## 4. 启动与验证命令（`5ease` 本地测试服）

```bash
# 语法检查
node --check live_viewer/cam_server.js

# 在本地启动 5ease 实景旁观 WebGL 服务（默认监听 :8766，自动跟随 mc-live :8765 的目标 Bot 坐标）
node live_viewer/cam_server.js \
  --mc-host 10.50.249.97 \
  --mc-port 25565 \
  --cam-user LiveCam_01 \
  --viewer-port 8766 \
  --follow-api http://127.0.0.1:8765/api/telemetry-pos
```
