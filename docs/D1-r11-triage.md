# D1 精炼根因报告 — `r11_22bot` 22 路首飞（2026-11-19）

> 数据源：`.worktrees/v2-integrated/runs/r11_22bot/`（22 bots，121 runs，1230 次 `tool.completed`，158 次 `guardian.incident`）
> 工具：`scripts/d1_triage.py`（本 worktree，纯只读解析，替代当前字段失配的 `v2_fleet.py triage`）
> 复现：`/home/xzq/projects/mc/.venv/bin/python scripts/d1_triage.py --wave r11_22bot --top 20`
>
> **度量口径（重要）**：events 里的 `elapsed` 是 **run 相对时间戳**，单次耗时 = `completed.elapsed - started.elapsed`。
> 本文所有"单次耗时"都按该差值统计；早期若直接读 `completed.elapsed` 会把它放大成整段 run，不可用。

---

## 0. 先修度量：`v2_fleet.py triage` 字段失配（Slice D2）

排行空白 + 全部分数为 0，不是没有 Bug，而是聚合器读错字段：

| 位置 | 现在读取 | 实际字段 | 后果 |
|---|---|---|---|
| `scripts/v2_fleet.py:227` | `type == "tool.finished"` | `tool.completed`，且 `ok` 在 `result` JSON 字符串里 | 错误榜永远为空 |
| `v2_fleet.py:243` | `ev.get("ok") is False` | 顶层无 `ok` | 同上 |
| `v2_fleet.py:208` | `d["final_score"]` | `d["curriculum_summary"]["final_score"]` | 22 路全部 score=0 |
| `v2_fleet.py:211` | `d["curriculum"]["episodes"]` | `d["curriculum_summary"]["episodes"]` | gained 全空 |
| `v2_fleet.py:221` | `'"429"'` 字符串计数 | 本次 0 次 429 | 无害但易误判 |

`scripts/d1_triage.py` 已按真实结构实现（含 `elapsed` 差值、小脑抢占剥离、跨模型矩阵），可直接抄逻辑或由 D2 合入。

---

## 1. 总览（真实数字）

- 工具失败率 **344 / 1230 = 28.0%**；剔除 33 次"小脑本能抢占接管"后，真实失败 **311 次**
- 单次工具耗时（start→finish）：`mine_dfs` 中位 **35.0s** / p90 **91.7s** / max **174.5s**；`staircase` 中位 **17.9s** / max **92.3s**；`move_to` 中位 **3.8s** / max **49.5s**
- `mine_dfs` 显式声明预算的 106 次中，**6 次 > 2× 预算**（climb_out 内部会把 32s 抬到 90s，故最坏 174.5s ≈ 内部上限的 1.9 倍）
- `model.failed(cancelled)`：**128 次 / 13 bots**，其中 114 次紧邻 `guardian.incident`；`cancel_cooldown_wait` 另计 **80 次 × ~4s ≈ 290s 纯等待**
- 跨模型矩阵（4 个模型家族全中）的 Top 失败：`path_not_found`、`staircase` 地形停止、`movement_not_confirmed`、`craft_gear:missing_ingredients`、`find_blocks:bad_args`

---

## 2. P0/P1 工单（按严重度）

### 【P0】BUG-002 传送门已点燃但 `enter=true` 永远进不去
- **影响**：`nju_guitest_01_nether` 连续 **15 次失败，跨度约 540s run 时间**，下界链路整体不可用（比赛模型 NJU）
- **证据（events）**：`20261119-023818-175064.events.jsonl`
  - `build_portal({"enter":true,"wait_s":8})` → `{"ok":false,"portal_origin":[6409,65,-6382],"placed_obsidian":10,"portal_ignited":true,"entered_nether":false,"dimension":"overworld"}` ×15
  - 之间穿插 `move_to(x=6409,y=67..70,z=-6382,range=0.0/0.1)` → `stalled`，`dist_left` 在 **0.7 / 1.6 / 2.6** 反复
  - 位置轨迹：`[6410.3,65,-6382.5]` → `[6409.3,70.1,-6381.5]`（爬到门框顶）→ `[6408.5,64,-6381.5]`（unstick 落地）；**从未出现在 y=66..68 的门内**
- **源码定位**：`src/mc_agent/skills/library.py:6803-6854`（`_walk_into_portal`）
  - 6805-6812：按最低 Y 选 portal 方块，目标取 `pos + (0.5, 0, 0.5)`
  - 6813-6816：**只发一次** `move_to(range=0.0)`，异常直接吞掉
  - 6817-6828：只轮询 `dimension`，**从不校验脚下/身体所在方块是否 `nether_portal`**
  - 6966-6971：清障只清门内 + 门前 `bz-1` 两格，**没清门框两侧/上方接近面**，寻路会绕到门框顶
  - 7034-7037：失败后直接返回 `entered_nether=false`
- **根因压缩**：进入动作是"一次性盲发 + 只看维度"，没有"确认实体在 portal 方块内"的不变量；寻路一旦绕到门框顶/侧面就永久失败，而 `reused_existing_portal` 分支每次复用同一个不可达门，形成 540s 死循环
- **最小修复**：`_walk_into_portal` 改为
  1. 先 `move_to` 到门前 1 格并 `mc_control(forward=True,jump=True,ms≈600)` 原子跨入；
  2. 每轮用 `status.pos` + `find_blocks(nether_portal,radius=2)` 校验 **feet/head 位于 portal 方块**；
  3. 失败 3 轮返回 `portal_enter_blocked` + `next_action`，并清理 `bz-1`、`bz-2` 与门框两侧各 1 格
- **对照成功样本**：`hy4_hy_8_nether` 同工具 `portal_origin y=66` 一次成功（单次 97s 内完成），说明是地形/接近面问题，不是协议问题

### 【P1】BUG-003 `mine_dfs` 预算穿透（不是"8~20 倍"，但确实穿透）
- **影响**：**20 bots**；156 次 `mine_dfs` 中位 **35.0s**、p90 **91.7s**、max **174.5s**；显式带预算的 106 次里 **6 次 > 2× 用户预算**
- **证据（真实单次耗时）**：
  - `hy4_hy_9_deep_water` `climb_out max_duration_s=32` → **174.5s**（`surface_reached`），另 131.9s / 115.2s / 107.1s 均 `climb_incomplete`
  - `hy4_hy_8_nether` `explore max_duration_s=80` → **127.0s**（`no_progress`）
  - `ds_ds_1_bare` `explore max_duration_s=90` → 95.8s；`hy4_hy_4_iron` `explore 32s` → 96.0s
- **源码定位**：
  - `skills/mining_dfs.py:4638`：预算被夹到 `[5,90]`
  - `mining_dfs.py:4559-4560`：`climb_out` 预算 `min(120, max(max_duration_s, 90))`——**32s 请求会静默变成 90s**，这是"用户预算"与"实际"差额的主要来源
  - `mining_dfs.py:2808-2818`：deadline 只在每次 `dig_staircase` 之前检查，单个子步骤可再吃掉 20~90s，故 174.5s 约为内部 90s 上限的 1.9 倍
  - `mining_dfs.py:5198-5231`：`time_slice_progress` 被当成功返回（119 次），上层无法感知"这一轮没挖到目标"
- **根因**：预算是协作式的，只在下钻一层循环检查；且 `climb_out` 存在**隐式预算抬升**，与工具契约（`max_duration_s`）不一致
- **最小修复**：① 把 `deadline` 透传进 `dig_staircase`/`tunnel`/`_navigate_to_node` 并逐 step 检查；② 若确需 90~120s，在返回值里显式回报 `budget_effective_s`，不要静默抬升；③ `time_slice_progress` 附带 `goal_met:false`

### 【P1】BUG-006 `mc_find_blocks` 一个坏名字毁掉整批查询
- **影响**：**22 次 / 9 bots / 4 模型**
- **证据**：`types:["log","planks","bed","wool"]` → `bad_args: unknown block log`；`["shield"]`→unknown block shield；`["flowing_water"]`、`["nether_rack"]`、`["cave"]`、`["leather"]`、`["sheep"]` 同类
- **源码定位**：
  - `src/mc_agent/harness/mc_tools.py:48-59` `_normalize_find_block_types` 只处理 `bed`，其余原样下发
  - `experiments/20261115-until-1900/backend_patched/src/session.js:1773` 任一未知名直接 `fail('bad_args')`
- **根因**：别名表过窄 + 后端 fail-fast，模型把实体/物品名混进方块查询时整次调用归零（`log` 这类高频别名都没有）
- **最小修复**：Python 侧补别名 `log→9 种 *_log`、`planks→*_planks`、`grass→short_grass/tall_grass`、`portal→nether_portal`、`nether_rack→netherrack`、`cave→cave_air`；无法识别的名字**剔除并继续**，返回 `ignored_types`；全部无法识别才报错并给出可执行示例

### 【P1】BUG-007 `craft_gear` 材料预检覆盖不全 / 泄漏服务器内部错误
- **影响**：**17 次 / 10 bots**（`hy4_hy_2_bare`、`hy4_hy_6_midgame`、`nju_guitest_01_nether`…）
- **证据**：
  - `craft_gear(stone_pickaxe)`，包内 `cobblestone:1` → `{"error":"missing_ingredients","message":"could not craft stone_pickaxe: server placed no result","example_recipe_needs":{"cobbled_deepslate":3,"stick":2}}`
  - `craft_gear(iron_pickaxe)` 包内 `iron_ingot:1` → 直接 `missing_ingredients`（这次是对的）
  - `hy4_hy_6_midgame` 连续 236.9s / 497.4s 的 run 时间跨度里两次等同一个必失败合成（单次调用 51s 上限）
- **源码定位**：
  - `src/mc_agent/skills/crafting.py:10-22` 预检只有 7 个配方（铁/钻/工作台），**没有 stone_* / shield / 石砖系列**
  - `src/mc_agent/skills/library.py:2826-2837` 预检未命中即放行
  - `experiments/.../backend_patched/src/session.js:2215-2219` 服务端把"配方请求被拒"统一包装成 `missing_ingredients: could not craft X: server placed no result`
- **根因**：预检 recipes 表是手写子集；未覆盖的配方把"缺料"伪装成"服务器异常"，模型无法据此换目标，只能重试
- **最小修复**：把预检改为读真实配方或至少覆盖常用工具/盾牌/石砖；错误信息区分 `missing_ingredients(missing={...})` 与 `server_recipe_rejected`，后者不提示"重试"

### 【P1】BUG-008 桶交互成功却被判 `interact_no_effect`（假阴性）
- **影响**：4 bots / 10 次；关键一条实际已成功
- **证据**：`hy4_hy_7_village` `mc_interact(item=bucket, lava)` →
  `{"ok":false,"result":{"before":"lava","after":"lava","changed":false,"used":{"bucket":1},"gained":{"lava_bucket":1}},"error":{"code":"interact_no_effect"}}`
- **源码定位**：`src/mc_agent/harness/mc_tools.py:1158-1171`——只要 `changed is False` 就强改 `ok=false`，**完全忽略 `used`/`gained`**
- **根因**：用"方块是否变化"作为唯一成功判据；桶装流体时方块可能被同 tick 回填/读取到旧快照
- **最小修复**：仅当 `changed is False` **且** 无 `used` **且** 无 `gained` 时才判失败；否则保留 `ok=true` 并附 `verified_by:"inventory_delta"`

### 【P1】BUG-009 取消风暴：128 次 `model.failed(cancelled)` / 13 bots + 290s 强制冷却
- **影响**：全模型；`glm_glm_4_enchant_anvil` 24 次、`hy4_hy_2_bare` 18 次、`glm_glm_3_village` 14 次
- **证据**：114/128 紧邻 `guardian.incident`（`fresh_hurt` 49、`pre_hurt_melee` 30、`pre_hurt_ranged` 16、`pre_hurt_creeper` 15）；另有 `model.cancel_cooldown_wait` **80 次 × ~4s = 290.1s** 纯睡眠
- **源码定位**：`harness/guardian.py` 抢占路径取消在途生成；触发面 `guardian.py:246` `_detect_pre_hurt_trigger`
- **根因**：抢占本身正确，但 `pre_hurt_*` 触发密度过高（对同一 creeper/zombie 反复触发），每次取消既丢 token 又强制 ~4s 冷却
- **最小修复**：`pre_hurt_*` 增加 2~3s 去抖 + 同目标/同坐标冷却；取消后的 4s 冷却改为"仅在真实受伤后生效"

### 【P1】BUG-010 复活瞬间 `eat` 必然失败并二次致死
- **影响**：`hy4_hy_10_enchant_anvil`（同一死亡链）
- **证据**：`guardian.action.completed eat → {"ok":false,"error":{"code":"not_ready","message":"world is still loading (after login, respawn or dimension change)"}}`（94.8s），紧接着 `wrong_server`（111.2s），随后 `player_explosion` 死亡
- **源码定位**：`src/mc_agent/harness/guardian.py:573-581` `_do_eat` 直接 `use_item`，未检查 `status.ready` / 刚 `death_or_connection_changed`
- **最小修复**：`deaths or not status.ready` 时跳过进食并标记 `retry_after_ready`；`guardian` 轮询到 `ready` 再补一次，不把 `not_ready` 计入动作失败

### 【P1】BUG-011 生存反射里的瞬态网络抖动会打死整集
- **影响**：`glm_glm_3_village` EP5、`hy4_hy_8_nether` EP7 → `status=runtime_error`，`score_delta=0`；全队 5 次 `ReadTimeout`/`ReadError`
- **证据**：episode report `error="生存响应 subcortical_combat 的网络结果不确定：ReadTimeout"`
- **源码定位**：`harness/guardian.py:195-196` 把 `httpx.HTTPError` 包成 `RuntimeError`；`harness/agent.py:1614-1620` 原生 `httpx.HTTPError` 会判为可继续的 `network_error`，但被包装后的 `RuntimeError` 落入 `runtime_error` **终止整集**
- **最小修复**：`_action` 捕获后返回 `ok:false, error:{code:"network_uncertain"}`（或退避重试），不得抛 `RuntimeError`

### 【P1】BUG-012 `not_loaded` 被当致命错误，整集报废
- **影响**：`hy4_hy_2_bare` EP5 `bootstrap_iron_age`（`actor_requests=20`，`score_delta=0`）
- **证据**：episode report `error="[not_loaded] chunk not loaded"`
- **源码定位**：`harness/agent.py:209 _API_RETRYABLE_CODES` 不含 `not_loaded`；`agent.py:1618-1620` 把 `ApiError` 一律判 `runtime_error`；后端 `session.js:1367/1866/1917` 在区块未加载时返回 `not_loaded`
- **最小修复**：`not_loaded`/`chunk_not_loaded`/`world_not_ready` 纳入短退避重试（0.5~2s，最多 2~3 次），仍失败降级 `network_error`

### 【P1】BUG-001（已在册）启动崩溃：确认
- `glm_glm_1_nether_fortress/stdout.log:11` `NameError: name 're' is not defined`，抛点 `scripts/l3_wild_endurance_runner.py:167`（`re.search`，文件缺 `import re`）；`glm_glm_2_nether_bastion`、`ds_ds_4_nether_fortress` 同因空跑
- 这是 **3/22 路完全无数据** 的唯一原因，优先级应高于其它表现层 Bug

---

## 3. P2 Backlog（不打断 P0/P1，已同步到 BUG_BOARD）

| # | 现象 | 证据/次数 | 归档理由 |
|---|---|---|---|
| P2-1 | `mc_move_to` 微距 `range=0.1` 反复 `movement_not_confirmed`（"已在 0.1m 内"） | 36 次 / 10 bots | 提示词/参数规范问题，非硬 Bug |
| P2-2 | `mc_move_to:path_not_found` 59 次 / 11 bots，短距失败后无自动 unstick；单次最长 49.5s | 59 次，15 次显式传 `y` | 策略层：应引导模型先 `unstick` |
| P2-3 | `mine_dfs(action="reset")` 空树时返回 `nothing_to_reset` 并继续劝重置 | 3 次 | 契约可用，只是提示冗余 |
| P2-4 | 对无配方物品调用 `craft_gear`（`wheat_seeds`、`shield` 缺料） | `no_recipe` 3 次 | 模型策略；可由 KB 提示 |
| P2-5 | `void_geofence_blocked`：模型朝 `(0,0)` 世界原点移动 | 4 次 / 2 bots | 提示词；地理围栏已正确拦截 |
| P2-6 | `mc_place` 连续 `no_support`/`blocked_by_self`/`place_failed(仍为 sugar_cane)` | 3 次 | 可加"先 dig 目标格"提示 |
| P2-7 | `attack_zero_hits` 对 45m 外的 glow_squid 空挥 20s | 3 次 | 策略：不可达目标应放弃 |
| P2-8 | 舰队共岛互伤：`player_explosion`/`spear` 致死 | `hy4_hy_10` 4 死、`hy4_hy_8` 3 死 | 场景/编队间距问题，非 Harness Bug |
| P2-9 | `find_blocks(water,radius=96)` 返回 `total:10000` | 1 次 | 数据量/性能小优化 |
| P2-10 | `out_of_reach_vertical` 桶交互（y 差 6.8/9.0） | 3 次 | 已有可执行提示，属策略 |
| P2-11 | `mine_dfs` 未达成目标时仍返回 `ok=true`（`quota_reached`/`shaft_exhausted`/`time_slice_progress`） | 约 130 次 | 语义优化：`ok` 与 `goal_met` 分离 |
| P2-12 | `island_recon_sweep` 24 次 `objective_verified=false` | 该 target 无 `advancement_id`，`curriculum.py:220-228` 必然 `return False` | 仅影响报表字段，进度看 `material_progress` |
| P2-13 | `dig_staircase` 地形边界停止（`movement_not_confirmed` 9、`missing_step_support` 7、`fluid_or_bedrock` 8） | 66 次调用中 5 次 >60s，max 92.3s，中位 17.9s | 原 BUG-004 复核后降级：不是延迟爆炸，而是正常地形停止；只需优化提示与换向策略 |

---

## 4. r12 收官验证（22/22 完成）——修复有效，但引入一个 P0 回归

| 指标 | r11 | r12 | 结论 |
|---|---|---|---|
| 启动崩溃（Traceback） | 3 路 | **0** | ✅ BUG-001 已消除 |
| 总分（22 路） | 1860（19 路） | **2160（22 路）** | 提升，但有 -75 回归 |
| 死亡 incident | 16 | 19 | 略升（lava/trident/共岛） |
| `build_portal:unknown` | 13 | **0** | ✅ BUG-002 未复发 |
| `find_blocks:bad_args` | 22 | **8** | ✅ 部分生效（BUG-006 仍 OPEN） |
| `interact_no_effect` | 10 | 8（2 次无 `used/gained`，属真实无效） | ✅ BUG-008 正确 |
| `mine_dfs` 单次 med/p90/max | 35.0/90.7/174.5s | **7.5/49.8/85.0s** | ⚠️ 过度收紧 → BUG-018 |
| `mine_dfs:unknown`（124 次"预算用尽"） | 27 | **154** | ❌ 回归 |
| `mine_dfs:repeated_identical_failure_blocked` | 1 | **56** | ❌ 回归（BUG-019） |
| `staircase:time_budget` | 0 | **87**（调用数 69→208） | 新硬上限生效但 churn 高 |
| `model.failed(cancelled)` | 128 | **137** | ❌ BUG-009 未修 |
| `cancel_cooldown_wait` | 290.1s | 272.6s | 同上 |
| 整集 `runtime_error`/`disconnected` | 3 | **10 / 2** | ❌ BUG-020 |

**分数回归定位**：`ds_ds_2_iron` 95→20、`hy4_hy_4_iron` 95→20（各 -75）。两路在 `shiny_gear` 上分别烧掉 55/62 个 actor 请求，每次 `mine_dfs` 4~8s 就 `time_slice` 且 0 进展。

### 【新增 P0 回归】BUG-018 `_BudgetSession` 把每次子调用压到 ~3.9s
- **触发**：`eea50d7`（修 BUG-003 时引入）
- **根因行**：`src/mc_agent/skills/mining_dfs.py:344`
  `request_timeout = min(timeout_s or 45.0, (remaining - 0.15) / 8)`
- **离线证据**：`scripts/d1_budget_check.py`（exit 1）
  ```
  slice_budget  move_to_timeout  dig_timeout  verdict
         31.5s            3.92s         3.92s  STARVED
         59.5s            7.42s         7.42s  STARVED
         89.5s           11.17s        11.17s  OK
  ```
  默认 32s 切片下每次 move_to/dig 仅 3.92s，短于 r12 `move_to` 中位 5.1s / p90 25.7s → 第一次调用即超时 → `time_slice` 0 进展
- **建议**：`request_timeout = min(timeout_s or 45.0, max(6.0, remaining - 0.5))`（deadline 已在调用前后检查，不必预先除以 8）

### 【新增 P1】BUG-013 下界"挖钻石"（已由 `fbd9548` 修复，待 r13）
5 路下界 bot 全部中招：`glm_1` diamond×3、`glm_2` diamond×4、`ds_4` diamond×7、`hy4_8` diamond×3（含 `target_y=12/15/20`）。根因：`ORE_DFS_PROFILES` 无维度校验 + prompt "钻石镐…" 歧义。

### 【新增 P1】BUG-019 / BUG-020
- **BUG-019**：`time_slice` 文案"可用相同 action 继续"→ 模型照做 → 被防卡死拦截，17 bots / 56 次
- **BUG-020**：瞬时故障报废 12 集：`not_loaded`×4、Qwen/CF `520/522/525`×4（525 不在任何重试清单）、`not_connected`×1、guardian `ReadTimeout`×1

---

## 4b. r13 收官验证（21/22，`nju_guitest_01_nether` 启动即崩）

| 指标 | r12 | r13 | 结论 |
|---|---|---|---|
| 整集 `runtime_error` / `disconnected` | 10 / 2 | **2 / 0** | ✅ BUG-020 基本修复 |
| `mine_dfs:repeated_identical_failure_blocked` | 56 | **11** | ✅ BUG-019 修复 |
| `mine_dfs` 单次 med / p90 / max | 7.5 / 49.8 / 85.0s | **20.5 / 51.8 / 84.7s** | ⚠️ BUG-018 由 /8→/2 缓解，未达标 |
| `mine_dfs` 0 进展 `time_slice` | 124/241 | **44/265（16.6%）** | ⚠️ 仍高 |
| `find_blocks:bad_args` | 8 | **1** | ✅ BUG-006 修复 |
| `craft_gear:server_recipe_rejected` | 0 | **9** | BUG-007 错误分流已生效（仍需补预检） |
| 死亡 incident | 19 | **42** | ❌ 新生存回归（trident 9 全在 deep_water） |
| 总分 | 2160（22 路） | **2030（21 路）** | ❌ 铁/钻链路仍卡 |
| `mine_skill_tunnel:missing_pickaxe` | 0 | **15** | 新 P2-18 |

**r13 新工单**
- **BUG-021 P1（D2）**：`nju_guitest_01_nether` 启动 `DeathLoopCooldownError`（129s remaining）直接退出 → 整路丢失。`l3_wild_endurance_runner.py:797` + `bot.py:192-204`
- **BUG-022 P1（Slice A）**：`ds_ds_2_iron` 95→20→25、`hy4_hy_4_iron` 95→20→20。下潜 12 次到 y=47 后 `shaft_exhausted` → 强制 `needs_surface_climb` → `climb_out` 反复超时，episode 烧完也到不了 diamond(-54)
- **BUG-005 补强（Slice B）**：`hy4_hy_9_deep_water` 9 次 trident 死亡，`escape_water ok=true` 仍被连击打死

---

## 4c. r14 收官（21/22，`ds_ds_4` 启动冷却再次丢路）

| 指标 | r13 | r14 | 结论 |
|---|---|---|---|
| 总分（21 路） | 2030 | **2040** | 持平；铁/钻仍是天花板 |
| `mine_dfs` 中位 / 0 进展率 | 20.5s / 16.6% | **20.6s / 9.7%** | BUG-018 的 `/2` 接近达标，但 HEAD 仍是 `/8` |
| `staircase` 调用数 / 中位 / max | 119 / 9.9s / 46s | **146 / 16.2s / 46s** | 45s 硬帽把长调用拆成更多短调用 → churn（新增 P2-20） |
| 死亡 incident | 42 | **28** | 改善，但 `hy4_hy_9` 仍有 10 次 trident 死亡 |
| `runtime_error` / `disconnected` | 2 / 0 | **4 / 2** | r14 进程早于 `26f7213`，未含 not_loaded 修复 |
| `mc_move_to:bad_args` | 0 | **8（8 bots）** | P2-17 参数校验生效，成功拦截 y=z 串位 |
| 启动丢路 | `nju_guitest` (129s) | **`ds_ds_4` (120s)** | BUG-021 修复已提交但未进本波进程 |

**分数天花板**：`ds_ds_2_iron` 20、`hy4_hy_4_iron` 20（连续 3 波），`ds_ds_1_bare`/`hy4_hy_1_bare` 10。`shiny_gear`（钻石）拿不下是主因 → **BUG-022**。

**r14 新工单**
- **BUG-023 P1**：`hy4_hy_9_deep_water` 10 次 trident 死亡（r12/13/14 = 3/9/10），`escape_water ok` 仍被连击；需"上岸优先 + 盾挡 + 不再下水"与场景侧浅滩出生点
- **P2-19**：`harvest_logs` 新增 `time_budget` 早退（18 次/6 bots），max 145.8→75.0s，需在提示中建议降 count
- **P2-20**：`staircase` 45s 硬帽导致 69(r11)→146(r14) 调用，模型轮次成本可能高于一次长调用，建议改为 60~75s 或按 steps 缩放

---

## 4d. r15 最终收官（22/22）

> 按"有返回即摊派"执行，不等 22/22。以下计数基于已落盘 events + 18 份 summary。

| 指标 | r14 | r15（22 路） | 结论 |
|---|---|---|---|
| `mine_dfs` 中位 / p90 / max | 20.6 / 54.4 / 83.8s | **27.9 / 58.8 / 86.3s** | ✅ BUG-018 修复生效（0 进展 `time_slice` 仅 5.3%） |
| iron 槽分数 | 20 | **100**（`hy4_hy_2_iron`） | ✅ BUG-022 解除 |
| `staircase` 失败 | 44 | **109**（`missing_step_support 55`） | ❌ **BUG-024**：上阶梯垫脚 `no_support` 45 次 |
| 死亡 | 28 | **57**（notable 去重 51） | ❌ **BUG-026**：新场景无生存调优 |
| 总分 | 2040 | **2210** | 略升（22 路） |
| `cancel_cooldown_wait` | 286s | **454s**（cancelled 141→247） | 同上 |
| `craft_gear` 缺木棍 | — | **39 次 / 10 bots** | ❌ BUG-025（HEAD 已修，r15 为旧数据） |
| `mc_move_to:movement_not_confirmed` | 33 | 38 | P2-1 未动 |
| `mc_dig:timeout` | 4 | **15** | P2-21 |

**r15 新工单（已写入 BUG_BOARD）**
- **BUG-024 P1（Slice C）**：`staircase vertical=up` 垫脚 `place` 返回 `no_support`（45 次 / 6 bots），代码只重试 `place_failed`，遇 `no_support` 直接失败。`library.py:5688-5698`
- **BUG-025 P1（Slice A）**：`_stone_pickaxe_headroom` 把 **1 木板折算 2 木棍**（实际 0），导致 `pickaxe_low` 误报"可立刻合成"，模型 4 轮死循环 `craft_gear`。`mining_dfs.py:848-853`。`ds_ds_1_bare` 391 石料 + 1 木板 + 0 木棍，20 分钟 0 分
- **BUG-026 P1（Slice B + D2）**：死亡 49（mob 23 / arrow 13 / fall 5 / explosion 3 / lava 3），集中于 `hy4_8` 8、`ds_3` 5、`hy4_6` 5；新场景 cave_combat/brewing 无生存节奏
  - 深度归因：**42/49 死在地表**、**10/49 死前无任何 guardian incident**、**0/49 死时手持盾**（但 17 路结束时背包仍有盾+剑）、`raise_shield` 仅 1 次 → 问题在"举盾时机/预判覆盖"，不是缺装备
- **P2-21/22/23**：`dig:timeout`、`place:no_support`、`staircase:time_budget` churn
- **SCENE-001（D2，已提单）**：`village_trade`/`brewing_alchemy`/`cave_combat` 只换出生点与背包，任务文本仍是通用课程。工具按关键词动态暴露（`agent.py:129-132`），结果 1593 次调用中 `mc_skill_trade 0 / mc_skill_brew 0 / mc_window_act 0`，场景目标完全未被验证

---

### 4d-1. 4 路未收官 bot 的即时复核（不等待 22/22）

| bot | 关键证据 | 归属 |
|---|---|---|
| `ds_ds_1_bare` | `craft_gear` 死循环 **9 次**：`stone_pickaxe{missing stick:2/4}` → `stick{仅 1 木板}` → 再来；另 `staircase:time_budget` 10 次 | **BUG-025 确认** |
| `nju_movetest_01_bare` | 2 次死循环（`wooden_pickaxe` 缺 `any_planks:3`）；`tunnel:missing_pickaxe` 3 次；2 死（arrow） | BUG-025 + BUG-026 |
| `hy4_hy_1_bare` | 5 死（mob 4 + arrow 1，`y=6` 地下无甲）；`harvest_logs` 失败 4 | BUG-026 |
| `glm_glm_5_midgame` | `mc_window_view` 10 次：**6 stalled + 3 no_window**，66.7s | **P2-24**（村民开窗卡实体碰撞箱） |

- `no_support` 在这 4 路中 **0 次** → 与 BUG-024「竖井内上阶梯」定位一致，不影响地表裸开。
- 4 路合计已跑 34 个 episode，说明长任务本身在推进，不存在"卡死不出 summary"。

### 4d-2. r15 收官后的修复复核（HEAD `3fb883e`）

| 工单 | 状态 | 证据 |
|---|---|---|
| BUG-025 | **FIXED** | HEAD 已改 `4*(planks//2)+8*logs`（`3d4ac1e`）；`scripts/d1_stick_check.py` **5/5 OK, exit 0** |
| BUG-024 | **部分修复** | 兜底加在 `_dig_staircase` 的 up 分支（`library.py:6310`）；真实报错来自 **`_dig_pillar_up`**（`:5492` 定义，jump-place `:5673`）→ 需在该函数内补 `has_place_face` 判定 |
| SCENE-001 | **已实现** | `7d5a571` 为三场景注入首集目标；待 r16 验证 `mc_skill_trade/brew` 首次出现 |
| BUG-026 | **OPEN** | 死亡 28→57、cancelled 141→247、冷却 286→454s |

### 4d-3. 可重复验收门禁（新增）

`scripts/d1_acceptance.py --wave <wave>` 直接把 BUG_BOARD 的验收标准变成 PASS/FAIL：

```
r15: 6 GATE(S) FAILED  (BUG-025 39→精确口径 9, no_support 46, deaths 57, cooldown 454s, trade/brew 0)
r16（早期 127 calls）: BUG-025 0 ✅  no_support 0 ✅  trade 1 ✅  brew 3 ✅
                       mine_dfs median 12.4s / zero-slice 14.3% 需等收官
```

- 口径修正：BUG-025 只统计 `missing_materials == {"stick": N}` 的石器缺口（`diamond_pickaxe` 缺钻石不再误计）
- 用法：`/home/xzq/projects/mc/.venv/bin/python scripts/d1_acceptance.py --wave r16_22bot`

### 4e. r16 早期（21:27 起，0/22 summary、52 个事件文件）

| 门禁 | 结果 | 说明 |
|---|---|---|
| BUG-025 木棍死锁 | **0** ✅ | 修复生效 |
| BUG-024 no_support | **0**（暂） | 需收官确认 |
| BUG-026 死亡/冷却 | 3 死 / 27.7s（暂） | 修复 `b920f24` **未进 HEAD**，预期不会大改 |
| BUG-020 runtime_error | 0 | — |
| SCENE-001 trade / brew | **1 / 8 次调用** ✅ | 交易成功 1 笔（paper→emerald×12）；酿造多次 ok |
| `mc_window_view` | `no_window ×6`（全在 `glm_3`） | **BUG-027**（村民 12~16m，`openTarget` 不校验 goNear） |

**新增 BUG-027（P1）**：`openTarget()` 在 `goNear` 失败后仍远距离 `activateEntity`，2.5s 等不到 windowOpen → 误报 `no_window`；实体距离 12.2/12.5/16.1m 已证实。

**新增 BUG-028（P0 崩溃）**：`hy4_hy_6_nether_bastion` 在 r16 踩岩浆时整路崩溃：
```
escape_lava → combat_env.py:1206 AttributeError: 'VoidGeofence' object has no attribute 'contains'
```
`combat_env.py` 有 **4 处**（`:1206/:1293/:1354/:1360`）调用了不存在的方法；`VoidGeofence` 只有 `check_point/check_ray/distance_from_center`。由 `e680b1f`（找岸边）引入，r15 未走到该分支所以未暴露。**必须优先修**，否则任何下界/水边 bot 都可能整路丢失。

## 4f. r16 收官（18/22，4 路被 P0 崩溃吞掉）

| 指标 | r15 | r16 | 结论 |
|---|---|---|---|
| 战争损失 | 0 崩溃 | **4 路崩溃**（`ds_1`、`glm_5`、`hy4_6`、`nju_guitest`） | **BUG-028** `.contains` AttributeError；`52b0d3f` 已修 |
| 死亡 incident | 57 | **14** | ✅ BUG-026 修复生效（`d2ac1ab`/`b920f24`） |
| `cancel_cooldown_wait` | 454s | **148s** | ✅ 同上 |
| `no_support` | 46 | **1** | ✅ BUG-024 修复（`7dbcdb1`） |
| 木棍死锁 | 9（精确口径） | **0** | ✅ BUG-025 |
| `mine_dfs` 中位 / 0 进展 | 27.8s / 5.9% | **28.0s / 3.1%** | ✅ BUG-018 |
| `runtime_error` | 0 | **0** | ✅ BUG-020 |
| trident 死亡（deep_water） | 1 | **0** | ✅ BUG-023 |
| trade / brew 调用 | 0 / 0 | **4 / 8** | ✅ SCENE-001（交易 paper→emerald×12 成功） |
| 总分 | 2210（22 路） | 1580（18 路） | 不可比：少 4 路高分手（`glm_5` 150、`ds_4` 未计入等） |

**结论**：除 BUG-028 外，r16 是一次全面转绿；4 路崩溃全部是同一个 4 行 API 误用，修复提交 `52b0d3f` 已被 r17 加载（21:51），r17 截至 22:00 无同类 Traceback。

## 4g. r17 中段（22:02，878 tool calls，0/22 summary）

```
ALL PASS — 11 项门禁全绿
  startup crash 0        stick fails 0        no_support 0
  deaths 8 (<=25)        cooldown 60.7s       mine median 28.1s / zero 3.9%
  runtime_error 0        trident deaths 0     trade 1 / brew 2
```

- **BUG-028 未复现**：r17 加载修复后 878 次调用 0 Traceback；`escape_water/escape_lava` 均正常返回
- **BUG-027 已被 `e65356d` 前置校验化解**：`out_of_reach` 10 次（带距离+建议），`no_window` 6→3；模型收到后会补 `mc_move_to`，`nju_3` 已成功开窗交易；残留是"贴脸仍被村民碰撞箱卡住"→ P2-24
- **P2-1 部分修复**：`6f54b6f` 把"已在 range 内"转为成功，但 r17 仍有 10 次（entity_id 未覆盖 / status 取失败）
- 死亡 8：`hy4_6` 5 次（lava 3）、`hy4_1` 2、`ds_4` 1；暂无 trident

## 4h. r17 收官（22/22，0 Traceback）与背包问题

**门禁**：9/11 PASS；仅 **BUG-026** 未达标 —— 死亡 incident 28（>25）、`cancel_cooldown_wait` 302.8s（>200）。
- 死亡 20（notable 去重）：`hy4_hy_6_nether_bastion` **11 次**（mob 8 / lava 6 合计口径），其余 1~2 次
- 分数：2090（r15 2210），`hy4_hy_2_iron` **100→20（-80）**，`ds_2_cave 60→25`，`hy4_7 170→135`
- `hy4_hy_2_iron` 的 4 集里 2 次 `shiny_gear` timeout（18/19 calls）。**已排除背包因素**：无 `inventory_full`/`no_drops_collected`，产物正常入包
  - 真实根因（**BUG-030**）：EP2 下潜到 y=27 后 `backtrack_blocked` 把位置拉回 **y=64（+37）**，整段作废；EP3 稳定下潜，但到 **第 17 次调用**才到 y=-54，随即 episode 超时——只差 1~2 次调用
  - 即：32s 默认切片 × 调用预算不足以覆盖 100+ 格下潜；backtrack 恢复还会清零深度进度

**BUG-029（P1，Slice C）背包/物品栏管理**
- r17 有 **7/22 路**超过保留上限：`hy4_2` 450 石料、`nju_3` 309、`hy4_9` 274、`ds_2` 220、`glm_2` 783 地狱岩
- 根因：`library.py:1465-1467` 在"空槽足够"时提前 `return`，导致 :1478 的 `cobble_over_cap` 检查**永远不可达**；且清理只在 craft/harvest 触发，`mine_dfs/mine_blocks` 后无触发点，r17 全程 **0 个 dropped 结果**
- 修复：cap 判断前置 + 采集后软清理（cobble≤384/地狱岩≤128/泥土≤64）+ 返回 `inventory_cleaned`

## 4i. r18 早期（22:20，0/22 summary）

- 门禁 10/11 PASS；唯一 FAIL 是新问题 **BUG-033**：`ds_ds_4` 的 `scene-watchdog-DS_4` 线程被 `ReflexPreempted(BaseException)` 打死（`except Exception` 抓不到），watchdog 静默失效
- 其余早段指标：`mine_dfs` 中位 27.4s / 零进展 4.3%、deaths 11、cooldown 145.5s、trade 2 / brew 2、无 no_support / 木棍死锁
- 验收脚本已修正口径：`Exception in thread` 不再误报为 startup crash，单列 `BUG-033 thread exceptions`

**新增 BUG-033（P1，原误用 BUG-031 已改号）修复建议**：guardian 对只读 GET 不抛抢占信号；runner 的 watcher 显式捕获 `ReflexPreempted` 并上报 `thread_health`。

### 4j. r18 中段（22:29，888 calls，0/22 summary）——取消风暴回潮

| 指标 | 值 | 结论 |
|---|---|---|
| `model.failed(cancelled)` | **186**（前 18 分钟） | 其中 `hy4_hy_4_cave_combat` **113 次** |
| `cancel_cooldown_wait` | **393.4s** | `hy4_hy_4` 占 259.7s |
| `pre_hurt_ranged` | **74 次**（melee 11 / creeper 5） | cave_combat 骷髅持续射击 |
| 死亡 incident | 17（<=25 门禁 PASS） | — |
| `mine_dfs` 中位 / 零进展 | 27.9s / 4.8% | 稳定 |
| thread exceptions | 1（`ds_4` 的 scene-watchdog） | BUG-033 |

**新增 BUG-034（P1，Slice B）**：`PRE_HURT_RANGED_COOLDOWN_S=10s` 对持续远程火力太短，且 `status.blocking=True` 不豁免取消；建议 30~45s combat stance + 举盾期间不取消。

**ID 冲突已处理**：watchdog 线程单原误用 BUG-031（与 Integrator 的长程单重号），已改为 **BUG-033**，验收脚本同步更新。

### 4k. r18 深入归因（22:36，18/22 summary）

**BUG-030 影响面（深挖全面失效）**：8 路发起 `target_y<=-40` 的调用，**0 路到达目标层**；合计 ~82 次调用 ≈ 38 分钟。

| bot | deep calls | 最深 y | 关键现象 |
|---|---|---|---|
| `hy4_hy_2_iron` | 23 | -28 | 48→41→**48** 回退；3 次 `time_slice` 0 收益 |
| `nju_3_village_trade` | 19 | 55 | 94→58 后 `shaft_exhausted` → 被迫 `needs_surface_climb` → 从 97 重开 |
| `hy4_hy_9_deep_water` | 17 | -28 | **y=2 / -6 已挖到 diamond×2**，仍被 `target_y=-54` 牵着下潜 |
| `hy4_hy_10_deep_water` | 11 | 18 | 未有效下潜 |

→ 结论：不仅是"预算不够"，还有 **`shaft_exhausted` 后强制回地表** 的深度清零。与 BUG-022 同源，但影响已扩散到 8 路。

**BUG-034 分集证据（`hy4_hy_4`）**：两集各 **22~23 次 pre_hurt / 40 次 cancel**，间隔中位 **12.1s**（10s 冷却）；骷髅距 3.5~8.4m，`subcortical_combat` 未击杀/未换掩体。

**BUG-029**：r18 目前 `inventory_full` 0、`no_drops_collected` 7、`partial_collection` 1 → 尚未造成硬失败，待 `owned_end` 收官复核。

## 4l. r18 收官（22/22）与 r19_short 收官（18/18）

| 门禁 | r18_22bot | r19_short | 说明 |
|---|---|---|---|
| startup crash | 0 ✅ | 0 ✅ | — |
| BUG-025 木棍 | 0 ✅ | 0 ✅ | — |
| BUG-024 no_support | 0 ✅ | 0 ✅ | — |
| BUG-026 死亡 | **37** ❌ | 13 ✅ | r18 死亡集中在下界：`ds_4` 7、`hy4_6` 6、`hy4_8` 4 |
| BUG-026 冷却 | **653.2s** ❌ | **205.6s** ❌ | `hy4_4` 单路 117 次取消 |
| BUG-018 mine median | 27.8s ✅ | 30.8s ✅ | — |
| BUG-018 zero-slice | 3.4% ✅ | 9.3% ✅ | 接近阈值 |
| BUG-020 runtime_error | 0 ✅ | 0 ✅ | — |
| BUG-033 thread | **1** ❌（旧码） | 0 ✅ | `2bfe3c7` 修复在 r19 生效 |
| BUG-023 trident | 0 ✅ | 0 ✅ | — |
| SCENE-001 trade/brew | 2 / 2 ✅ | 1 / 2 ✅ | — |

**修复核对**
- **BUG-033 FIXED**：`2bfe3c7` 对 GET/只读/非 actor 线程不再抛 `ReflexPreempted`；r19 0 异常（提交信息误写 BUG-031）
- **BUG-029 部分修复**：cap 判断已可达，但 r19 仍有 **3/18** 超限（`nju_guitest` 234、`ds_2` 261 cobble、`glm_2` 138 netherrack）→ 缺"挖矿结束后软清理"触发点
- **BUG-030 未修**：r18/r19 的 iron 槽都只有 **20** 分，`mining_dfs.py:4818` 仍是 `min(90, max_duration_s)`
- **BUG-026/034 未达标**：r18 冷却 653s，主因仍是下界 lava/on_fire + `pre_hurt_ranged` 取消风暴

**监听**：通用 watcher 已自动发现并接管 `r20_long` / `r20_short`（不依赖波次名），r19 收官后已重启（PID 1637626）。

### 4m. r20 早期：新 P0 **BUG-035**（熔炼收取 ReadTimeout 打死进程）

`runs/r20_short/hy4_hy_3_midgame/stdout.log`：
```
curriculum.py:1813 → collect_job → ensure.py:443 → library.py:4121 smelt_collect
  bot.call("window/view") → httpcore.ReadTimeout
  → 未捕获 → main 退出，整路丢失
```
- `smelt_collect` 只 `except ApiError`，未捕获 `httpx.HTTPError`；`collect_job`/`curriculum.run` 也无兜底
- 修法：ReadTimeout 视为"稍后再收"（保留 job、返回 deferred），不要让恢复流程崩溃
- 影响：任何有未收熔炼任务 + 网络抖动的 bot 都会整路丢失

### 4n. r20_short / r20_long 早期（23:12）

- **BUG-035 连续第 2 波复现**：`hy4_hy_3_midgame` 再次在 `library.py:4121 smelt_collect` 的 `ReadTimeout` 整路退出 → P0 确认
- r20_short 其余门禁：deaths 8、cooldown 66.4s、thread exceptions 0、trade 2 / brew 2、mine 耗时/预算 0.93、零进展 6.0%
- r20_long（4 路长任务）：ALL PASS（trade/brew 门禁按 stage 自动 N/A）
- 门禁改进：
  1. `BUG-018` 从"绝对中位 25~40s"改为 **耗时/声明预算 ≤1.2**（r20 大量用 60/90s 预算，旧门禁误报）
  2. `SCENE-001`/`BUG-023` 在波次不含对应 stage 时自动 **N/A**，不再误报
  3. 线程异常与进程崩溃分开统计（`Exception in thread` → BUG-033 门禁）

### 4o. r20 第二轮复核（23:13）

- **BUG-030**：r20_short **45 次**深挖 `target_y<=-40`，**0 次到达**；最深 -22，多路仍停在 y=60~74 → "深度清零/拒绝续潜"比预算更致命
- **BUG-034**：r20_short 取消仅 **43 次 / 70.4s**（pre_hurt_ranged 10），本波安静；但 r18 的 653s 仍说明机制未改，保持 OPEN
- **BUG-035**：仅 `hy4_hy_3` 1 路复现（连续第 2 波）
- r20_long（4 路 bare/midgame）：ALL PASS；新 P2-28 `smelt:missing_fuel`

### 4p. r20 修复核对（23:15）

| 工单 | 状态 | 证据 |
|---|---|---|
| BUG-029 | **FIXED** | `53835cf` 在 `mine_dfs` 返回路径调用 `_ensure_free_slots` 并回报 `inventory_cleaned`（缺失触发点已补） |
| BUG-034 | **FIXED（待收官）** | `guardian.py:144` 改为 30s；r20_short 早期 43 次/70.4s（r18 311/653） |
| P2-24 | **FIXED** | `9ecb239` NoAI 村民 + 4m 前置；r20 `window_view ok 2 / out_of_reach 1 / no_window 0`，trade ok 2 |
| BUG-035 | **未修** | `library.py:4120` 仍只 catch ApiError；r20 第 2 波复现 |
| BUG-030 | **未修** | `mining_dfs.py:4818` 未改；r20 45 次深挖 0 到达 |

### 4q. r20_long 增量分析（23:18）

- **BUG-036（P1）**：`nju_movetest_01_bare` 45 分钟长任务在 **425.7s** 结束，`status=auth_error`，错误 `[invalid_token] token not found or expired`。curriculum 在集边界遇 401 直接终止整轮，没有用 TokenStore 重新登录续跑；另有"同名 bot 跨波次并发"的互斥风险（`MoveTest_01` 共用 token 文件）。
- 其余 3 路长任务仍在跑（32~52 次工具调用）：
  - `ds_ds_1_bare` 末尾 **连续 4 次 `staircase:time_budget`** → 见 BUG-037
  - `hy4_hy_1_bare` 2 死、`mine_dfs` 出现 `backtrack_blocked@...`（深度清零老问题）
  - `glm_glm_5_midgame` 正常（`move_to` ok、少量 `no_such_entity`）

**新增 BUG-037（P1，Slice C）**：r20_short 的 `staircase:time_budget` 28 次里 **21 次 `steps_done=0`**，单次中位 15.3s ≈ `per_step_timeout=15s` → **第一步被 per-step 超时饿死**。建议第 0 步用总预算（45s）或把 per-step 提到 20~25s。

### 4r. r20 BUG-035 触发链 + 裸开卡点（23:22）

- **BUG-035 触发链**：`hy4_hy_3` 的 curriculum 里 `active_world_jobs: 1`，第 2 集 `use_lodestone` 为 `paused_for_world_job`；下一集 resume 时 `collect_job → smelt_collect` 撞 ReadTimeout。全波仅 1 个 active job，但有 job 的 bot 必中。
- **BUG-037 影响裸开**：`ds_ds_1_bare` 的 `bootstrap_iron_age` 13 次调用里 6 次 `staircase:time_budget` → 铁器时代直接超时。
- **P2-28 升级**：`ds_1` 报 `missing_fuel`（零燃料、零木头）；建议提示先 `harvest_logs(2)`，并把"有原木"纳入自动燃料选择。

### 4s. BUG-030 根因修正（23:25）

原判断"每片只下潜几格"不准确：实测**每次平均下潜 11.5 格**。真正的问题：

- r20_short 50 次深挖调用中 **29 次从 y>50 地表重新起挖**（58%）
- `hy4_hy_2_iron`：文件 2 开头 `-17 → hurt` 后位置 **67** → 一次死亡清零 80 格下潜
- `nju_3`：新一集 `start_y=95`，集边界同样重置
- 显式回撤（dy>5）整波仅 1 次 → 不是回撤，而是**死亡/集边界重置**

→ 修复重点改为：新集/复活后**复用已建竖井**（restore shaft + 走到最深节点），而非从地表重挖；预算自适应降为次要。

## 5. 附录：工具时间占比（r11，start→finish 差值，已验证口径）

- 全队工具墙钟合计：`mine_dfs` **7338s**、`move_to` 2310s、`staircase` 1606s、`mine_blocks` 1165s，其余 <800s
- 单 bot 工具时间占 run 墙钟：**32%~83%**（中位约 65%），其余为模型生成/等待
- 最高：`hy4_hy_9_deep_water` 1071s/1298s（83%，其中 mine_dfs 1008s）、`ds_ds_2_iron` 995s/1207s（82%）
- 最低：`hy4_hy_5_midgame` 389s/1200s（32%）、`glm_glm_3_village` 458s/1200s（38%）
- 结论：`mine_dfs` 是最大时间池，但单次已收敛（中位 35s / max 174.5s）；优化重点应放在"减少无效 mine_dfs 调用"（BUG-013、P2-11），而不是继续压缩单次时长。

---

## 6. 交接建议（省额度）
- **Slice A（#3）**：**BUG-018（P0，`/8` 一行修复，最高优先）**；BUG-019 的 `time_slice` 文案改 `suggested_next`
- **Slice B（#2）**：BUG-009 去抖与冷却、BUG-010 复活进食、BUG-011 反射网络抖动不可致命
- **Slice C（#4）**：BUG-002 进传送门、BUG-006 别名、BUG-007 配方预检、BUG-008 桶假阴性
- **Slice D2（#6）**：BUG-001 的 `import re` + `v2_fleet.py triage` 字段对齐（可直接参考 `scripts/d1_triage.py`）
- **Integrator（#1）**：BUG-020（525/526 入重试清单、`not_loaded`/`not_connected` 不判整集失败）
- 其余 P2 已归档，不需要任何人现在放下 P0/P1
