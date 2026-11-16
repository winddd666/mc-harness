# MC 竞赛接口能力、实测数据与踩坑说明（队友速览版）

> 更新日期：2026-11-14  
> 适用范围：南大 MC 智能体竞赛（基于官方接口规范 v1.3.7、大模型基准实测与生存服真机联调结果整理）

---

## 1. 赛事系统四端全景图

目前整套比赛环境由 **4 个线上端点** 组成，均已跑通实测：

| 模块 | 地址 / 入口 | 实测技术栈与版本 | 关键机制说明 |
|---|---|---|---|
| **① NMO 皮肤站（账号与角色管理）** | `https://skin.nmo.net.cn` | Blessing Skin `6.0.2` + Yggdrasil 外置登录 (`https://skin.nmo.net.cn/api/yggdrasil`) | 仅限南大邮箱注册（带图片验证码 + 邮箱激活）；激活后**单账号可建多个角色**（`100` 积分/角色，每日签到送积分，改名免费）。已开启 `non_email_login`，**人类大号与多个 Bot 角色可同时在线互不顶号**。 |
| **② MC 游戏服务器** | `mc.nmo.net.cn:25565` → 子服 `survivaltest` | `Velocity 1.7.2-26.2` 代理 + **Java 版 `1.21.11`** | 人类队友可用 `1.21.11` 客户端配置 Authlib-Injector 进服，输入 `/server survivaltest` 实地旁观或配合调试 Bot。 |
| **③ MC Bot 控制 API** | `https://apiin.nmo.net.cn/api/v1`（校外备选 `https://mc-api.windywindie.top/api/v1`） | 规范 **v1.3.7**（托管 Headless Mineflayer 实例） | Bearer Token 鉴权；共暴露 **29 个 HTTP 接口**（认证连接 4 + 无锁观察 7 + 无锁实时控制 3 + 互斥动作 15）。无视觉截图；`v1.3.2–1.3.7` 已补充 `/control`（50–3000ms 原生按键/视线挥砍）、`/look`（定向视角）、`/posture jump` 与 `/status` 速度字段。 |
| **④ 比赛大模型 API** | `https://api.nju-aia.com/v1` | OpenAI 兼容接口，`model="default"`（响应自报 `qwen3.6-35b-a3b`） | **全队共享硬限流 30 次/分钟（失败请求也计入！）**；不支持图片输入（传图返 HTTP 500）；支持 JSON Schema 工具调用。 |

> **关于赛制与计分规则的现状核实**：  
> 截至目前，官方公开页面与接口文档**尚未公布初赛/决赛具体赛制或正式计分权重**。此前同学收集的 `26.3` 进度规划 Excel 并非官方规则文件；经向服务器直接握手核实，当前比赛服真实版本为 **Minecraft Java `1.21.11`**，原版非配方进度共 **125 项**（完整清单已提取至 [docs/advancements-1.21.11.md](advancements-1.21.11.md) 与 `src/mc_agent/data/advancements_1_21_11.json`）。

---

## 2. 感知能力：我们能从接口拿到什么数据？

所有观察接口**不占动作锁**，可在 Bot 寻路、挖矿、战斗的同时并发调用。

### 2.1 七大观察接口与返回内容

1. **`GET /status`（自身全量实时遥测）**
   - **空间与环境**：坐标 `pos: [x, y, z]`（保留 1 位小数）、朝向 `yaw`（弧度）、维度 `dimension`（`overworld` / `the_nether` / `the_end`）、游戏模式 `gamemode`、时间 `time_of_day`、昼夜 `is_day`、天气 `raining` / `thundering`、群系 `biome`、是否着地 `on_ground`。
   - **生存指标**：生命 `health`（0–20）、饱食度 `food`（0–20）、饱和度 `saturation`、氧气 `oxygen`（满值 300）、护甲值 `armor_points`（0–20）、等级 `xp_level` 与升级进度 `xp_progress`（0–1）。
   - **状态标志**：着火 `on_fire`、睡觉 `is_sleeping`、泡水 `in_water`、**头部没入水下 `submerged`**、踩岩浆 `in_lava`、当前载具 `in_vehicle`（`{id, name}` 或 `null`）、潜行 `sneaking`、举盾 `blocking`、正在使用的物品 `using_item`。
   - **装备快览**：主手 `held`、副手 `offhand`。
   - **调度与威胁感知**：**是否在比赛服就绪 `ready`**、**当前占用锁的动作名 `busy`**、**24 格内最近敌对生物 `nearest_hostile: {id, name, dist}`**、最近一次死亡信息 `last_death: {pos, t, cause}`。
2. **`GET /inventory`（完整背包与装备详情）**
   - 返回所有非空槽位 `items[]`（含 `name`、`count`、`slot`）、四件护甲 `armor`（`head`/`chest`/`legs`/`feet`）、`offhand`、`held`、剩余空格数 `empty_slots`。
   - 特殊物品自动附带：耐久 `durability: [当前, 最大]`、附魔 `enchantments: [{name, level}]`、自定义名称 `custom_name`、药水类型 `potion`、旗帜图案 `banner_patterns`。
3. **`POST /find_blocks`（半径 `1–64` 格方块扫描）**
   - **摘要模式**（不传 `types`）：一次性返回周围所有可见方块种类的数量、最近坐标与距离（如 `{"stone": {"count": 120, "nearest": [...], "dist": 5.2}}`），极适合低成本感知周边资源分布。
   - **精确检索模式**（传 `types`、`properties`、`mature`）：搜原木、工作台、熔炉、箱子、床、成熟农作物（`mature: true`）等，按距离返回坐标。
4. **`POST /get_block`（单坐标方块探针）**
   - 返回方块名、`properties`、作物生长阶段 `growth: [当前, 最大]` 与 `mature`、是否可挖掘 `diggable`、硬度 `hardness`，以及非常关键的 **`harvest_tool_ok`（当前手持工具挖它能否正常掉落实物）**。
5. **`POST /get_entities`（半径最高 `64` 格实体雷达）**
   - 支持按大类（`hostile` / `animal` / `player` / `item` / `other`）或具体生物名（如 `zombie`、`villager`、`cow`）过滤，按距离由近到远最多返回 **40 个**实体（含 `id`、`type`、`name`、`pos`、`dist`、`health`，掉落物额外带 `item: "cobblestonex1"`）。
6. **`GET|POST /events`（基于自增序号 `seq` 的增量事件流）**
   - 传入上次返回的 `last_seq` 作为 `since`（单次最多取 500 条），确保轮询间隙不漏掉任何瞬时事件：
     - 战斗与生存：`hurt`（扣血量 + 16 格内疑似攻击者 `likely_attacker`）、`death`（死亡点 + **遗物掉落坐标 `dropped_items_at`**）、`spawn`、`effect_start` / `effect_end`。
     - 交互与世界：`pickup`（捡起何种物品多少个）、`chat`（公屏聊天与系统提示，如 `"Respawn point set"`）、`sleep` / `wake`、`vehicle`（上下载具）。
     - 连接与保护：`server_confirmed`、`kicked` / `disconnected`、`death_loop`（触发死亡循环冷却）。
7. **`POST /advancements`（进度与子条件完成度）**
   - 支持按 `status`（如 `"completed"`）筛选，返回总体完成统计 `summary` 以及每条进度的 `id`、`title`、`frame`、`status`、`completed_at` 和细粒度 `criteria` 进度（例如成双成对、探索所有群系还差哪几个）。

### 2.2 四大感知盲区（设计策略时必须注意）

1. **纯盲玩（无任何画面/截图）**：Bot 拿不到第一人称图像，也没有射线视线 API；一切空间理解都建立在坐标、周围暴露方块与实体列表之上。
2. **反矿透限制**：`/find_blocks` **只返回至少有一面接触空气或透明方块的“暴露方块”**。埋在实心岩层里的钻石/铁矿从地表扫不到，必须进天然矿洞贴着洞壁扫，或人工掘进暴露新表面。
3. **进度树按需下发**：`/advancements` **只包含服务器已下发的可见进度子树**（某大类根节点解锁后，服务端才同步该根节点及往下 2 层）。全新 Bot 刚进服查询时 `total: 0`，因此**长程规划必须读取我们本地提取好的 125 项完整进度树**（`src/mc_agent/data/advancements_1_21_11.json`），不能只看接口返回值做全局规划。
4. **线上字段怪癖**：实测生存服 `/status` 的 `biome` 有时返回空字符串 `""`；`/get_entities` 中地面掉落物（`name: "item"`）和船（`name: "oak_boat"`）的 `type` 均归类为 `"other"`，代码过滤时务必同时匹配 `name` 字段。

---

## 3. 行动能力：我们能让 Bot 做什么？

### 3.1 动作通用规则与硬限制

- **全局动作互斥**：除 `/posture`（切换潜行/举盾）和 `/stop`（紧急中断）外，同一时刻只能跑 1 个动作，并发调用直接返回 `error.code = "busy"`。
- **内置自动寻路**：`/dig`、`/place`、`/interact`、`/attack`、`/window/*` 等均会自动先走到可操作距离内再执行。
- **受伤自动打断保护**：除 `attack` 和 `shoot` 默认只在 `death` 时中断外，其余动作默认 `stop_on: ["hurt", "death"]`——挖矿或赶路途中一旦挨打会立即中断并返回 `interrupted_by: "hurt"` 及 `nearest_hostile`，方便立刻接战斗或逃跑逻辑（注意：**API 不会自动反击**）。
- **连接与防刷屏/防送死限制**：
  - 断线后重连间隔必须 **≥ 30 秒**；
  - **死亡循环保护**：**60 秒内死亡 3 次，Bot 会被强制断开冷却 5 分钟**（返回 `death_loop_cooldown`）；
  - 不支持鞘翅飞行、不支持逐帧跳跃/跑酷微操。

### 3.2 暴露 HTTP 接口速查与锁关系（v1.3.7 共 29 个：核心 27 项 + 新增 `/control`、`/look`）

Base URL：`https://apiin.nmo.net.cn/api/v1`（校园网直连默认，keep-alive 实测 ~9–11ms；校外备选 `https://mc-api.windywindie.top/api/v1`，~164–177ms）

- **认证与连接（4 个）**：`POST /auth/login`、`POST /auth/logout`、`POST /connect`、`POST /disconnect`
- **只读观察（7 个，不占动作锁，动作进行中可高频轮询）**：`GET /status`、`GET /inventory`、`POST /find_blocks`、`POST /get_block`、`POST /get_entities`、`GET|POST /events`、`POST /advancements`
- **实时干预（2 个，不占动作锁，可在动作执行中途随时插入！）**：`POST /posture`（潜行/举盾持久开关）、`POST /stop`（立即刹停当前动作）
- **互斥动作（14 个，同时仅能运行 1 个，并发返回 `busy`）**：

| 类别 | 接口 | 核心能力与自动化程度 |
|---|---|---|
| **移动 / 驾驶** | `POST /move_to` | 走向目标坐标（可省略 `y` 只管水平到达）或追踪 `entity_id`。**骑乘态自动变掌舵**：`in_vehicle` 非空时自动开船（8格/s）、骑马（9格/s）、骑猪（5格/s）、骑炽足兽（4格/s），但**只会直线开不会绕障**（撞岸超时报 `drive_timeout`）。 |
| **载具脱离** | `POST /dismount` | 从船、矿车、马等载具下来。 |
| **挖掘采集** | `POST /dig` | 自动寻路、**自动切换背包内最合适工具**、挖掉方块并自动拾取掉落物，返回 `gained: {物品: 数量}`（工具不对会有 `warning`）。 |
| **放置方块** | `POST /place` | 在指定坐标或身旁空位放方块。**放置瞬间服务端自动潜行**，可直接往箱子、熔炉、工作台、床上放方块而不误触界面。 |
| **目标交互** | `POST /interact` | **对方块**：开关门/拉杆/按钮、睡觉、锄地、播种、撒骨粉、对准方块倒水/舀水/打火石点火。<br>**对实体**：上船/上马/上矿车，或手持物品喂养、繁殖、驯服、剪羊毛、挤奶、系拴绳。 |
| **使用物品** | `POST /use_item` | 等效果结束才返回：吃食物/喝药水牛奶、举盾、扔珍珠传送、**扔末影之眼并直接返回水平方位角 `bearing_deg` 与 `eye_vector`（可直接做双点三角定位找要塞！）**、自动钓鱼（抛竿→等咬钩→收竿）、对准目标举望远镜（刷望远镜进度）、吹山羊角、放船/矿车。 |
| **近战攻击** | `POST /attack` | 追击指定 `entity_id` 直至目标死亡或超时，**自动换近战武器、自动捡战利品**。 |
| **弹道射击** | `POST /shoot` | 支持 `bow`、`crossbow`、`trident`；**服务端自动按原版重力与空气阻力解算仰角，并对移动生物计算提前量**，返回预计飞行 tick 与误差。 |
| **配方合成** | `POST /craft` | 自动用背包 2×2 或附近 3×3 工作台合成指定数量物品；缺材料时在错误详情里直接返回 `example_recipe_needs` 与当前背包。 |
| **容器与交易** | `POST /window/view`<br>`POST /window/act` | **2 个接口**统一抽象 `put` / `take` / `select` / `text` 四种原子操作，支持 **箱子、熔炉/高炉/烟熏炉、工作台、附魔台、铁砧、砂轮、锻造台、切石机、织布机、酿造台、信标、村民交易** 全部 12 类 GUI！ |
| **穿戴切换** | `POST /equip` | 将物品装备到 `hand`、`off-hand`、`head`、`torso`、`legs`、`feet`。 |
| **丢弃物品** | `POST /drop` | 丢弃指定数量的背包物品（可用于猪灵换物、悦灵投递或多 Bot 物资交接）。 |
| **公屏聊天** | `POST /chat` | 最长 100 字符，限频 1 条/秒，严禁 `/` 开头服务端指令。 |

### 3.3 我们的真实操作自由度与硬禁区

**一句话定性**：我们拥有 **“单格级三维空间精度 + 100ms 级异步打断精度 + 全 GUI 槽位级控制 + `50–3000ms` 原生按键/视角微调”**，但没有 **“逐 tick（50ms）连续物理闭环微操与直接视觉画面”**。全游戏 125 项原版进度中约 **95%（~118 项）** 机制完全可达：

- **`v1.3.2–v1.3.7` 关键增强与服务端修复**：
  - **新增接口与参数**：`POST /control`（绕过寻路的原生方向键/跳跃/视线左键 `hit`，支持船顶/墙角脱困、跳劈暴击、微调站位）、`POST /look`（无锁主动转向坐标/实体/弧度，可用于漏斗/活塞等定向放置）、`POST /posture {"jump": true}`（无锁持续踩水浮面或配合 `/place` 垫高）、`POST /attack` 支持 `max_hits` 与 `interval_ms`（方便风筝苦力怕）；`GET /status` 新增水平速度 `speed` 与三轴速度 `velocity`（PR #1）。
  - **已知问题修复（`v1.3.6`）**：`/dig` 改为等待服务端方块变化回执（未破坏时返回 `dig_unconfirmed` 而非假成功）；bot 现在会被生物/玩家/船/矿车碰撞推开；`ready` 变 `true` 会等区块与实体到齐（解决刚进服 `get_entities` 为空）；修复进服第 4 秒内部位置校准吞击退、持非工具误算 500ms 挥击间隔等问题。
- **能做到的高阶细节操作**：
  1. **高空/岩浆海潜行搭桥**：`POST /posture {"sneak": true}` 持久锁住潜行 → 循环 `/place` 脚下前方方块 + `/move_to` 推进，稳过下界峡谷与末地虚空。
  2. **走A拉扯与动态举盾（伪微操）**：后台协程并发盯 `/get_entities`，苦力怕闪白或骷髅抬手瞬间调无锁的 `POST /stop` + `POST /posture {"shield": true}` 格挡弹反，再后撤 `/shoot` 或 `POST /attack {"max_hits": 1}` 风筝。
  3. **原地垫高 / 2×1 阶梯垫高**：`v1.3.7` 可通过 `POST /posture {"jump": true}` 或 `POST /control {"jump": true}` 配合 `/place` 原地垫高；也可往身旁交替放台阶做 2×1 阶梯垫到 `Y=319` 或拆末地水晶铁笼。
  4. **速通式浇筑黑曜石门 / 召唤铁傀儡与凋灵 / 满级信标塔**：`/place` 精确到 `[x,y,z]` 单格，配合 `/interact` 拿水桶、岩浆桶、打火石，无钻石镐也能浇出下界传送门。
  5. **刷村民附魔大厅 + 全自动炼药救僵尸村民 + 猪灵换珍珠**：拆放讲台刷 `/window/view` 交易项；`/use_item` 砸喷溅虚弱药水 + `/interact` 喂金苹果；朝猪灵 `/drop` 金锭等珍珠。
  6. **微航点折线开船**：把长水路切成 10 格一段的水面航点连续 `/move_to`，绕开“载具只走直线”的限制。
- **真正做不到的硬禁区（切勿在这些方向死磕）**：
  1. **极限帧级物理微操**：无图像反馈且 HTTP 单跳仍有延迟，无法做高空落地水（MLG）、重锤高空下落瞬劈（`overoverkill`）、低头跳射风弹（`who_needs_rockets`）。
  2. **复杂红石依赖点击面的精细朝向**：`/place` 仍不支持直接指定点击方块哪一面（面向放格可用 `POST /look` 预先调整玩家朝向改善）。
  3. **长距离悬崖跑酷**：>1 格断崖仍应使用潜行搭桥或挖绕行路，而非原生盲跳。
  4. **鞘翅滑翔**：官方明确不支持鞘翅飞行控制。
  5. **对着方块持续长按类冷门工具**：如考古刷子持续刷可疑沙子（`/use_item` 不接方块目标，`/interact` 为单次点击）。

---

## 4. 比赛大模型 API (`api.nju-aia.com/v1`) 实测能力与红线

详情与原始压测日志见 [benchmarks/nju-api-20261109/REPORT.md](../benchmarks/nju-api-20261109/REPORT.md)：

1. **必填模型名**：请求参数 `model` **必须写 `"default"`**（直接填 `"qwen3.6-35b-a3b"` 会直接 HTTP 403）。
2. **全队硬限流 30 RPM**：每分钟最多 30 次请求，**连 4xx/429 失败请求也扣配额**，且不返回 `Retry-After` 头。**全队所有在线进程/Bot 必须共用限速预算（建议压在 ≤ 24 RPM）**，绝不能让循环每秒无脑调模型。
3. **关闭思考的正确参数**：必须传 `"chat_template_kwargs": {"enable_thinking": false}`（或 `"reasoning_effort": "none"`）。实测顶层传 `enable_thinking: false` **无效**，开启思考时极易把 768–2048 `max_tokens` 全耗光而产不出最终工具调用。
4. **吞吐与长尾抖动**：单并发流式解码中位数约 **82 token/s**，正常首字延迟约 **0.7–1.6 秒**；但实测出现过 **17s、24s、38s 的偶发长尾卡顿**，且当输入上下文堆到 4 万 token 时首字延迟升至 **~19 秒**。
   - **推论**：① 溺水/挨打/躲苦力怕等保命反应**必须由本地 Python 毫秒级反射接管**，不能等 LLM；② 历史对话必须做状态摘要压缩，不能无限追加。
5. **工具格式稳，但原版细节记忆偶发幻觉**：
   - 49 次工具调用 JSON Schema 参数校验全部一次通过；
   - 但 MC 规则题实测 10/12，且模拟任务中出现过：**包里有铁锭却先拿木镐挖钻石**、**把烧 17 个粗铁所需煤炭算成 2 块（`ceil(17/8)` 应为 3 块）**、**声称合成木镐不需要 3×3 工作台**。
   - **推论**：工具等级校验、配方材料表、熔炼燃料数学计算**必须写在本地确定性代码里做前置拦截**，不能靠模型背诵。

---

## 5. 今日生存服真机踩出的 4 个关键坑（必看）

今天我们新建了专用测试号 `5Ease_Bot`，从出生点空中平台护送到生存基地并完成绑床下线，实测踩出 4 个文档没写的真机陷阱：

1. **坑 1：在水里空闲（`busy == null`）会直接沉底淹死，且默认 `stop_on` 会打断水下自救（`v1.3.4+` 已支持 `posture jump` 踩水）**
   - **现象**：Bot 泡在水里一旦没有动作在跑，Mineflayer 不会自动踩水，会一路下沉直至 `oxygen = 0` 溺亡；且默认 `stop_on: ["hurt", "death"]` 会在溺水扣血后打断上浮。
   - **解法**：涉水移动与逃生显式传 **`"stop_on": ["death"]`** 直到上岸；`v1.3.4+` 还可先调无锁 `POST /posture {"jump": true}` 持续踩水浮面（离开水面后再复位）。
2. **坑 2：船没贴岸就 `/dismount` 会导致寻路器假死（`v1.3.3+` 新增 `/control` 可直接脱离）**
   - **现象**：船停在离岸 >1 格的水面调 `/dismount`，原版会把 Bot 放在船体碰撞箱顶上，寻路器会假死；此外旧版空手拆船也因间隔过长被回血抵消。
   - **解法**：
     1. **标准流程**：开船贴岸 `≤ 1` 格再 `/dismount`。
     2. **已卡在船顶或 `stalled` 时的自救**：`v1.3.7` 可直接调 `POST /control {"forward": true, "jump": true, "ms": 300}` 脱离，或重坐 `/interact` / 用剑斧 `/attack` 拆船。
3. **坑 3：白天空手点床会被本地拦截报 `cannot_sleep`，无法绑重生点**
   - **现象**：白天直接调 `/interact {"pos": 床坐标}`，Mineflayer 内部会调 `bot.sleep()` 并在本地直接抛错 `it's not night and it's not a thunderstorm`，根本不给服务器发右键包，导致白天设不了重生点。
   - **解法**：走到离床头 **≤ 3 格**，在 `/interact` 里**显式带上背包里任意一个非方块物品**（例如 `{"pos": [448, 63, 220], "item": "bone"}`），后端就会走“手持物品右键方块”分支直接向服务器发交互包，**白天也能稳稳弹出 `"Respawn point set"` 绑定重生点**！
4. **坑 4：单皮肤站账号多角色登录不能填邮箱**
   - **现象**：当皮肤站账号下有 ≥ 2 个角色（例如大号 + Bot 号）时，用邮箱调 MC API `POST /auth/login` 必返 `401 no_profile`；且同一角色重复登录会让旧 Token 立即作废。
   - **解法**：始终用 **`username="<Bot角色名>"` + 皮肤站密码** 登录，并通过本仓库的 `.state/tokens.json` 探活复用缓存 Token。

---

## 6. 当前仓库就绪进度与团队分工建议

### 6.1 已落地的底座能力（开箱即用）

仓库现已完成 Python 包 `mc_agent` 与命令行 `mc-bot` / `mc-harness` / `scripts/expedition.py`（见 [README.md](../README.md)、游戏向交接专版 [docs/mc-gameplay-handoff.md](mc-gameplay-handoff.md) 与远征实测报告 [docs/expedition-20261114.md](expedition-20261114.md)）：

- **皮肤站与认证会话守护**（`src/mc_agent/skin_station.py`、`src/mc_agent/auth.py`、`src/mc_agent/bot.py`）：查询积分、自动每日签到、幂等新建 Bot 角色、Token `0600` 缓存探活复用、401 自动续期、重连 ≥30s 冷却与 `wait_until_ready()`。
- **1.21.11 零膨胀知识库与进度前沿规划**（`src/mc_agent/kb/`、`src/mc_agent/data/advancements_1_21_11.json`、[docs/advancements-1.21.11.md](advancements-1.21.11.md)）：涵盖 125 项非配方进度目录、`mc_kb_lookup` 按需检索、`mc_advancement_frontier` 可解锁前沿推导，以及工具报错自动注入 `kb_hint`。
- **L0 保命与确定性宏技能库**（`src/mc_agent/skills/safety.py`、`src/mc_agent/skills/library.py`）：已实现 `mc_skill_stabilize`、`mc_skill_harvest_logs`、`mc_skill_mine_blocks`、`mc_skill_craft_gear`（含被埋工作台 `windowOpen` 清理恢复）、`mc_skill_smelt`、`mc_skill_collect_drops`、`mc_skill_bind_bed`、`mc_skill_unstick`、`mc_skill_bunker`、`mc_skill_staircase` 等。
- **多模型异步 Agent Harness 与分阶段托管脚本**（`src/mc_agent/harness/`、`scripts/expedition.py`）：支持 `nju`（24 RPM 节流 + 正文 `tool_calls` 兜底解析）、`deepseek` 与 `dashscope` 路由及分阶段 diff 摘要。
- **当前测试 Bot 状态**：已完成多阶段自主开荒验证并累计解锁 **5 / 125** 项进度（`story/root`、`adventure/root`、`story/mine_stone`、`story/upgrade_tools`、`adventure/kill_a_mob`）；`5Ease_Bot` 当前处于确认 `disconnect` 离线状态，停泊于主世界出生点附近 `[-1.5, 107, 1.5]`（`health=20`、`food=20`，末阶段受雪山细雪冻伤死亡清空背包）。

### 6.2 推荐的三层 Harness 架构与分工切分

结合主流开源方案（`Voyager`、`Mindcraft`、`I0G4N/minecraft-harness`）经验与本赛事 **“无视觉 + 高层阻塞 API + 30 RPM 严苛限流”** 的特点，建议把工作拆成三个解耦模块并行开发：

1. **模块 A：L0 生存守护与状态机层（0 模型调用，毫秒级响应）**
   - 后台轮询 `/status` + `/events` 维护统一 `WorldState`；
   - 本地反射规则：入水防溺水（强制 `stop_on=["death"]` 浮水）、低饱食度自动 `/use_item` 进食、挨打/遇苦力怕自动举盾或撤退、60s 内累计 2 死立刻停工保命（绝不触发第 3 死 5 分钟封号）。
2. **模块 B：L1 确定性宏技能库（Skill Library，0 模型调用）**
   - 把高频长链条封装成带前置校验与失败恢复的 Python 函数：
     - `chop_wood`（搜树→砍树→捡苗补种）、`ensure_crafting_table`（自动放/收工作台）、`upgrade_pickaxe`（木→石→铁→钻全链路合成校验）；
     - `smelt_items`（熔炉放料 + `ceil(n/8)` 精确配煤/木炭 + 定时取产物）；
     - `mine_exposed_ores`（下洞扫描暴露矿 + `harvest_tool_ok` 校验 + 潜行搭路/堵岩浆）；
     - `safe_boat_travel`（贴岸上下船封装）、`set_bed_spawn`（全天候绑床封装）、`chest_sync`（基地公共箱存取物资）。
3. **模块 C：L2 大模型高层规划与多 Bot 协调器（全局共享 ≤ 24 RPM 令牌桶）**
   - 输入压缩后的结构化摘要（自身状态 + 背包汇总 + 基地箱子库存 + 本地 125 进度树当前可解锁的 Frontier 节点 + 最近一次技能报错）；
   - LLM **只负责挑选下一个宏技能目标**（如“去挖 12 个铁矿升级铁套盾牌”或“解锁 `story/smelt_iron` -> `story/obtain_armor`”），不再一格一格指挥走路合成；
   - 若规则允许单队多 Bot 并发，可直接用同一套 `BotManager` 拉起 3–4 个专职 Bot（矿工、基地熔炼合成管家、地面农牧/村民采集员），通过基地坐标共享箱子异步协作。
