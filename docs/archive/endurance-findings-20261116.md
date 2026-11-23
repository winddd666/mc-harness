# 「钻石护体 (`story/shiny_gear`)」长程生存压测实测与双并发复盘报告（2026-11-16）

> 测试目的：在阶段一“拒绝卡死”补丁合入后，通过长程实机自主生存暴露边界卡点、工具误用、思考模式时延收益与机制缺口，为二阶段（`WorldLedger` / `MiningDFS` / 装备防御前置）以及比赛多 Bot 调度积累一手数据。

---

## 1. 测试概览与两轮对照基线

- **目标成就**：`story/shiny_gear`（「钻石护体」）
- **统一预算与配置**：`max_calls=400`、`run_timeout_s=12600s` (3.5h)、`--provider nju`（`model="default"`, `qwen3.6-35b-a3b`）、`--tool-profile compact`（19 核心工具）、`--context-mode sliding`、`--hud-mode ephemeral`、`--survival-guard`

| 轮次 | Turn ID | 思考门控与提示策略 | 实测时长 / 请求数 | 结果与核心观察 |
|---|---|---|---|---|
| **Run #1** | `20261116-154639-182952` | 全程关闭思考（`enable_thinking=False`）+ 提示含“清理杂物” + 28 工具膨胀 | `75` 轮人工止损 | 验证了满背包 **Auto-Trash** 与熔炉零超时（`R20` 耗时 `18.9s`），但无思考在树冠上方（`*_log`）与 4-shot 口号间陷入复读死循环 |
| **Run #2** | `20261116-162553-371455` | **选择性思考门控** + 纯目标提示 + 19 核心工具 + `descend_canopy` 修复 | **`3904.0s` (65m 04s) / `168` 轮 / `125` 次工具执行** | 零复读死循环；自主合出 **4 把 `iron_pickaxe`**，最深下潜 `Y=-37` 并在 `Y=-32` 贴近暴露 **`deepslate_diamond_ore` 仅 `0.7` 格**；最终在夜间出生点 47 秒内连死 3 次触发 `death_loop` 断线（客户端未捕获 `ApiError(not_connected)` 退出） |

---

## 2. Run #2 全轨迹五阶段复盘（`R01–R168`）

事件流：[`runs/20261116-162553-371455.events.jsonl`](../runs/20261116-162553-371455.events.jsonl)

### 2.1 五阶段演化过程

1. **阶段一 `R01–R26`（`0–423s`，夜间地表破局与第 1 把铁镐）**
   - 夜间于 `[-180, 68, -407]` 起步，`R08` 合成 `stone_pickaxe`，`R17–R24` 通过隧道与阶梯下探挖得铁矿，`R25` 熔炼铁锭，**`R26` 合成并装备第 1 把 `iron_pickaxe`**。
   - 暴露早盘小摩擦：`R01/R02/R04` 空区仅修改 `count` 重试、`R13` `craft_gear("stick")` 未将原木先合木板、`R22` `mc_find_blocks(["iron_ore"], mature=True)` 触发 `bad_args`、`R03/R05` 命中 `2048` 思考长度截断。
2. **阶段二 `R27–R52`（`423–1599s`，首轮深潜 `Y=-37` 与首把铁镐磨爆）**
   - `R27–R41` 一路阶梯下探到 `[-194.5, -37, -390.9]`，期间 `R37` 熔炼 `raw_iron` 再次触发 Auto-Trash 自动丢弃 `diorite`/`gravel`/`dirt` 零卡死入包，地下两次遭怪袭均由 `guardian` 脱险。
   - `1191.8s`（`R44`）在 `[-194.5, -37, -390.9]` 首次遇怪阵亡并传回地面 `[-166.5, 70, -382.5]`（服务器开启死亡保留背包，此时第 1 把铁镐仅剩 `22` 耐久，随后在 `R45–R46` 重新凿梯时耗尽碎裂）。
3. **阶段三 `R53–R95`（`1599–2131s`，`Y=40` 补铁、`R72` 假阳性自愈与第 2 把铁镐）**
   - 背包已有 `iron_ingot: 2`，差 `1` 铁锭合成铁镐；期间 `1669.3s`（`R59`）在 `Y=40` 阵亡 1 次（死亡 #2）。
   - `R68/R72/R74/R78` 在 `Y=40` 调用 `mc_skill_mine_blocks(["iron_ore"], count=1, radius=15)` 时，API 确实搜到并挖碎了铁矿（`dug_count=1`），但当时包里 `916` 块 `cobblestone` 占满 15 槽触发脚下 Auto-Trash 清杂，移动/挖矿途中踩回刚丢弃的 `dirt`/`granite`/`gravel`，导致 `_dig_target_blocks` 因 `bool(gained) == True` 误判 `ok: true` 却未入包 `raw_iron`。
   - **思考自愈**：模型在 **`R80` 思考**中明确识破“只挖到 `granite`/`gravel`/`rotten_flesh`、并未拿到 `raw_iron`”，`R91` 调 `mc_find_blocks(radius=20)` 确认近处无铁后，于 **`R93` 主动把半径扩至 `radius=50`** 成功采回 `raw_iron: 1`，`R94` 烧铁、**`R95` 合成第 2 把 `iron_pickaxe`**。
4. **阶段四 `R96–R143`（`2131–3621s`，三度深潜、第 3 把铁镐与贴脸钻石矿 `0.7` 格饮恨）**
   - `R96–R100` 快速下潜至 `Y=3` 遭女巫魔法（`indirect_magic`）击杀（`2356.6s`，死亡 #3）；重生后重新从地面凿梯下潜，在 `R110` 将第 2 把铁镐耐久耗尽（紧接着 `R111` 尝试 `diamond_ore` 被 `tool_tier_insufficient` 精准拦截）。
   - 随后在地表补树（`R119`）+ `Y=2` 采 6 块铁矿并熔炼（`R123–R128`，其中 `R124` 因重生打断出现 1 次 `action_timeout`，`R126` 立即恢复成功），于 **`R129` 合成第 3 把 `iron_pickaxe`**。
   - `R131–R140` 三度深潜至 `[-160.5, -32, -385.3]`，`R140` 阶梯触发 `ore_detected` 扫到暴露 **`deepslate_diamond_ore @ [-150, -33, -385]`（距离 `10.6m`）**；`R141` 直接挖掘遇隔墙 `out_of_reach` 后，`R142` 思考决策向东调用 `mc_skill_tunnel(direction="east", steps=7, stop_on_ore=True)` **推进 6 步抵达 `[-150.7, -32, -385.3]`（距离钻石矿仅 `0.7` 格）**，挖通暗洞瞬间遭洞内骷髅连射（`arrow`）致死（`3621.8s`，死亡 #4；全程无护甲、无盾牌、无食物）。
5. **阶段五 `R144–R168`（`3621–3904s`，第 4 把铁镐与夜间出生点 `death_loop` 崩退）**
   - 传回夜间地面出生点后，第 3 把铁镐在 `R144` 下探中磨爆；虽在怪群围攻间隙于 **`R159` 用库存铁锭合出第 4 把 `iron_pickaxe`**，但因夜间露天未建掩体或躲避，在 `3856.9s`、`3881.6s`（苦力怕 `player_explosion`）、`3904.0s` **47 秒内连死 3 次**（整局累计 `10` 次死亡：第 `44/59/100/143/149/152/156/162/165/168` 轮），触发服务端 5 分钟 `death_loop` 强制断开。
   - 断开后 `ApiError(code="not_connected")` 逃逸出 `agent.py` 的 `except RuntimeError:`，导致任务以 `STATUS=runtime_error` 退出，且 `finally` 未更新 `report["status"]` 而在 `run.finished` 事件中错写了 `status="running"`。

### 2.2 Run #2 工具调用统计（共完成 `125` 次）

| 工具名 | 调用次数 | `ok=True` 次数 | 主要用途 / 典型失败原因 |
|---|---:|---:|---|
| `mc_skill_staircase` | 38 | 16 | 阶梯下探主主力；失败多为 `cave_or_drop_ahead` / `fluid_or_bedrock` / 遇袭打断 |
| `mc_skill_mine_blocks` | 25 | 7 | 采矿；部分返回 `no_targets_available`、`partial_collection` 或 `interrupted` |
| `mc_skill_craft_gear` | 16 | 8 | 共合成 4 把铁镐及多把石镐/木镐；失败多为缺料 `missing_ingredients` |
| `mc_skill_smelt` | 13 | 5 | 铁矿熔炼（含多次 Auto-Trash 清杂零卡死） |
| `mc_skill_tunnel` | 10 | 6 | 水平开矿道近矿（含 `R142` 准确掘进距钻石矿 0.7 格） |
| `mc_skill_flee` | 6 | 5 | 遇敌撤离 / 竖井掩体坚守 |
| `mc_craft` | 5 | 3 | 零星木板/木棍兜底（`R81` 出现 1 次远处工作台寻路 `stalled`） |
| `mc_find_blocks` | 3 | 1 | `R22`/`R80` 误带 `mature` 参数报 `bad_args` |
| `mc_skill_harvest_logs` | 3 | 1 | 地表补木（含夜战打断） |
| `mc_equip` | 2 | 1 | 尝试吃不存在的 `bread` / 持腐肉 |
| `mc_skill_unstick` | 2 | 2 | 卡墙脱困 |
| `mc_dig` | 1 | 0 | `R112` `out_of_reach` |
| `mc_attack` | 1 | 0 | `R134` `attack_zero_hits` |

---

## 3. 思考耗时、吞吐与 RPM 配额实测分析

基于 [`runs/20261116-162553-371455.events.jsonl`](../runs/20261116-162553-371455.events.jsonl) 全量 `168` 轮统计（扣除限速等待后净耗时；另有 `34` 轮在等待期间因受击护盾被 `guardian` 主动取消重算）：

| 指标 | `thinking=True`（完成 `n=92`） | `thinking=False`（完成 `n=33`） | 物理工具执行（完成 `n=125`） |
|---|---:|---:|---:|
| **总耗时 / 占整局墙钟 (`3904.0s`)** | `613.5s`（`15.7%`） | `106.6s`（`2.7%`） | **`2740.7s`（`70.2%`）** |
| **单次均值** | `6.67s` | `3.23s` | `21.93s` |
| **单次中位数** | **`5.66s`** | **`2.96s`** | — |
| **P90 / 最小–最大** | `10.16s` / `1.85–20.95s` | `4.68s` / `1.33–8.51s` | — |
| **输出 token（均值 / 中位 / 最大）** | `324.5` / `307.0` / `965` | `61.2` / `56.0` / `93` | — |
| **端到端生成速率（均值 / 中位）** | `52.2` / `52.3 tok/s` | `22.0` / `19.6 tok/s` | — |
| **KV Cache 命中率均值** | `46.7%` | `47.9%` | — |
| **限速等待 (`rate_wait`)** | `0` 次 / `0.0s` | `0` 次 / `0.0s` | — |

**实测核心结论**：
1. **思考为什么比预想快这么多**：`qwen3.6-35b-a3b` 是小激活参数 MoE，本局开启思考后的输出中位数仅 `307` tokens，**中位数耗时 `5.66s`，只比不思考（`2.96s`）多出 `2.70s`**（不到一次工具平均耗时 `21.93s` 的八分之一）。整局 `15.7%` 时间在思考，`70.2%` 时间其实在走 Minecraft 物理寻路与挖矿！
2. **思考的性价比极高**：多花 `2.7s` 换来了 `R80 -> R93` 自行识破“挖铁假阳性”并扩半径找到真铁矿，以及 `R141 -> R142` 被岩壁挡住后精确朝东开矿道贴到钻石矿 `0.7` 格。
3. **截断与限速**：旧进程 `max_tokens=2048` 曾引发 `8` 次 `finish_reason=length` 截断（`R3/R5/R40/R64/R77/R86/R89/R120`，注：事件无 `usage` 的截断轮未计入上表完成桶；新代码已把思考下限提至 `4096`）。整局 `65m` 共发 `168` 次请求，**均频仅 `2.58 RPM`**，离 `30 RPM` 红线差一个数量级。

---

## 4. 比赛双并发与“一开思考、一关思考”架构评估

结合 [`benchmarks/nju-api-20261109/performance.csv`](../benchmarks/nju-api-20261109/performance.csv) 与 [`docs/team-briefing.md`](team-briefing.md) 的接口实测数据：

- **NJU API 双并发算力表现**：在 `concurrency=1 -> 2` 时，单路解码速率中位数保持 **`82.10 -> 82.15 tok/s` 零衰减**，首段延迟中位数 `1.61s -> 0.97s`，整批吞吐从 `57.3` 线性翻倍至 **`114.9 tok/s`**；全队硬限流 `30 RPM`（安全预算 `24 RPM`），即使两个 Bot 同时运行，合计常态频率也只有约 **`5–8 RPM`**，完全不会撞限流墙（只要两个进程通过全局/代理共享 24 RPM 限速器或在本地分配如 `14 + 10 RPM` 配额）。
- **结论：支持且强烈推荐“双 Bot 异构并发”，但不推荐“同 Bot 双脑并发”**：

| 方案 | 可行性与收益判断 | 推荐度 |
|---|---|---|
| **方案 A：双 Bot 分工并发（一主攻常/高频思考 + 一工兵默认关思考、遇错唤醒）** | **可行且收益最大**。**Bot A（拓荒/深潜/下界主攻手）**：遇到矿洞遮挡、装备缺口拆解、跨层寻路等复杂决策，采用高频或全开思考（单轮多 `~2.7s` 却显著减少走弯路）；**Bot B（地表后勤/农牧/成就工兵）**：砍树、种小麦、杀牛羊、合床与盾牌、制作工具与烤肉并存进基地箱，常规动作 `enable_thinking=False`（`~3s` 极速连续收割），仅在 `requires_replan` / 连续失败时局部开思考脱困。 | **强烈推荐** |
| **方案 B：单 Bot 内“快脑+慢脑”双并发同时请求 LLM** | **性价比极低，不建议**。因为单 Bot 在 `mc-agent-api` 上除 `/posture`、`/stop` 外 **14 个动作全部全局互斥**（并发直接报 `busy`），且思考仅比不思考慢 `2.7s`，快脑刚发出工具动作，世界状态在 `20s` 内就变化，慢脑算完的结果大多作废还白扣双倍 RPM。 | 不推荐 |

---

## 5. 已修复项、待修阶段一 Bug 与二阶段设计输入汇总

### 5.1 已在本地代码库修复并单测通过（待下次重启 `mc-console` 全量加载）
- `mc-live`（`:8765`）与 `mc-console`（`:8770`）前端思考内容实时/默认展开展示（`mc-live` 已热重启 `b99a827960648379` 生效）。
- 思考模式 `max_tokens` 下限提至 `4096`（[`model.py`](../src/mc_agent/harness/model.py)）。
- `craft_gear("stick")` 原木自动先合木板（[`library.py`](../src/mc_agent/skills/library.py)）。
- `mine_blocks` 空区提示明确化 + `_RepeatFailureGuard` 忽略 `count`/`radius` 去重（[`agent.py`](../src/mc_agent/harness/agent.py)）。
- `mc_find_blocks` 对非作物目标自动剥离 `mature`（[`mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）。

### 5.2 Run #2 新抓出的 3 个阶段一必修底层 Bug
1. **`[P0 崩溃]` `ApiError(not_connected / death_loop_cooldown)` 未捕获重连**：[`agent.py`](../src/mc_agent/harness/agent.py#L1115-L1144) 的主循环只捕获了 `RuntimeError`，导致 Bot 断线/冷却后直接崩退，且 `finally` 未校正异常状态使 `run.finished` 错写 `status="running"`。需捕获 `ApiError` 并调用 `ensure_ready()` 等待 30s 冷却或 5 分钟 `death_loop_cooldown` 自动续跑。
2. **`[P1 挖矿假阳性 + 背包膨胀]`**：`R72/R74/R78` 捡起脚下杂物导致 `_dig_target_blocks` 以 `bool(gained)` 误判 `ok: true`；同时 `cobblestone` 攒到 `916` 块占满 15 格背包加剧原地清杂回捡循环，需改为按预期掉落物族核验成功并为圆石等废料设置保留槽位上限（如最多保留 2–4 组）。
3. **`[P0 防连死]` 夜间出生点重启护航**：`R144–R168` 夜间传回出生点后连续露天合成/砍树导致 47s 3 死；在夜间连死 ≥ 2 次或刚重生处于 `is_day=False` 时，需强引导或触发 `bunker`/优先避夜掩体，避免撞上 `death_loop` 5 分钟冷却。

### 5.3 注入二阶段（`WorldLedger` / `MiningDFS` / 装备前置）的 3 条实证结论
1. **矿井入口与竖井坐标持久化（`WorldLedger`）**：本局共死 10 次回地面，Bot 每次都从 `Y=70` 重新凿一条 100 格新阶梯，硬生生把第 1、2、3 把 `iron_pickaxe`（250 耐久）全磨在重新下潜上。必须记录已打通的安全矿道坐标与熔炉/工作台站点，复活后沿现成通道速降。
2. **掘进工程优先用石镐保留铁镐耐久**：`dig_staircase` / `tunnel` 挖普通 `stone`/`deepslate` 时优先用 `stone_pickaxe`，把 `iron_pickaxe` 留给 `iron_ore`/`diamond_ore`（或由阶段二技能按方块硬度动态切镐）。
3. **深层探测前强制防御前置**：背包有铁锭与木材时优先补 **`shield`（1 铁 + 6 木板）** 与基础食物/床位，避免再出现 `R142` 贴脸钻石矿 `0.7` 格却被洞穴骷髅两箭带走的局面。
