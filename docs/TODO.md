# `mc-agent` 阶段成绩、底座修复待办与 AltoClef 脚本化路线图

> 更新日期：2026-11-17  
> 适用架构：**单 Bot 自主运行**；**LLM 负责高层战略规划与决策（选择性思考 <3 RPM），Python 确定性脚本 / 状态机负责绝大部分执行**（重点参考 AltoClef 任务架构并结合 [`mc-agent-api v1.3.14`](https://github.com/Zyzzyva0381/mc-agent-api)）。

---

## 1. 当前阶段成绩基线（2026-11-16 Baseline）

- **工程、测试与运维基线**：
  - 离线单元测试：`uv run --offline pytest -v` **`132 passed`**；`mc-live` / `mc-console` JS 语法校验通过。
  - 四端完整运行：`mc-bot`（NMO 皮肤站建号/签到/Token 复用）、`mc-harness`（选择性思考门控 + 上下文滑动去重 + 19 核心工具 + 护盾守护）、`mc-live`（`:8765` 只读实景直播/轨迹回放 + `:8766` Prismarine 3D 代理，支持 `reasoning_content` 推流与 `restart-live` 热重启）、`mc-console`（`:8770` Web 交互控制台）。
- **竞赛进度与单步链路基线**：
  - 线上比赛服前期托管实测累计解锁 **`5 / 125`** 项原版进度（`story/root`、`adventure/root`、`story/mine_stone`、`story/upgrade_tools`、`adventure/kill_a_mob`）；
  - `nju-beast` 实机跑通石镐 -> 挖铁 -> 熔炉 -> 铁镐全链路（`story/smelt_iron`、`story/iron_tools`）。
- **65 分钟自主长程深潜压测实测成绩**（Run #2 `20261116-162553-371455`，详见 [`docs/endurance-findings-20261116.md`](endurance-findings-20261116.md)）：
  - **连续运行与防卡死**：在纯目标提示词（未硬喂步骤）下自主连续运行 **`3904.0s`（65 分 04 秒，`168` 轮请求，`125` 次工具动作）**，**全程零口号复读死循环**、满背包 Auto-Trash 清杂与熔炉取物**零 30s 卡死**。
  - **自主突破深度**：无人工插队下自主合成 **4 把 `iron_pickaxe`**；第 `72–93` 轮触发思考自行识破“回捡杂物导致 `raw_iron` 未入包”假阳性，主动扩半径 `radius=50` 补铁；三度阶梯下潜最深抵达 **`Y=-37`**，并在第 `140–142` 轮发现暴露 **`deepslate_diamond_ore @ [-150, -33, -385]`** 后向东掘进至距钻石矿仅 **`0.7` 格**（被洞内骷髅射杀）。
  - **模型思考实测性价比**：`qwen3.6-35b-a3b` 开启思考（`n=92`）单次净耗时中位数 **`5.66s`**（`52.2 tok/s`），仅比关闭思考（`2.96s`）多 `2.70s`（不到一次宏动作均耗时 `21.93s` 的 `1/8`）；整局平均请求率仅 **`2.58 RPM`**（`< 10%` 比赛 30 RPM 硬限流）。

---

## 2. Phase 1 底座收口待办（确定性代码层修复，0 LLM 开销）

> **Phase 1 状态**：✅ 已完成封存（Tag `phase1-frozen`，含所有底座卡死、掉落物精准核验、冷却重连与夜间防连死补丁，通过全量离线单测）。

### 2.1 早盘热修复与限长调整（已合入并生效）
- [x] 思考内容展示热修复：`mc-live`（`state.py` + `live/static/app.js`）输出 `[思考]` 区块并支持 `scripts/mc_services.sh restart-live`；`mc-console`（`console/static/app.js`）默认展开 `💭 模型思考规划`（`mc-live` 已重启上线 `b99a827960648379`）。
- [x] 思考模式输出上限防截断：`QwenModel.chat(..., enable_thinking=True)` 时将 `max_tokens` 下限从 `2048` 提至 `4096`（[`src/mc_agent/harness/model.py`](../src/mc_agent/harness/model.py)）。
- [x] `craft_gear("stick")` 原木转木板修复（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）。
- [x] `mine_blocks` 零目标提示明确化 + `_RepeatFailureGuard` 对 `mc_skill_mine_blocks` 忽略 `count`/`radius` 签名去重（[`src/mc_agent/harness/agent.py`](../src/mc_agent/harness/agent.py)）。
- [x] `mc_find_blocks` 对非作物目标自动剥离 `mature` 参数（[`src/mc_agent/harness/mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）。

### 2.2 Run #2 新抓出的底座必修待办（已于 Phase 1 封存前全部完成）

1. **`[P0] 断线与 `death_loop_cooldown` 自动等待重连（`bot.py` / `guardian.py` / `mc_tools.py` / `agent.py`）**
   - [x] 新增 `BotSession.ensure_connected_and_ready` 与 60 秒死亡去重窗口 `record_death_event` / `recent_deaths_in_window`；
   - [x] `run_mc_agent()` 显式捕获 `ApiError` 并开启 `McToolset(wait_for_cooldown=True)`，自动等待 30s 重连间隔或 `death_loop_cooldown` 到期后重连继续；
   - [x] `GenerationGuard.poll()` 捕获动作后 `status()` 断线异常平滑交回重决策，且 `finally` 兜底修正 `run.finished` 的 `status="runtime_error"`（不再错写 `"running"`）。

2. **`[P0] 夜间重生点防连死保护（`library.py` & `agent.py`）**
   - [x] 新增 `MacroSkills.emergency_death_loop_shelter`，当 60 秒滚动窗口内累计死亡 ≥ 2 次时自动建造封闭掩体并等待窗口安全清零，注入 `[防死亡循环保护]` 强制提示直接向下掘进；单次夜间死亡注入 `[夜间重生预警]`。

3. **`[P1] 掉落物识别、定向拾取与防回吸 4 步根治（`src/mc_agent/skills/library.py`）**
   - [x] **按方块预期掉落物族校验真成功**：建立 `EXPECTED_BLOCK_DROPS` 与 `_expected_drops_for_blocks`，`_dig_target_blocks` 将增益拆为 `target_gained` 与 `incidental_gained`，仅当 `target_gained` 入包才返回 `ok: true`，路过误吸杂物触发定向搜捡并在未获目标产物时准确返回 `no_drops_collected`。
   - [x] **定向白名单拾取 + 刚丢杂物黑名单**：`_collect_nearby_items` 按 `target_items` 与价值排序，跳过 `auto_dropped` 刚丢出的物品与低优废石。
   - [x] **岩壁 `1×1` 凹槽卡物跳吸微操**：对高台/矿窝（`0.75 < dy <= 1.85`、`hdist <= 2.4`）自动朝向掉落物执行 `control(forward=True, jump=True)` 跳吸。
   - [x] **圆石封顶配额与背向丢弃**：清杂 `min_free=8`，将圆石 + 深板岩圆石总留存上限压回 `<= 192`（3 组），且首次 `drop` 前先调 `look(yaw=yaw + pi)` 背离当前面朝方向抛出避免回吸。

---

## 3. 已落成 Phase 2 核心底座（2026-11-17 完成，`162 passed`）

- [x] **路径压缩 `MiningDFS` + 工具耐久分级**（[`src/mc_agent/skills/mining_dfs.py`](../src/mc_agent/skills/mining_dfs.py)，详见 [`docs/dfs-mining-multi-location-test-20261117.md`](dfs-mining-multi-location-test-20261117.md)）：
  - 路线已正式收口至鱼骨及其改版：默认策略锁定为 **`fishbone_scan`**（鱼骨骨架 + 目标层侧墙剥皮/受控短切口，兼顾几何安全与钻层采摘率；同时保留 `MC_DFS_STRATEGY=fishbone | v5 | radar` 备查）；落地 0 步死循环根治、目标矿物真实入包校验（防圆石冒充与封墙埋矿）、悬空矿 Catch-Block 托盘、流体/洞穴破口封堵、回溯走廊保护、无石镐自动补石镐保护铁镐与跨层防误恢复。
- [x] **`WorldMemory` 单 Bot 五层世界地图与原位透穿**（[`src/mc_agent/harness/world_memory.py`](../src/mc_agent/harness/world_memory.py)）：
  - 五层轻量底座（核心锚点 `spawn/bed/home/portals/last_death`、仓储箱快照 `containers`、非矿脉白名单稀缺地标 `pois` + 32m 惰性销账 + 300s 冷却降级、生物群系与聚落 `biomes/habitats`、已探索区块足迹 `visited_chunks`）；
  - HUD 末尾 `<= 84` 字单行离线记忆目录 + 0 新工具透穿增强 `mc_find_blocks`、`mc_get_entities`、`mc_skill_mine_blocks`、`mc_move_to`（`>36m` 自动切 `24m` 分段折线远航）与 `craft_gear` 缺料提示。
  - **2026-11-18 实机回测通过**：`MapTest_01` 独立完成 M1–M6 **17/17**（5 区块足迹、107.9m 记忆回捞并真实挖到金块、陈旧 POI 惰性销账、Home Base/容器/缺料提示、床锚点、栖息地、HUD 77 字记忆行、`0600` 持久化重载、下界维度隔离、`last_death`）。报告见 [`runs/world-memory-live-20261118-122519.json`](../runs/world-memory-live-20261118-122519.json)，复盘见 [`docs/world-memory-regression-20261118.md`](world-memory-regression-20261118.md)，复跑脚本 [`scripts/world_memory_live_test.py`](../scripts/world_memory_live_test.py)。实测限制：当前 Leaves/API 的 `status.biome` 为空，群系层仅由离线单测覆盖。
  - **2026-11-18 90 分钟自由巡游**：`MapTest_01` 续跑 208 步，轨迹 **4.1km**、直线位移 **2.08km**、**84** 个区块足迹、**40** 个 POI（water×32/pumpkin×6/lava×2）、**11** 个栖息点（牛/羊/猪/鸡/狼）；后 30 分钟在 `Y≈38` 洞穴卡住，未遇到村庄/容器。复盘见 [`docs/world-memory-roam-20261118.md`](world-memory-roam-20261118.md)，报告见 [`runs/world-memory-roam-20261118-125531.json`](../runs/world-memory-roam-20261118-125531.json)，巡游脚本 [`scripts/world_memory_roam.py`](../scripts/world_memory_roam.py)、摘要脚本 [`scripts/world_memory_roam_report.py`](../scripts/world_memory_roam_report.py)。
  - **2026-11-18 轨迹深审收口（`195 passed`）**：
    - `habitats` 持久化纳入 `save/load`（最近 6 个 cluster，`seen_at` 重载重置并受 15 分钟 TTL 约束）；
    - 全新记忆首帧存活轮询忽略跨会话残留 `status.last_death`，仅记录会话内真实新死亡；
    - `navigate_segmented` 增加 `skip_direct=True` 与单跳 `timeout=12000ms`（消除封闭海底洞窟内单步 `mc_move_to` 阻塞 1003s 问题）；
    - 纯地表探索任务（兼容 `"不要原地挖矿"` / `"观察裸露矿脉"` 表述）自动抑制 HUD 挖矿催促并在禁挖矿模式下剔除 `mc_skill_mine_dfs`。

---

## 4. 待办分级清单（按实现难度与职责边界严格隔离）

### 🟢 外包搬砖区（`[L1 / L2]`：纯客观数据 & 纯单接口原子封装）

> **⚠️ 外包硬红线**：本区任务**严禁编写任何前置阻断门禁、任务调度优先级、自动跳转挖矿或主观战术规则**；只允许填写客观原版事实字典，或编写“给定明确参数 -> 调单一窗口接口返回结果”的无策略纯封装函数。

- [x] **`[L1-Data]` 99 个计分成就的 `1.21.11` 客观事实与微攻略填表**（[`src/mc_agent/kb/data_1_21_11.py`](../src/mc_agent/kb/data_1_21_11.py)）
  - 已覆盖 `RULES.md` 全部 99 项计分成就（`how_to` 均 `<= 50` 字），并补充末影之眼、纸/书、标靶、音符盒、蛋糕、拴绳、狼铠、信标、重生锚、比较器、雕纹书架、铜灯等 1.21.11 客观配方；**未改动 `curriculum.py` 的目标筛选与排序逻辑**。
- [x] **`[L2-GUI-Chest]` 箱子/木桶容器批量存取原子函数**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）
  - 已封装 `container_batch` / `container_deposit` / `container_withdraw`：按给定物品数组生成 `sections.container` 的 `put`/`take` 单次 `window/act`，完成后调用 `world_memory.update_container_from_window` 同步库存快照。
- [x] **`[L2-GUI-Enchant]` 附魔台单次连招原子函数**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）
  - `enchant_item_once` 在**同一次 `window/act`** 中按 `put item -> put lapis -> select option -> take item` 顺序执行；青金石数量省略时按 API 规则取 `option + 1`。
- [x] **`[L2-GUI-Merchant]` 村民/流浪商人按索引交易原子函数**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）
  - `merchant_trade` 先 `window/view` 读取 `options`（`give`/`get`/`available`），再按指定 `option` 或匹配 `get_item` 执行 `window/act select(option, times)`；不添加任何调度/前置判断。
- [x] **`[L2-GUI-Stations]` 酿造台 / 铁砧 / 锻造台 / 切石机原子函数**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）
  - `brew_once`（`bottles`/`ingredient`/`fuel` + 轮询 `brew_time_left`）、`anvil_operate`（`left`/`right` + `text`）、`smithing_apply`（`template`/`base`/`addition`）、`stonecutter_cut`（`input` + `select`）均已按比赛 API 固定 sections 名封装；对应 7 个 `mc_skill_*` 工具仅在 full profile 注册，不膨胀 compact 核心工具集。
  - **2026-11-18 实机验收通过**：独立 `GuiTest_01`（独立 `.state-gui-test` Token，不触碰 `BeastBot_01`）在 nju-beast 天空测试台完成 10/10 项冒烟；随后完成 **A–F 六组 33/33 全量回归**（容器/WorldMemory、顺序敏感合成台、附魔/铁砧/锻造/砂轮/织布机、两段酿造/熔炉三件套/切石机、流浪商人与双材料图书管理员、信标）。报告见 [`runs/gui-window-full-A-B-C-D-E-F-20261118-120840.json`](../runs/gui-window-full-A-B-C-D-E-F-20261118-120840.json)，复盘与上游连续附魔窗口陈旧问题见 [`docs/gui-window-regression-20261118.md`](gui-window-regression-20261118.md)，复跑脚本 [`scripts/gui_window_full_live_test.py`](../scripts/gui_window_full_live_test.py)。
  - **连续附魔防护与生产接线收口（`195 passed`）**：
    - `mc_skill_enchant` 默认 `verify=True` + `reconnect_on_stale=True` 校验落包；`_reset_window_session` 支持比赛服 30s 重连冷却自动回退；
    - 7 个新增 `mc_skill_*` GUI 工具全部接入 `McToolset._run_skill(...)`（统一断线/隔离自愈与坐标缓存更新），并在 `compact` 模式按任务/进度关键词精准按需解锁（单轮总工具预算保持 `< 3k tokens`）；
    - 无坐标开箱（仅传 `block="chest"` / `"barrel"`）自动在 6 格内补全容器坐标同步 `WorldMemory`；`mc_skill_anvil` 支持 `left_enchanted`/`right_enchanted` 筛选与满耐久/互斥附魔 `no_output` 原因提示；`BotSession.call` 首轮捕获 HTTP keep-alive 空闲断连 `RemoteProtocolError` 自动重建连接重试。

---

### 🔴 核心主攻区（`[L3 / L4]`：前置树、全局调度、空间策略与异步锁博弈 —— 禁外包）

> **说明**：凡涉及硬/软前置边界、跨阶段状态流转、虚空岛空间巡航策略、互斥动作锁时序及双并发协调的模块，统一留在本区主攻把关。

- [x] **`[L3-Sched-1]` 课程持久单会话化与动态轮数预算**（[`src/mc_agent/harness/curriculum.py`](../src/mc_agent/harness/curriculum.py) & [`agent.py`](../src/mc_agent/harness/agent.py)）
  - `CurriculumSupervisor` 全场共享唯一持久 `McToolset`（跨集保留 `WorldMemory` 与 `MiningDFS` 矿井树），按 `micro/short/medium/deep/expedition` 5 档动态分配 `8~45` 轮与 `180~1800s` 预算，并对真实钻石/黑曜石/区块增量实行免 Strike 保护。
- [x] **`[L3-Sched-2]` 独立 `ensure_items` 0-LLM 物理硬前置递归器**（[`src/mc_agent/skills/ensure.py`](../src/mc_agent/skills/ensure.py)）
  - **仅编码客观原版配方、熔炼对与采集镐级硬定律**（0 主观软门禁），优先计入副手/护甲与 `WorldMemory` 基地仓箱提货、自动走回 28m 内既有熔炉/工作台复用，并在步间调用 `check_and_stabilize` 守护生存、遇看脸材料或近敌受击立即干净交回。
- [x] **`[L3-Sched-3]` `select_next_target` 联动 `WorldMemory` + 套餐合并 + 环岛摸底**（[`src/mc_agent/harness/curriculum.py`](../src/mc_agent/harness/curriculum.py)）
  - 联合盘点背包+穿戴+`WorldMemory.containers` 库存并注入取箱提示；`Y=-54` 钻石套餐支持先合 `diamond_boots` 锁 75 分再同层顺采钻镐/附魔台钻料；`trade`/`lava_bucket`/`form_obsidian`/`sleep_in_bed`/`breed_an_animal` 绑定 `WorldMemory` 探明后动态提权；铁器成型支持单次有界地表环岛摸底。
- [ ] **`[L3-Explore]` 基地自动归仓策略与虚空岛广域巡航开图技能**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py) & [`world_memory.py`](../src/mc_agent/harness/world_memory.py)）
  - 包满/远征前返 `home` 分类归仓与提货；结合 `unexplored_directions` 与 `VoidGeofence` 安全边界的环岛开图巡航。
- [ ] **`[L3-Router]` 按场景动态裁剪工具集 `SceneRouter`（延后储备）**
  - 待总工具扩展至 25+ 后，按场景档位硬裁剪至 `9~13` 个工具并在切档瞬间归档滑动窗口旧 `tool_calls`。
- [ ] **`[L4-Combat-Core]` 主动战斗防御闭环（[`docs/战斗fight(1).txt`](战斗fight(1).txt) 模块 1–6 + `GenerationGuard` 协同）**
  - 脆弱度分级撤退阈值 + 12s 纠缠主目标锁定防抖 + 无锁实时转向举盾（`/look` + `/posture(shield=true)` + `/stop`）与占锁三区拉扯近战 / `POST /shoot` 远程反制的微秒级锁时序对齐。
- [ ] **`[L4-Combat-1.21]` 1.21 特殊生物专项博弈状态机（模块 7）**
  - 卫道士破盾拉扯、旋风人禁箭切近战、嘎吱无敌避让挖心、劫掠兽挡冲后撤、烈焰人“着火 3s 盾挡 -> 熄火 5s 输出”节拍控制与恶魂 64 格预警。
- [ ] **`[L4-Sched-4]` 第 2 路 LLM 并发槽「异步只读战略参谋」**
  - 前 3 步稳定后，利用比赛 `Concurrency = 2` 的第 2 路槽位低频运行只读 `Strategic Planner`（`enable_thinking=True`），动态重排目标套餐队列与卡点止损跳线。
