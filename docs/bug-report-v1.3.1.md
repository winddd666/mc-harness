# Minecraft Agent API v1.3.1 内测真机反馈与底层动作接口诉求

- **测试时间**：`2026-11-11 11:17Z ~ 11:42Z`（北京时间 `19:17 ~ 19:42`）
- **测试环境**：`https://mc-api.windywindie.top/api/v1` → `mc.nmo.net.cn` (`survivaltest`, Java `1.21.11`)
- **测试角色**：`5Ease_Bot`

---

## 核心结论与接口诉求摘要

在 `survivaltest` 真机实测过程中，我们发现当前 API 将 **100% 的位移能力强绑定在 `mineflayer-pathfinder` 高层寻路上**，且攻击接口仅有“按固定慢速追杀到底”一种模式，缺少最基础的**无寻路短距移动、跳跃/浮水、单次/原速攻击**等底层原语。

一旦 Bot 在连续物理世界中进入 `pathfinder` 无法处理的中间态（如水中回合间隙、站在船体碰撞箱顶、被睡莲/方块边角卡住、需要拉扯苦力怕或空手收船），就会直接陷入**必死循环或永久死锁**。与其针对单一场景逐个打补丁，**我们希望申请补充一组受控的底层基础动作接口（做成带持续时长 `ms` 或次数参数的单次 HTTP 调用即可，无需逐 tick 高频发包）**：

1. **基础方向移动与跳跃接口（如 `POST /control` 或 `/step`，并在 `/posture` 中支持 `jump`）**：不经过 `pathfinder`，支持指定朝向 `yaw/pitch` 与基础键位（`forward`/`back`/`left`/`right`/`jump`/`sprint`）持续 `100~2000ms`，同时允许在 `/posture` 开启持久 `jump` 用于水中自动踩水浮面。
2. **细粒度攻击接口（扩展 `POST /attack` 支持 `max_hits` 单次/定次挥击，并按手持物品原版攻速计算冷却）**：支持打一下就撤（风筝苦力怕、反弹火球），并解除当前硬编码 `650ms` 挥击间隔限制（恢复原版空手 `250ms` 冷却即可正常收船/拆矿车）。
3. **基础视角与原生方块右键（如 `POST /look` 或允许指定放置/交互朝向，修复白天点床本地拦截）**：支持控制朝向放置方向性方块，并允许白天对床发送右键交互包设置重生点。

以下为支撑上述诉求的 **4 组真机复现现场与原始报文证据**：

---

## 现场一：缺少 `jump`/踩水原语 —— 水中回合间隙垂直沉底，且默认 `move_to` 被溺水伤害自我打断

### 1.1 触发机理
- 每次动作结束（`busy: null`）后端会清空所有按键状态，而 `/posture` 仅有 `sneak` 和 `shield`，**没有 `jump`（空格踩水）**。只要 Bot 在水里因动作报错或等待大模型响应（受 `30 RPM` 限流约束）停顿几秒，就会像松开空格一样自由沉入水底。
- 一旦氧气耗尽（顺带反馈文档差异：规范 3.1 写 `oxygen` 满值为 `300`，1.21 实测满值为 **`20`**，`-1` 开始扣血），调用默认参数的 `POST /move_to` 尝试游回水面时，默认 `stop_on: ["hurt", "death"]` 会在 **`< 1s` 内被自身的溺水扣血强行掐断**，导致永远游不出深水区。

### 1.2 真机抓包数据（`2026-11-11T11:20:09Z ~ 11:20:29Z`）
`POST /move_to` 游至 4 格深水面 `[434.5, 62.6, 226.6]` 结束后保持 `busy: null`，每 `1.5s` 轮询 `GET /status`：

| 相对时间 | `pos` (`[x, y, z]`) | `in_water` | `submerged` | `oxygen` | `health` | `busy` |
|---|---|---|---|---:|---:|---|
| `+0.2s` | `[434.5, 62.6, 226.6]` | `true` | `false` | `20` | `20.0` | `null` |
| `+1.8s` | `[434.5, 61.3, 226.6]` | `true` | `true` | `20` | `20.0` | `null` |
| `+6.8s` | `[434.5, 59.0, 226.6]` | `true` | `true` | `13` | `20.0` | `null` |
| `+17.2s` | `[434.5, 59.0, 226.6]` | `true` | `true` | `0` | `20.0` | `null` |
| `+18.8s` | `[434.5, 59.0, 226.6]` | `true` | `true` | `-1` | `19.5` | `null` |

随后调用默认 `POST /move_to {"x": 447, "y": 63, "z": 220, "range": 1}` 尝试上浮逃生，仅上浮 `1.4` 格（`681 ms`）即被溺水 `hurt` 打断：
```json
{
  "ok": false,
  "ms": 681,
  "error": {
    "code": "interrupted",
    "message": "stopped because of hurt (The goal was changed before it could be completed!)",
    "cause": "hurt",
    "health": 16.33333396911621,
    "pos": [434.8, 60.4, 226.5]
  }
}
```

---

## 现场二：缺少非寻路移动原语 —— 水面 `/dismount` 踩在船体碰撞箱顶（`on_ground: false`），全局寻路永久死锁

### 2.1 触发机理
- 在离岸 > 1 格的水面上调用 `POST /dismount` 时，原版会将角色直接放在**船实体的硬碰撞箱正上方**（`pos.y = 63.1`），此时脚下 `Y=63` 为空气、`Y=62` 为水，`GET /status` 为 `in_vehicle: null, in_water: false, on_ground: false`。
- 由于 API **没有任何绕开 `mineflayer-pathfinder` 的基础方向键移动接口**，而 `pathfinder`（及底层 `prismarine-physics`）只检测方块网格、不检测船实体的硬碰撞箱，看到 `on_ground: false && in_water: false` 直接陷入无限死锁——不走动、不报错、不释放 `busy` 锁，直到客户端 HTTP 超时（人类玩家只需按 `0.2s` 方向键或跳一下即可脱离）。
- **补充说明：即便分两步“先 `move_to` 进水里、再 `move_to` 去陆地”也同样死锁**。抓包现场 `X=443..446` 全是纯水域，调用 `move_to` 后 Bot 的 `yaw` 已从 `1.21` 转向 **`-1.57`**（正对 `+X` 紧邻水格 `[444.5, *, 218.5]`），证明 A* 规划出的第一个路点 `path[0]` 本身就是脚边的水格；而 `mineflayer-pathfinder` 的 `monitorMovement` 与 `Physics.canStraightLine` 每 tick **只读取 `path[0]`**（与终点是水还是陆地无关），连迈向相邻第一格水都会因船顶 `on_ground: false && in_water: false` 状态卡死（若目标水格落在 `range` 内则会在 `stateGoal.isEnd` 直接 `0ms` 原地假成功，一步不动）。

### 2.2 真机抓包数据

复现时间：`2026-11-11T11:17:59Z ~ 11:18:10Z`（及 `11:32:18Z` 二次复现）

- `POST /api/v1/dismount` 返回：
  ```json
  {"ok": true, "ms": 952, "result": {"dismounted": true, "vehicle": {"id": 88479, "name": "oak_boat"}, "pos": [443.8, 63.1, 218.5]}}
  ```
- 下船后 `GET /api/v1/status`：
  ```json
  {"pos": [443.8, 63.1, 218.5], "yaw": 1.21, "in_water": false, "in_vehicle": null, "on_ground": false, "busy": null}
  ```
- 调用 `POST /api/v1/move_to {"x": 447, "y": 63, "z": 220, "range": 1}` → 等待 `8009 ms` 触发客户端 `ReadTimeout`；调 `/stop` 前查 `GET /status` 确认 `yaw` 已对准相邻水格（`-1.57`），但后端仍死锁在原地（位移 `0.0` 格）：
  ```json
  {"pos": [443.8, 63.1, 218.5], "yaw": -1.57, "on_ground": false, "in_water": false, "busy": "move_to"}
  ```

---

## 现场三：`/attack` 硬编码 ~`650ms` 间隔且无单次攻击 —— 空手连打 16 拳无法收船，也无法拉扯怪物

### 3.1 触发机理与三次计时实证
我们对脚下的橡木船（`id=88479`）分别用 `6s`、`8s`、`10s` 超时空手调用 `POST /attack`，三次返回结果如下：

| 轮次 | `timeout` | 返回 `hits` | 返回 `ms` | 平均每击间隔 | `killed` |
|---|---:|---:|---:|---:|---|
| 第 1 次 | `6000` | `10` | `6503` | **`650.3 ms`** | `false` |
| 第 2 次 | `8000` | `13` | `8456` | **`650.5 ms`** | `false` |
| 第 3 次 | `10000` | `16` | `10402` | **`650.1 ms`** | `false` |

- **原因分析**：后端 `/attack` 固定以 **`650ms`（13 ticks）** 周期挥击。在原版 `VehicleEntity` 机制中，船只空手受击累计伤害 `+10`，但每 tick 自动衰减 `-1`（`13 ticks` 衰减 `-13`），每一拳挥出前船只伤害都已归零（而原版玩家空手满蓄力冷却仅 `250ms` / 5 ticks，连点 8 下即可拆掉船）。
- 同时，因为 `/attack` 只能“追砍到目标死亡或超时”（实测不传 `timeout` 默认跑满 `20000ms`），Agent 无法对苦力怕做“砍一刀就退（Hit-and-Run）”的基础拉扯操作，也无法通过 `jump + hit` 打出跳劈暴击。

---

## 现场四：高层封装吞掉原生交互 —— 白天无 `item` 调 `/interact` 点床被本地 `0ms` 拦截，无法绑重生点

- 在白天（`is_day: true, time_of_day: 6846`）站在床旁调用 `POST /interact {"pos": [448, 63, 220]}`，后端直接进入 `bot.sleep()` 本地校验，耗时 **`"ms": 0`** 直接返回：
  ```json
  {"ok": false, "ms": 0, "error": {"code": "cannot_sleep", "message": "it's not night and it's not a thunderstorm"}}
  ```
  完全未向游戏服发右键包，导致白天无法正常设置重生点（只有额外传一个非方块物品如 `{"pos": [448, 63, 220], "item": "apple"}` 绕过 `sleep` 分支时，才会走 `activateBlock` 耗时 `432ms` 发包成功）。

---

## 建议补充的轻量底层接口设计（防高频发包、易实现）

为兼顾服务端负载（避免 20Hz 按键流打穿隧道）与 Agent 的物理脱困/微操自由度，建议补充或小幅扩展以下接口：

1. **`POST /control`（或 `POST /step`：受控时长的无寻路基础移动与跳跃）**
   ```json
   {
     "forward": true,
     "back": false,
     "left": false,
     "right": false,
     "jump": true,
     "sprint": false,
     "yaw": 1.57,
     "ms": 400
   }
   ```
   - 后端直接 `setControlState` 保持 `ms`（限制 `50 ~ 3000ms`）后自动释放并返回最新 `pos`。一次请求即可完成“跳离船顶碰撞箱”“水中上浮”“助跑跳沟”“原地 `jump` 配合 `place` 垫高”等全部非寻路动作。
2. **扩展 `POST /posture` 支持浮水/跳跃保持**
   - `{"sneak": false, "shield": false, "jump": true}`（或后端在 `in_water && busy == null` 时默认开启自动浮水），彻底消除大模型思考间隙沉底溺亡问题。
3. **扩展 `POST /attack`（或新增单次攻击 `POST /hit`）**
   - 增加可选参数 `"max_hits": 1`（打够次数立即返回，支持拉扯苦力怕与单次敲击）；
   - 将挥击间隔改为按当前主手物品的真实攻击冷却计算（空手 `250ms`、剑 `625ms`、斧 `1000ms`），使空手拆船/拆矿车恢复原版正常表现。
4. **补充 `POST /look`（设置 `yaw` / `pitch`）及白天床 `activateBlock` 兜底**
   - 支持主动转向（便于观察/定向放置漏斗活塞等方向性方块）；白天对床调用 `/interact` 时仍发送一次 `activateBlock` 右键包以支持全天候绑定重生点。
