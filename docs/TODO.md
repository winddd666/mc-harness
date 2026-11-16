# `mc-agent` 阶段成绩、底座修复待办与 AltoClef 脚本化路线图

> 更新日期：2026-11-16  
> 适用架构：**LLM 负责高层战略规划与决策（选择性思考 <3 RPM），Python 确定性脚本 / 状态机负责绝大部分执行**（重点参考 AltoClef 任务架构并结合 `mc-agent-api v1.3.7`）。

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

## 3. 战斗与主动防御脚本化待办（基于 [`docs/战斗fight(1).txt`](战斗fight(1).txt)）

> 对应接口前提：我们无逐 tick 视觉，且 14 个动作全局互斥（不能边挖边砍），但拥有不占动作锁的 `GET /status`、`POST /get_entities`、`POST /posture {"shield": true}`、`POST /look`、`POST /stop`，以及支持 `max_hits`/`interval_ms` 的 `POST /attack` 与自带重力/移动提前量解算的 `POST /shoot`。

按 [`docs/战斗fight(1).txt`](战斗fight(1).txt) 归纳的 7 个模块拆解 Python 战斗防御脚本（`MobDefenseSkill` + `GenerationGuard` 升级）：

- [ ] **① 自身脆弱度与危险生物分级撤退（模块 2、3、6）**
  - 按 `health`、`armor_points`、是否有 `shield` 及周围敌对数量/类型计算脆弱阈值；
  - 命中「点燃的苦力怕、凋灵骷髅、疣猪兽、僵尸疣猪兽、守卫者、远古守卫者、坚守者」以及高风险环境时直接触发击退/举盾撤退或封口撤离，不硬拼。
- [ ] **② 怪物持续纠缠计时器与主目标锁定防抖（模块 4、5）**
  - 为视野/警戒半径内敌对实体 ID 记录首次持续威胁时间戳（对骷髅、女巫放大关注半径），超过默认 **12 秒** 且存在可达视线时加入“主动清除集合”；
  - 锁定单一主目标 ID，除非目标死亡、消失或寻路判不可达否则不频繁换怪；当主目标进入 `10m` 内时，将其他杂兵的自动击退/防干扰范围收缩至 `2m`。
- [ ] **③ 盾牌实时格挡 + 三区距离近战与 `POST /shoot` 远程接管（模块 1、5、7）**
  - **三区距离控场**：过远用 `move_to(entity_id)` 接近；进入有效近战区按武器冷却间隔执行 `attack(max_hits=1..2, interval_ms=625)`；过近贴脸时执行击退后撤或跳劈；
  - **无锁举盾与远程反制**：骷髅/流浪者/沼骸/女巫/唤魔者进入视野后，无弓箭且近身前先调 `look` 面向威胁 + `posture(shield=true)` 挡箭；持有 `bow` / `crossbow` 与 `arrow` 时，对骷髅群、女巫、唤魔者及烈焰人熄火间隙优先调用 `shoot`。
- [ ] **④ 1.21 特殊生物专项硬规则（模块 7）**
  - **幻翼**：刷新即带高威胁关注；
  - **卫道士**：会破盾，禁止依赖纯举盾站撸，优先拉开距离/远程或垫高；
  - **旋风人**：免疫弓箭伤害，强制切近战并注意高跳位移；
  - **嘎吱**：本体无敌，识别后优先撤离或转交挖除 `creaking_heart`；
  - **劫掠兽**：`1v1` 先举盾挡冲击后砍 `1–2` 刀即迅速后撤躲咆哮；
  - **烈焰人 / 恶魂**：恶魂 64 格高危火球预警、烈焰人 48 格警戒与“着火 3s（盾挡 3 小火球）-> 熄火 5s（输出窗口）”节拍控制及防掉落物坠崖；
  - **溺尸**：35 格警戒，白天不主动下水即可规避大部分仇恨；
  - **史莱姆 / 岩浆怪**：分裂体列入主动防御清理优先级。

---

## 4. Phase 2 架构路线图：LLM 战略规划 + AltoClef 式确定性脚本树

核心范式：**LLM 只做阶段目标选择、异常重规划与多目标统筹（`enable_thinking=True` 按节点触发，消耗 `<3 RPM`）；Python 脚本树（借鉴 AltoClef）承担 90%+ 的分解采集、合成、探矿、战斗与容错状态迁移**。

1. **优先级 `TaskChain` 仲裁环（对标 AltoClef `TaskRunner` / `TaskChain`，0 LLM 开销）**
   - [ ] 在长脚本每个 `5–10s` 执行切片之间运行固定优先级仲裁：
     `SurvivalChain`（水/岩浆/着火/夜间防连死） → `FoodChain`（安全进食） → `MobDefenseChain`（[`docs/战斗fight(1).txt`](战斗fight(1).txt) 7 模块） → `ActiveGoalTask`（当前任务脚本）。
2. **`TaskCatalogue` 递归前置树（对标 AltoClef `ResourceTask`）**
   - [ ] 实现确定性 `EnsureResourceTask(target_item, count)`：收到高层目标（如“准备 1 套铁镐 + 盾牌 + 铁胸甲”或“凑齐 4 钻石合成钻石靴”）时，在本地配方与采集表里递归展开前置缺口（木头 → 木板/工作台 → 石镐 → 铁矿 → 熔炉烧铁 → 铁镐 & `shield`），单轮脚本自动跑完全链路，仅在彻底缺生态条件或异常中断时交还 LLM。
3. **`WorldLedger` 世界与容器账本（对标 AltoClef `BlockTracker` / `ContainerTracker`）**
   - [ ] 持久化记录：井口 `Collar` 与地下安全航点 `waypoints`（死亡回 `Y=70` 后 0 挖掘沿现成矿井速降，避免再次磨爆多把铁镐）、已放置工作台/熔炉/箱子与存储物资、床重生点坐标、`chunk` 级别不可达/水体黑名单冷却。
4. **路径压缩 `MiningDFS` + 工具耐久分级（详见 [`docs/mining-dfs-design-20261116.md`](mining-dfs-design-20261116.md)）**
   - [ ] 落地 `Root Collar` / `Switchback Landing` / `Spine Junction` 三类节点、悬空矿石托盘（Catch-Block）、洞穴破口圆石封堵、航点倒序零挖掘回溯；
   - [ ] **工具耐久分级策略**：开凿普通 `stone`/`deepslate` 矿道强制优先使用 `stone_pickaxe`，严格将 `iron_pickaxe` 耐久留给铁/红石/金/钻石矿脉；下深层前强制完成 **`shield`（1 铁 + 6 木板）** 与食物准备。
5. **比赛双 Bot 异构分工并发（结合 `benchmarks/nju-api-20261109` 实测）**
   - [ ] 基于共享 `24 RPM` 全队限速桶支持双 Bot 协同：
     - **Bot A（深潜/探险/下界主攻手）**：高频或全开思考处理复杂洞穴与深层目标；
     - **Bot B（地表基地/砍树/农牧/盾牌与食物工兵）**：常规动作默认 `enable_thinking=False` 极速运行脚本、遇异常再局部唤醒思考，向基地公共箱存肉/木/盾/备用镐。
