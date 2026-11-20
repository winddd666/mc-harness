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

口径更正（先前"58% 从地表重挖"是把每个 episode 的首次调用误计为重启）：

- **11 个深挖 episode = 11 次首调用 + 39 次续调用**；续调用里只有 **1 次**净上升 >15 格后重开
- 每片平均下潜 **11.5 格**，从 y≈64 到 -54 需 **10~11 次调用 ≈5 分钟**；episode 还要分给备料/战斗，常在到达前超时
- `hy4_hy_2_iron` 文件 2 的 `-17 → hurt → 67` 是唯一一次深度清零样本

→ 修复重点：**跨 episode 复用竖井**（restore shaft，走到最深节点）；单次预算自适应只减少轮次开销，不改变总挖掘时间。

### 4t. r20_short 收官（17/18，`hy4_hy_3` 被 BUG-035 吞掉）

| 门禁 | 结果 |
|---|---|
| startup crash | **1 ❌**（BUG-035） |
| BUG-025 stick | 2 ❌（均为真缺原木） |
| BUG-029 over-cap | 2 ❌（`glm_3` cobble 242、`hy4_8` netherrack 182） |
| BUG-026 死亡 / 冷却 | 14 ✅ / **157.7s ✅**（r19 205.6、r18 653.2） |
| BUG-018 耗时/预算 / 零进展 | 0.93 ✅ / 6.2% ✅ |
| BUG-020 / 033 / 023 / SCENE-001 | 全部 ✅ |

- 分数 1760（18 路 r19）→ 1680（17 路 r20），人均 97.8→98.8
- `objective_verified` 17→21（变好）
- BUG-029 残留成因：清理只挂在 `mine_dfs`，`mine_blocks`/`tunnel` 收集的石料没有触发点

### 4u. BUG-037 模式拆分（r20_long，23:28）

| 波次 | `steps_done=0`（per-step 15s 饿死） | `steps_done>0`（45s 总预算 vs 长请求） | 请求 steps 中位 |
|---|---|---|---|
| r20_short | **24** | 12 | 12 |
| r20_long | 0 | 8（完成中位 8 步） | **40** |

- `ds_ds_1_bare` 铁器集 6 次 `time_budget` 全是 7~9 步 → 不是首步问题；模型一次要 40 步，45s 必然做不完
- `missing_fuel` 后**没有恢复动作**（未 harvest_logs），铁器时代卡死 → P2-28 提示必须给出下一步

### 4v. BUG-030 竖井树追踪（23:30）

| bot | 现象 | 结论 |
|---|---|---|
| `hy4_hy_1_bare`（r20_long） | `node_count 1→…→15` 跨集保留，`surface_reached` 后继续下潜到 **-27** | **期望行为**（树可复用） |
| `hy4_hy_2_iron`（r20_short） | 树 1→7 后某次 `action=explore` 返回 `node_count=0`、y=64（**参数里没有 reset**） | 树在两次调用间被清，需加 `tree_reset{reason}` 事件定位 |
| `hy4_hy_4_cave_combat` | 树保留 19，但 `active_node_id` 回退 16/9/5、位置 y=39/51 | 有记忆但**不回最深节点** |

→ 结论修正：**树通常是保留的**；要修的是"偶发归零"（先打点定位）与"恢复时导航到最深节点"，而不是假设受伤必清树。

**BUG-036 损失量化**：`nju_movetest` 425.7s / 2700s → 损失 **84%** 预算。

### 4w. 修复核对（23:27）

- **BUG-035 FIXED**：`3aea714` 在 `collect_job` 捕获瞬态异常并返回 `deferred/still_burning`；`d1_smelt_timeout_check.py` **exit 0**
  ```
  collect_job returned: {'ok': False, 'deferred': True, 'still_burning': True, ...}
  OK: timeout is deferred and the job is preserved
  ```
- **BUG-029 残留**：`989d757` 已提交 `_cleanup_inventory_after`（挂 harvest/mine_blocks/tunnel），但 **HEAD 未合入**，r21 前需 #1 合并
- 仍在跑：r20_long 1/4（预计 ~23:45）

### 4x. r20_long 裸开恢复死锁 → BUG-038（23:29）

`ds_ds_1_bare` 断镐后的完整恢复链：

```
pickaxe_low            y=20→17
climb_out  53.0s       17→20
climb_out  27.7s       20→20   (time_slice, 0 进展)
climb_out  40.9s       20→20   (time_slice, 0 进展)
climb_out  43.3s       20→25
climb_out  25.6s       25→26
staircase  25.8s       26→27   (time_budget)
unstick    25.7s
staircase  16.2s       27→27   (time_budget, 0 进展)
→ 下一集 craft_gear missing / harvest_logs no_targets
```

- **~250s 只上升 10 格**，`bootstrap_iron_age` 超时，整轮停在 15 分
- 两次 `climb_out` 0 进展说明"沿自建巷道走"未生效，退化成重新开阶梯；再叠加 BUG-037
- 已提 **BUG-038（P1，Slice A）**：climb 应沿 corridor waypoints 直达、单片 ≥8 格；断镐+无木时给量化回地表建议

### 4y. glm_5 附魔/睡觉链路（r20_long，23:30）

**正向**：
- `mine_dfs` 4 片 × 55s 下潜 → `quota_reached`；`craft_gear(diamond_pickaxe)`、`craft_gear(enchanting_table)`、`mc_place`（无坐标自动落位）、`mc_skill_enchant(iron_sword)` 全部成功 → `enchant_item +30`
- `staircase(up, steps=20)` 先 `missing_step_support`，随后 **`pillar_fallback` 成功**（BUG-024 修复在长任务中同样生效）

**问题（P2-31）**：找床阶段半径 64→128→200→300→400→600→800→1000 连发 10+ 次扫描，全部空手；随后 `craft_gear(white_bed)` 缺羊毛。建议半径上限 128 + "先找羊"。

### 4z. r20_short 收官 + r19→r20 对比 + r21 启动损失

**r20_short（17/18，`hy4_hy_3` 被 BUG-035 吞）终验**：仅 2 项 FAIL —— `startup crash`（BUG-035，`3aea714` 已修）与 `over-cap 2`（BUG-029 残留，`989d757` 未合入）；其余 9 项 PASS。

**r19→r20 逐 bot 分数**（18→17 路，sum 1760→1680，人均 97.8→98.8）：

| 变化 | bot |
|---|---|
| +45 / +30 / +15 | `nju_guitest`、`ds_4`、`hy4_4`/`hy4_9`/`nju_3` |
| -20 / -20 / -15 | `hy4_7`、`hy4_8`、`glm_1` |
| 丢路 | `hy4_3`（BUG-035） |

失败项趋势：`staircase:time_budget` 30→36（BUG-037）、`path_not_found` 31→41、`tunnel:unknown` 9→17；反过来 `move_to:bad_args` 7→2、多类 `repeated_identical_failure_blocked` 归零。

**r21_short（23:25 启动，已含 BUG-035 修复）**：`glm_1` 启动即丢 —— `ensure_bot_ready` 3 次/186s 后 `RuntimeError`（**BUG-039**）。

### 4aa. r21_short 早期（23:34，0/18 summary）

- **BUG-035 未复发**：全波 stdout `smelt_collect` 报错 0 次；唯一 Traceback 是 `glm_1` 的 `ensure_bot_ready`（BUG-039）
- 门禁：`startup crash 1`（BUG-039）、`zero-slice 11.8%`（早期偏差，r20 收官 6.2%）两项 FAIL；其余 9 项 PASS（deaths 2、cooldown 23.5s、thread 0、trade/brew 2/2）
- `mine_dfs` 51 次调用、6 次 0 进展；`staircase:time_budget 10`（BUG-037 继续）

### 4ab. BUG-037 第三种模式（r21，23:35）

`ds_ds_4_nether_fortress` 在下界 `[705,66,640]`：

```
staircase(north, down, steps=40) → steps_requested=25（被 clamp）
   steps_done=0, dug_count=0, movement_not_confirmed
   viable_directions=[south,east,west]
staircase(south, ...) → 同样 0 步失败
```

→ 建议：0 步 0 挖时自动 `unstick` 或用 `viable_directions[0]`，并把 `steps` 被 clamp 到 25 回传给模型（`steps_clamped`）。否则模型按 40 步规划、在失败方向上来回试。

## 4ac. r20_long（4/4）与 r21_short（17/18）终验（23:57）

**r20_long（45 分钟长任务）**

| bot | 分数 | 集数 | 备注 |
|---|---|---|---|
| `glm_glm_5_midgame` | 180 | 12 | enchant/sleep 链路正常 |
| `hy4_hy_1_bare` | 40 | 7 | 断镐恢复慢（BUG-038）、deepslate 493 超限 |
| `ds_ds_1_bare` | 15 | 9 | pickaxe_low + missing_fuel 卡铁器 |
| `nju_movetest_01_bare` | **0** | 3 | **auth_error（BUG-036）** |

门禁：仅 `BUG-029 over-cap 2` 失败；deaths 4、cooldown 57s、mine 0.86/8.1%、thread 0。

**r21_short（17/18）**

- 唯一 FAIL：`startup crash 1` = BUG-039（`glm_1`）
- **BUG-035 实机 0 复发**；上波被它吞掉的 `hy4_hy_3` 本波 **130 分**
- deaths 14→9、cooldown 157.7→111.7s、over-cap 0、trade/brew 2/2
- 分数 1680→1690（17 vs 17）；若 `glm_1` 不丢约 **1850**

**对比 r20_short→r21_short**：`+30`：`glm_2`、`hy4_3`；`+15`：`hy4_5`、`hy4_7`、`hy4_8`；`-20`：`hy4_9`；丢路：`glm_1`。

## 4ad. r22 早期（00:00，586 tool calls）

- **BUG-029 残留修复已合入**：`989d757` 进入 HEAD（`e60a8d5` 23:47 合并，`grep` = 4 处）；r22 加载于 23:48 → 本波可验收 `owned_end`
- **r22_short**：0/18 summary、**0 Traceback**（BUG-035/039 均未复发）；唯一 FAIL 是 `mine zero-slice 15.7%`（早期偏差，dur/budget 0.65）；deaths 10、冷却 93.7s、trade 3 / brew 2
- **r22_long**：0/4、0 Traceback，ALL PASS（早期）

### 4ae. r22 新 P1：虚空围栏中心错位，重创 NJU 两路（00:03）

| bot | spawn | 围栏 center | 被拦次数 |
|---|---|---|---|
| `nju_guitest` | (6381.5, -6415.5) | **(797.6, -814.5)** | 8（staircase/move_to） |
| `nju_nju_3` | (-5703.2, 5728.0) | **(-153.5, -385.5)** | 11（tunnel/collar/staircase） |
| `ds_ds_3` | (-2974.5, 9974.5) | 同 spawn ✓ | 1（213.9m，真实贴边） |

- 根因假设：`agent.py:1234-1250` 的 ephemeral-HUD **自动回中**（overworld 且距中心 >400m 时把中心设为 bot 当前位置）与场景 teleport 竞态；NJU 网关较慢，teleport 未落地时被回中，围栏永久钉在旧坐标
- 后果：场景内所有 `staircase`/`tunnel`/`move_to` 都被判"离岛"（距离 7600~8300m）
- 已提 **BUG-040（P1，Integrator + D2）**：runner 围栏加 pinned、或回中前校验；`verify_scene_entry` 增加围栏一致性断言

### 4af. r22 进度快照（00:09，r22_short 15/18）

- `void_geofence_blocked` 累计 **29 次**：`nju_3` **19**（分数 25，r21 40）、`nju_guitest` 9（本波 180）、`ds_3` 1 → **BUG-040 确认是 NJU 掉分主因**
- `mine` 零进展 15.7% → **11.3%**（持续收敛）；`staircase:time_budget` 17（BUG-037）
- `r22_long` 44 次 mine、0 零进展、0 void、0 Traceback
- `glm_1`/`hy4_8` 未收官但**无 Traceback** → BUG-039 本波未复发

### 4ag. r22_short 终验（18/18）与 r21→r22 对比

**分数**：1690（r21，17 路）→ **1845**（r22，18 路）；人均 99.4 → 102.5。

| 变化 | bot |
|---|---|
| +60 / +20 / +15 | `hy4_6`（250）、`nju_guitest`（180）、`hy4_10` |
| -35 / -30 / -15 | `glm_2`（160）、`hy4_8`（150）、`nju_3`（25，BUG-040）、`hy4_5`、`hy4_7` |

**门禁**：3 FAIL —— deaths **29**、cooldown **258.2s**、zero-slice **11.3%**；其余 9 项 PASS（含 **BUG-029 over-cap 0 ✅**）。
死亡原因（21 notable）：`arrow 7 / mob_attack 7 / lava 4`，集中于 `nju_guitest 6`、`hy4_6 6`、`hy4_8 5`。
取消 131 次：`hy4_8 30`、`hy4_6 19`、`nju_guitest 16`、`hy4_10 14`；触发仍以 `fresh_hurt 61` 为主。

**结论**：BUG-029 修复通过终验；生存与取消在 r22 回潮（r21 为 9 死/111.7s），需 Slice B 复核 30s pre-hurt 冷却是否在真实受击场景下过钝。

### 4ah. r22_long 铁器死锁链（00:15）

`hy4_hy_1_bare` 在 y=39 地下形成闭环：

```
craft_gear(stone_pickaxe) → missing {"stick":2}（无木）
harvest_logs              → "y=39 低于树木带，先 staircase(up)"
staircase(north, up, steps=20/30) → time_budget ×11（模式二）
mine_dfs(iron)            → insufficient_pickaxe_tier（仅木镐）
```

→ 24 分钟仍 10 分。修 BUG-037 模式二（clamp steps 或按 steps 缩放预算）可直接解锁。
`ds_ds_1_bare` 本波 iron_age 已 `goal_satisfied +10`（20 分），说明恢复路径可行、但依赖地形。

### 4ai. BUG-041：木镐硬岩导致 30s/格（00:17）

`hy4_hy_1_bare` 的 staircase 1 步超时调用：

```json
{"stop_reason":"time_budget","steps_done":1,"dug_count":4,
 "timeout_path":"dig","dig_target":[-11054,36,-11051],
 "steps_capped_from":20,"steps_clamped":true}
```

- 8 次调用（north/east/south/west）全部 `steps_done=1`，耗时 24~32s
- 原因是**木镐挖 stone/deepslate**：单块 ~25s；不是 45s 预算或 clamp 问题
- 与 BUG-037（clamp 已回传）、BUG-038（climb 慢）共同构成裸开铁器死锁

**新增 BUG-041（P1，Slice C）**：低镐等级时应优先软方块方向（dirt/gravel/sand），并在 `timeout_path=dig` 时直接提示补石镐。

### 4aj. r22_long 中段快照（00:20，约 28/45 分钟）

| bot | 分数 | 集数 | 状态 |
|---|---|---|---|
| `glm_glm_5_midgame` | **195** | 11 | 正常推进 |
| `ds_ds_1_bare` | 20 | 4 | 铁器已过 |
| `nju_movetest_01_bare` | 20 | 5 | 缓慢 |
| `hy4_hy_1_bare` | **10** | **6** | 6 集无进展 → BUG-041 死锁实证 |

- `hy4_hy_1` 6 集仍停在石器（10 分），与 BUG-041 的"木镐硬岩 30s/格"完全吻合
- `glm_5` 出现 `move_to(entity_id=skeleton@31.5m, ignore_hurt=True)` → `stalled ×2`（P2 级，仅 4 次/2 bots，暂不提单）
- 全波 `mine_dfs` 0 零进展、0 void、0 Traceback

### 4ak. BUG-041 成本量化（00:21）

`hy4_hy_1_bare` 整轮工具时间：

```
staircase 426s（7.1 分钟）  ← 两个 episode 282s / 144s，占该集 59% / 94%
mine_dfs  654s
unstick    46s
```

只换来 y 33→43（仍在树木带 y<58 之下），6 个 episode 仍 10 分。→ BUG-041 是当前裸开分数天花板的直接原因。

### 4al. r23_short 早期 + BUG-040 修复验证（00:22）

- **BUG-040 FIXED 待终验**：`165e652`（00:13）在每个 episode 开始时检测"bot 是否已在围栏半径外"并重锚；r23 加载于 00:14 → 已含修复
- r23 前 65 个事件文件：**`void_geofence_blocked` = 0**（r22 同期已多次出现）
- 早期门禁：仅 `mine zero-slice 20%` FAIL（样本小，需观察）；trade **5 次**、brew 2 次、thread 0、runtime 0

### 4am. r22_long 成就天花板对比（00:23，约 32/45 分钟）

| bot | 分数 | 集数 | 成就 |
|---|---|---|---|
| `glm_glm_5_midgame` | **195** | 11 | 13 个：`shiny_gear 60`、`enchant 30`、`lodestone/lava_bucket/deflect_arrow/form_obsidian/mine_diamond 各 15`… |
| `ds_ds_1_bare` | 20 | 4 | stone + iron 四件套 |
| `nju_movetest` | 20 | 5 | stone + iron 四件套 |
| `hy4_hy_1_bare` | **10** | **7** | 只有 stone age |

- 说明：**深挖/钻石链路在 GLM 上完全可行**（45 分钟内拿到 shiny_gear+enchant），BUG-030 不是全局阻塞
- `hy4_1` 7 集仍 10 分，是 BUG-041（木镐硬岩）单点卡死，与模型能力无关

### 4an. BUG-040 残余：死亡回城导致围栏钉在出生点（00:25）

- r23 的 NJU 两路已 **0 次** `void_geofence_blocked`（r22 为 9/19 次）→ `165e652` 主要场景已修好
- 残留 1 例：`hy4_hy_8_nether` 第 3 集 **elapsed 238s** 时被拦，center=(972.4,350.5)、目标 (7795.5,2806)、距离 7251m
- 推断链：死亡 → 回世界出生点 (972,350) → HUD 自动回中把围栏钉在出生点 → 返回场景时越界
- 追加修复：死亡/重生时按**场景锚点**重锚；或在工具检查前惰性重锚

### 4ao. r23 zero-slice 17.5% 的根因（00:27）

- 10 次 0 进展 `time_slice` 全部是**下界** `ancient_debris/gold @ y=15`：`pos_before == pos_after`，耗时 28~50s（预算 60~75）
- tree：`active_node 5` 挂着 `pending: stair_down south, steps=35`，无 pruned 分支
- → `mine_dfs` 内部 `dig_staircase` 在**第一步**就超时退出（与 BUG-037 per-step 15s 同源），分支未执行，引擎仍返回 `time_slice`
- 建议：内部阶梯同样豁免第 0 步；0 进展 + pending 分支 → `branch_failed` → relocate

### 4ap. P2-14 趋势（00:28）

`bad_target_for_dimension`（下界要挖钻石）每波稳定 **4~7 次**：

```
r13 5  r14 5  r15 4  r17 4  r18 7  r19 7  r20 4  r21 6  r22 6  r23 4
```

- 维度守卫工作正常（每次都给 ancient_debris/quartz/gold 建议）
- 但模型仍反复尝试 → 每波浪费 4~7 个模型轮次；根因是 prompt "钻石镐在下界…" 的歧义
- 建议 D2 改为"用钻石镐挖 ancient_debris（钻石仅存在于主世界）"

### 4aq. BUG-041 修复已在 fix/s-skills（00:29）

- `1bbeb4f`：dig 超时按 `pickaxe_tier` 命名根因（"木镐挖 X 约 25s/格"），并给四条出路
- `0264145`：同一 dig 格超时后 `_avoid_temporarily(..., "dig_timeout", 30s)` + 下次快速失败
- 与 D1 建议一致；**尚未合入 HEAD**（`grep low_tier_slow_dig` = 0），需 #1 合并后 r24 复验

## 4ar. r22_long 终验（4/4）与 r20_long 对比

| bot | r20_long | r22_long | 说明 |
|---|---|---|---|
| `glm_glm_5_midgame` | 180 | **195** | 15 集，shiny_gear+enchant |
| `nju_movetest_01_bare` | 0（auth） | **35** | token 正常，恢复 |
| `ds_ds_1_bare` | 15 | 20 | 铁器已过 |
| `hy4_hy_1_bare` | 40 | **10** | 11 集，BUG-041 + **5 集被 429/502 吞** |

**门禁**：`deaths 6 / cooldown 82.1s / mine 0.92 / zero-slice 1.1%` 全 PASS；2 FAIL：
- `runtime_error 5` → 新增 **BUG-042**（Qwen 429 "overloaded" 未被识别为并发限流，3 次退避后整集报废）
- `over-cap 1` → `nju_movetest` cobble 262（`989d757` 已合入但仍残留，可能来自 `harvest/mine_blocks` 之外的路径）

**结论**：长任务能力上限明显提升（glm_5 195、nju 恢复），但 `hy4_1` 同时被 BUG-041 与 BUG-042 打击。

## 4as. r23_short 终验（18/18）—— provider 过载吞掉 21 集

| 门禁 | 结果 |
|---|---|
| BUG-020 runtime_error | **21 ❌**（18× 429 overloaded + 3× 502） |
| BUG-018 zero-slice | 13.5% ❌（下界 ancient_debris 内部阶梯问题） |
| BUG-026 死亡 / 冷却 | 17 ✅ / 165.5s ✅ |
| BUG-029 over-cap | **0 ✅** |
| BUG-040 void | 5 次（`hy4_8`，死亡回城残余） |
| trade / brew | 5 / 2 ✅ |

- 分数 1845 → 1765（18 vs 18）；死亡 29 → 17（改善）
- 报废集分布：`hy4_7 5`、`hy4_6 4`、`hy4_2 3`、`hy4_9 3`、`hy4_5 2`、`hy4_8 2`、`hy4_3 1`、`hy4_10 1`
- **r24 实时**：`r24_short` 已有 100 次 `http_retry` + 14 次 429，provider 仍在过载

## 4at. r24 早期：provider 过载仍在恶化（00:46）

- `r24_short`：**107 次 http_retry + 14 次失败 429**，已产生 **14 个 runtime_error 集**（`hy4_3/6/9` 各 3、`hy4_7` 2、`hy4_2/5` 各 1）
- `r24_long`：13 次重试、0 失败（长任务请求频率低，受影响小）
- **BUG-041 已合入**：`7b6e64d`（00:35），r24 加载于 00:37 → 本波可复验木镐提示/快速失败
- 建议 #1：暂停新波次或切 provider，避免继续用 429 稀释分数

### 4au. r24 BUG-037/041 交叉验证（00:47）

| 波次 | stair time_budget | zero-step | dig_timeout | 低镐新文案 |
|---|---|---|---|---|
| r23_short | 11 | 7 | 10 | 0 |
| r24_short | 9 | 5 | 9 | 0 |

- r24 的 `timeout_path=dig` 消息仍是旧版（无"木镐"）→ 这些 bot 镐等级 ≥2
- 说明**首步 15s 超时与镐等级无关**（黑曜石/堡垒砖同样触发）→ BUG-037 的第 0 步豁免仍必须
- BUG-041 的低镐文案需等裸开（`r24_long`）出现 low-tier 场景才能实机验证

### 4av. BUG-042 定位到 hy4 免费通道（00:48）

- `r24_short` 的 **18 个 runtime_error 全部是 `hy4-flash-free`**：`hy4_7` 4、`hy4_3/6/9` 各 3、`hy4_5/8` 各 2、`hy4_2/10` 各 1
- **glm / ds / nju 0 次** → 过载是 hy4 免费通道专属
- 429 事件持续到 elapsed 386s（仍在发生）
- 运维建议：暂停/降并发 hy4 squad，保留 glm/ds/nju；代码侧仍按 BUG-042 修 429 分类

### 4aw. 评分口径问题（00:49）

- `midgame` setup 预授予 6 个成就（`mine_stone/upgrade_tools/smelt_iron/iron_tools/mine_diamond/shiny_gear`）= **95 分**
- r24_long `glm_5` 在 **180s、EP1 timeout** 时已 115 分 → 其中 95 分是白送
- 影响：`glm_5 195 vs bare 10` 不能直接当作能力差异；跨场景/跨波次分数不可比
- 建议（P2-32）：episode 起始做 advancement 基线，final_score 只计增量；或报告单列 baseline/earned

### 4ax. r24_short：hy4 通道丢失 46% 集（00:51）

- 72 个 episode 中 **33 个 runtime_error（46%）**，全部 `hy4-flash-free`：`hy4_3` 7、`hy4_7` 7、`hy4_6/9` 各 4、`hy4_8/10` 各 3
- `r24_long` 仍 0（长任务频率低）
- 门禁脚本已修正：**runtime_error 现在也读取进行中的 `curriculum-*.json`**，此前 0 summary 时会漏报（r24_short 曾显示 0）
- 建议：#1 立即暂停 hy4 squad 或切 provider

### 4ay. hy4 通道报废率（00:50）

| bot | 总集 | runtime | 成功 |
|---|---|---|---|
| `hy4_6_nether_bastion` | 4 | **4（100%）** | 0 |
| `hy4_7_village` | 9 | **8（89%）** | 0 |
| `hy4_3_midgame` | 12 | **8（67%）** | 1 |
| `hy4_10_deep_water` | 4 | 3（75%） | 0 |
| `hy4_9_deep_water` | 5 | 4（80%） | 1 |
| `hy4_8_nether` | 6 | 4（67%） | 2 |
| `hy4_2_iron` | 3 | 2（67%） | 0 |

`glm / ds / nju` 全部 0 次 runtime_error。→ hy4 10 路当前几乎不产出，建议立即暂停或换 provider。

### 4az. BUG-030 埋点已生效（00:52）

`depth_reset` 新 stop_reason 在 r24_short 出现 3 次，带净上升量：

| bot | pos_before → pos_after | 净上升 |
|---|---|---|
| `ds_2_cave_combat` | 106 → 125 | **+19** |
| `hy4_2_iron` | -25 → -5 | **+20** |
| `ds_4_nether_fortress` | 44 → 54 | **+10** |

- 这是 D1 建议的埋点，现已可量化"深度清零"代价
- 行为本身仍需修：复位后应导航回 deepest node，而不是从 collar 重挖
- 另：`glm_2` 在下界 y=64 连续 5 次 `ancient_debris` 0 进展（内部阶梯首步，BUG-037）

## 4ba. 看板状态对账（00:55）

把 7 个已修复但仍标 OPEN 的工单核对代码后更新，避免团队重复修：

| 工单 | 依据 |
|---|---|
| BUG-005 | 并入 BUG-026 |
| BUG-008 | `ef6a8ac`（桶 used/gained 判据） |
| BUG-010 | `dd274ba`（`retry_after_ready` / `eat_deferred`） |
| BUG-011 | `dd274ba`（httpx 不再包成 RuntimeError） |
| BUG-012 | `26f7213`（`_API_RETRYABLE_CODES` 含 not_loaded） |
| BUG-021 | `26f7213`（残余为 BUG-039） |
| BUG-023 | `e680b1f`（深水盾+岸，r16/r17 trident 0） |

当前看板：**38 单 → 27 FIXED / 1 PARTIAL / 10 OPEN**。
仍 OPEN：BUG-007、009、026、030、032、036、037、038、039、042。

### 4bb. BUG-007 / BUG-032 复核（00:57）

- **BUG-007**：r24 的 8 次 craft 失败全部是**真实缺料**，`missing_materials` 正确；但 7 次 `message=None` → 模型需自己解析 map。剩余工作缩窄为"生成一句标准缺料提示"。
- **BUG-032**：与 BUG-038（climb 慢）/BUG-041（木镐硬岩）是同一"地底断木断镐"现象的不同层面；032 负责 curriculum 层"下矿前木料保底"。

### 4bc. hy4 过载未缓解（00:54）

- r24_short：**93 集 / 40 runtime_error（43%）**，4 分钟内 +7
- r24_long：仅 hy4_1 1 个 runtime_error（长任务频率低）
- 结论：过载持续，暂停 hy4 squad 的建议仍然有效

### 4bd. 429 诊断：通道级饱和，非上下文问题（00:55）

- 失败集 `actor_requests` 中位 **3**（max 30），成功集中位 **6**（max 45）→ 失败发生在 episode 开头
- 429/retry 按分钟：16:37~16:51 持续，峰值 16:48（10 次）
- → 缩小上下文/减少历史**不会**缓解；只有暂停 hy4 或换通道有效

## 4be. r24_short 终验 + 进入过夜低频审计模式（01:08）

**r24_short（18/18，0 Traceback）**：仅 2 项 FAIL
- `runtime_error 40`（全 hy4，BUG-042）
- `over-cap 2`：`glm_2` netherrack 265、`hy4_8` netherrack 169（BUG-029 残留，均为下界）
- 其余全 PASS：deaths 17、cooldown 116s、mine 0.93、zero-slice **3.9%**、trade/brew 2/2
- 分数 1765 → **1870**（18 vs 18，尽管丢了 40 集）；`objective_verified` 15→23

**过夜模式（01:08 起）**
- 双轨：主轨 `overnight_12bot`（12 路 nju/glm/ds，4h）+ 审计轨 `hy4_audit_*`（4 路 hy4，rpm=6，20min）
- 审计脚本 `scripts/d1_overnight_watch.py`：**只在波次收官或每 45 分钟**输出快照（PID 1728153），不做连续轮询
- 快照字段：summaries/bots、episode 状态分布、http_retry、失败 429/502、Traceback —— 直接回答"hy4 4 并发 + rpm=6 是否 0 次 429"
- 追踪重点：BUG-030 `depth_reset`、BUG-037 阶梯、`unstick` 慢调用、过夜长程新 P0/P1

## 4bf. r24_long 终验（4/4）与 r22_long 对比（01:23）

| bot | r22_long | r24_long | 变化 |
|---|---|---|---|
| `glm_glm_5_midgame` | 195 | 180 | -15 |
| `hy4_hy_1_bare` | **10**（11 集） | **35**（6 集） | **+25** |
| `ds_ds_1_bare` | 20 | 40 | +20 |
| `nju_movetest_01_bare` | 35 | 35 | 0 |

- **门禁 ALL PASS**：deaths 5、cooldown 64.5s、mine 0.90、zero-slice **0.0%**、over-cap **0**、runtime_error 1
- `hy4_1` 从 10→35：r24 已含 BUG-041 修复（低镐提示 + 同格快速失败），裸开卡死明显缓解
- `ds_1` 20→40 同步改善

**过夜双轨已启动（01:23）**
- `overnight_12bot`：12 路，刚起（14 事件文件，2 集 goal_satisfied）
- `hy4_audit_r25`：4 路，刚起（4 事件文件）
- 低频审计器 PID **1736176**（45 分钟抽检或收官即报）；hy4 轨 429 目标 = 0

## 4bg. 过夜双轨首轮审计（01:45）

### hy4 审计轨 `hy4_audit_r25`（4 路，rpm=6，20 分钟，4/4 收官）

```
retries 0 | failed429 0 | 502 0 | tracebacks 0
门禁：ALL PASS（deaths 7、cooldown 8.4s、mine 0.89、zero-slice 0.0%、over-cap 0）
```

| bot | 分数 | 集数 |
|---|---|---|
| `hy4_6_nether_bastion` | **210** | 3 |
| `hy4_5_brewing_alchemy` | 75 | 8 |
| `hy4_9_deep_water` | 30 | 3 |
| `hy4_2_iron` | 20 | 3 |

- `mine_dfs` 66 次、**0 零进展**；`depth_reset` 仅 1 次（`hy4_6`）
- `unstick` 4 次：中位 **4.35s**、max **8.3s** → **无慢调用**
- 结论：**hy4 的 429 风暴是 10 路并发导致，4 路 + rpm=6 完全安全**

### 主轨 `overnight_12bot`（12 路，4h，进行中 22 分钟）

```
48 集：answered 28 / goal_satisfied 14 / timeout 3 / limit 2 / paused 1
runtime_error 0 | failed429 0 | retries 26 | tracebacks 0
```

- nju/glm/ds 全部正常，429 为 0（重试均成功）
- 下一次抽检约 02:29 或主轨收官时

## 4bh. 过夜审计 02:30 抽检

### 主轨 `overnight_12bot`（1h07m，进行中）

```
133 集：answered 90 / goal_satisfied 24 / timeout 11 / limit 5 / paused 3 / aborted 3
retries 105 | failed429 0 | 502 0 | tracebacks 0
```

- **429/502 = 0**（105 次重试全部成功）→ 12 路 nju/glm/ds 稳定
- `mine_dfs` 386 次、零进展 20（**5.2%**）
- `depth_reset` 5 次：`glm_5` 2、`nju_guitest`/`glm_2`/`ds_1` 各 1
- `staircase`：completed 97 / **time_budget 76（44%）** / pillar_fallback 36 / **dig_cell_avoided 35**（BUG-041 fail-fast 生效）/ dig_failed 34
- `unstick` >20s 仅 5 次，max **33.4s**（无 >60s）
- **新 P1（BUG-043）**：`ds_2` 连续 3 集 `aborted / lost_iron_pickaxe`（EP4 烧了 33 次请求才判定；EP7/EP8 各 1 次又重选同一目标）

### hy4 审计轨

- `hy4_audit_r25`：4/4，**0 retries / 0 429 / 0 502 / 0 Traceback**
- `hy4_audit_r26`（进行中）：24 集、retries 4、**0 429**、**1 个 502** runtime_error

**结论**：双轨 429 均为 0，hy4 的 4 路 + rpm=6 配置可固定；当前最大 churn 是 `staircase:time_budget`（BUG-037）与 `lost_iron_pickaxe` 重选（BUG-043）。

## 4bi. 过夜审计 03:15 抽检

### 主轨 `overnight_12bot`（1h52m）

```
243 集：answered 169 / goal_satisfied 29 / timeout 25 / limit 12 / paused 3 / aborted 3 / runtime_error 2
retries 272 | failed429 0 | 502 0 | tracebacks 0
```

- **429/502 = 0**（272 次重试全成功）
- `mine_dfs` 565 次、零进展 **3.9%**；`depth_reset` **10 次**（`ds_1` 3、`nju_guitest` 3、`glm_5` 2、`glm_2`/`ds_2` 各 1）
- `staircase`：completed 162 / **time_budget 146** / `dig_cell_avoided` 64 / fluid 56 / cave 43
  - 146 次 time_budget 中 **143 次 `timeout_path=dig`**、**64 次触发"木镐/镐等级"新文案**、`steps_done` 中位 **1**（请求中位 20）
  - → **BUG-041 的命名+快速失败已生效，但缺"软方块方向优先"，仍 1 步/次**
- `unstick` 170 次：中位 **4.9s**、p90 17.5s、max **45s**（无 >60s 慢调用）
- **新 P1（BUG-044）**：3 集因 `MC API 网络请求异常（ReadTimeout）` 整集报废（`ds_4` EP11/12、`hy4_2` EP15）

### hy4 审计轨 `hy4_audit_r26`（进行中）

```
58 集：answered 37 / goal_satisfied 6 / timeout 6 / limit 7 / runtime_error 2
retries 4 | 429 0 | 502 1
```

- 429 仍为 **0**；1 个 502 + 1 个 MC API ReadTimeout

## 4bj. hy4 审计轨 r26 终验（03:45，4/4）

```
retries 4 | 429 0 | 502 1 | tracebacks 0
门禁：仅 deaths 54 FAIL（>25）；cooldown 191s、mine 0.86、zero-slice 1.7%、over-cap 0
```

| bot | 分数 | 集数 | 死亡 |
|---|---|---|---|
| `hy4_6_nether_bastion` | **210** | 20 | **20（每集 1 次）** |
| `hy4_9_deep_water` | **120** | 35 | 3 |
| `hy4_5_brewing_alchemy` | 105 | 19 | 2 |
| `hy4_2_iron` | 100 | 18 | 13 |

- **0 次 429**（4 路 + rpm=6 再次验证安全）
- `in_lava` incident **45 次**、`fresh_hurt` 89；死因 `mob 13 / lava 11 / arrow 4 / fall 3`
- 堡垒场景每集必死 1 次，但分数仍达 210（复活后继续）→ 生存质量是下一步重点
- `depth_reset` 仅 1 次；`staircase` completed 29 / pillar_fallback 28 / time_budget 19
- `unstick` 60 次：中位 3.7s、max 37.1s（无 >60s）

## 4bk. 过夜审计 04:30 抽检

### 主轨 `overnight_12bot`（3h07m）

```
429 集：answered 310 / goal 38 / timeout 42 / limit 23 / runtime_error 9 / aborted 4
retries 612 | failed429 6 | 502 0 | tracebacks 0
```

- 9 个 runtime_error：**6× Qwen 429 `rate_limit_error`**（`glm_1` 3、`glm_2` 2、`ds_4` 2…）+ **3× MC API ReadTimeout**
- 新 429 body：`{"type":"rate_limit_error","message":"Rate limit exceeded. Please try again."}` → 同样不被 `_is_concurrency_limit_429` 识别（BUG-042 需扩关键词）
- 612 次 retry 绝大多数成功；6 集报废
- aborted 4：`ds_2` 3（shiny_gear）+ `ds_1` EP19（shiny_gear，BUG-043 扩散到第 2 路）

### hy4 审计轨

- `hy4_audit_r27`（进行中）：26 集、**0 retries / 0 429 / 0 502**
- r25/r26 已收官：均 **0 次 429**

## 4bl. 过夜审计 05:16 抽检（主轨 3h54m，即将收官）

```
557 集：answered 410 / goal 46 / timeout 52 / limit 30 / runtime_error 12 / aborted 4
retries 835 | failed429 9 | 502 0 | tracebacks 0
```

- 12 个 runtime_error：**9× 429 `rate_limit_error`**（`glm_1` 4、`glm_2`/`glm_4`/`glm_5`/`ds_4` 各 2）+ **3× MC API ReadTimeout**
- `mine_dfs` **1025 次**、零进展 37（**3.6%**）；`depth_reset` **16 次**（`nju_guitest` 4、`ds_1` 4、`ds_2` 2、`glm_5` 2…）
- `staircase`：**time_budget 351** > completed 292；`pillar_fallback` 137
  - 351 次中 **342 次 `timeout_path=dig`**、**135 次触发低镐文案**、`steps_done` 中位 **1**（请求中位 15）
  - 分布：`nju_3` 71、`ds_2` 59、`glm_3` 35、`glm_1` 33、`glm_4` 32
  - → **BUG-041 的软方块方向优先是当前最大 churn 来源**
- `unstick` 406 次：中位 5.4s、p90 25.2s、max **55.1s**（仍无 >60s）

### hy4 审计轨 `hy4_audit_r27`（进行中）

56 集、**0 retries / 0 429 / 0 502**、1 次 aborted；三波审计轨累计 0 次 429。

## 4bm. 过夜主轨终验 `overnight_12bot`（12/12，4 小时）

| bot | 分数 | 集数 | over-cap |
|---|---|---|---|
| `glm_2_nether_bastion` | **270** | 30 | netherrack 551 |
| `ds_4_nether_fortress` | **210** | 43 | — |
| `glm_1_nether_fortress` | 205 | 34 | — |
| `glm_4_brewing_alchemy` | 195 | 56 | deepslate 247 |
| `glm_5_midgame` | 185 | 72 | — |
| `nju_guitest_01_nether` | 180 | 75 | — |
| `glm_3_village_trade` | 155 | 61 | cobble 300 |
| `nju_3_village_trade` | 150 | 56 | deepslate 320 + cobble 256 |
| `ds_3_enchant_anvil` | 150 | 41 | cobble 320 |
| `ds_2_cave_combat` | 120 | 58 | cobble 382 + deepslate 301 |
| `ds_1_bare` | 55 | 35 | cobble 571 |
| `nju_movetest_bare` | 25 | 21 | cobble 256 |

- 总分 **1900**；最高 `glm_2` 270、`ds_4` 210（43 集）
- 门禁 4 FAIL：`deaths 144`、`cooldown 1417.8s`、`runtime_error 12`、`over-cap 8`
- **429：9 次失败 / 879 次重试（1.0%）**，全部 `rate_limit_error`；无 502、无 Traceback
- `mine_dfs` 1025 次、零进展 **3.6%**；`depth_reset` **16 次**
- `staircase`：**time_budget 351**（342 次 dig、135 次低镐文案，steps_done 中位 1）
- `unstick` 406 次：中位 5.4s、p90 25.2s、max 55.1s
- **死亡 110（notable）**：`ds_4` **41**、`ds_3` 23、`ds_1` 13；`mob 38 / lava 22 / arrow 15 / hot_floor 13`

### BUG-029 长程未达标（根因已定位）

`pre_over_cap` 只让函数"进入"，但 `plan_garbage_drops` 的 `need = max(0, min_free - empty_before)` 在有 ≥3 空槽时为 0 → **一块都不丢**。修复：超 cap 时把 `need` 提升到"清到 cap"所需槽数，或加 `enforce_caps=True`。

## 4bn. 过夜审计 06:17 抽检

### 主轨第二波 `overnight_12bot_part2`（进行中，约 20 分钟）

```
86 集：answered 60 / goal 14 / limit 6 / timeout 4 / paused 2 / runtime_error 2
retries 170 | failed429 2 | 502 0 | tracebacks 0
```

- 2 个 runtime_error 均为 `rate_limit_error` 429（`glm_4`、`glm_5`）
- 与第一波相同的模式：重试大多成功，少数仍报废

### hy4 审计轨 `hy4_audit_r27`（**3 小时**长审计，进行中）

```
95 集：answered 71 / goal 7 / timeout 14 / limit 2 / paused 1 / aborted 1
retries 0 | 429 0 | 502 0 | tracebacks 0
elapsed 8509/10800s（约 2h22m）
```

- **rpm=6、4 路，连续 2h22m 零 retry / 零 429** → 这是比 20 分钟抽检更强的证据
- 1 次 aborted（需后续确认是否 BUG-043）

## 4bo. hy4 审计轨 r27 终验（06:51，3 小时）

```
126 集：answered 93 / timeout 22 / goal 7 / limit 2 / paused 1 / runtime_error 1 / aborted 1
retries 0 | 429 0 | 502 0 | tracebacks 0
```

| bot | 分数 | 集数 |
|---|---|---|
| `hy4_6_nether_bastion` | **195** | 50 |
| `hy4_5_brewing_alchemy` | 165 | 31 |
| `hy4_2_iron` | 100 | 27 |
| `hy4_9_deep_water` | 60 | 19 |

- **3 小时 × 4 路 × rpm=6：0 retries / 0 429 / 0 502** → 429 与模型无关，纯并发问题
- 门禁仅 `over-cap 1` FAIL（BUG-029）；deaths 22、cooldown 128s、mine 0.86、zero-slice 2.2%
- 1 次 runtime_error = `max_tokens` 截断（P2-33）
- 1 次 aborted = `lost_iron_pickaxe`（`hy4_9` EP7，BUG-043 已扩散到 3 路）

## 4bp. 06:52 抽检：Qwen 429 突发窗口的精确根因

### 主轨第二波 `overnight_12bot_part2`（进行中 ~80 分钟）

```
171 集：answered 108 / goal 18 / limit 12 / timeout 11 / runtime_error 19 / aborted 1 / paused 2
```

- runtime_error 19 = **17× Qwen 429 `rate_limit_error`** + 1× MC API ReadTimeout + 1× `[not_connected]`
- **硬失败率对照**：

  | 波次 | 请求 | 重试 | 重试率 | 429 硬失败 | 硬失败率 |
  |---|---|---|---|---|---|
  | `overnight_12bot` | 10100 | 879 | 8.0% | 9 | **0.08%** |
  | `overnight_12bot_part2` | 3175 | 313 | 9.0% | 17 | **0.49%** |

  重试率几乎相同 → **不是流量总量问题，是突发窗口**。第一波 9 次散布在 19:33–20:53（UTC）；第二波 **15/17 集中在 22:23–22:33（10 分钟）**，此时主轨 12 路 + hy4 审计轨 4 路 = **16 并发**。

- **每次硬失败的调用序列完全一致**（5 个文件逐一核对）：
  ```
  model.request
  → http_retry(attempt=1, wait=15.0, concurrency_limit=false)
  → http_retry(attempt=2, wait=15.0, concurrency_limit=false)
  → model.failed  Qwen API 返回 HTTP 429 {"type":"rate_limit_error"}
  → run.finished  runtime_error
  ```
  **2 次重试、共 30 秒退避后放弃**。

- **根因（`model.py:652-653`）**：
  ```python
  standard_max_attempts = 3
  concurrency_max_attempts = 8
  ```
  `_is_concurrency_limit_429` 只认 `concurrency`/`simultaneous`；Qwen 的 `rate_limit_error` 不匹配 → 走 standard 分支（3 次尝试、`2*(attempt+1)` 秒）；并发分支才有 8 次 + `6/10/15/20/22/25/28` 秒（≈126s）。**Qwen 限流窗口 ≥60s，30s 必然不够。**

- **最小修复**：① 关键词增补 `rate_limit` / `rate limit` / `overloaded` / `try again later` / `server_error`；② 对**任意 429** 直接使用 8 次尝试的预算（429 语义就是"稍后重试"）。

### hy4 审计轨 `hy4_audit_r28`（刚起）

4 路、23 次请求、**0 retry / 0 429**。前三波（r25/r26/r27）累计 **0 次 429**。

### 新增 P2

- **P2-34**：`ds_4` EP9 连续两次 `mob_attack` 死亡后以 `[not_connected]` 收尾 → `runtime_error`，白烧 28 次请求。`not_connected` 在 `agent.py:206` 属 transient，但收尾阶段抛出的未被识别。
- **P2-33 量化**：过夜双轨 **65 次** `finish_reason=length`，**64 次被 `length_recovered` 救回**，仅 1 次报废 → 优先级维持 P2。
- **BUG-043 扩散**：`nju_movetest_01_bare` EP7（shiny_gear）也 `lost_iron_pickaxe` → 已 4 路。

## 4bq. 07:37 抽检：429 的第二个根因 —— 无抖动惊群（thundering herd）

### 天然 A/B 对照（同一波次、同一时段）

| provider | retry 事件 | `concurrency_limit` | 硬失败 429 |
|---|---|---|---|
| NJU `nju_*` | 16 | **true** → 8 次尝试 + 6~28s | **0** |
| Qwen `glm_*` | 527 | **false** → 3 次尝试 + 2~4s | **26** |
| DeepSeek `ds_*` | 0 | — | **0** |

同样压力下，**被识别成并发限流的 NJU 一次没死，未被识别的 Qwen 死了 26 次** → 直接证明"给任意 429 用 8 次预算"就能消除这批报废。

### 惊群证据

- 545 次 retry 中 **259 次 `wait_s=15.0`**（服务端 `Retry-After: 15` 被原样采用，无抖动）
- **51 个惊群簇**：≥3 个不同 bot 在 2 秒内同时重试
  ```
  21:50:40  glm_1 / glm_3 / glm_5 同秒
  21:50:56  glm_1 / glm_3 / glm_5 同秒（+16s，正好是 15s 退避的整点）
  21:53:55  glm_1 / glm_4 / glm_5 同秒
  ```
- 对照：`overnight_12bot` 77 簇；hy4 4 路波次 **0 簇**
- **机制**：所有 bot 在同一秒醒来 → 再次撞限流 → 再次同秒重试，形成自持碰撞循环。修复：对 `Retry-After` 结果加 **0.5~1.5× 抖动**（或每 bot 固定随机相位）。

### 主轨 `overnight_12bot_part2`（2h05m / 3.9h）

```
269 集：answered 198 / goal 24 / limit 22 / timeout 22 / runtime_error 29 / aborted 1 / paused 4 / network_error 1
retries 545 | 429 26 | 502 0 | tracebacks 0
```

- 26 次 429 硬失败**全部集中在 `glm_*`**：`glm_1` 9、`glm_5` 7、`glm_2` 5、`glm_4` 3、`glm_3` 2
- 除 22:23–22:33 的 10 分钟高峰外，22:55 / 23:07 / 23:15 / 23:17 仍有小簇（同一批 glm bot）

### 新 P2

- **P2-35** `network_error` 状态未被 `_is_transient_llm_error` 识别（`:887` 只接受 `runtime_error`）→ 纯传输抖动（`[not_loaded] chunk not loaded`）会计入 strike。实测 `ds_1` EP13 `sleep_in_bed`。

### hy4 审计轨 `hy4_audit_r28`（2.5h 波，32 分钟）

23 集、**0 retry / 0 429**、1 aborted。四波审计轨累计 **0 次 429**。

## 4br. 08:23 抽检：Qwen 通道饱和，18% 墙钟耗在退避（新 P1：BUG-045）

### 主轨 `overnight_12bot_part2`（2h50m / 3.9h）

```
429 集：answered 298 / goal 26 / limit 27 / timeout 29 / runtime 53 / aborted 1 / paused 4 / network_error 1
retries 846 | 429 49 | 502 0 | tracebacks 0
```

- 53 个 runtime_error = **49× Qwen 429** + 3× MC API ReadTimeout + 1× `[not_connected]`
- 429 已从"10 分钟突发"演变为**持续劣化**：23:46–23:59（14 次）、00:04–00:22（12 次）连续出现

### 关键量化：按通道分组的 retry 率

| bot | 集 | 请求 | retry | retry% | 硬失败 | 退避墙钟占比 |
|---|---|---|---|---|---|---|
| `glm_1_nether_fortress` | 63 | 1018 | 213 | **17.3%** | 15 | **24.4%** |
| `glm_5_midgame` | 79 | 950 | 201 | **17.5%** | 15 | **22.7%** |
| `glm_2_nether_bastion` | 24 | 702 | 169 | 19.4% | 9 | 18.4% |
| `glm_4_brewing_alchemy` | 34 | 516 | 135 | 20.7% | 8 | 14.7% |
| `glm_3_village_trade` | 19 | 313 | 105 | 25.1% | 2 | 10.5% |
| `nju_*`（3 路） | 81 | 1475 | 22 | **1.2~2.1%** | **0** | ≤0.5% |
| `ds_*`（4 路） | 139 | 2627 | 1 | **0~0.2%** | **0** | 0% |

- **5 路 Qwen retry 率成比例一致（17~25%）** → 共享 key/通道配额饱和，不是单 bot 行为异常
- **Qwen 合计 18.1% 墙钟（9251s / 51036s）在 429 退避里睡觉** —— 相当于 5 路里约 1 路全程空转
- 对照：同期的 hy4 审计轨（另一通道，4 路 + rpm=6）**0 次 429**

### 新 P1：BUG-045（通道级 RPM 缺失）

`v2_fleet.py launch` 对 Qwen squad 没有按 provider 的 RPM 上限（hy4 已有 `rpm=6` 且实测有效）。建议：Qwen 加 `rpm=6`，或并发从 5 降到 3；按 provider 配置而不是全局 default。

### hy4 审计轨 `hy4_audit_r28`

39 集、**0 retry / 0 429**、1 aborted、10 集 goal_satisfied。

## 4bs. 09:08 抽检：`max_episodes=80` 硬上限让快 bot 提前空转（新 P1：BUG-046）

### 已收官的两路（`status=completed`，但墙钟未满）

| bot | 集数 | 实际墙钟 | 剩余 | 集状态分布 |
|---|---|---|---|---|
| `glm_5_midgame` | **80/80** | **10355s（2h53m）** | **1h02m 空转** | answered 57 / **429 15** / goal 3 / limit 3 / timeout 1 / paused 1 |
| `glm_1_nether_fortress` | **80/80** | **12433s（3h27m）** | 28m 空转 | answered 58 / **429 16** / limit 5 / goal 1 |

- 波次参数（stdout 实证）：`duration_s=14100.0 max_episodes=80`
- `max_episodes` 与 `duration_s` **互相独立**：跑得快的 bot 先撞集数上限，剩下的墙钟全部浪费
- **429 集同样吃配额**：`glm_1` 80 集里 **16 集是 429 报废**（20%），`glm_5` 15/80（19%）
- 叠加效应：Qwen 通道同时损失 **18% 墙钟（退避）+ 19~20% 集数槽位**
- **评测口径副作用**：快 bot 被截断、慢 bot 跑满 → 跨 bot 分数不可比（`glm_5` 名义 4h 实际 2h53m）

### 当前战况（12 路，10 路在跑）

| bot | 集 | 分数 |
|---|---|---|
| `glm_1_nether_fortress` | 80（已收） | **240** |
| `glm_2_nether_bastion` | 30 | 270 |
| `glm_5_midgame` | 80（已收） | 180 |
| `ds_4_nether_fortress` | 53 | 210 |
| `nju_guitest_01_nether` | 36 | 185 |
| `nju_3_village_trade` | 44 | 150 |
| `ds_2_cave_combat` | 56 | 150 |
| `glm_4_brewing_alchemy` | 43 | 145 |
| `ds_3_enchant_anvil` | 43 | 135 |
| `ds_1_bare` | 34 | 105 |
| `glm_3_village_trade` | 30 | 90 |
| `nju_movetest_01_bare` | 28 | 70 |

### hy4 审计轨 `hy4_audit_r28`（2.7h / 2.5h，1/4 收官）

150 集、**0 retry / 0 429 / 0 502**、1 aborted；`hy4_2` 135、`hy4_6` 210、`hy4_9` 135、`hy4_5` 已收。
注意：r28 集数（150）远高于 r27（126 / 3h），说明短集比例上升，需在收官时核对分数质量。

## 4bt. hy4 审计轨 r28 终验（09:22，4/4，2.5h 波）

| bot | 分数 | 集数 | 死亡 |
|---|---|---|---|
| `hy4_6_nether_bastion` | **210** | 24 | **17** |
| `hy4_5_brewing_alchemy` | 165 | 50 | 4 |
| `hy4_2_iron` | 135 | 50 | 5 |
| `hy4_9_deep_water` | 135 | 43 | 4 |

- **总分 645**（r27 3h 为 520；r28 只跑 2.5h 却更高）
- **0 retry / 0 429 / 0 502 / 0 Traceback**（167 集，仅 1 次 retry）→ **四波审计轨累计 0 次 429**
- 门禁 2 项 FAIL：
  - `deaths 43`（>25）：`hy4_6` **17 次**（岩浆 7 + mob 8），堡垒场景仍是生存黑洞
  - `cancel cooldown 200.6s`（>200，**仅超 0.6s**，边缘）
- `mine_dfs` 269 次、零进展 **4.8%**；`depth_reset` **3 次**
- `staircase`：**pillar_fallback 73 > time_budget 50 > completed 30** —— 本波"支柱上浮"成为主要失败路径（堡垒/深水场景）
- `unstick` 101 次：中位 4.3s、max 41.6s
- **BUG-043 第 5 路**：`hy4_9` EP3（shiny_gear）`lost_iron_pickaxe`

### 四波审计轨汇总（hy4，4 路 + rpm=6）

| 波次 | 时长 | 集数 | retry | 429 | 502 | 总分 |
|---|---|---|---|---|---|---|
| r25 | 20min | 17 | 0 | 0 | 0 | 335 |
| r26 | 20min | 92 | 4 | 0 | 1 | 535 |
| r27 | 3h | 126 | 0 | 0 | 0 | 520 |
| r28 | 2.5h | 167 | 1 | 0 | 0 | 645 |

→ **约 6.2 小时 / 402 集 / 0 次 429**：hy4 的 4 路 + rpm=6 配置可以定稿。

## 4bu. 主轨第二波 `overnight_12bot_part2` 终验（12/12，09:28）

### 12 路对照（同配置、同场景、连续两波）

| bot | wave1 | part2 | Δ | over-cap (part2) |
|---|---|---|---|---|
| `glm_2_nether_bastion` | 270 | **270** | 0 | — |
| `glm_1_nether_fortress` | 205 | **240** | **+35** | — |
| `ds_4_nether_fortress` | 210 | 210 | 0 | — |
| `nju_guitest_01_nether` | 180 | **200** | +20 | — |
| `glm_5_midgame` | 185 | 180 | -5 | — |
| `nju_3_village_trade` | 150 | 150 | 0 | — |
| `ds_2_cave_combat` | 120 | **150** | +30 | — |
| `glm_4_brewing_alchemy` | 195 | 145 | **-50** | deepslate 333 + cobble 225 |
| `ds_3_enchant_anvil` | 150 | 135 | -15 | — |
| `ds_1_bare` | 55 | **105** | **+50** | — |
| `glm_3_village_trade` | 155 | 90 | **-65** | — |
| `nju_movetest_01_bare` | 25 | **70** | **+45** | cobble 273 |
| **合计** | **1900** | **1945** | **+45** | |

- 总分 1900 → **1945**（+2.4%），集数 582 → 596
- **退步全在 Qwen**：`glm_3` -65、`glm_4` -50 —— 正是 429 硬失败最多的两路（2 次 / 8 次）加上 `glm_4` 背包超限
- **进步全在弱项**：`ds_1` +50、`nju_movetest` +45、`glm_1` +35、`ds_2` +30（裸开/低分 bot 提升明显）

### 门禁

| 项 | 值 | 判定 |
|---|---|---|
| startup crash / stick craft / thread exc | 0 | PASS |
| no_support | 5 | PASS（边缘） |
| mine dur/budget med | 0.91 | PASS |
| mine zero-slice | 3.0% | PASS |
| trade / brew | 2 / 4 | PASS |
| **deaths (incidents)** | **207** | **FAIL** |
| **cancel cooldown** | **1663.2s** | **FAIL** |
| **runtime_error** | **58** | **FAIL** |
| **over-cap bots** | **2** | **FAIL** |

### 关键指标（wave1 → part2）

| 指标 | wave1 | part2 |
|---|---|---|
| `mine_dfs` 次数 | 1025 | 1137 |
| 零进展占比 | 3.6% | **3.0%** |
| `depth_reset` | 16 | 17 |
| `staircase` time_budget | 351 | **241** |
| `steps_done` 中位 | 1 | **2** |
| `pillar_fallback` | 137 | **140** |
| `unstick` 中位 / p90 / max | 5.4 / 25.2 / 55.1s | 5.1 / 17.6 / **404.4s** |
| notable 死亡 | 110 | **153** |
| 429 硬失败 | 9 | **52** |
| retry 总数 | 879 | 931 |

- **死亡恶化**：`ds_4` 单路 **53 次**（wave1 41）、`glm_1` 18、`glm_2` 16；死因 `mob 49 / lava 25 / arrow 21 / hot_floor 18`
- **`staircase:time_budget` 351→241**（-31%），`steps_done` 中位 1→2 → BUG-041 的快速失败在持续生效
- **`unstick` max 404.4s**：`ds_1` 一次 unstick 被集时限中止，但**实际超预算 104s**

### 新 P1：BUG-047（阻塞调用不受预算约束）

- `ds_1` 一次 `mc_skill_unstick` **404.4s** → 结果 `工具执行中止`，随后 `运行总时限已到（300.0s）`
- **33 次** `mc_move_to` 耗时 **102~103s** 后返回 `busy`（预检 `/status.busy` 为空但租约仍被占）
- 根因：`bot.py:34 timeout=45.0` + `call()` 重试一次 ≈ 90s/次；控制类工具沿用默认超时；episode 时限只在调用之间检查
- 修复：控制类工具传 `timeout_s=min(15, remaining)`；`move_to` 遇 `busy` 先 `/stop` 再试一次，仍 busy 立即返回；`unstick` 总时长上限 30s

### 两波主轨合计

```
≈95 bot-小时（12 路 × 4h + 12 路 × 3.9h），1178 集，总分 3845
429 硬失败 61（其中 part2 52）| 502 0 | Traceback 0
```

## 4bv. 过夜审计总结（01:08 → 10:13，6 个波次全部收官）

### 波次总览

| 波次 | 配置 | 时长 | 集数 | retry | 429 | 502 | Traceback | 总分 |
|---|---|---|---|---|---|---|---|---|
| `overnight_12bot` | 12 路 nju/glm/ds | 4h | 582 | 879 | 9 | 0 | 0 | 1900 |
| `overnight_12bot_part2` | 12 路 nju/glm/ds | 3.9h | 596 | 931 | 52 | 0 | 0 | 1945 |
| `hy4_audit_r25` | 4 路 hy4, rpm=6 | 20min | 17 | 0 | 0 | 0 | 0 | 335 |
| `hy4_audit_r26` | 4 路 hy4, rpm=6 | 20min | 92 | 4 | 0 | 1 | 0 | 535 |
| `hy4_audit_r27` | 4 路 hy4, rpm=6 | 3h | 126 | 0 | 0 | 0 | 0 | 520 |
| `hy4_audit_r28` | 4 路 hy4, rpm=6 | 2.5h | 167 | 1 | 0 | 0 | 0 | 645 |

**规模**：主轨 ≈95 bot-小时 / 1178 集；hy4 审计轨 ≈6.2h / 402 集。**全程 0 次 Traceback、0 次 502（主轨）**。

### 通道级结论（本次审计最重要的产出）

| 通道 | retry 率 | 429 硬失败 | 退避墙钟占比 | 结论 |
|---|---|---|---|---|
| Qwen（`glm_*`，5 路） | **17~25%** | **52** | **18.1%**（`glm_1` 24.4%） | **配额饱和，是唯一的问题通道** |
| NJU（`nju_*`，3 路） | 1.2~2.1% | 0 | ≤0.5% | 健康（并发 429 走 8 次预算，从不报废） |
| DeepSeek（`ds_*`，4 路） | 0~0.2% | 0 | 0% | 最健康 |
| hy4（4 路 + rpm=6） | 0~0.24% | **0** | 0% | **6.2h/402 集零 429，可定稿为默认** |

→ **429 与模型无关，与"并发数 + 通道配额 + 重试策略"有关**。同一时段同一机器：NJU 3 路 1.5%、Qwen 5 路 19%、hy4 4 路 0%。

### 待修 P1 清单（按性价比排序）

| # | 票 | 一句话 | 关键证据 | 预期收益 |
|---|---|---|---|---|
| 1 | **BUG-042** | `_is_concurrency_limit_429` 关键词太窄；Qwen `rate_limit_error` 只给 3 次尝试/30s | 天然 A/B：NJU 16 次并发 429 全存活，Qwen 527 次普通 429 死 52 | 直接消除 52 集报废 |
| 2 | **BUG-042b** | `Retry-After` 无抖动 → 惊群 | 51 个"≥3 bot 2 秒内同重试"簇；259/931 次 `wait=15.0` | 降低碰撞率 |
| 3 | **BUG-045** | Qwen squad 无按 provider 的 RPM 上限 | 5 路 17~25% retry、18.1% 墙钟 | 回收约 1/5 墙钟 |
| 4 | **BUG-046** | `max_episodes=80` 与 `duration_s` 独立，快 bot 提前空转 | `glm_5` 80 集只跑 2h53m/3h55m，空转 1h02m | 每波多跑 1 路·小时 |
| 5 | **BUG-047** | 阻塞工具调用不受预算约束 | unstick 404s（超预算 104s）；33 次 move_to 白等 102s | 每集多出 1~2 次有效请求 |
| 6 | **BUG-043** | `lost_iron_pickaxe` 中止后不冷却、重复选中 | 5 路（`ds_2`×3、`ds_1`、`hy4_9`、`nju_movetest`） | 每波回收 3~5 集 |
| 7 | **BUG-044** | MC API ReadTimeout 打死整集 | 6 集（主轨 5 + hy4 1） | 每波回收 2~3 集 |
| 8 | **BUG-029** | 长程 `plan_garbage_drops` 的 `need=0` | 8/12 超限（`ds_1` cobble 571、`glm_2` netherrack 551） | 修复背包超限门禁 |
| 9 | **BUG-030** | `depth_reset` 行为未修 | 16/17 次（`ds_1` 6、`nju_3` 5） | 减少重复下潜 |
| 10 | **BUG-037/041** | 缺"软方块方向优先" | 241~351 次 time_budget，`steps_done` 中位 1~2 | 挖矿效率 |
| 11 | **BUG-026/040** | 下界堡垒生存 | 死亡 110→153，`ds_4` 53 次、`hy4_6` 17 次；`hot_floor` 18 | 每波多活 30~50 次 |

### 已证伪的判断（避免重复劳动）

- ❌ "hy4 过载是上下文膨胀导致" → 失败集中位 `actor_requests=3`，按分钟统计是通道级饱和
- ❌ "缩小历史能缓解 429" → 只有降并发 / 加 RPM / 改重试策略有效
- ❌ "长任务请求频率低所以不 429" → r27/r28 长波同样 0 次，说明是并发而非频率
- ❌ "`max_tokens` 截断是主要报废源" → 65 次截断 64 次被救回，仅 1 次报废
- ❌ "`unstick` 有 55s 慢调用" → 55s 是 p-max 正常值；真正的慢调用是 404s 的**被中止调用**（BUG-047）

### 给 Integrator 的落地顺序建议

1. `_is_concurrency_limit_429` 关键词 + 任意 429 用 8 次预算 + 退避加抖动（一个 PR，收益最大）
2. `max_episodes` 改为按 duration 推导、transient 集不占配额
3. 控制类工具短超时（15s）+ `move_to` busy 快速失败
4. `lost_iron_pickaxe` 前置检查 + abort 冷却
5. MC API 只读请求短退避 + `transient_api_error` 暂停重排

## 4bw. 10:59 深挖：`staircase` 成功率与 `pillar_fallback` 自锁（P2-36）

### `staircase` 全量分布（`overnight_12bot_part2`，1180 次调用）

| stop_reason | 次数 | 占比 |
|---|---|---|
| `time_budget` | 241 | 20.4% |
| `completed` | **203** | **17.2%** |
| `pillar_fallback` | 140 | 11.9% |
| `None`（异常返回） | 123 | 10.4% |
| `dig_cell_avoided` | 101 | 8.6% |
| `fluid_or_bedrock` | 86 | 7.3% |
| `dig_failed` | 68 | 5.8% |
| `cave_or_drop_ahead` | 68 | 5.8% |

→ **成功率仅 17.2%**，与 BUG-037 的判断一致：阶梯下潜仍是最大 churn。

### `pillar_fallback` 是"安全垫高"，但会自锁

- 语义（`library.py:6634-6639`）：目标台阶四周没有可放置面 → `_dig_pillar_up(steps=1)` → `stop_reason=pillar_fallback`，返回 **`ok=True, requires_replan=False`** + 提示"从新高度重试"
- 模型照做 → 再次 `no_place_face` → 再垫高 1 格 …… 形成**同工具连续失败循环**

| 波次 | pillar 次数 | 连续 ≥3 段 | 累计耗时 | 最长一段 |
|---|---|---|---|---|
| `overnight_12bot_part2` | 140 | 14 | **723s** | `nju_guitest_01_nether` **29 连击**（188s） |
| `hy4_audit_r28` | 73 | 10 | **437s** | `hy4_6_nether_bastion` 10 连击（134s） |

- 已核验：那 29 次**之间没有任何其他工具调用**（窗口内工具序列全为 `mc_skill_staircase`）→ 真·自锁，不是交替重试
- 单次 pillar 仅 3~4s，但每段包含 N 次 **LLM 往返**，所以 29 连击 ≈ 3 分钟
- 修复（约 5 行）：连续 ≥2 次返回 `requires_replan=True` 并建议 `unstick`/水平换向；≥3 次给 target 加 120s 冷却

## 4bx. 11:45 深挖：`void_geofence_blocked` 是隐藏的大 churn（新 P1：BUG-048）

### 先排除一个假疑点

`staircase` 的 123 次 `stop_reason=None` **不是 bug**，是三段正常错误路径：
- `void_geofence_blocked` 66
- `preempted_by_reflex` 36（`cover_or_flee` 20 / `creeper_evade` 7 / `counter_killed` 3 / `cover_pocket` 3 / `flee` 2 / `shield_rush_ranged` 1）
- `repeated_identical_failure_blocked` 21

### 真问题：`void_geofence_blocked` 不触发重复失败保护

| 波次 | bot | 被拦次数 | 不同坐标数 | 距离范围 | safe_radius |
|---|---|---|---|---|---|
| `overnight_12bot` | `nju_guitest_01_nether` | **154** | **118** | 554.5m ~ **12927.4m** | 212.0m |
| `overnight_12bot_part2` | `glm_4_brewing_alchemy` | **129** | **110** | 212.2m ~ 254.4m | 212.0m |
| `part2` 其余 5 路 | — | 17 | — | — | — |
| `hy4_audit_r28` | `hy4_9_deep_water` | 5 | — | — | — |

**两种形态**：

1. **"差一点"型（`glm_4`）**：目标 212.2~254.4m，只超出 0~42m —— 意图合理（去挖看见的矿），但围栏只拒绝不引导。`glm_4` 本波 **145 分（-50 vs wave1）**，129 次被拦 ≈ 8~12 分钟。
2. **"离谱坐标"型（`nju_guitest`）**：目标 554m~**12.9km**（最远 12927m），同一坐标重复 14 次 —— 幻觉坐标，工具层未识别"距离明显异常"。

**为什么保护失效**：`repeated_identical_failure_blocked` 按"完全相同的参数"匹配，模型每次换一个差 0.5~5 格的坐标就绕过了（`glm_4` 129 次里 110 个不同坐标）。

**修复**：
1. `distance ≤ safe_radius × 1.25` → **自动钳制**到边界内继续执行（意图本来是对的）
2. `distance > safe_radius × 3` → 立即 `requires_replan=True` + "坐标疑似错误，请用 mc_status 的 pos 重新定位"
3. 每 episode 累计 ≥3 次 → 强制 `requires_replan=True` + target 冷却 120s

**验收**：单 bot 单波 ≤5 次；`glm_4` 类场景分数回到 ≥195。

## 4by. 逐轨迹卡点全量审计（6 波 / 40 条 bot 轨迹 / 1,581 个 episode）

> 审计口径：`overnight_12bot`（12 路 ×4h）+ `overnight_12bot_part2`（12 路 ×3.9h）+ `hy4_audit_r25/r26/r27/r28`（4 路 ×20min/3h/2.5h）。逐 episode 读 `report_file` 的 `events`，逐 bot 汇总最后工具 / stop_reason / guardian 动作 / 死亡。

### 0. 总体结论：**91.1% 的 episode 得分为 0**

| 状态 | 集数 | 占比 |
|---|---|---|
| `answered` | 1,133 | 71.7% |
| `timeout` | 168 | 10.6% |
| `goal_satisfied` | 111 | **7.0%** |
| `limit_reached` | 74 | 4.7% |
| `runtime_error` | 73 | 4.6% |
| `paused_for_world_job` | 13 | 0.8% |
| `aborted` | 7 | 0.4% |
| `network_error` / `paused` | 2 | 0.1% |

**零分集 1,440 / 1,581 = 91.1%**（分波：92.3% / 91.4% / 70.6% / 88.0% / 90.6% / 89.8%）。
→ `answered` ≠ 成功：它是"模型自行收尾/判定不可达"，绝大多数同样 0 分。**真正的成交率只有 7.0%。**

### 1. 逐波次逐 bot 卡点表（每个 bot 的 top 卡点）

| 波次 | bot | 集 | 成交 | 主要卡点 |
|---|---|---|---|---|
| overnight | `ds_1_bare` | 35 | 26 | mine_slice×3、limit:move_to、limit:find_blocks |
| overnight | `ds_2_cave_combat` | 58 | 44 | mine_slice×5、**lost_pickaxe×3**、limit:flee×2 |
| overnight | `ds_3_enchant_anvil` | 41 | 30 | limit:move_to×2、limit:get_entities×2、staircase_budget×2 |
| overnight | `ds_4_nether_fortress` | 43 | 35 | limit:move_to×3、**MCAPI_timeout×2**、limit:get_entities |
| overnight | `glm_1_nether_fortress` | 34 | 28 | **429×3**、mine_slice×2、MCAPI_timeout |
| overnight | `glm_2_nether_bastion` | 30 | 25 | 429×2、limit:dig、limit:mine_dfs |
| overnight | `glm_3_village_trade` | 61 | 49 | mine_slice×3、**limit:flee×3**、timeout:staircase×2 |
| overnight | `glm_4_brewing_alchemy` | 56 | 43 | staircase_budget×2、**climb_livelock×2**、429×2 |
| overnight | `glm_5_midgame` | 72 | 56 | **staircase_budget×4**、timeout:staircase×2、429×2 |
| overnight | `nju_guitest_01_nether` | 75 | 74 | unstick_loop×1（几乎全成交） |
| overnight | `nju_movetest_01_bare` | 21 | 8 | **craft_hang×7（实为 escape_water 499s）**、timeout:get_entities×3 |
| overnight | `nju_3_village_trade` | 56 | 50 | mine_slice×2、timeout:staircase×2 |
| part2 | `ds_1_bare` | 40 | 28 | mine_slice×3、limit:flee、limit:attack |
| part2 | `ds_2_cave_combat` | 64 | 59 | timeout:mine_dfs、mine_slice、limit:find_blocks |
| part2 | `ds_3_enchant_anvil` | 45 | 32 | mine_slice×3、limit:flee×2、climb_livelock×2 |
| part2 | `ds_4_nether_fortress` | 57 | 46 | **MCAPI_timeout×3**、limit:move_to×2、limit:mine_blocks×2 |
| part2 | `glm_1_nether_fortress` | 80 | 59 | **429×16**、limit:craft_gear×2 |
| part2 | `glm_2_nether_bastion` | 33 | 22 | **429×9**、limit:unstick、timeout:move_to |
| part2 | `glm_3_village_trade` | 32 | 17 | 429×3、mine_slice×2、timeout:staircase×2 |
| part2 | `glm_4_brewing_alchemy` | 45 | 30 | **429×9**、mine_slice×3、staircase_budget×2 |
| part2 | `glm_5_midgame` | 80 | 60 | **429×15**、limit:move_to×2 |
| part2 | `nju_guitest_01_nether` | 41 | 26 | mine_slice×4、staircase_budget×2 |
| part2 | `nju_movetest_01_bare` | 30 | 20 | **climb_livelock×3**、staircase_budget×3、lost_pickaxe |
| part2 | `nju_3_village_trade` | 49 | 42 | staircase_budget×3、mine_slice×2 |
| r25 | `hy4_2_iron` | 3 | 1 | mine_slice×2 |
| r25 | `hy4_5_brewing_alchemy` | 8 | 6 | timeout:smelt、timeout:mine_dfs |
| r25 | `hy4_6_nether_bastion` | 3 | 2 | timeout:get_entities |
| r25 | `hy4_9_deep_water` | 3 | 1 | mine_slice×2 |
| r26 | `hy4_2_iron` | 18 | 12 | timeout:move_to、limit:smelt、limit:mine_dfs |
| r26 | `hy4_5_brewing_alchemy` | 19 | 15 | mine_slice×2、climb_livelock、limit:smelt |
| r26 | `hy4_6_nether_bastion` | 20 | 15 | limit:find_blocks、limit:move_to、runtime_error |
| r26 | `hy4_9_deep_water` | 35 | 31 | timeout:find_blocks、timeout:staircase、timeout:smelt |
| r27 | `hy4_2_iron` | 27 | 24 | timeout:move_to、mine_slice、timeout:find_blocks |
| r27 | `hy4_5_brewing_alchemy` | 31 | 25 | timeout:move_to、unstick_loop、limit:move_to |
| r27 | `hy4_6_nether_bastion` | 50 | 47 | limit:find_blocks、runtime_error、timeout:find_blocks |
| r27 | `hy4_9_deep_water` | 19 | **4** | **craft_hang×6（escape_water 560s）**、climb_livelock×2 |
| r28 | `hy4_2_iron` | 50 | 46 | mine_slice×2、staircase_budget |
| r28 | `hy4_5_brewing_alchemy` | 50 | 48 | timeout:mine_dfs、limit:get_entities |
| r28 | `hy4_6_nether_bastion` | 24 | 22 | limit:unstick、timeout:staircase |
| r28 | `hy4_9_deep_water` | 43 | 36 | timeout:mine_dfs×2、lost_pickaxe、mine_slice |

### 2. 卡点类别（按影响排序）

#### A. `escape_water` 无界阻塞 —— **P0，19 集整集报废（新票 BUG-056）**
- `nju_movetest_01_bare` **EP11–EP20 连续 10 集**，每集只有 1 次 `craft_gear` 调用，然后 `guardian.action=boundary_escape_water` 跑 **499.0s**，集预算 300s → 整集报废（**占该 bot 48% 集数**）
- `hy4_hy_9_deep_water`（r27）**EP11–EP19 连续 9 集**，每次 **560.6s**
- 全量：557 次调用 / **16,895s = 4.69h**；>30s 113、>60s 35、>120s 24、>300s 22
- 根因：`library.py:4803 escape_water()` 无 deadline（四层嵌套循环），`guardian.py:294` 直接调用不传预算

#### B. `hot_floor`（岩浆块）无反射 —— **P1，2,129 次伤害 / 34 死（新票 BUG-057）**
- `ds_4` **1,232 次**伤害、25 死；`glm_2` 754 次、7 死；`hy4_6` 101 次、2 死
- 典型：`[受伤 health=19 by hot_floor]×10` → HP 20→13.3；`emergency_bunker=unsafe_open_side`、`flee=no_safe_retreat` 双双失败后继续掉血
- 根因：`hot_floor` 在 `NON_COMBAT_HURT_SOURCES` 里**压制了 `fresh_hurt`**，只有 HP≤6 才升级；全程没有"离开该方块"的反射

#### C. `path_not_found` —— **P1，1,137 次 / 446 集（新票 BUG-058）**
- `mc_move_to` 1,099 次；`hy4_6` **237**、`glm_1` **151**、`glm_4` 88
- 典型（`hy4_6` r28 EP1，45 请求 0 分）：在可达中转点 (228,205) 与不可达目标 (232,205) 之间**来回横跳**
- `find_blocks radius=96` 每次都说"有 2 个 ancient_debris"，模型知道位置但到不了，也没有"挖过去"的建议

#### D. `climb_out` 回不到地表 —— **P1，58 集 / 3.6h（新票 BUG-059）**
- `climb_progress` **400 次** vs `surface_reached` **102 次**（成功率 20%）
- 331 样本：**+5 格 / 29.3s = 7 秒/格**；`nju_guitest` EP37 从 **y=-48** 爬 16 次（458s）只到 y=1，仍需 ~672s > 600s 预算 → **数学上不可能**
- 与并行 slice 的 BUG-051（地下 y<45 被派地表任务）叠加，形成"下得去、上不来、还被派地表活"的死循环

#### E. 实体 id 失效循环 —— **P2，43 次 + 118 次超大半径扫描（新票 BUG-060）**
- `glm_5` EP39 `make_a_sign_glow`：radius 256→512→…→**65,536** 连发 9 次 → `move_to(entity_id)` 两次 `no_such_entity` → 摔死 → 22 请求 0 分

#### F. `void_geofence_blocked` —— **P1，307 次（BUG-048）**
- `nju_guitest` wave1 **154** 次 / 118 个不同坐标 / 最远 **12.9km**；`glm_4` part2 **129** 次 / 110 个不同坐标（仅超界 0~42m）
- 重复失败保护按"完全相同参数"匹配 → 每次换个坐标就绕过

#### G. `pillar_fallback` 自锁 —— **P1（BUG-055 / P2-36）**
- 213 次，其中 **24 段 ≥3 连击**（`nju_guitest` **29 连击** 188s、`glm_2` 18 连击、`hy4_6` 10 连击）

#### H. 规划锁死（并行 slice 已立 BUG-049/050/051）—— **零分集的最大来源**
- 下界死亡丢镐后仍派主世界 `bootstrap_stone_age/iron_age`：`glm_1` **75/80 集（94%）** 全废
- 无鞍派 `ride_strider`、无钻镐派 `ancient_debris`、无结构派 `heart_transplanter`：`ds_4` 0/13+0/12+0/10、`glm_5` 0/14+0/8+0/7
- 地下 y<45 被派地表任务（`sleep_in_bed`/`breed_an_animal`/`plant_seed`）：`glm_4` 连续 17 集

#### I. `staircase:time_budget` —— **P1（BUG-037/041）**
- 两波 592 次（351+241），`steps_done` 中位 1→2，仍缺"软方块方向优先"

#### J. `depth_reset` —— **P1（BUG-030）**
- 两波 41 次（16+17），`ds_1` 6、`nju_3` 5；行为修复（导航回最深节点）仍未实现

#### K. 维度错配挖矿 —— **P2-37**
- `bad_target_for_dimension` **80 次**：`diamond` **63**（下界要钻石）、`iron` 16

#### L. `craft_gear:missing_ingredients` —— **P2-38**
- **652 次**（占 craft 调用 43%，成功 380）；`stone_pickaxe` 132、`wooden_pickaxe` 125、`diamond_pickaxe` 90

#### M. 死亡 —— **P1（BUG-026/040）**
- 两波 notable 死亡 **263 次**（110+153）：`mob 87 / lava 47 / arrow 36 / hot_floor 31 / fall 16`；`ds_4` 单路 94 次

### 3. 票号对账（与并行 slice 的 16 路复盘）

| 并行 slice 的票 | 我的对应证据 | 关系 |
|---|---|---|
| BUG-049 下界派主世界 bootstrap | `glm_1` 75/80 集全废 | 一致，我方补充 `bad_target_for_dimension` 80 次 |
| BUG-050 缺前置强派 + 固定 20min 冷却 | `ride_strider`/`ancient_debris` 0/13 等 | 一致 |
| BUG-051 地下被派地表任务 | 与本方 BUG-059（climb 7s/格）**互为因果** | 需一起修 |
| BUG-052 缺工作台/满包 | `craft:inventory_full` 69、`need_crafting_table` 185 | 一致 |
| BUG-055 pillar 自锁 | 本方 P2-36 同源 | 已合并引用 |
| **BUG-056** escape_water 无界 | 19 集报废 / 4.69h | **本方新增（P0）** |
| **BUG-057** hot_floor 无反射 | 2,129 次伤害 / 34 死 | **本方新增** |
| **BUG-058** path_not_found | 1,137 次 / 446 集 | **本方新增** |
| **BUG-059** climb_out 回不来 | 400 次 progress / 3.6h | **本方新增** |
| **BUG-060** 实体 id 失效 | 43 次 + 118 次超大扫描 | **本方新增（P2）** |

### 4. 修复优先级（按"每修一项能救回多少零分集"）

1. **BUG-056**（escape_water 预算）→ 直接救回 19 集 + 4.69h
2. **BUG-049/050**（下界/前置门控）→ 救回 `glm_1` 75 集级别的整段空转
3. **BUG-042/045**（429 重试预算 + Qwen RPM）→ 救回 61 集 + 18% 墙钟
4. **BUG-057**（hot_floor 反射）→ 救回 34 死 + 58 集
5. **BUG-058**（path_not_found 改挖）→ 释放 446 集里的无效往返
6. **BUG-059 + BUG-051**（climb 加速 + 地下不派地表）→ 解决 58 集回程超时
7. **BUG-048 / BUG-055**（围栏钳制 + pillar 自锁）→ 各 ~300/213 次
8. **BUG-030 / BUG-037**（depth_reset + 软方块路由）→ 长期效率

## 4bz. 逐 target 复盘：18 个目标成功率 0%，吃掉 31.2% 集数

（统计口径：6 波 1,581 集，按 `target` 聚合；`ok` = `goal_satisfied` 或 `objective_verified`）

### 0% 成功率目标池

| target | 集数 | 请求 | 得分 |
|---|---|---|---|
| `husbandry/make_a_sign_glow` | **116** | 2,402 | 30 |
| `nether/ride_strider` | 57 | 1,581 | 25 |
| `husbandry/breed_an_animal` | 51 | 665 | **0** |
| `adventure/heart_transplanter` | 47 | 448 | **0** |
| `nether/obtain_crying_obsidian` | 34 | 826 | **0** |
| `island_recon_sweep` | 30 | 229 | 65 |
| `adventure/kill_mob_near_sculk_catalyst` | 28 | 270 | **0** |
| `nether/distract_piglin` | 25 | 344 | **0** |
| `husbandry/tame_an_animal` | 18 | 149 | **0** |
| `husbandry/fishy_business` | 18 | 233 | **0** |
| `husbandry/allay_deliver_item_to_player` | 17 | 116 | **0** |
| `nether/get_wither_skull` | 10 | 409 | **0** |
| `husbandry/obtain_sniffer_egg` | 10 | 86 | **0** |
| `story/cure_zombie_villager` | 9 | 90 | **0** |
| `husbandry/ride_a_boat_with_a_goat` | 7 | 43 | **0** |
| `adventure/trade_at_world_height` | 6 | 91 | **0** |
| `husbandry/silk_touch_nest` | 6 | 19 | **0** |
| `nether/find_bastion` | 5 | 77 | **0** |
| **合计** | **494（31.2%）** | **8,078** | 120 |

### ROI 排名（分/100 请求）

| target | 分/100 请求 | 集数 | 成功率 |
|---|---|---|---|
| `nether/brew_potion` | **87.2** | 18 | 78% |
| `adventure/use_lodestone` | **74.9** | 41 | 37% |
| `story/shiny_gear` | **61.0** | 75 | 20% |
| `adventure/kill_a_mob` | 46.2 | 8 | 50% |
| `story/deflect_arrow` | 20.4 | 34 | 15% |
| `story/mine_diamond` | 19.2 | 9 | 33% |
| `nether/obtain_ancient_debris` | **11.6** | 91 | 2% |
| `nether/find_fortress` | **5.9** | 45 | 4% |

- **`shiny_gear` 一项贡献 1,245 分（占两波总分 3,845 的 32%）**，`brew_potion` + `use_lodestone` 再贡献 325 分
- 而 18 个 0% 目标只贡献 120 分，却消耗 **8,078 次请求 / 494 集**
- `obtain_ancient_debris` 91 集 3,237 请求只换 375 分（11.6 分/100 请求）→ **性价比低于 `shiny_gear` 5 倍**

### 结论

课程层的问题是**"派发不看 ROI、不看前置、不看维度"**（对应 BUG-049/050），已在本方数据里得到量化确认：
- 31.2% 的集数花在 0% 成功率目标上
- 分数高度集中在 3 个目标（`shiny_gear` / `brew_potion` / `use_lodestone`）
- 建议：`curriculum.py` 用每波实测 `score_delta / actor_requests` 做滑动平均权重（P2-40），并把 0% 目标池在拿到前置（鞍/钻镐/结构观测）前直接跳过

## 4ca. BUG-063 Fleet 死亡口径与 r26 对账

`hy4_audit_r26/hy4_hy_6_nether_bastion` 的已归档榜单显示 **51 = 31 条含 `death_or_connection_changed` 的 Guardian 事件 + 20 条含“死亡”的 `mc.notable_events`**。从终版 `curriculum_summary` 的 20 集逐一回放 JSONL，得到 **20 条明确死亡回执（11 集）**、**22 条重生或出生回执**、**0 条显式连接变化**。这个历史格式下实际连接变化数未知。四路同口径的死亡总数由旧榜单的 **92** 变为 **38 条明确回执**。 [旧榜单单路](../../v2-integrated/runs/hy4_audit_r26/triage-hy4_audit_r26.md#L11) · [旧榜单聚合](../../v2-integrated/runs/hy4_audit_r26/triage-hy4_audit_r26.md#L20) · [终版摘要](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/wild-endurance-hy4_audit_r26_hy_6_nether_bastion-20261119-094358.json)

| EP | 旧混合事件 | 明确死亡 | JSONL 死亡回执行 |
|---:|---:|---:|---|
| 02 | 1 | 1 | [1020](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1020) |
| 03 | 4 | 2 | [186](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-095549-483226.events.jsonl#L186)、[421](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-095549-483226.events.jsonl#L421) |
| 04 | 1 | 1 | [174](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-100000-535411.events.jsonl#L174) |
| 07 | 2 | 2 | [140](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-102339-461292.events.jsonl#L140)、[933](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-102339-461292.events.jsonl#L933) |
| 10 | 3 | 2 | [267](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-104042-796771.events.jsonl#L267)、[462](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-104042-796771.events.jsonl#L462) |
| 11 | 2 | 1 | [567](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-104948-594350.events.jsonl#L567) |
| 12 | 5 | 4 | [195](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-105826-709979.events.jsonl#L195)、[422](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-105826-709979.events.jsonl#L422)、[638](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-105826-709979.events.jsonl#L638)、[871](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-105826-709979.events.jsonl#L871) |
| 14 | 4 | 2 | [277](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-111228-027934.events.jsonl#L277)、[469](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-111228-027934.events.jsonl#L469) |
| 15 | 4 | 2 | [100](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-111904-903239.events.jsonl#L100)、[656](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-111904-903239.events.jsonl#L656) |
| 16 | 4 | 2 | [132](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-112555-551328.events.jsonl#L132)、[540](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-112555-551328.events.jsonl#L540) |
| 19 | 1 | 1 | [293](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-113756-751113.events.jsonl#L293) |

**根因。** EP02 的 Guardian 事件和同一反射窗口内的死亡、重生回执分别进了两条旧分支。31 条混合事件中，30 条仅给出未分类的 `event_seqs`，EP16 L545 连源序号列表都是空的。Guardian 同一原因还覆盖连接变化、出生、死亡循环及某些状态查询失败。回执生成将后端 `death` 渲染为 `[死亡 pos=…]`，将 `spawn/respawn` 同时渲染为 `[已重生 pos=…]`，同文案合批压成 `×N`。JSONL 外层 `seq` 是每集写入序号。 [旧 Fleet 分支](../../v2-integrated/scripts/v2_fleet.py#L566) · [EP02 混合事件](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1017) · [EP16 空序号](../../v2-integrated/runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-112555-551328.events.jsonl#L545) · [Guardian 归因](../../v2-integrated/src/mc_agent/harness/guardian.py#L682) · [Guardian 序号列表](../../v2-integrated/src/mc_agent/harness/guardian.py#L2017) · [类型转文本](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L908) · [回执压缩](../../v2-integrated/src/mc_agent/harness/agent.py#L1131) · [日志信封序号](../../v2-integrated/src/mc_agent/harness/events.py#L22)

**离线口径。** Fleet 与 D1 共用同一计数器。显式死亡、重生或出生、连接变化分别累计。顶层类型化记录的 `event_id` 或 `source_seq` 以及 Guardian/Notable 的 `events` 列表中类型化子项的 `seq` 可作为源 ID，按 Bot 与稳定源 ID 跨集去重。未来双路同时输出同一事件时应共用源 ID。旧文本按每条明确回执展开 `×N`，并单列无 ID 死亡数和混合 Guardian 事件。同一次事件若跨记录混用有 ID 与无 ID 的表示，历史证据无法可靠关联。BUG-026 的既有混合事件门槛仍按独立告警计数。 [计数器](../scripts/fleet_lifecycle.py#L32) · [Fleet 入口](../scripts/v2_fleet.py#L546) · [离线夹具](../tests/fixtures/fleet_lifecycle_bug063.events.jsonl) · [测试](../tests/test_fleet_lifecycle.py)

在 D1 工作树复现（只读 runs）。

```bash
/home/xzq/projects/mc/.venv/bin/python -m pytest -q tests/test_fleet_lifecycle.py
/home/xzq/projects/mc/.venv/bin/python scripts/v2_fleet.py triage --wave hy4_audit_r26 --runs-root /home/xzq/projects/mc/.worktrees/v2-integrated/runs --no-markdown
```

## 4cb. 九票离线门禁与合入检查

截至集成树 `29893fb`，指定前缀下仍只有已收官的六波基线。队列里有三组待实测的合入变更。BUG-042/045 已有 `c15e9c7` 的 429 尝试预算与抖动，以及 `103eaa9` 的 GLM 每 Bot 10 RPM 配置。BUG-056 已有 `46f882b` 和 `a586958` 的浮水时限路径。BUG-057 已有 `3594833` 的岩浆块受伤时下蹲路径。集成树的 `bug056 or bug057` 离线测试通过 5 项。仍需运行产物确认实际加载的版本、场景覆盖和门禁结果。 [429 代码](../../v2-integrated/src/mc_agent/harness/model.py#L674) · [Fleet 配置](../../v2-integrated/scripts/v2_fleet.py#L101) · [浮水时限](../../v2-integrated/src/mc_agent/harness/guardian.py#L316) · [岩浆块反射](../../v2-integrated/src/mc_agent/harness/guardian.py#L557) · [集成树离线测试](../../v2-integrated/tests/test_generation_guard.py#L3877)

| 工单 | `d1_acceptance.py` 核对项 |
|---|---|
| BUG-042 | 12 路约 4h 的硬 429 集 ≤2，持续限流尝试 ≥6，长任务 429/502 报废 ≤1，同类 HY4 长任务分数 ≥60。 |
| BUG-045 | 原票 `glm_*` 通道重试率和 429 退避墙钟各 ≤5%，整波硬 429 集 ≤3。 |
| BUG-046 | 约 4h 波次最短 Bot 实际墙钟比例 ≥95%。 |
| BUG-047 | 工具 p99 ≤60s，`unstick` ≤30s，`move_to:busy` ≤20s，单集超预算 ≤10s。 |
| BUG-056 | 浮水 p99 ≤25s、max ≤40s，指定 Bot 的单请求超时集为 0。 |
| BUG-057 | 每集岩浆块伤害 ≤10 次，整波岩浆块死亡 ≤3 次。 |
| BUG-058 | `path_not_found` 每集 ≤3 次，整波 ≤300 次。 |
| BUG-059 | `climb_progress/surface_reached` ≤1:1，连击 ≥3 的集数 ≤5，深挖回程超时需逐集核定。 |
| BUG-060 | `no_such_entity` 每波 ≤5 次，以半径 ≥4096 扫实体为 0 次。 |

终版以 `wild-endurance-*.json` 中的 `curriculum_summary` 为准。无终版、缺少样本或待逐集判定的门禁显示 `PENDING`。计时只在集内配对开始和结束事件。用修复前后的波名运行 `d1_compare.py 前波 后波 --ticket BUG-NNN` 可得到该票各项指标及证据位置。单集示范为 `hy4_audit_r27/hy4_hy_9_deep_water` EP13，JSONL L21 与 L42 两次 `boundary_escape_water` 分别为 560.6412s 与 560.6349s，L46 在 600s 预算下记录实际 1132.1628s。 [门禁实现](../scripts/d1_ticket_gates.py) · [前后对照](../scripts/d1_compare.py) · [离线测试](../tests/test_d1_ticket_gates.py) · [EP13 L21](../../v2-integrated/runs/hy4_audit_r27/hy4_hy_9_deep_water/20261119-132625-966581.events.jsonl#L21) · [EP13 L42](../../v2-integrated/runs/hy4_audit_r27/hy4_hy_9_deep_water/20261119-132625-966581.events.jsonl#L42) · [EP13 L46](../../v2-integrated/runs/hy4_audit_r27/hy4_hy_9_deep_water/20261119-132625-966581.events.jsonl#L46)

## 4cc. 监听漏检范围与改造

原一轮监听只匹配 `overnight_12bot*` 和 `hy4_audit_*`。按各 Bot 的首条 `run.started.time` 盘点，漏检从北京时间 **11 月 20 日 13:01** 启动的 `r29_newmix` 就已开始。到 14:23 的盘点共 **6 波**，其中 **4 波已收官，57 条 Bot 轨迹、306 集**，另有 **2 波在跑，20 条 Bot 轨迹、23 集临时记录**。收官集数来自终版的 `curriculum_summary`。 [r29 首跑](../../v2-integrated/runs/r29_newmix/glm_glm_2_nether_bastion/20261119-210114-477304.events.jsonl#L3) · [r31 首跑](../../v2-integrated/runs/r31_short/glm_glm_1_nether_fortress/20261119-215952-195509.events.jsonl#L3)

现运行 `d1_overnight_watch.py --follow --since 2026-11-20T05:00:00Z --every-min 45`。启动时将截止点以后的产物纳入清单，并发出发现和已有收官通知。运行中每 15 秒只查文件元数据，识别任意命名的新波次，完整终版收齐即通知。迟到的 Bot 目录会触发重新打开。每 45 分钟输出状态快照。启动实测一次发现 r29、r30 两波、r31 两波和 r32，三份历史短波及 r30 长波均已追补。 [监听实现](../scripts/d1_overnight_watch.py) · [离线回归](../tests/test_d1_overnight_watch.py)

## 4cd. r29–r31 收官追补与门禁对照

| 波次 | 终版 Bot | 集数 | 验证成交 | 总分 | Python 指纹 / 对应源码 |
|---|---:|---:|---:|---:|---|
| `r29_newmix` | 20/20 | 104 | 28 | 1810 | `846df6514030546e` / `0464355` |
| `r30_short` | 17/17 | 85 | 18 | 1640 | `25a97da76d029656` / `29893fb` |
| `r30_long` | 3/3 | 34 | 8 | 235 | `25a97da76d029656` / `29893fb` |
| `r31_short` | 17/17 | 83 | 13 | 1615 | `33b3bccc0e714a16` / `5c020ff` |

对应源码以 `run.started.runtime.source_revision` 与历史 Git 树的 Python 文件 SHA-256 对账。四个已收官波次均跑过 acceptance、逐集审计和代表性轨迹。 [指纹算法](../../v2-integrated/src/mc_agent/runtime.py#L10) · [r29 榜单](../../v2-integrated/runs/r29_newmix/triage-r29_newmix.md) · [r30 短波榜单](../../v2-integrated/runs/r30_short/triage-r30_short.md) · [r30 长波榜单](../../v2-integrated/runs/r30_long/triage-r30_long.md) · [r31 短波榜单](../../v2-integrated/runs/r31_short/triage-r31_short.md)

- **BUG-045 已达标。** 过夜第二波→r29 的同脚本前后对照为 GLM HTTP 重试 **16.87%→0.44%**、429 退避 **15.38%→0.13%**、硬 429 **52→0**。r29 五路各 10 RPM，唯一的队内 429 在首次重试恢复。r30 短/长波和 r31 短波同样通过三项门禁。BUG-042 的 12 路约 4 小时、持续限流尝试次数及 HY4 长程 60 分仍待验。BUG-046 缺约 4 小时收官样本。 [r29 GLM2 EP01](../../v2-integrated/runs/r29_newmix/glm_glm_2_nether_bastion/20261119-210114-477304.events.jsonl#L43) · [r31 GLM1 EP01](../../v2-integrated/runs/r31_short/glm_glm_1_nether_fortress/20261119-215952-195509.events.jsonl#L303) · [重试分支](../../v2-integrated/src/mc_agent/harness/model.py#L674)
- **BUG-056 守护浮水已达标。** r27→r29 的 `boundary_escape_water` p99/max **560.644s→20.053s**，指定 Bot 单请求超时 **7→0**。r31 短波 24 次动作 p99/max 均 **20.029s**，HY4_9 EP01 在 5.8s 浮水成功。 [r29 HY4_10 EP02](../../v2-integrated/runs/r29_newmix/hy4_hy_10_enchant_anvil/20261119-210319-676548.events.jsonl#L147) · [r31 HY4_9 EP01](../../v2-integrated/runs/r31_short/hy4_hy_9_deep_water/20261119-220016-062292.events.jsonl#L74) · [Guardian 浮水入口](../../v2-integrated/src/mc_agent/harness/guardian.py#L367)
- **BUG-047 仍超限。** r29 HY4_5 EP05 的 actor `unstick` 阻塞 **622.934s**，45s 自报预算后仍在原地水中却返回成功。r30 HY4_8 EP06 的 `move_to:busy` 花 **102.079s**。r31，NJU_3 EP01 的 `unstick` 花 **45.031s**，GUITest EP04 在集预算剩约 21s 时进入同步采木，耗时 **80.558s**，整集超预算 **59.539s**。该波尚未加载 `968d1ce` 的 `unstick` stop 预留，采木超时在另外的调用路径。 [r29 HY4_5 EP05](../../v2-integrated/runs/r29_newmix/hy4_hy_5_brewing_alchemy/20261119-210552-217591.events.jsonl#L110) · [r30 HY4_8 EP06](../../v2-integrated/runs/r30_short/hy4_hy_8_nether/20261119-214925-046294.events.jsonl#L190) · [r31 NJU_3 EP01](../../v2-integrated/runs/r31_short/nju_nju_3_village_trade/20261119-215947-734163.events.jsonl#L147) · [r31 GUITest EP04](../../v2-integrated/runs/r31_short/nju_guitest_01_nether/20261119-221356-261049.events.jsonl#L747) · [调度后的 deadline 检查](../../v2-integrated/src/mc_agent/harness/agent.py#L1522)
- **BUG-057 仍开。** r30 短波伤害峰值 8，r31 GLM2 EP02 峰值 50。该集 **42 次**下蹲读回 `sneaking=false`，每次取消一次模型生成，始终停在同一坐标。下一集首批回执在该坐标给出岩浆块伤害至 0 血以及空原因死亡。r29 GUITest EP05 也有同一批回执中 HP=0、空原因死亡的先例。基于显式 cause 的死亡门禁漏掉这些回执，现场按时间和位置复核。 [r31 GLM2 EP02](../../v2-integrated/runs/r31_short/glm_glm_2_nether_bastion/20261119-221753-374008.events.jsonl#L72) · [r31 GLM2 EP03](../../v2-integrated/runs/r31_short/glm_glm_2_nether_bastion/20261119-221849-499250.events.jsonl#L11) · [r29 GUITest EP05](../../v2-integrated/runs/r29_newmix/nju_guitest_01_nether/20261119-211405-406198.events.jsonl#L640) · [下蹲入口](../../v2-integrated/src/mc_agent/harness/guardian.py#L588)
- **BUG-058/060 待对新版本。** r30 短波→r31 短波 `path_not_found` 为 **25→38**、单集峰值 **3→5**。r31 HY4_3 EP07 在坐标或实体 ID 之间重试 5 次。r31 短波在 `090ad3d` 的挖掘提示和重规划修复之前。BUG-060 在短波达标，但 r30 长波 GLM5 EP19 单集 5 次半径 ≥4096 的扫实体，长程未通过。 [r31 HY4_3 EP07](../../v2-integrated/runs/r31_short/hy4_hy_3_midgame/20261119-220654-759086.events.jsonl#L43) · [r30 GLM5 EP19](../../v2-integrated/runs/r30_long/glm_glm_5_midgame/20261119-220939-161874.events.jsonl#L86) · [新寻路提示](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1158)
- **BUG-059 仍开。** r30 短波的 7 集初筛里有 1 集回程未到地表、1 集到达时已超时，另 5 集继续执行其他任务后才超时。r31 短波 `climb_progress/surface_reached` **9/7**。HY4_2 EP03 在四次连续回程中用 **107.74s** 升 14 格，超时仍在 Y−40；其余三候选为两集晚起未完成回程和一集越过预算后到达。r30 长波唯一候选则回到地表后再次下探。 [r30 HY4_9 EP02](../../v2-integrated/runs/r30_short/hy4_hy_9_deep_water/20261119-213430-773429.events.jsonl#L304) · [r31 HY4_2 EP03](../../v2-integrated/runs/r31_short/hy4_hy_2_iron/20261119-221109-705383.events.jsonl#L368) · [r30 MoveTest EP06](../../v2-integrated/runs/r30_long/nju_movetest_01_bare/20261119-215609-040139.events.jsonl#L805) · [回程实现](../../v2-integrated/src/mc_agent/skills/mining_dfs.py#L3069)
- **BUG-063 已进入真实 Fleet 榜单。** r31 短波共 15 条明确死亡回执。GUITest 的 5 条死亡与 7 条重生或出生、8 条 Guardian 混合事件分别列出。 [r31 Fleet 榜单](../../v2-integrated/runs/r31_short/triage-r31_short.md#L24)

## 4ce. 自动发现后的首个收官通知：r32_short

监听在北京时间 **14:42:58** 发出 `WAVE SETTLED r32_short`。17/17 终版，**71 集、17 集验证成交、26 集超时、1645 分**。67 条有 `run.started` 的记录均为 Python 指纹 `bf7f94900fb66b4b`，对应 `53fe964` 的源码，包含 `968d1ce` 的脱困 stop 预留，早于 `090ad3d` 的寻路提示和 `8b69003` 的深轴保留。 [r32 榜单](../../v2-integrated/runs/r32_short/triage-r32_short.md) · [GLM1 EP01 指纹](../../v2-integrated/runs/r32_short/glm_glm_1_nether_fortress/20261119-222148-738485.events.jsonl#L3)

- **BUG-047 部分通过。** r31→r32 的 `unstick` max **45.031→28.036s**（45→26 次），满足 30s 门槛。GLM2 EP01 距 1200s 集预算只剩 27.57s 时启动 90s 的 `mine_dfs(explore)`，完成耗时 87.16s，整集超限 **59.603s**。已测工具 p99 为 87.145s，另有 2 条缺时长。该票继续 OPEN。 [GLM1 EP01](../../v2-integrated/runs/r32_short/glm_glm_1_nether_fortress/20261119-222148-738485.events.jsonl#L429) · [GLM2 EP01](../../v2-integrated/runs/r32_short/glm_glm_2_nether_bastion/20261119-222150-480408.events.jsonl#L310) · [采矿入口](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L2381)
- **BUG-056/045 复验通过。** 守护浮水 26 次 p99/max **19.239s**，指定 Bot 单请求超时 0。GLM 116 请求、0 重试、0 退避、0 硬 429。BUG-057 缺岩浆块伤害暴露。 [浮水最长 GLM3 EP05](../../v2-integrated/runs/r32_short/glm_glm_3_village_trade/20261119-222936-862975.events.jsonl#L56) · [GLM1 请求首行](../../v2-integrated/runs/r32_short/glm_glm_1_nether_fortress/20261119-222148-738485.events.jsonl#L4)
- **BUG-058/060。** `path_not_found` 由 r31 的 38 次降到 **19**，单集上限由 5 升到 **7**，漏洞样本 GLM1 EP01 首次等了 93.4s。BUG-060 的过期实体 2 次、超大扫描 0 次都达标，长程仍等对照。 [GLM1 EP01](../../v2-integrated/runs/r32_short/glm_glm_1_nether_fortress/20261119-222148-738485.events.jsonl#L334) · [HY4_4 EP03](../../v2-integrated/runs/r32_short/hy4_hy_4_cave_combat/20261119-222337-964780.events.jsonl#L198) · [新寻路提示](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1158)
- **BUG-059 人工核对。** `climb_progress/surface_reached` **9/7→0/3**。三条深挖超时候选均在时限内到地表。HY4_2 EP02/03 又下探，GUITest EP03 到地表后仍发起了一次生成，三者均无未完成回程造成的超时。后续以实际加载新版本的波次复验。 [HY4_2 EP02](../../v2-integrated/runs/r32_short/hy4_hy_2_iron/20261119-222315-781768.events.jsonl#L90) · [HY4_2 EP03](../../v2-integrated/runs/r32_short/hy4_hy_2_iron/20261119-223301-232412.events.jsonl#L332) · [GUITest EP03 地表后生成](../../v2-integrated/runs/r32_short/nju_guitest_01_nether/20261119-222430-023563.events.jsonl#L1333)

## 4cf. r31_long 与 r33_short 双收官

监听北京时间 **15:05:04** 和 **15:05:19** 分别报收官。`r31_long` 为 **3/3 Bot、30 集、5 集验证成交、190 分**，Python 指纹 `bf7f94900fb66b4b` 对应 `53fe964`。`r33_short` 为 **17/17 Bot、73 集、14 集验证成交、1625 分**，指纹 `a434246b11095084` 对应 `50f74fc`。启动时漏掉的六波已全部审完，终版合计 **77 条 Bot 轨迹、407 集**。加上新监听直接发现的 r33，追踪期合计 **7 波、94 条 Bot 轨迹、480 集**。 [r31 长波榜单](../../v2-integrated/runs/r31_long/triage-r31_long.md) · [r33 榜单](../../v2-integrated/runs/r33_short/triage-r33_short.md)

- **BUG-042 长波只通过报废子门槛。** 三路 2700s 的硬 429/502 报废均为 0。HY4_1 终版 **20/60**，EP01 为唯一验证成交，EP03 因区块未加载，EP04/10 超时。12 路约 4h 和持续 429 暴露仍待验。 [HY4_1 终版](../../v2-integrated/runs/r31_long/hy4_hy_1_bare/wild-endurance-r31_long_hy_1_bare-20261119-221907.json) · [EP10 收尾](../../v2-integrated/runs/r31_long/hy4_hy_1_bare/20261119-230411-792716.events.jsonl#L10)
- **BUG-047 仍有采矿长尾。** r31 长波和 r33 短波 `unstick` 最大 **12.102s／28.041s**，均过门槛。整集最大仍超预算 **43.362s／54.930s**，对应 MoveTest EP09 和 NJU_3 EP04 的最后一次 `mine_dfs(explore)`，两集启动时均不足以用完所请求的 90s 或 85s。将采矿钳至 55s 且共享剩余集预算的 `9ebf316` 晚于两波加载版本。集成树预算、寻路、扫描和岩浆块相关的 11 个离线测试通过。 [r31 MoveTest EP09](../../v2-integrated/runs/r31_long/nju_movetest_01_bare/20261119-225100-136198.events.jsonl#L478) · [r33 NJU_3 EP04](../../v2-integrated/runs/r33_short/nju_nju_3_village_trade/20261119-224635-843085.events.jsonl#L267) · [采矿预算钳制](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L2502) · [离线测试](../../v2-integrated/tests/test_v2_harness.py#L195)
- **BUG-057 仍失败。** r33 GLM2 EP01 的 5 次 `hot_floor_sneak` 均返回 `sneaking=true`，未触发 `e2144d7` 的下蹲失败短移。在第一轮下蹲之后，紧接的采矿和反射阶段同一坐标又报告 `sneaking=false`。至最后一次下蹲之后，模型执行 20s 零命中攻击，之后一次收到 **33/52** 条岩浆块伤害；期间未再次介入。失败条件从“下蹲无法确认”扩展为“下蹲确认后伤害仍在累积，后续动作可解除状态”。 [GLM2 EP01 下蹲](../../v2-integrated/runs/r33_short/glm_glm_2_nether_bastion/20261119-224439-075002.events.jsonl#L144) · [状态回落](../../v2-integrated/runs/r33_short/glm_glm_2_nether_bastion/20261119-224439-075002.events.jsonl#L161) · [33 条伤害](../../v2-integrated/runs/r33_short/glm_glm_2_nether_bastion/20261119-224439-075002.events.jsonl#L329) · [下蹲回执](../../v2-integrated/src/mc_agent/harness/guardian.py#L615)
- **BUG-058 单集门槛仍失败。** `090ad3d` 已在 r33 加载，r32→r33 单集峰值 **7→4**，整波 **19→28**。GLM1 EP01 在第三次 `move_to` 失败时收到重规划并实际换成隧道、挖掘、采矿。第四条为 `mc_dig` 的寻路失败。整波 28 条由 27 条移动和 1 条挖掘组成。 [GLM1 EP01 第三次](../../v2-integrated/runs/r33_short/glm_glm_1_nether_fortress/20261119-224438-456095.events.jsonl#L184) · [第四次](../../v2-integrated/runs/r33_short/glm_glm_1_nether_fortress/20261119-224438-456095.events.jsonl#L566) · [移动重规划](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1261)
  HY4_3 EP06 四次失败均收到 `dig_toward`，其中三次有 `requires_replan`。第二次后试挖和阶梯，再回到同一片区域寻路，第四次之后才用隧道离开。修复已经走到，回执建议仍无法阻止区域内重复寻路。 [HY4_3 EP06 第二次](../../v2-integrated/runs/r33_short/hy4_hy_3_midgame/20261119-225345-169801.events.jsonl#L66) · [第四次](../../v2-integrated/runs/r33_short/hy4_hy_3_midgame/20261119-225345-169801.events.jsonl#L392) · [隧道](../../v2-integrated/runs/r33_short/hy4_hy_3_midgame/20261119-225345-169801.events.jsonl#L410)
- **BUG-059 长短波均失败。** r31 长波回程比 **13/3**，GLM5 EP08 的活动树从 12 节点归零，三次回程共 164.929s，只到 Y−31/−12。r33 短波回程比 **11/6**。四条超时候选中，GLM3 EP04 的 83.776s 回程超过剩余约 44s，DS_2 EP04 也未完成回程且仅剩约 32s、此前有战斗抢占；另外两集先到地表，再因继续探矿超时。新 `8b69003` 对应 DS_2 发生退路堵塞时仍保留 2 个节点，路径时间问题仍在。 [r31 GLM5 EP08](../../v2-integrated/runs/r31_long/glm_glm_5_midgame/20261119-223935-867850.events.jsonl#L176) · [r33 GLM3 EP04](../../v2-integrated/runs/r33_short/glm_glm_3_village_trade/20261119-224742-727958.events.jsonl#L141) · [DS2 EP04](../../v2-integrated/runs/r33_short/nju_ds_2_cave_combat/20261119-225515-225063.events.jsonl#L217) · [回程实现](../../v2-integrated/src/mc_agent/skills/mining_dfs.py#L3082)
- **BUG-060 长波仍失败，BUG-045/056 维持通过。** r30→r31 长波超大实体扫描 **5→5**，r31 GLM5 EP06 半径 64→65536，4096 起的 5 次仍见同一批近处实体。r33 短波过期实体／超大扫描为 **0/0**。`a896f86` 扫描上限修复晚于两波。r31 长波与 r33 短波均无 GLM 429 重试及硬失败，守护浮水最大为 **1.977s／13.980s**。 [GLM5 EP06](../../v2-integrated/runs/r31_long/glm_glm_5_midgame/20261119-223015-242650.events.jsonl#L89) · [扫描上限](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L200) · [r33 HY4_3 EP08 浮水](../../v2-integrated/runs/r33_short/hy4_hy_3_midgame/20261119-230013-632529.events.jsonl#L120)

## 4cg. r34_short 收官：采矿长尾收敛，移动锁与 NJU 5xx 残余

北京时间 **15:30:12** 收到 17/17 收官。**83 集、17 集验证成交、26 集超时、1730 分**。79 条运行指纹为 `7424defec73f3f7c`，与 `071977d` 的源码对应，包含 `9ebf316` 采矿时限和 `a896f86` 扫描回执。 [r34 榜单](../../v2-integrated/runs/r34_short/triage-r34_short.md) · [GLM1 EP01 指纹](../../v2-integrated/runs/r34_short/glm_glm_1_nether_fortress/20261119-230808-255878.events.jsonl#L3)

- **BUG-020 重开。** 7 个 `runtime_error` 均在 NJU 四路波头。终端 `run.finished` 对账为 **502×5、503×1、500×1**。NJU_3 EP01 两次 502 各等待 20s 后再次 502，DS_2 EP02 前两次 502 后为 503 `Loading model`。另一份逐集报告中没有留下 NJU_3 EP02 的完整错误文本，原始 JSONL 确认也是 502。GLM 的 136 个请求无重试，整波硬 429 为 0。 [NJU_3 EP01](../../v2-integrated/runs/r34_short/nju_nju_3_village_trade/20261119-230802-518258.events.jsonl#L7) · [EP02](../../v2-integrated/runs/r34_short/nju_nju_3_village_trade/20261119-230856-187955.events.jsonl#L11) · [DS_2 EP02](../../v2-integrated/runs/r34_short/nju_ds_2_cave_combat/20261119-230908-867093.events.jsonl#L25) · [重试预算](../../v2-integrated/src/mc_agent/harness/model.py#L674)
- **BUG-047 两项通过、两项失败。** r33→r34 工具 p99 **86.285→59.223s**，脱困最大由 28.041s 到 **29.396s**，均满足阈值。HY4_6 EP03 两次移动返回 `busy`，分别耗时 **102.606s** 与约 **101.8s**。第二次在集预算只剩约 6s 时启动，整集超出 **95.916s**。日志记录了现象和回执，后端租约持有者仍未知。 [首次 busy](../../v2-integrated/runs/r34_short/hy4_hy_6_nether_bastion/20261119-232351-607318.events.jsonl#L251) · [第二次](../../v2-integrated/runs/r34_short/hy4_hy_6_nether_bastion/20261119-232351-607318.events.jsonl#L294) · [预检实现](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1159)
- **其余门禁。** BUG-056 13 次浮水 p99/max **17.141s**、指定单请求超时 **0**。BUG-057 峰值 **52→11**，仍超 10 次门槛，GLM2 EP01 的四次下蹲均返回成功。BUG-058 峰值 **4→5**，HY4_10 EP03 是四次移动和一次实体交互失败。BUG-059 回程比 **4/5**，深挖候选中 NJU_3 EP10 到地表后继续探索，DS_1 EP03 三次回程仍未确认地表，末段又收到 `needs_surface_climb`。BUG-060 短波过期 ID **2**、超大扫描 **0**，等同版本长波复验。 [GLM2 EP01](../../v2-integrated/runs/r34_short/glm_glm_2_nether_bastion/20261119-230809-778708.events.jsonl#L177) · [HY4_10 EP03 交互失败](../../v2-integrated/runs/r34_short/hy4_hy_10_enchant_anvil/20261119-230956-618307.events.jsonl#L367) · [DS_1 EP03](../../v2-integrated/runs/r34_short/nju_ds_1_iron/20261119-231814-122829.events.jsonl#L619) · [NJU_3 EP10](../../v2-integrated/runs/r34_short/nju_nju_3_village_trade/20261119-231917-689889.events.jsonl#L177)

## 4ch. r32_long 与 r35_short 收官：实体扫描长波验收

北京时间 **15:52:48** 和 **15:53:03** 分别收到 r35 短波、r32 长波收官。`r32_long` 为 **3/3 Bot、34 集、8 集验证成交、295 分**，31 条运行指纹为 `7424defec73f3f7c`，对应 `071977d`。`r35_short` 为 **17/17 Bot、84 集、18 集验证成交、1805 分**，81 条指纹为 `68ad14a55577b3a2`，对应 `bf2ebd2`。移动时限修复 `7f3da7c` 的指纹为 `52296cdba55f2208`，晚于此处两波。 [r32 长波榜单](../../v2-integrated/runs/r32_long/triage-r32_long.md) · [r35 榜单](../../v2-integrated/runs/r35_short/triage-r35_short.md)

- **BUG-060 标 FIXED。** r31→r32 长波 `radius≥4096` 请求 **5→0**，过期 ID **1→1**，以 `a896f86` 的已加载版本通过双门槛。两波的 GLM5 EP06 均以发光告示牌为目标。旧波把半径从 64 连推至 65536，新波两个同地 64m 空扫描后回执要求换区，后续该集半径最高 96。r32 整波最大请求半径为 256，r35 短波过期 ID／超大扫描为 **3/0**。 [r31 倍增](../../v2-integrated/runs/r31_long/glm_glm_5_midgame/20261119-223015-242650.events.jsonl#L137) · [r32 换区提示](../../v2-integrated/runs/r32_long/glm_glm_5_midgame/20261119-231713-963826.events.jsonl#L176) · [r32 过期 ID](../../v2-integrated/runs/r32_long/glm_glm_5_midgame/20261119-231713-963826.events.jsonl#L247) · [扫描回执](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L200)
- **BUG-047 的采石、采木仍超集预算。** r32 长波 p99 **74.430s**，首集最后一次采石反复破块耗时 74.430s，300s 预算超 **40.361s**。r35 HY4_10 EP09 余约 7s 时启动采木，耗时约 90.4s，超预算 **83.504s**。r35 `move_to:busy` 最大 **102.020s**（2 次），两波脱困 max **26.733s／28.008s** 均通过。 [r32 HY4_1 EP01](../../v2-integrated/runs/r32_long/hy4_hy_1_bare/20261119-230752-142336.events.jsonl#L208) · [r35 HY4_10 EP09](../../v2-integrated/runs/r35_short/hy4_hy_10_enchant_anvil/20261119-234654-160737.events.jsonl#L69) · [r35 HY4_7 EP05 busy](../../v2-integrated/runs/r35_short/hy4_hy_7_village/20261119-234400-965591.events.jsonl#L68) · [采木与采石入口](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L2333)
- **BUG-056 单请求门禁原始计 1。** r35 HY4_9 EP04 的热启动集只分到 **1.424s**，单次模型请求越过截止，没有工具或浮水动作。浮水已配对 22 次，最大 15.826s。HY4_9 EP01 的浮水在 `run.finished` 前 2.207s 启动，日志未记录终止回执，时长待判。r32 长波配对 32 次、最大 16.361s，指定 Bot 未看到浮水暴露。 [r35 EP04](../../v2-integrated/runs/r35_short/hy4_hy_9_deep_water/20261119-234719-299605.events.jsonl#L9) · [EP01 最后浮水](../../v2-integrated/runs/r35_short/hy4_hy_9_deep_water/20261119-233220-885224.events.jsonl#L114)
- **BUG-058 下界替代动作失效。** r35 HY4_8 EP03 10 次移动寻路失败，9 次收到重规划提示。仅一次满足近距离 `dig_toward` 提示条件，模型实际尝试东／西三个隧道，均在首步因脚下 `netherrack` 触发 `unsafe_or_unknown_floor`。`tunnel.GROUND` 未纳入该方块。同期北向阶梯能推进，远目标仍失败，44 次请求后目标未验证。 [两次相邻失败](../../v2-integrated/runs/r35_short/hy4_hy_8_nether/20261119-233433-043228.events.jsonl#L290) · [首次隧道拒绝](../../v2-integrated/runs/r35_short/hy4_hy_8_nether/20261119-233433-043228.events.jsonl#L334) · [末次隧道拒绝](../../v2-integrated/runs/r35_short/hy4_hy_8_nether/20261119-233433-043228.events.jsonl#L758) · [地板集合](../../v2-integrated/src/mc_agent/skills/tunnel.py#L11)
  整波 11 条同类拒绝中有 5 条因空气。另 6 条分布于四个 Bot，判定对象为实体下界岩或玄武岩。这六条在独立流体与坠落检查之前就退出。 [GLM1 EP02](../../v2-integrated/runs/r35_short/glm_glm_1_nether_fortress/20261119-234839-285232.events.jsonl#L124) · [GLM2 EP01](../../v2-integrated/runs/r35_short/glm_glm_2_nether_bastion/20261119-233157-510543.events.jsonl#L691) · [HY4_6 EP01](../../v2-integrated/runs/r35_short/hy4_hy_6_nether_bastion/20261119-233212-680176.events.jsonl#L455) · [判定分支](../../v2-integrated/src/mc_agent/skills/tunnel.py#L131)
- **BUG-059 与 042 继续待修验。** r32／r35 回程比分别 **6/2、18/5**。r35 三个深集候选只有 DS_1 EP03 第二次回程未完成，六次耗时 160.700s，Y22 距目标 Y47，最后一次回程被剩余预算门槛拒绝。HY4_9 和 DS_2 均已到地表，然后继续下探。r32 的 MoveTest EP01 由 NJU 502 报废，长波 429/502 为 **1**，HY4_1 仍仅 **20/60**。 [DS_1 EP03 最终拒绝](../../v2-integrated/runs/r35_short/nju_ds_1_iron/20261119-234212-807490.events.jsonl#L565) · [HY4_9 EP02 到地表](../../v2-integrated/runs/r35_short/hy4_hy_9_deep_water/20261119-233521-880028.events.jsonl#L188) · [DS_2 EP04 到地表](../../v2-integrated/runs/r35_short/nju_ds_2_cave_combat/20261119-234249-389646.events.jsonl#L91) · [MoveTest EP01 502](../../v2-integrated/runs/r32_long/nju_movetest_01_bare/20261119-230748-105190.events.jsonl#L12)

## 4ci. r33/r34 长波及 r36–r39 短波收官

持续监听记录了这六波的发现和收官，收齐 **74 条 Bot 轨迹、365 集、78 集验证成交**。自补漏截止点起，已审计 **16 波、205 条 Bot 轨迹、1046 集、224 集验证成交**。这些数字来自各 Bot 的终版 `curriculum_summary`。

| 波次 | Bot | 集数 | 验证成交 | 总分 | Python 指纹 / 对应源码 |
|---|---:|---:|---:|---:|---|
| [r33_long](../../v2-integrated/runs/r33_long/triage-r33_long.md) | 3/3 | 32 | 4 | 190 | `61a60ffe4295c9a1` / 与 `cf4c926` 同 Python 树 |
| [r34_long](../../v2-integrated/runs/r34_long/triage-r34_long.md) | 3/3 | 31 | 7 | 195 | `c5f0a55a57396f34` / `f50eef6` |
| [r36_short](../../v2-integrated/runs/r36_short/triage-r36_short.md) | 17/17 | 80 | 18 | 1895 | `61a60ffe4295c9a1` / `cf4c926` |
| [r37_short](../../v2-integrated/runs/r37_short/triage-r37_short.md) | 17/17 | 79 | 19 | 1905 | `44e212043738b24f` / `63f39e3` |
| [r38_short](../../v2-integrated/runs/r38_short/triage-r38_short.md) | 17/17 | 72 | 16 | 1755 | `c5f0a55a57396f34` / `f50eef6` |
| [r39_short](../../v2-integrated/runs/r39_short/triage-r39_short.md) | 17/17 | 71 | 14 | 1685 | `d83429348cf50b82` / `278749d` |

- **BUG-042 持续尝试门槛得到实证，整票仍开。** r36 DS_2 EP01 的同一请求遇到 5 次 429，累计退避 **83.897s**，第 6 次完成。r33/r34 两个 2700s 长波均 0 次硬 429/502 报废，HY4_1 均 **20/60**。BUG-046 与 BUG-042 的约 4 小时波次仍缺。 [r36 五次退避](../../v2-integrated/runs/r36_short/nju_ds_2_cave_combat/20261119-235444-144417.events.jsonl#L293) · [第六次完成](../../v2-integrated/runs/r36_short/nju_ds_2_cave_combat/20261119-235444-144417.events.jsonl#L330)
- **BUG-047 直接移动已实测止损，剩余超预算。** r35→r36→r37 移动最长 **102.020→85.900→68.483s**，每波受控 `move_timeout` **0→11→17**，r36/r37 各有约 25.1s 的直接移动界限。较远目标进入独立分段路径，仍有 85.900s 和 68.483s 个案。r36/r37 最大超预算 **34.708s／42.180s** 均由约 90.1s 采木拖出。r38/r39 工具 p99 **52.164s／52.129s**，r39 校正工具门禁后通过该项，但 HY4_10 EP07 在集将结束时启动 13.3s 攻击，仍超 **13.057s**。r33→r34 长波 p99 **52.376→51.162s**，最大超时 **12.744→0.096s**，两波无 `busy` 样本。 [r36 受控停止](../../v2-integrated/runs/r36_short/hy4_hy_10_enchant_anvil/20261120-000316-533427.events.jsonl#L197) · [r36 分段移动](../../v2-integrated/runs/r36_short/hy4_hy_6_nether_bastion/20261120-000416-692883.events.jsonl#L40) · [r37 采木](../../v2-integrated/runs/r37_short/hy4_hy_2_iron/20261120-001905-827093.events.jsonl#L324) · [r39 攻击](../../v2-integrated/runs/r39_short/hy4_hy_10_enchant_anvil/20261120-011658-949353.events.jsonl#L147) · [分段入口](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1298)
- **计时门禁修正。** r37 的 7 条未配对是 `tool.skipped` 紧跟调度器生成的完成回执，工具实际没有运行。r39 的 1 条也是该类。门禁现在过滤这些合成完成，真实缺时长仍使计算保留待验。r37 校正后的 p99 为 **52.721s**（923 次），r39 **52.129s**（895 次），均过 60s。 [r37 成对回执](../../v2-integrated/runs/r37_short/hy4_hy_10_enchant_anvil/20261120-002752-006717.events.jsonl#L137) · [调度器](../../v2-integrated/src/mc_agent/harness/scheduler.py#L55) · [门禁夹具](../tests/test_d1_ticket_gates.py)
- **BUG-056/057。** r36/r38 的单请求超时都来自 HY4_9 的波末余量（约 0.5s／1.647s），均没有浮水。r39 的 22 次浮水最大 **8.550s**，目标 Bot 单请求超时为 0。r39 有岩浆块暴露，伤害峰值 **4**、死亡 **0**。GUITest EP03 记录了攻击前脱离成功和攻击拒绝两条回执。BUG-057 标 FIXED。 [r38 波末模型超时](../../v2-integrated/runs/r38_short/hy4_hy_9_deep_water/20261120-005619-706181.events.jsonl#L9) · [r39 浮水](../../v2-integrated/runs/r39_short/hy4_hy_4_cave_combat/20261120-010354-840058.events.jsonl#L148) · [r39 攻击前脱离](../../v2-integrated/runs/r39_short/nju_guitest_01_nether/20261120-010509-472786.events.jsonl#L1118) · [Guardian 入口](../../v2-integrated/src/mc_agent/harness/guardian.py#L666)
- **BUG-058/060。** r36→r39 短波寻路单集峰值 **4、3、5、7**，当前仍超 3 次门槛。r39 HY4_6 EP03 的 7 次失败均为移动，覆盖多个不同坐标。r37 仍有下界岩和黑曜石触发隧道地板拒绝。`r33_long` 和 `r34_long` 的超 64m 请求各 4 次，八次均返回有效半径 64，并无请求达到 4096。 [r39 HY4_6 EP03](../../v2-integrated/runs/r39_short/hy4_hy_6_nether_bastion/20261120-011458-027936.events.jsonl#L817) · [r37 黑曜石误拦](../../v2-integrated/runs/r37_short/nju_guitest_01_nether/20261120-002742-795577.events.jsonl#L199) · [r34 长波实体半径](../../v2-integrated/runs/r34_long/glm_glm_5_midgame/20261120-005000-152082.events.jsonl#L122)
- **BUG-059 有回升，维持 OPEN。** r36 深挖有一集回程未完成，r37 两条候选均无直接回程超时证据。r38 HY4_2 EP02 从 Y−12 六次上升到 Y31、未达到 Y51，随后超时。r39 回程比 **4/6**，≥3 连击 **1 集**，三条初筛候选都已回地表才再次下探，短波门槛通过。r33/r34 长波比例仍为 **8/1、11/4**，r34 唯一候选已回地表后继续采矿。 [r36 NJU_3 EP05](../../v2-integrated/runs/r36_short/nju_nju_3_village_trade/20261120-000635-246748.events.jsonl#L645) · [r38 HY4_2 最后回程](../../v2-integrated/runs/r38_short/hy4_hy_2_iron/20261120-004253-882042.events.jsonl#L411) · [r39 HY4_9 到地表](../../v2-integrated/runs/r39_short/hy4_hy_9_deep_water/20261120-011527-319926.events.jsonl#L114) · [r34 MoveTest 到地表](../../v2-integrated/runs/r34_long/nju_movetest_01_bare/20261120-012034-132914.events.jsonl#L236)

## 4cj. r40_hy4_combat 收官

北京时间 **21:38:59** watcher 通知 **9/9** 收官。终版 **47 集、7 集验证成交、13 集超时、915 分、5 条明确死亡回执**，46 条 `run.started` 指纹为 `fba71088421c00cf`，与 `97595de` 的 Python 树一致。与 r39 同 Bot、同配置时长的九路比较，r39→r40 为 **41→47 集、6→7 集验证成交、925→915 分**。两个波次只用于观察对照。自 r29 起累计 **17 波、214 条 Bot 轨迹、1093 集、231 集验证成交**。 [r40 榜单](../../v2-integrated/runs/r40_hy4_combat/triage-r40_hy4_combat.md) · [r39 榜单](../../v2-integrated/runs/r39_short/triage-r39_short.md) · [r40 源码指纹](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-051821-812289.events.jsonl#L1)

- **主动战斗工具仍缺实机调用。** 全波 `mc_skill_combat` 开始回执 **0**，旧 `mc_attack` 完成 **4**，Guardian `subcortical_combat` 完成 **42**（苦力怕规避 23、掩护或撤退 11、远程盾冲 4、反杀 3、撤退 1）。HY4_4 EP01 在模型尚未发出主动攻击时，Guardian 已完成近战反杀与盾冲；随后成就验证成功。战斗适配器离线 5 项通过，本波验证到的是既有 Guardian 路径，主动战术入口仍需真实回执。 [HY4_4 EP01 近战](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-051821-812289.events.jsonl#L7) · [盾冲](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-051821-812289.events.jsonl#L18) · [验证完成](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-051821-812289.events.jsonl#L45) · [主动工具](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L2395) · [离线测试](../../v2-integrated/tests/test_combat_v1_adapter.py#L125)
- **BUG-047 未过单集截止。** r39→r40 工具 p99 **52.129→51.450s**，脱困最大 **14.016→15.113s**，均达标。HY4_3 EP07 在 `mine_dfs` 已按余量压到约 1s 后切换 `mine_blocks`。采块在集预算剩 **3.579s** 时启动，执行 **59.979s**，最终超预算 **56.406s**。采块入口仍使用自己的时限。两波都没有 `move_to:busy` 暴露。 [收缩的 DFS](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-052644-245586.events.jsonl#L335) · [启动采块](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-052644-245586.events.jsonl#L359) · [结束](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-052644-245586.events.jsonl#L362) · [采块独立时限](../../v2-integrated/src/mc_agent/skills/library.py#L2580)
- **BUG-056/057/060。** 浮水 7 次，p99/max **1.980s**。HY4_9 EP04 的一次单请求超时出现在波末 **0.5s** 窗口，JSONL 中没有浮水动作。BUG-057 单集岩浆块伤害峰值 **5**、死亡 **0**，BUG-060 过期 ID **1**、≥4096 扫描 **0**，均通过。 [HY4_8 浮水](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_8_nether/20261120-051822-193844.events.jsonl#L43) · [HY4_9 波末](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_9_deep_water/20261120-053329-348728.events.jsonl#L9) · [岩浆块伤害](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_6_nether_bastion/20261120-053502-926680.events.jsonl#L438) · [过期 ID](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_7_village/20261120-053428-541841.events.jsonl#L158)
- **BUG-058/059 仍开。** HY4_8 EP04 有 **5** 次寻路失败，整波 **24**。爬升进展/到达 **3/1**。两条深挖超时候选中，HY4_2 EP02 到 Y52 后再次下探，HY4_3 EP08 在短预算和战斗抢占下未完成回程。后者最初提示的井口上界为 Y58，随后回程目标 Y83。恢复较高历史井口可以改变目标，具体选中的井口未写入轨迹。 [HY4_8 五次中的第一条](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_8_nether/20261120-052824-311714.events.jsonl#L53) · [HY4_2 回程完成](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_2_iron/20261120-051923-764129.events.jsonl#L564) · [HY4_3 回程目标](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-053640-739752.events.jsonl#L163) · [恢复井口更新上界](../../v2-integrated/src/mc_agent/skills/mining_dfs.py#L2923)
- **P2-41 脱困只挑东向。** 37 次执行中 **30** 次与 `guardian.unstick_exit_blocked` 配对，涉及 **16 集、7 路**，中位 **1.466s**，合计 **83.425s**。没有立即重试同一工具，三个超时集均有独立耗时原因。Guardian 在执行前正确拒绝当前方向。底层循环无条件选择首个东向，其余方向没有进入安全判定，按 P2 记录可尝试的备选方向。 [HY4_4 EP03 守护拒绝](../../v2-integrated/runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-052020-665582.events.jsonl#L48) · [候选朝向](../../v2-integrated/src/mc_agent/skills/library.py#L5325) · [安全判定](../../v2-integrated/src/mc_agent/harness/guardian.py#L1563)

## 4ck. r41_day1_15bot 收官：第一天目标与实际回执

北京时间 **22:21:42** watcher 通知 **15/15** 收官。全部配置 **1200s**，终版 **73 集、13 集验证成交、22 集超时、205 分、26 条明确死亡回执**。66 条 `run.started` 的指纹均为 `97c83dc1ab33672a`，匹配 `e5cdd31`。自 r29 起累计 **18 波、229 条 Bot 轨迹、1166 集、244 集验证成交**。 [r41 榜单](../../v2-integrated/runs/r41_day1_15bot/triage-r41_day1_15bot.md) · [指纹样本](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_4_bare/20261120-060118-277390.events.jsonl#L1)

- **核心进度。** 石器 **13/30** 集验证，其中 actor **6/23**，零模型预准备 **7/7**，涉及 12 个 Bot。每 Bot 第一次尝试石器，共 **4/15** 验证。铁器 **0/25**，涉及 13 个 Bot，其中 5 集有材料进展。环岛 12 集有 9 集材料进展，该目标未配置可验证成就。HY4_10 EP01 的预准备自己合出石镐，回执仍缺 5 块原木，终版 `answered`、零请求且验证通过。石铁关验收的是实时背包镐等级。 [HY4_10 预准备](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_10_bare/20261120-060123-518953.events.jsonl#L2) · [其终版](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_10_bare/wild-endurance-r41_day1_15bot_hy_10_bare-20261120-060115.json) · [镐等级验证](../../v2-integrated/src/mc_agent/harness/curriculum.py#L222)
- **采木与存箱。** 石器 30 集有 41 条结构化采木回执，单次 actor 采木未达到 **12** 块，最高 **7**，零模型预准备没有完整采木数量。背包快照显示 **7/30** 集持有至少 12 块，含 **4/15** 个石器首集。HY4_9 铁器 EP04 一次真实增加 **16** 块桦木。石器集有 **6** 次木斧合成，没有据点箱合成、放置或存入的确认回执。6 次容器调用均在铁器集，均无成功转移。 [石器最高单次回执](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_5_bare/20261120-060113-362353.events.jsonl#L83) · [该集持有 15 块](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_5_bare/20261120-060113-362353.events.jsonl#L102) · [铁器 16 块](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060915-293692.events.jsonl#L54) · [取箱失败](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_2_bare/20261120-060409-719507.events.jsonl#L23) · [Day 1 任务定义](../../v2-integrated/src/mc_agent/harness/curriculum.py#L436)
- **肉食覆盖。** 环岛三次主动攻击为僵尸、山羊、猪。HY4_9 EP03 的猪攻击前后背包增加 **3** 块生猪排，但攻击回执仍是 `killed=false`。当前背包只有石斧这一种近战武器，实际手持未写明。全波 `mc_skill_combat` 开始 **0**，旧 `mc_attack` 开始 **14**，Guardian 自动战斗完成 **61**。 [HY4_9 看见猪](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060738-900352.events.jsonl#L102) · [攻击前背包](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060738-900352.events.jsonl#L124) · [攻击后背包](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060738-900352.events.jsonl#L145) · [攻击自动选武器](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1760)
- **P2-42 黄昏提示丢失。** r41 的 25 个铁器准备回执为 7 成功、18 失败，后续 actor 任务都没有提示。选择器为黄昏时的铁器目标添加标记，两条重渲染路径却从静态 playbook 重新生成文案。离线按相同参数回放，两条路径都丢失提示。HY4_10 EP04 集内确有黑夜状态，选择时刻的昼夜未存档。 [成功预准备](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_10_bare/20261120-060436-157419.events.jsonl#L2) · [失败预准备](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_10_bare/20261120-061436-382140.events.jsonl#L2) · [集内黑夜](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_10_bare/20261120-061436-382140.events.jsonl#L212) · [选择器添加提示](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1414) · [从静态模板重渲染](../../v2-integrated/src/mc_agent/harness/curriculum.py#L2215)
- **BUG-066 两路裸装轨迹跑偏。** HY4_6、HY4_7 均从通过校验的主世界开局，EP01 取得下界成就。两路首个 actor 工具前已有相邻出场坐标回执，后续心跳直接确认下界，具体触发方式缺失。随后的 **5 集**堡垒或遗迹均未验证，UTC 起止时间共 **2006.324 Bot·s**，占这两路计划时长 **83.6%**。两路均未完成核心 Day 1 验证，205 总分中 **60** 分来自这两条进下界成就。现有 watchdog 和集入口只把下界场景离开下界视作偏离。 [HY4_6 终版](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_6_bare/wild-endurance-r41_day1_15bot_hy_6_bare-20261120-060107.json) · [HY4_7 终版](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_7_bare/wild-endurance-r41_day1_15bot_hy_7_bare-20261120-060109.json) · [HY4_6 心跳](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_6_bare/stdout.log#L38) · [HY4_7 心跳](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_7_bare/stdout.log#L47) · [watchdog 维度判定](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L1180) · [集入口](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L1902)
- **BUG-047 仍失败。** 工具 p99 **90.071s**（821 次），23 个超过 60s 的调用中 20 个是采木。HY4_9 铁器 EP04 已回到地表，剩 **4.829s** 时开始熔炼，执行 **94.349s**，整集超 **89.529s**。这是熔炼的整集截止问题。脱困最大 **18.310s** 过门槛，移动 `busy` 依然缺样本。P2-41 的 `unstick:unsafe_landing` 再出现 **30** 次。 [采木 p99](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_8_bare/20261120-060119-277958.events.jsonl#L133) · [熔炼启动](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060915-293692.events.jsonl#L309) · [整集收尾](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060915-293692.events.jsonl#L312) · [熔炼默认等待](../../v2-integrated/src/mc_agent/skills/library.py#L4247)
- **BUG-026 未过死亡门槛。** 39 条 Guardian 混合事件和 26 条显式死亡分别保留，箭伤致死 **6**，冷却 **140.4s**。HY4_1 共 7 条死亡，EP02 三次撤退被安全落脚判定拒绝，从 11 血降至 4 血后死于箭伤。已有拒绝正确拦截了不安全出口，缺的是当时可用的备选掩体和撤退。 [最后两次拒绝](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_1_bare/20261120-060611-164914.events.jsonl#L220) · [死亡回执](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_1_bare/20261120-060611-164914.events.jsonl#L242)
- **其余门禁。** 0 硬 429/502、0 `runtime_error`。47 条模型重试为 NJU 429 **44**、HY4 502 **2**、HY4 无 HTTP 状态 **1**，无持续六次尝试样本。BUG-056 9 次浮水 p99/max **20.056s**，指定 Bot 未触发该动作。BUG-057 无岩浆块暴露。BUG-058 单集峰值 **7**、整波 **29**，仍失败。BUG-059 回程比 **3/7**，无深挖预算分组暴露，四个有回程的其他超时集都已到地表。BUG-060 过期 ID **2**、超大扫描 **0**，通过。BUG-042/046 仍等待符合长度的波次。 [HY4_2 浮水](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_2_bare/20261120-061214-359454.events.jsonl#L141) · [HY4_7 七次寻路的首条](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_7_bare/20261120-060432-630687.events.jsonl#L45) · [HY4_9 回到地表](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060915-293692.events.jsonl#L293)

## 4cl. r42 Day 1 / Day 2 双收官

北京时间 **22:51:36** 收到 Day 2 收官，**22:51:51** 收到 Day 1 收官。两个波次的 64 条 `run.started` 都是指纹 `8f34d559864e703a`，对应功能提交 `eab741b` 的 Python 树。自 r29 起累计 **20 波、243 条 Bot 轨迹、1230 集、249 集验证成交**。

| 波次 | 终版 Bot | 集数 | 验证成交 | 分数 |
|---|---:|---:|---:|---:|
| [r42_day1_5bot](../../v2-integrated/runs/r42_day1_5bot/triage-r42_day1_5bot.md) | 5/5 裸装 | 22 | 5 | 60，预授 0 |
| [r42_day2_village_9bot](../../v2-integrated/runs/r42_day2_village_9bot/triage-r42_day2_village_9bot.md) | 9/9 铁器 | 42 | 0 | 185，预授 180 |

- **Day 1 同槽对照。** 同五个 Bot、每路配置 1200s，r41→r42 为 **26→22 集、5→5 集验证、40→60 分**。石器 **5/12→5/7**，铁器均 **0/10**，r42 有 6 集铁器材料进展。两条采木回执实际新增至少 12 块，HY4_3 EP01 一次入包 15 块仍因时限返回失败，无成功存箱。两条铁器 actor 任务有黄昏标记，均只有预准备开始、无完成回执，所以没有经历 r41 丢标记的重渲染路径。没有进下界偏离，五槽没有重跑 HY4_6/7。BUG-064 的 `28052f1` 和 BUG-066 的 `5e871e6` 在两波启动后才合入，待下一轮验收。 [采木 15 块](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_3_bare/20261120-063104-637104.events.jsonl#L77) · [另一路采木 13 块](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063056-477918.events.jsonl#L74) · [HY4_1 黄昏任务](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_1_bare/20261120-064250-450553.events.jsonl#L2) · [HY4_3 黄昏任务](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_3_bare/20261120-065103-308280.events.jsonl#L2)
- **Day 2 有派发和实物回执。** 九路全部跑过首轮环岛与寻村，终版为 **9** 集环岛、**16** 集 Day 2 寻村、**17** 集钻石装备。HY4_7 EP02 找到路、农田、箱和床，箱内确实转入 **3 苹果、3 金锭、1 铜马铠**。唯一一次绑床工具返回成功交互，未核实重生点持久。16 个寻村任务均未记录具体当前群系，扫描有村庄方块，无村民实体或干草块回执。原始狩猎工具 9 次报告 5 次击杀，4 路观察到新鲜生肉，且对应回执仍持有预授的 16 块熟牛肉，缺少低食物和斧头狩猎暴露。Day 2 未设置 `advancement_id`，已取箱和床交互的 EP02 两个进展字段均为 false。 [HY4_7 方块](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L78) · [实际取箱](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L170) · [床交互](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L229) · [狩猎回执](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L229) · [Day 2 条件](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1482) · [验证配置](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1326)
- **BUG-047 依场景分化。** Day 1 工具 p99 **91.048s**、整集最长超 **57.027s**，峰值来自采木。Day 2 对应 **51.283s、1.797s**，两项通过且未调用采木。两波脱困最大 **25.034s／24.972s** 通过，移动 `busy` 仍无样本。 [Day 1 p99](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_2_bare/20261120-063537-526458.events.jsonl#L23) · [最大超预算](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_3_bare/20261120-063104-637104.events.jsonl#L79) · [Day 2 p99](../../v2-integrated/runs/r42_day2_village_9bot/nju_ds_1_iron/20261120-064426-706446.events.jsonl#L245)
- **其余门禁。** 两波 0 硬 429/502。Day 1/2 浮水最大 **8.082s／8.460s**，指定 Bot 缺浮水样本，岩浆块未暴露。寻路失败峰值 **2／0**，Day 2 另有 **22** 次整波 `move_timeout`。Day 2 爬升比 **8/3**，三个深挖超时候选中 DS_1 EP03 五次共约 120s 仍差 **17** 格，下一集延续同矿道后成功返回；其余候选已到地表后再下探。两波均 0 超大半径扫描。 [DS_1 末次推进](../../v2-integrated/runs/r42_day2_village_9bot/nju_ds_1_iron/20261120-063526-419884.events.jsonl#L361) · [下一集完成](../../v2-integrated/runs/r42_day2_village_9bot/nju_ds_1_iron/20261120-064426-706446.events.jsonl#L102)

### BUG-070 逐集取证

**HY4_7 EP02 找到了村庄建筑，村民环节没有取证。** 单集 +9.657s，`mc_find_blocks` 在基准坐标 `(-7806, 2822)` 附近扫到 22.9 格外的土路 `(-7804,83,2844)`。+21.505s 到路边 `(-7805.5,83.9,2841.6)`。+24.196s 看见箱、农田和堆肥桶，+44.278s 到箱旁。两次取箱分别因 `take_all` 和 `*` 被拒，从第一条写入尝试到成功用了 45.081s。+107.272s 按物品名取空 `(-7796,78,2862)` 的箱。+137.851s 扫到三处床与另外两只箱，+150.051s 在 `(-7813,70,2876)` 的床交互返回 `ok=true, changed=false`，同一调用记录到一条不可信公屏 `Respawn point set`。+215.219s 半径 64 的干草专扫为 0。三次实体请求传的类型只覆盖敌对、动物、玩家和物品中的子集，没有一次显式查 `villager` 或不传类型，工具会把过滤原样交给后端。+232.539s actor 按照提示汇报收工，后两集派钻石目标。选择器看到村庄 POI 后就不给第二次寻村，目标自身也没有成就验证器。SCENE-002 可在同一已定位村庄逐项查 `villager`、床交互、箱子和后续交易回执。 [土路](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L24) · [到路边](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L48) · [建筑线索](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L78) · [两次拒绝](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L120) · [库存窗口](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L146) · [取箱成功](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L170) · [床型与坐标](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L195) · [公屏权限](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L229) · [空干草扫描](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L266) · [第三次实体过滤](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L286) · [终版进展字段](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_7_iron/wild-endurance-r42_day2_village_9bot_hy_7_iron-20261120-063107.json#L126) · [实体过滤透传](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L884) · [POI 派发门禁](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1493) · [客观验证器](../../v2-integrated/src/mc_agent/harness/curriculum.py#L222)

**黄昏只在派发时拦截。** HY4_5 EP03 的 Guardian 在 +223.594s 已记录 `time_of_day=11191`，超过 `11000` 阈值。actor 继续在 +229.645s 发移动，+233.745s 发扫描，+245.499s 达调用上限。SCENE-002 应覆盖派发后跨过黄昏，分别核对停止推进和转入避险。 [黄昏观测](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L244) · [随后移动](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L266) · [到达调用上限](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L308) · [黄昏阈值](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1370) · [派发判断](../../v2-integrated/src/mc_agent/harness/curriculum.py#L1487)

**四路铁装，8 死与 9 条混合事件。** 每次实际死亡用同一批回执中紧邻死亡、`health=0` 的伤害归因。两次发生在寻村集，六次发生在后续钻石集。表内为单集相对秒数。

| Bot / EP | 混合预警 → 死亡回执 | 致命伤害 |
|---|---:|---|
| HY4_4 EP03 | 8.168 → 12.262 | [箭伤 L30–33](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_4_iron/20261120-063850-632250.events.jsonl#L30) |
| HY4_4 EP04 | 430.603 → 435.238 | [爆炸 L123–126](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_4_iron/20261120-064217-563301.events.jsonl#L123) |
| HY4_6 EP04 | 12.170 → 16.736 | [箭伤 L33–36](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_6_iron/20261120-064356-542885.events.jsonl#L33) |
| HY4_6 EP04 | 51.609 → 55.692 | [箭伤 L119–122](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_6_iron/20261120-064356-542885.events.jsonl#L119) |
| HY4_8 EP04 | 0.985 → 4.538 | [近战 L9–12](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_8_iron/20261120-064415-904634.events.jsonl#L9)，附近有溺尸，最后一击未记录实体 ID |
| HY4_8 EP04 | 72.668 → 76.784 | [爆炸 L46–49](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_8_iron/20261120-064415-904634.events.jsonl#L46) |
| HY4_9 EP02 | 27.491 → 32.325 | [箭伤 L78–81](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_9_iron/20261120-063303-601221.events.jsonl#L78) |
| HY4_9 EP04 | 191.425 → 196.044 | [箭伤 L122–125](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_9_iron/20261120-064121-207531.events.jsonl#L122) |

HY4_4 EP04 +435.255s 多出一条已重生、满血的连接恢复打点。五次箭伤前都有撤退失败。三条 `water_escape_failed` 分别来自 HY4_6 EP02/03、HY4_8 EP02，三集都无显式死亡。BUG-069 的死亡与浮水拆开验收。 [恢复 L131](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_4_iron/20261120-064217-563301.events.jsonl#L131) · [撤退样本](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_6_iron/20261120-064356-542885.events.jsonl#L114) · [浮水 HY4_6 EP02](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_6_iron/20261120-063425-959787.events.jsonl#L93) · [浮水 HY4_6 EP03](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_6_iron/20261120-063929-176223.events.jsonl#L109) · [浮水 HY4_8 EP02](../../v2-integrated/runs/r42_day2_village_9bot/hy4_hy_8_iron/20261120-063411-155076.events.jsonl#L155) · [事件文本](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1073) · [Guardian 混合分组](../../v2-integrated/src/mc_agent/harness/guardian.py#L1002)

**Day 1 MoveTest 三死。** EP04 +59.817s 在 Y27 先遭爆炸降至 1.2 血，随后箭伤致死。同集 +162.028s 在 Y83 由近战致死。EP05 +38.473s 在 Y84 再死于箭伤。 [EP04 箭伤](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064425-812878.events.jsonl#L122) · [EP04 近战](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064425-812878.events.jsonl#L330) · [EP05 箭伤](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064940-063319.events.jsonl#L65)

- **MoveTest 矿层。** EP03 楼梯到 Y52 后，`mine_blocks` 用 34.311s 挖 10 格，部分入包 1 粗铁、1 煤，回执无铁矿块坐标。该集 6 次 `mine_blocks` 共 48.295s，4 次楼梯共 91.823s。开梯遇矿停下后，actor 在 Y50/Y49 主动各挖 5 粗铜，两次共 11.005s。单次铁 DFS 从 Y49 到 Y42 用 27.867s，挖 22 格未入铁。EP04 铁 DFS 又用 24.082s，在 Y34.3 处被抢占，第一次死亡前背包有 1 粗铁、20 粗铜。20 是当时库存，可归到明确手挖回执的为 10。 [楼梯](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063822-913453.events.jsonl#L214) · [采铁回执](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063822-913453.events.jsonl#L244) · [Y50 铜](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063822-913453.events.jsonl#L451) · [Y49 铜](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063822-913453.events.jsonl#L535) · [铁 DFS](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-063822-913453.events.jsonl#L563) · [EP04 抢占](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064425-812878.events.jsonl#L61) · [库存](../../v2-integrated/runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064425-812878.events.jsonl#L98)
- **GUITest 矿层。** EP03 6 次铁 DFS 共 268.985s。首片 Y48→40 用 50.932s，入包 1 粗铁、2 煤。下一片 56.644s 被中止，Y22 附近箭伤致死前库存 1 粗铁、7 粗铜。其后一次熔炼用 93.626s 实取 7 铜锭。EP04 3 次铁 DFS 共 127.507s，覆盖 Y63→40，Y59→48 的一片入包 2 粗铜，铁未增长。两路都没有观测到铁器目标要求的 Y16 下探完成。 [首片](../../v2-integrated/runs/r42_day1_5bot/nju_guitest_01_bare/20261120-063800-887294.events.jsonl#L46) · [中止](../../v2-integrated/runs/r42_day1_5bot/nju_guitest_01_bare/20261120-063800-887294.events.jsonl#L77) · [库存](../../v2-integrated/runs/r42_day1_5bot/nju_guitest_01_bare/20261120-063800-887294.events.jsonl#L88) · [熔铜](../../v2-integrated/runs/r42_day1_5bot/nju_guitest_01_bare/20261120-063800-887294.events.jsonl#L307) · [EP04 铜](../../v2-integrated/runs/r42_day1_5bot/nju_guitest_01_bare/20261120-064818-282561.events.jsonl#L162) · [铁器要求](../../v2-integrated/src/mc_agent/harness/curriculum.py#L454)

## 4cm. r43 前置验收补齐 BUG-064 / BUG-066

- **BUG-064 采木。** 按同一集的 `tool.started` 与 `tool.completed` 配对。r41 终版 15 路有 **20 次 ≥60s**，其中 **9 次 ≥90s 且原木入包为 0**。r42 Day 1 旧代码五路 16 条采木回执中，**8 次 ≥60s、0 次 ≥90s 且零原木**。任一正例会失败。给出通过需 15 路裸装终版都有集、采木有实际配对样本，且整集超预算另核对 BUG-047 的 10s 门槛。集成树 `28052f1` 的四次不可达早停、低处树干优先、部分挖掘保留三个离线场景均通过。 [r41 空转](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_1_bare/20261120-060104-983247.events.jsonl#L19) · [r42 慢采木](../../v2-integrated/runs/r42_day1_5bot/hy4_hy_1_bare/20261120-063326-506457.events.jsonl#L247) · [离线夹具](../../v2-integrated/tests/test_skills_and_providers.py#L313)
- **BUG-066 场景。** r41 仍识别出 HY4_6/7 的 **5 次**提前派发下界目标，首集偶得进下界成就仍算提前派发的背景。r42 五槽观察 0 次，终版无新版 runner 的 `scene.day1=true`。实机复验同时检查 15 槽启用保护、原受影响两槽在场、明确派发传送门前的越界派题为 0。显式传送门集真有成就且被验证时才记录合法跨维度，以防修复同时挡住正常晋级。 [r41 首次提前派发](../../v2-integrated/runs/r41_day1_15bot/hy4_hy_6_bare/20261120-060443-325661.events.jsonl#L1) · [runner 开关](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L1819) · [终版 runner 字段](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L2115) · [D1 验收](../scripts/d1_ticket_gates.py#L422)

## 4cn. r43 收官，近村重生已验证，楼梯仍可穿透时限

北京时间 **23:36:33** 收到 Day 1 收官，**23:37:04** 收到 Day 2 收官。60 条 actor 启动均为 `57a2bd03c94e7a39`，与 `b34df13` 的 Python 树哈希一致。Day 1 另有 3 集零模型预准备验成石器。自 r29 起累计 **22 波、257 个终版槽位、1293 集、256 集验证成交**。 [指纹算法](../../v2-integrated/src/mc_agent/runtime.py#L10) · [Day 1 actor](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L1) · [Day 2 actor](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L1)

| 波次 | 终版槽／开集 | 集数 | 验证成交 | 分数 |
|---|---:|---:|---:|---:|
| [r43_day1_5bot](../../v2-integrated/runs/r43_day1_5bot) | 5/4 | 22 | 4 | 45，预授 0 |
| [r43_day2_village_near_9bot](../../v2-integrated/runs/r43_day2_village_near_9bot) | 9/9 | 41 | 3 | 370，预授 180 |

- **Day 1 完成口径。** 同四路、各 1200s，r42→r43 为 **18→22 集、4→4 集验证、50→45 分**。r43 石器 **4/11**，铁器 **0/8**。四路终版均有 `scene.day1=true` 并通过主世界入口，无提前派下界。GuiTest 在九个局部候选中用尽搜索，有两条传送门检查拒绝，其余七条拒因未保留。扫描只接受明确空数组，日志无法区分发现门与缺少确认。该槽 `not_ready`、0 集；MoveTest 拒第一个候选后成功换点。原 HY4_6/7、15 路暴露和显式过门均待 BUG-066 实机验收。 [零模型石器样本](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072955-495937.json#L1) · [GuiTest 启动](../../v2-integrated/runs/r43_day1_5bot/nju_guitest_01_bare/stdout.log#L1) · [GuiTest 终版](../../v2-integrated/runs/r43_day1_5bot/nju_guitest_01_bare/wild-endurance-r43_day1_5bot_guitest_01_bare-20261120-071614.json#L17) · [MoveTest 选点](../../v2-integrated/runs/r43_day1_5bot/nju_movetest_01_bare/stdout.log#L1) · [落点检查](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L160)
- **BUG-064/067 时限。** Day 1 采木配对 **16→31**，最大 **91.048→45.027s**，≥60s **8→0**，90s 零原木 **0→0**。四路结果达当前实测子门禁，15 路留待复验。五次熔炼最大 **93.626→49.975s**，HY4_2 EP04 两次 `partial_smelt` 实际取出 **4+1** 铁锭。没有调用熔铜，源码中的限制落在 Day 1 目标提示里。0 次硬 429/502；Day 1 零重试、Day 2 三条 429 重试。 [最慢采木](../../v2-integrated/runs/r43_day1_5bot/nju_movetest_01_bare/20261120-071622-437822.events.jsonl#L20) · [熔铁首片](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_2_bare/20261120-073158-377280.events.jsonl#L367) · [再取铁锭](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_2_bare/20261120-073158-377280.events.jsonl#L390) · [铁器提示](../../v2-integrated/src/mc_agent/harness/curriculum.py#L465)
- **SCENE-002 近村与补给。** 九路主世界落点距各自定位中心 **135.85–164.78m**。首集强制寻村共 **9** 次，均 `answered`、用满 **14** 请求、零正式验证。4 路找到村庄线索。全波无取箱、干草入包或交易回执。HY4_6 EP01 扫到 **6** 个干草块和箱子，部分航点到达较高地面后收工；DS_1 EP01 到箱旁约 3.5 格用满请求后收工；HY4_7 接近箱子时 `stalled`。首集 25 次实体扫描只 1 次含村民，未查到村民。8 路转回 Day 1 侦察，DS_1 EP02 查到 4 位村民，位于其原定位中心约 315–326m 之外。 [落点范围下端](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_4_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_4_day2_village_near-20261120-071619.json#L37) · [范围上端](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_6_day2_village_near-20261120-071623.json#L37) · [干草](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-071636-357863.events.jsonl#L255) · [DS_1 到箱旁](../../v2-integrated/runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-071627-037271.events.jsonl#L656) · [HY4_7 箱前停滞](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L310) · [后续村民](../../v2-integrated/runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-071902-718275.events.jsonl#L225) · [场景首集](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L1695)
- **床的持久验证。** HY4_7 EP01 在 `(-7838,70,2873)` 绑定床。EP04 地下死亡后重生在 `(-7837.5,70,2874.5)`，距该床约 **1.58m**，距原落点逾百格。EP05 另放一张黄床完成睡床成就。另两集成交是 HY4_9 EP02、HY4_10 EP04 的钻石装备。三集成交均在寻村首集之后，Day 2 寻村自身仍为 **0/9**。 [首集绑定](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L266) · [地下死亡及床旁重生](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-072929-890490.events.jsonl#L140) · [另放黄床](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-073334-621845.events.jsonl#L169) · [睡床完成](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-073334-621845.events.jsonl#L192)
- **分段移动和实地推进。** Day 2 `move_timeout` 整波 **22/109→5/130**，寻村子集 **20/81→2/65**。r43 首集有 **38** 次 `partial_move` 和 **8** 次 `stalled`；已回执最远水平推进 **139.621m**，距 BUG-067 的 180m 尚差约 40m。r42 是随机野地，r43 是近村场景。BUG-071 的 280m 用随机场景 r44 验收。 [最远推进](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-071636-357863.events.jsonl#L300) · [航点结果](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L1429)
- **BUG-047 / BUG-072。** Day 1/2 工具 p99 **51.305/51.889s**，脱困最大 **11.883/15.301s**，移动 `busy` 无样本。Day 1 的 19 条 actor 集末均在限内，3 条零模型集另记。Day 2 HY4_9 EP04 剩 **8.927s** 仍启动六步上楼梯，单次 **27.269s**，整集超 **18.287s**。已有技能接口可传 deadline，actor 的直走、pillar 和重试应共用同一截止。DS_1 EP03 另有一次 **79.655s** `retreat_shaft`，沿矿道退 3 段、封 9 格，从 Y−17 到 Y−3；撤退验收需覆盖矿道回撤、封堵与地表路线。 [楼梯起点](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-073353-791901.events.jsonl#L306) · [集尾](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-073353-791901.events.jsonl#L309) · [矿道撤退](../../v2-integrated/runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-072028-965473.events.jsonl#L225) · [楼梯入口](../../v2-integrated/src/mc_agent/harness/mc_tools.py#L2604) · [技能接受截止](../../v2-integrated/src/mc_agent/skills/library.py#L6554) · [矿道回撤](../../v2-integrated/src/mc_agent/skills/mining_dfs.py#L3987)
- **BUG-059/068/073。** Day 2 回程比 **8/3→2/2**，连续三次及以上 **2→0 集**。HY4_4 EP03 两片由 Y−6→23→51，第二片返回地表。唯一深挖超时候选 NJU_3 EP04 已在 +87.1s 完成回程，随后五片 `explore` 才超时。榜单 3 路的 **11 条** `unknown_fail` 都有 `preempted_by_reflex=true`，全量配对共 **13 条／5 路**，另外两条来自 DS_1 和 HY4_9。Guardian 有结构化抢占字段，榜单分类器未读，未见同一批有缺少原因的实际探矿失败。 [HY4_4 回程](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_4_day2_village_near/20261120-072005-911070.events.jsonl#L203) · [NJU_3 回程](../../v2-integrated/runs/r43_day2_village_near_9bot/nju_nju_3_day2_village_near/20261120-073021-002663.events.jsonl#L89) · [抢占例](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-072048-411226.events.jsonl#L174) · [Guardian 回执](../../v2-integrated/src/mc_agent/harness/guardian.py#L2060) · [榜单分类](../../v2-integrated/scripts/v2_fleet.py#L422)
- **其余门禁。** Day 1 八死均为 HY4_1 箭伤，Day 2 明确死 **8→2**，两死均近战。同九槽不同场景，浮水失败 **3→0**，Day 1/2 浮水最大 **3.440/3.784s**。指定 Bot 浮水子门槛和岩浆块仍缺暴露。BUG-058 单集峰值 **2/3**、整波 **3/5**，BUG-060 过期 ID **0/1**、超大扫描 **0/0**。BUG-029 Day 2 三路终版圆石深板岩 **293/296/332**，超上限。BUG-042/046 继续等待指定长波。BUG-067/068/069 相关 13 个离线用例通过。 [Day 1 重复箭伤](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L174) · [近村撤到岸](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L171) · [HY4_9 超库存](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_9_day2_village_near-20261120-071629.json#L63)

## 4co. r44 收官，备用落点通过，随机斥候半径待验

北京时间 **00:02:26** Day 1 收官，**00:04:42** Day 2 收官。61 条 actor 启动均为 `4f3f74b6e07e67b5`，与集成提交 `d1d572c` 一致。Day 1 另有 5 集零模型。自 r29 起累计 **24 波、271 个终版槽位、1359 集、266 集验证成交**。 [指纹算法](../../v2-integrated/src/mc_agent/runtime.py#L10) · [Day 1 启动](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L1) · [Day 2 启动](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074208-367761.events.jsonl#L1)

| 波次 | 终版／开集槽 | 集数 | 验证成交 | 分数 |
|---|---:|---:|---:|---:|
| [r44_day1_5bot](../../v2-integrated/runs/r44_day1_5bot) | 5/5 | 28 | 7 | 85，预授 0 |
| [r44_day2_wild_scout_9bot](../../v2-integrated/runs/r44_day2_wild_scout_9bot) | 9/9 | 38 | 3 | 440，预授 180 |

- **Day 1 启动和晋级。** GuiTest 再次拒绝 r43 相同两个候选，随后使用 `c1ba4c3` 的独立西侧备用区，在 `(-23597.5,71,-6400.5)` 验证入场。这次 5 路均有 `scene.day1=true` 且 0 次提前派下界，HY4_6/7、15 路和合法过门未覆盖。r43→r44 **22→28 集、4→7 验证、45→85 分**，r44 石器 **5/10**、铁器 **2/10**，两条铁器来自 HY4_2 EP04 和 HY4_3 EP05。采木 **27** 次，最大 **45.013s**，≥60s 与 90s 零原木均 0。 [GuiTest 换区](../../v2-integrated/runs/r44_day1_5bot/nju_guitest_01_bare/stdout.log#L1) · [备用区域生成](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L172) · [采木峰值](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_2_bare/20261120-074027-502156.events.jsonl#L39)
- **BUG-071 时限与工具往返。** 9 路各一集寻村，九集都用斥候开局、零客观验证。43 次配对中 41 条技能回执，2 次数字字符串进校验即被拒。41 条中位 **19.836s**、最大 **42.151s**，全部低于 50s，离线斥候 13 项通过。30 次实移、90 段，常见停止原因是段数上限 **14**、落水 **9**、围栏 **5**。同九路首轮寻村，r42→r44 直接移动超时 **10/42→0/12**，直接方块/实体扫描 **32/24→7/5**。整波直接移动超时 **22/109→1/75**，唯一一条在 DS_1 首集环岛。 [最长斥候](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074208-367761.events.jsonl#L16) · [数字格式拒绝](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-074143-250000.events.jsonl#L100) · [环岛超时](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074441-268332.events.jsonl#L267)
- **BUG-071 位移。** 总路程 **3059.56m**，6/9 的单集累计路程达 280m。HY4_10 EP02 的 **606.15m** 来自四次斥候沿不同方向累计，距交叉核对的出发点最远 **175.33m**。NJU_3 EP02 累计 **483.10m**，距该集出发点最远 **206.89m**，距场景锚点最远斥候回执 **208.82m**。同一锚点口径 r42 已观察到 **209.45m**。runner 安全航点半径 **212m**（220−8），当前场景无法从落点验到 280m；后续真实位移复验须有精确集起点与适配围栏。 [HY4_10 出发点](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074046-347060.events.jsonl#L120) · [HY4_10 第四次斥候](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074208-367761.events.jsonl#L90) · [NJU_3 出发点](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_nju_3_iron/20261120-074030-208077.events.jsonl#L281) · [NJU_3 最远回执](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_nju_3_iron/20261120-074200-755033.events.jsonl#L219) · [r42 锚点对照](../../v2-integrated/runs/r42_day2_village_9bot/nju_nju_3_iron/20261120-063310-342581.events.jsonl#L409) · [围栏配置](../../v2-integrated/scripts/l3_wild_endurance_runner.py#L2056)
- **寻村闭环。** HY4_7 EP02 两次命中土路，现场发现堆肥桶、讲台、箱和床。其中同坐标的箱子在 r42 已成功取物，本波显式村民查询仅返回动物，整波无取箱、绑床或交易。Day 2 三集验证均为之后的钻石装备目标。DS_1 EP02 在一次斥候调用中传入损坏的方块名 `d食物_path`，随后改正并推进五段。混合扫描保留有效目标的 `2d5040a` 未被本波加载。 [土路](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-074151-880865.events.jsonl#L69) · [村民查询](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-074151-880865.events.jsonl#L146) · [箱床回执](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-074151-880865.events.jsonl#L250) · [损坏名称](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074702-509440.events.jsonl#L164) · [更正后五段](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074702-509440.events.jsonl#L217)
- **BUG-047/072 仍有场景超额。** Day 1/2 工具 p99 **51.115/51.588s**、脱困最大 **1.708/28.037s**，移动 `busy` 无暴露。GuiTest EP05 剩 **2.753s** 时启动上楼梯，50 步钳为 10 步后仍运行 **36.090s**，集尾超 **33.343s**。Day 2 HY4_10 EP03 剩 **8.222s** 时启动矿道撤退，运行 **18.929s**，整集超 **10.713s**。DS_1 EP03 同分支运行 **93.012s**（退 3 路点、封 9 格），比 r43 同 Bot 的 **79.655s** 更慢。楼梯 `76020c6`、撤退 `fa4cd15`/`cfde702` 和抢占分类 `d8bb2fa` 均在 r44 所加载提交之后合入。 [楼梯开始](../../v2-integrated/runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.events.jsonl#L450) · [保存预算](../../v2-integrated/runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.json#L6012) · [Day 2 集末撤退](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074538-967229.events.jsonl#L290) · [最长撤退](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074936-839471.events.jsonl#L168)
- **BUG-056/058/059/060。** Day 1/2 浮水最大 **4.277/8.370s**。HY4_9 的真实浮水暴露使 Day 2 子门槛可验，4 集无仅一请求超时。Day 2 寻路峰值 **4** 超门槛，HY4_5 EP02 三次斥候和一次直接移动均 `path_not_found`。回程比 **1/1**，唯一深挖超时候选 HY4_6 EP03 在 +389.3s 已回到地表，继续探矿后才超时。两波 0 过期 ID、0 超大扫描。D1 对照器按抢占字段识别 Day 2 **16** 次采矿抢占，真实未分类采矿失败 **0**。 [浮水](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-074226-462662.events.jsonl#L89) · [第四个寻路失败](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-074143-250000.events.jsonl#L163) · [回程成功](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-074558-153223.events.jsonl#L178) · [D1 分类](../scripts/d1_compare.py#L93)
- **其余终版门禁。** Day 1/2 显式死亡 **9/3**、混合 **12/3**。Day 1 HY4_1 单路七死（3 箭伤、4 近战）；Day 2 三死分别为爆炸、近战、箭伤。Day 2 浮水失败仍是 0。两波各有两路库存超限。两波硬 429/502 为 0，分别有 4/5 条 429 重试，BUG-042/045/046 仍需对应长程或 GLM 样本，BUG-057 仍无岩浆块暴露。 [Day 1 致命近战](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L171) · [Day 2 致命爆炸](../../v2-integrated/runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-075449-818461.events.jsonl#L340) · [Day 1 圆石](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_3_bare/wild-endurance-r44_day1_5bot_hy_3_bare-20261120-074022.json#L53) · [Day 2 圆石深板岩](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_nju_3_iron/wild-endurance-r44_day2_wild_scout_9bot_nju_3_iron-20261120-074017.json#L52)

## 4cp. 新版楼梯与撤退离线验收，实机仍需分支暴露

在集成树 `500cca5` 上运行新增的楼梯截止、矿道回撤、混合扫描目标和被堵后换钻分支夹具，**16 项全过**。楼梯夹具包括内层挖掘、移动或扫描耗尽截止，9s 集预算、近集末拒启动、pillar 和自动换向共享预算。撤退夹具覆盖 35s 硬限、集时限更短时的预算、慢走和封堵清理。混合扫描保留合法目标。 [楼梯内层](../../v2-integrated/tests/test_skills_and_providers.py#L2018) · [不足时拒绝楼梯](../../v2-integrated/tests/test_skills_and_providers.py#L3083) · [撤退预算](../../v2-integrated/tests/test_skills_and_providers.py#L2941) · [Guardian 撤退](../../v2-integrated/tests/test_generation_guard.py#L4494) · [混合扫描](../../v2-integrated/tests/test_skills_and_providers.py#L2581) · [更换下降分支](../../v2-integrated/tests/test_mining_dfs.py#L3324)

`d1_acceptance.py` 现在单列集末 10s 发起的楼梯，以及 actor 撤退和其中 `retreat_shaft` 子集。仍按单集内 start→completed 配对。r43 Day 2／r44 Day 1 的最后楼梯在集限后 **18.281s／33.337s** 返回，整集收尾数见前两节。r43／r44 Day 2 矿道撤退最长 **79.655s／93.012s**，两波各只有 3 条该分支的实际回执。r44 Day 1 的普通撤退虽通过，矿道未暴露，保留 PENDING。 [门禁实现](../scripts/d1_ticket_gates.py#L388) · [r43 楼梯返回](../../v2-integrated/runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-073353-791901.events.jsonl#L307) · [r44 楼梯返回](../../v2-integrated/runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.events.jsonl#L451) · [r44 矿道撤退](../../v2-integrated/runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074936-839471.events.jsonl#L168)

BUG-073 两个合入后的榜单分类器离线读取 r44 九路轨迹，输出的探矿失败榜没有 `unknown_fail`。独立夹具同时输入同一次 Guardian 抢占的事件与工具回执、一次明确矿道错误、一次确实缺码的失败。两个分类器均只计一次抢占，后两条失败均保留。新进程榜单仍待下一波实机验收。 [Fleet 分类](../../v2-integrated/scripts/v2_fleet.py#L582) · [单 Bot 分类](../../v2-integrated/scripts/v2_triage.py#L112)

## 4cq. BUG-076 裸装 HY4_1 的 15 次显式死亡

r43 的全波 8 死均来自 HY4_1，r44 的 9 死中该槽占 7 死，另两次在 MoveTest。HY4_1 各波 `guardian_totals.incidents` 为 **37／41**，不用于推算死亡。r44 该槽 8 条混合记录中 3 条为满血、已就绪的恢复记录。EP04 与 EP07 另有显式死亡，未打混合标记。表内时间为各集 JSONL 的死亡回执 `elapsed`。用同一回执内 HP=0 的伤害归因，附近敌人只提供现场上下文。 [r43 Fleet](../../v2-integrated/runs/r43_day1_5bot/triage-r43_day1_5bot.md#L9) · [r44 Fleet](../../v2-integrated/runs/r44_day1_5bot/triage-r44_day1_5bot.md#L9) · [恢复回执样本](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L228)

| 波次 / EP | 相对秒 | 致死伤害与坐标 | 退路或掩体证据 |
|---|---:|---|---|
| r43 EP01 | +33.981 | [箭伤 `(-10931.8,63,-10992.8)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L84) | [落脚拒绝，缺封堵材料，之后短退仍中箭](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L47) |
| r43 EP02 | +32.754 | [箭伤 `(-10937.3,63,-11001.6)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071852-494956.events.jsonl#L144) | [落脚拒绝，缺封堵材料，后退约 7 格仍被射中](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071852-494956.events.jsonl#L118) |
| r43 EP03 | +34.730 | [箭伤 `(-10983.3,75,-10984.7)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072151-787723.events.jsonl#L85) | [撤退约 6.8 格仍中箭](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072151-787723.events.jsonl#L41) |
| r43 EP04 | +11.613 | [箭伤 `(-10986.3,64,-11022.9)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L35) | [撤退 7.3 格，之后箭伤致死](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L23) |
| r43 EP04 | +56.945 | [箭伤 `(-10986.7,72,-11010.5)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L174) | [第一次重生后再遭遇，actor 撤退拒绝](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L89) |
| r43 EP05 | +83.907 | [箭伤 `(-10956.3,67,-10982.8)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072638-675955.events.jsonl#L176) | [actor 落脚拒绝，随后缺实墙、短退](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072638-675955.events.jsonl#L146) |
| r43 EP07 | +31.312 | [箭伤 `(-10923.5,65,-10996.6)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-073014-734391.events.jsonl#L106) | [连续没有安全退路](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-073014-734391.events.jsonl#L93) |
| r43 EP07 | +186.475 | [箭伤 `(-10989.9,55,-11055.3)`](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-073014-734391.events.jsonl#L358) | [第二次遭遇中两次 actor 撤退拒绝](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-073014-734391.events.jsonl#L349) |
| r44 EP01 | +194.436 | [箭伤 `(-10933,62,-10981.9)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L268) | [白天见掠夺者，最后两次退路拒绝、位移为 0](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L255) |
| r44 EP04 | +157.478 | [近战 `(-11052.3,70,-11087.6)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L171) | [长采矿中触发抢占，拿到战斗回执时 HP 已为 0](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L168) |
| r44 EP05 | +47.611 | [近战 `(-11019.8,79.2,-11066)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L67) | [曾撤 12.6 格，同一僵尸仍近，下一次撤退已死](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L52) |
| r44 EP05 | +134.917 | [箭伤 `(-10968.5,75,-10975.4)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L223) | [撤 4.7 格仍在危险点，落脚拒绝早约 36.6s](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L209) |
| r44 EP06 | +28.617 | [箭伤 `(-10955.9,64.2,-10979.5)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L48) | [近掠夺者、无安全退路，浮水仍被射死](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L44) |
| r44 EP06 | +73.004 | [近战 `(-10964.7,71,-11006.9)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L104) | [两次掩体缺屋顶，两次只退 1.75／2.02 格](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L98) |
| r44 EP07 | +42.825 | [近战 `(-10932.3,62.4,-11016.7)`](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L55) | [低血量撤进水，浮水及上岸均失败](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L32) |

- **地形与攻击分支。** r43 八死均回到 `(-10977.5,70.1,-11016.5)`，r44 七死均回到 `(-10988.5,69.1,-11017.5)`，两锚点仅差 **11.05m**。r44 EP01、EP06 首死距两处 r43 箭伤点各 **10.97m、3.32m**。r43 EP04 两死相隔 **45.331s**，两次紧急掩体分别因侧边暴露和未着地失败。r44 EP01 与 EP04 的 HUD 仍标白天。17 条 r44 小脑战斗状态均未配副手，所见样本包括空手、木/石镐与护甲分数 0。 [r43 掩体失败](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L37) · [r44 白天掠夺者](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L236) · [EP04 白天且近战](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L168)
- **压缩到三个可复验分支。** r44 有 **9** 个字面 `unsafe_landing` 结果，actor、Guardian 直接撤退和小脑嵌套各 3 个。五死在本集、当次死亡前出现该拒绝，其中只两次落在最后 20s。同槽三次 actor 脱困为二成一次水中抢占。第一类要验证台阶或水边安全拒绝后能否找到可证实的干岸或遮挡，短位移成功后仍要核验风险；第二类复验近敌抢占：EP04 +110.411s 开始采铁煤，至 +149.820s 才开始战斗动作，+157.443s 回执已 HP0、`not_ready`，原始事件批次有七次近战受伤；第三类是无屋顶材料时掩体失败，两次各退约 2 格，随后仍近战致死。D1 为所见链路留下逐条坐标和时间线，Slice B 负责修复。 [7 次伤害与采矿抢占](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L166) · [退到水里，低血量](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L20) · [无屋顶动作](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L98) · [防守入口](../../v2-integrated/src/mc_agent/harness/guardian.py#L391) · [落脚判定](../../v2-integrated/src/mc_agent/skills/combat_env.py#L1458)

### BUG-077 启动历史死亡误触发避难

r43 HY4_1 EP01 在 +1.510s 告警最近死亡 8 次，掩体因 `not_grounded` 失败，实际撤 5.5 格。下一条近敌状态的后端 `last_death.t` 为 **14:36:43.709 UTC**，距告警 **2416.537s**。本集首条显式死亡直到 +33.981s。r44 同槽 EP01 +1.474s 报 9 次，实际撤 6.2 格。仍按上表的显式回执计死亡，启动避难的次数单列。 [r43 启动告警](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L8) · [后端最近死亡时间](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L13) · [本集首死](../../v2-integrated/runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L84) · [r44 启动告警](../../v2-integrated/runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L5)

`GenerationGuard.prime()` 读旧事件只关闭战斗观察，登记死亡照常使用事件 ID，而且未传入原始时间。计数器默认为当前单调时钟，actor 的 60s 窗因此变成到达时间窗。离线夹具给 8 个约 40 分钟前的死亡，历史 cursor 正常推进但近期计数错误地变成 8。使用 `--runxfail` 单测确认了该断言失败。正对照的两条新死亡附带一次重复回放，结果仍为 2。修复应按真实事件时间给历史定龄，保留游标与去重。 [预准备调用](../../v2-integrated/src/mc_agent/harness/curriculum.py#L2194) · [事件入账](../../v2-integrated/src/mc_agent/harness/guardian.py#L465) · [默认计时](../../v2-integrated/src/mc_agent/bot.py#L52) · [避难读取](../../v2-integrated/src/mc_agent/harness/agent.py#L1288) · [离线夹具](../tests/test_d1_repro.py#L95)

## 4cr. r45 六路撤退异常，地下箱造成 29 次地表斥候误停

北京时间 **00:56:13**（Day 2）、**00:56:49**（Day 1）收到两条已校验 Fleet 进程的退出通知。14 槽均有 actor 轨迹，只有 8 槽写出终版，另外 6 槽退出。56 个 actor 启动皆为 `4415809c8c8cdb72`（`8b7d1ad` 与 `eb4a736` 同 Python 树）。自 r29 起累计 **26 波、279 个终版槽位、1398 集、269 集验证成交**。缺终版轨迹、进行中摘要和 Fleet 进程账另行核对。 [Day 1 Fleet](../../v2-integrated/runs/r45_day1_5bot/triage-r45_day1_5bot.md#L3) · [Day 2 Fleet](../../v2-integrated/runs/r45_day2_wild_scout_9bot/triage-r45_day2_wild_scout_9bot.md#L3)

| 波次 | 终版槽 | 终版集数 | 验证成交 | 终版分数 |
|---|---:|---:|---:|---:|
| r45_day1_5bot | 2/5 | 12 | 2（MoveTest） | 25，预授 0 |
| r45_day2_wild_scout_9bot | 6/9 | 27 | 1（HY4_5 钻石装备） | 210，预授 120 |

- **BUG-072 实机回归。** 6 路均启动课程之后在 `mc_skill_flee` 退出。带截止的矿道会话传 `httpx.Timeout` 给 Guardian，Guardian 的标量 `min(timeout_s, remaining)` 比较抛 TypeError，无工具正常完成回执。Day 1 HY4_1 EP04、HY4_2 EP05、HY4_3 EP01，Day 2 HY4_4 EP03、HY4_6 EP03、HY4_7 EP01。D1 门禁现在单列已开集却缺终版的崩溃，两波回放各精确识别 3 路，启动崩溃均 0。新增的相位超时夹具先复现原错误，随后在相位修正后的集成树通过。真实撤退入口和普通箱不早停的相关 5 个参数化用例也通过。r46 的首批指纹 `5f06bad4e03a9ef3` 对应 `b0882ad`，加载后仍需实机查全路径。 [HY4_1](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_1_bare/20261120-084446-500156.events.jsonl#L299) · [HY4_2](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_2_bare/20261120-084904-311884.events.jsonl#L191) · [HY4_3](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L24) · [HY4_4](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_4_iron/20261120-083925-644268.events.jsonl#L57) · [HY4_6](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-084026-929245.events.jsonl#L226) · [HY4_7 回溯](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_7_iron/stdout.log#L82) · [相位夹具](../tests/test_d1_repro.py#L126)
- **BUG-076／077。** HY4_1 在第 4 集中断前有一条显式近战死亡，由于落点距 r44 约 39m 且高 12 格，且两条防护分支均缺暴露，不能验收同槽死亡下降。Day 1 五路中四路在首个模型前出现近期死亡避难，HY4_1、HY4_3、GuiTest、MoveTest 各报 **17／2／3／4**。HY4_3 回执的后端最新死亡比告警早 **1433.112s**，再证 BUG-077。HY4_1 首集掩体于 +61.392s 返回，`held_s=0`，首个模型于 +61.467s 发起。 [HY4_1 显式死亡](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_1_bare/20261120-084446-500156.events.jsonl#L244) · [旧死亡时间](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L3) · [历史误避难](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L8) · [HY4_1 掩体与首请求](../../v2-integrated/runs/r45_day1_5bot/hy4_hy_1_bare/20261120-083545-758409.events.jsonl#L5)
- **BUG-074／075／078。** 匹配六个写完终版的同名槽，r44→r45 涉水、前方液体或围栏早停 **8/26→3/37**，围栏 **5→0**。零位移 **7/26→16/37**，r45 的 16 条全是普通箱误停。另两条中断槽的寻村原件加入现场暴露后，为 **49 次配对、47 条实际技能回执、29 次地下箱独占 `target_found`／5 槽**，其中 **24** 次 0m。HY4_9 同位置只指定村民与铁傀儡后继续 5 段 **134.84m**，仍带箱观察。HY4_10 追 Y46 箱被围栏钳制。没有确认村庄标志、村民、绑床、取箱、采干草或混扫分组的正例，所有 47 条群系为空，也没有数字字符串实机暴露。该关联的代码补丁 `cb69343` 在 r45 之后合入。 [默认箱早停](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L225) · [同位置恢复移动](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L254) · [追地下箱](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-083723-524840.events.jsonl#L177)
- **BUG-071。** 斥候最长 **43.274s**，超过 50s 为 0。DS_1、NJU_3 分别累计 **702.18m／739.51m**，距采样确认的出发点最远 **153.49m／151.12m**。距场景锚点最远证实回执为 **211.12m**，280m 半径仍待验。 [DS_1 位移](../../v2-integrated/runs/r45_day2_wild_scout_9bot/nju_ds_1_iron/20261120-083727-019381.events.jsonl#L557) · [NJU_3 最长调用及位移](../../v2-integrated/runs/r45_day2_wild_scout_9bot/nju_nju_3_iron/20261120-083720-071880.events.jsonl#L385) · [距锚点最远](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-083723-524840.events.jsonl#L229)
- **指定门禁。** 已存终版和快照的部分样本工具 p99 **51.110s／51.490s**，整集最大超额 **0.030s／0.089s**，末 10s 楼梯、移动 `busy` 和完整矿道撤退均无样本。Day 2 一条单请求超时来自 HY4_9 EP05，分到 **0.5s** 后模型超时，轨迹无涉水动作。Day 2 有 **6** 条岩浆块伤害、0 死亡，单集寻路失败 0，过期实体 ID 1。BUG-059 回程比 **4/2**，DS_1 EP04 三片约 117s 从 Y−28 到 Y9，距 Y47 尚差 38 格，末次被余量门禁拒绝，确属未回地表。HY4_8、NJU_3 的两个候选已到地表后继续。HY4_5 EP03 另有一次 502 流错误仍写入终版。BUG-042/045/046 的长程或模型组门槛继续待样本。 [0.5s 集尾](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-085556-898377.events.jsonl#L11) · [岩浆块样本](../../v2-integrated/runs/r45_day2_wild_scout_9bot/nju_ds_1_iron/20261120-083727-019381.events.jsonl#L284) · [DS_1 末次回程推进](../../v2-integrated/runs/r45_day2_wild_scout_9bot/nju_ds_1_iron/20261120-085117-337871.events.jsonl#L218) · [HY4_5 流错误](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-083948-888930.events.jsonl#L166)

## 4cs. r46 两波完整收官，撤退异常与地下箱误停归零

北京时间 **01:21:15／01:21:18** 收到两波 Fleet 退出，升级后的总 watcher 在 **01:21:24** 收到两波全量终版。64 条 actor 启动均为 `5f06bad4e03a9ef3`，对应 `b0882ad`，另有 Day 1 四集零模型。自 r29 起累计 **28 波、293 个终版槽位、1466 集、275 集验证成交**。 [Day 1 Fleet](../../v2-integrated/runs/r46_day1_5bot/triage-r46_day1_5bot.md#L3) · [Day 2 Fleet](../../v2-integrated/runs/r46_day2_wild_scout_9bot/triage-r46_day2_wild_scout_9bot.md#L3)

| 波次 | 终版槽 | 集数 | 验证成交 | 分数 |
|---|---:|---:|---:|---:|
| r46_day1_5bot | 5/5 | 28 | 5 | 70，预授 0 |
| r46_day2_wild_scout_9bot | 9/9 | 40 | 1（NJU_3 钻石装备） | 310，预授 180 |

- **BUG-072／073。** r45 六路在 actor 撤退抛相位类型异常且缺终版，r46 成功写出全部终版。合计 **7** 条 actor 撤退正常取得回执，Day 1／2 最大 **1.815s／3.169s**，0 次类型异常。仍无 `retreat_shaft` 样本，35s 硬限和终集余量的嵌套分支保留待验。同两波 D1 回放有 **5／10** 条真实探矿抢占，未分类的真实探矿失败均 0，Fleet 榜单也无该未分类键。新版 watcher `51c37e0` 已切换，遇新鲜 Fleet 崩溃报告会收官，继续只用终版计集。 [Day 1 撤退](../../v2-integrated/runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L199) · [Day 2 撤退](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-091722-147672.events.jsonl#L117) · [Fleet 兜底](../scripts/d1_overnight_watch.py#L134)
- **BUG-078。** r45 有 **29/33** 默认斥候把孤立普通箱报为命中，包含两条只在原始轨迹可核的寻村集。r46 **0/24**，5 次默认观察到普通箱仍继续移动。HY4_7 EP02 读到 r45 HY4_6 曾反复误停的同一只 `(-7803,33,2795)` 箱，仍实际推进 **72.66m**，而 HY4_6 的真实堆肥桶与讲台仍立即止停。原型 P1 通过此波实机。 [r45 同箱误停](../../v2-integrated/runs/r45_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-083808-891436.events.jsonl#L18) · [r46 同箱继续走](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-090127-093178.events.jsonl#L73) · [真地标](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L20)
- **BUG-071／074。** 6 集实际派发寻村、0 集客观验证，30 次斥候配对有 27 条实际技能回执、26 次有位移、82 段，累计 **2949.04m**。最长 **43.278s**。HY4_7 EP02 累计 **671.65m**，从核对过的集起点最远 **182.09m**，锚点半径本波最大样本 **209.98m**，280m 待验。11 次落水、0 围栏和 0 前方液体。同三路终版有寻村的对照，r45→r46 落水 **2/13→8/16**。实机未传字符串数字，27 条群系均空。 [位移和最长调用](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-090127-093178.events.jsonl#L216) · [锚点极值](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-090128-100471.events.jsonl#L200)
- **BUG-075 到村后的断点。** HY4_6 EP02 找到白床和 Y78 的地表箱，与床交互成功。公屏重生点提示仍是不可信输入，本波无随后重生确认。两次赴箱只传 XZ，回执显示到 Y35.9／39.9，实际在 Y40 发三次容器调用。前两次参数错误，第三次 `no_window`。未出现取物、干草、村民、交易或拥挤混扫保留回执。 [床和箱的真实高度](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L100) · [床交互](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L143) · [第二次错层到达](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L207) · [取物失败](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L295)
- **BUG-076／077。** r46 HY4_1 落点距 r44 约 **4.24m**，7 集仍有 **3** 次箭伤（r43 8 死、r44 7 死）。两次安全拒绝后成功换边。其中 EP02 确认撤离 **2.21m**，约 **5.215s** 后仍被箭伤致死，距 r43 河岸旧死点约 **4.58m**。MoveTest 的低血湿地上岸以重生收尾，未记录起手谓词。3/5 Day 1 槽首个模型前触发避难，12／7／2；HY4_1、HY4_2 已知有一条距告警不足 45s 的真死亡，其余无单独时间戳。历史误算的可定龄证据仍用 r43、r45。 [两次换路的首条](../../v2-integrated/runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L67) · [EP02 致死](../../v2-integrated/runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090239-082942.events.jsonl#L69) · [湿地低血](../../v2-integrated/runs/r46_day1_5bot/nju_movetest_01_bare/20261120-090715-754576.events.jsonl#L266) · [最新已知死亡时间](../../v2-integrated/runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L3)
- **BUG-059 真回程失败 2/3。** Day 2 回程比 **6/1**，一集至少三次连续推进。HY4_7 EP04 先从 Y−9 到 Y21，之后两次同向任务 `climb_out` 的实测坐标却依次为 **Y15、Y−3**，丢失 24 格，目标 Y61，收尾超时。第一轮倒退仍是同一单节点树，第二轮末才出现 24 节点，日志未证明由哪个内部导航分支执行。DS_1 EP04 到 Y30／目标 Y48 后停滞。HY4_10 EP03 四片到达目标容差，继续探矿才超时。 [HY4_7 首次进展](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L293) · [倒退 6 格](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L314) · [再退 18 格](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L340) · [DS_1](../../v2-integrated/runs/r46_day2_wild_scout_9bot/nju_ds_1_iron/20261120-091635-725498.events.jsonl#L226) · [HY4_10 回到地表](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-090533-792285.events.jsonl#L433)
- **其余门禁。** Day 1 石器 **5/9**、铁器 **0/11**。29 次采木均低于 60s，15 路待验。0 次明确传送门派发前往下界，受影响两槽和合法过门样本仍缺。两波工具 p99 **51.911／51.554s**，最大超额 **3.572／0.229s**，未暴露临末楼梯和移动 `busy`。Day 2 浮水最大 **20.275s** 过时限门槛，HY4_9 一次单请求超时只分到 **2.512s**，无涉水或工具，原数量门禁仍失败。Day 1/2 寻路失败峰 **4／2**，Day 1 未过；过期 ID **1／1**，超大扫描均 0。0 硬 429/502，重试 **3／5**。BUG-042/046 仍无 4h 实机样本，BUG-045 无 GLM 暴露，BUG-057 无岩浆块伤害。Day 2 7 次显式死亡有 6 次集中在 HY4_8，最后三集是两次死亡后的断开和一次 `not_loaded`，后者正确降级成 `network_error`。末次冷却 60s 超所余 58s。 [Day 1 路径失败](../../v2-integrated/runs/r46_day1_5bot/hy4_hy_3_bare/20261120-090237-438331.events.jsonl#L314) · [Day 2 波尾模型](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-092014-538120.events.jsonl#L10) · [区块错误](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-091722-147672.events.jsonl#L158) · [死循冷却](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/stdout.log#L300)

## 4ct. 回程净降高门禁与新版离线复验

`d1_acceptance.py` 增加 `climb_out` 起止 Y 对账。即使工具以 `time_slice` 返回，只要结束 Y 低于起始 Y 就单列失败。仅 `return_collar` 的移动不计入。r46 Day 1 为 **0/2**，Day 2 为 **2/12**。两条失败都在 HY4_7 EP04，实际各掉 **6／18** 格。第一片结束在 Y21，随后第一次倒退结束在 Y15，两份回执都还记录现役矿口 `(-7877,-9,2666)`，符合旧版重入后会尝试走回低矿口的条件。无起止 Y 的波次仍待样本，整集真实回程超时继续以逐集追踪裁定。 [门禁](../scripts/d1_ticket_gates.py#L438) · [首次倒退](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L314) · [第二次倒退](../../v2-integrated/runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L340)

集成 `2044a63` 包含 `16df57e`，回地面时跳过不高于当前站位的现役矿口和归档矿口。另有 `50adb01`，剩余爬升超过首片进展且预算较小时提示下一片 55s。现役、归档及续片提示 3 个离线用例通过。`4757c8a` 的退路被拒后只对贴身单个近战进行一次反击，4 个离线用例通过。r46 的 `b0882ad` 尚未包含这些变更，下一次实机需先核对指纹，再查低位矿口和受阻反击是否得到暴露。 [两类矿口夹具](../../v2-integrated/tests/test_mining_dfs.py#L2443) · [续片夹具](../../v2-integrated/tests/test_mining_dfs.py#L2548) · [反击夹具](../../v2-integrated/tests/test_generation_guard.py#L1714)

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
