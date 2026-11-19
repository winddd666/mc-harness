# D1 精炼根因报告 — `r11_22bot` 22 路首飞（2026-11-19）

> 数据源：`.worktrees/v2-integrated/runs/r11_22bot/`（22 bots，121 runs，1230 次 `tool.completed`，158 次 `guardian.incident`）
> 工具：`scripts/d1_triage.py`（本 worktree，纯只读解析，替代当前字段失配的 `v2_fleet.py triage`）
> 复现命令：`/home/xzq/projects/mc/.venv/bin/python scripts/d1_triage.py --wave r11_22bot --top 20`

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

`scripts/d1_triage.py` 已按真实结构实现，可直接抄逻辑或由 D2 合入。

---

## 1. 总览（真实数字）

- 工具失败率 **344 / 1230 = 28.0%**
- `mc_skill_mine_dfs`：**161 次调用，中位 249.7s，最大 655.0s**；显式声明 `max_duration_s` 的 106 次里有 **86 次超过预算 2 倍**（预算常见 32s/60s/90s，代码硬上限 90s）
- `mc_skill_staircase`：91 次调用中 **71 次 > 60s**，最大 376s（steps=10 只走了 3 步）
- `model.failed(cancelled)`：**128 次 / 13 bots**，其中 114 次紧邻 `guardian.incident`
- 跨模型（NJU / GLM-5.3 / DeepSeek-v4.1 / HY4）共现才算 P1；单模型独有但属硬契约违反的也保留

---

## 2. P0/P1 工单（按严重度）

### 【P0】BUG-002 传送门已点燃但 `enter=true` 永远进不去
- **影响**：`nju_guitest_01_nether` 连续 **15 次失败 / 540s**，下界链路整体不可用（比赛模型 NJU）
- **证据（events）**：`20261119-023818-175064.events.jsonl`
  - `build_portal({"enter":true,"wait_s":8})` → `{"ok":false,"portal_origin":[6409,65,-6382],"placed_obsidian":10,"portal_ignited":true,"entered_nether":false,"dimension":"overworld"}` ×15
  - 之间穿插 `move_to(x=6409,y=67..70,z=-6382,range=0.0/0.1)` → `stalled`，`dist_left` 在 **0.7 / 1.6 / 2.6** 之间反复
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
  2. 每轮 `status.pos` 与 `find_blocks(nether_portal,radius=2)` 校验 **feet/head 位于 portal 方块**；
  3. 失败 3 轮返回 `portal_enter_blocked` + `next_action`，并清理 `bz-1`、`bz-2` 与门框两侧 1 格，而不是只清两格
- **对照成功样本**：`hy4_hy_8_nether` 同一工具 `portal_origin y=66` 一次成功，说明是地形/接近面问题，不是协议问题

### 【P0，强烈建议升级】BUG-003 `mine_dfs`/`climb_out` 超时穿透 8~20 倍
- **影响**：**20 bots**；`mine_dfs` 106 次带预算调用中 **86 次 > 2×预算**，最大 **655.0s**（声明 32s）
- **证据**：
  - `hy4_hy_9_deep_water`：`climb_out max_duration_s=32` → elapsed **654.98s**，`stop_reason=surface_reached`
  - `glm_glm_5_midgame`：5 次 `explore max_duration_s=60` → 242.9 / 312.6 / 383.9 / 454.2 / 530.1 / 600.7s
  - `hy4_hy_1_bare`：5 次 `explore max_duration_s=90` → 242/336/436/529/624s
- **源码定位**：
  - `src/mc_agent/skills/mining_dfs.py:4638` 预算被夹到 `[5,90]`
  - `mining_dfs.py:4559-4560` climb 预算 `min(120, max(max_duration_s, 90))`
  - `mining_dfs.py:2808-2818` deadline 只在**每次 `dig_staircase` 之前**检查；单次 `dig_staircase` 自身可达 376s（见 BUG-004），所以预算无法约束墙钟
  - `mining_dfs.py:5198-5231` 分支级 `time_slice` 也只在子步骤结束后才判定，`time_slice_progress` 被当作成功返回（119 次）
- **根因**：预算是"协作式"的，只在下钻一层循环检查；底层 `dig_staircase`/`navigate` 不接收也不遵守 `deadline`，导致一个子步骤吃掉全部预算
- **最小修复**：把 `deadline` 透传进 `dig_staircase`/`tunnel`/`_navigate_to_node`，在每个 step 之间检查；`climb_out` 超预算后强制返回 `ok:false, requires_replan:true, stop_reason:"time_slice"`，禁止把 `time_slice_progress` 当成功

### 【P1】BUG-004 `dig_staircase` 单步延迟爆炸（71/91 次 > 60s）
- **影响**：6+ bots，含 `nju_nju_3_village`、`ds_ds_2_iron`、`glm_glm_5_midgame`
- **证据**：`staircase(steps=10)` elapsed **376.3s / steps_done=3 / time_budget**；`steps=6` 373.9s completed；`steps=10` 298.2s / 232.8s / 203.0s
- **源码定位**：`src/mc_agent/skills/library.py:6066-6076`（stop_reason 归一）+ `5480-5500`、`5930-6000`（每步 `movement_not_confirmed` 重试）；`movement_not_confirmed` 9 次、`missing_step_support` 7 次仍在同一调用内反复重试
- **根因**：单步失败后的"换方向/垫脚/重试"没有单步墙钟上限，steps=10 最坏会串行尝试 4 方向 × 多轮
- **最小修复**：为 `dig_staircase` 增加 `deadline` 与 `per_step_timeout`（建议 8~12s），超时立即返回结构化 `stop_reason` 交回模型，不再内部穷举

### 【P1】BUG-006 `mc_find_blocks` 一个坏名字毁掉整批查询
- **影响**：**22 次 / 9 bots / 4 模型**
- **证据**：`types:["log","planks","bed","wool"]` → `bad_args: unknown block log`；`["shield"]`→unknown block shield；`["flowing_water"]`、`["nether_rack"]`、`["cave"]`、`["leather"]`、`["sheep"]` 等同类
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
  - `hy4_hy_6_midgame` 连续 236.9s / 497.4s 两次等同一个必失败合成
- **源码定位**：
  - `src/mc_agent/skills/crafting.py:10-22` 预检只有 7 个配方（铁/钻/工作台），**没有 stone_* / shield / 石砖系列**
  - `src/mc_agent/skills/library.py:2826-2837` 预检未命中即放行
  - `experiments/.../backend_patched/src/session.js:2215-2219` 服务端把"配方请求被拒"统一包装成 `missing_ingredients: could not craft X: server placed no result`
- **根因**：预检 recipes 表是手写子集；未覆盖的配方把"缺料"伪装成"服务器异常"，模型无法据此换目标，只能重试
- **最小修复**：把预检改为读真实配方（`itemsByName` 已由后端提供）或至少覆盖常用工具/盾牌/石砖；错误信息区分 `missing_ingredients(missing={...})` 与 `server_recipe_rejected`，后者不提示"重试"

### 【P1】BUG-008 桶交互成功却被判 `interact_no_effect`（假阴性）
- **影响**：4 bots / 10 次；关键一条实际已成功
- **证据**：`hy4_hy_7_village` `mc_interact(item=bucket, lava)` →
  `{"ok":false,"result":{"before":"lava","after":"lava","changed":false,"used":{"bucket":1},"gained":{"lava_bucket":1}},"error":{"code":"interact_no_effect"}}`
- **源码定位**：`src/mc_agent/harness/mc_tools.py:1158-1171`——只要 `changed is False` 就强改 `ok=false`，**完全忽略 `used`/`gained`**
- **根因**：用"方块是否变化"作为唯一成功判据；桶装流体时方块可能被同 tick 回填/读取到旧快照
- **最小修复**：仅当 `changed is False` **且** `not result.used` **且** `not result.gained` 时才判失败；否则保留 `ok=true` 并附 `verified_by:"inventory_delta"`

### 【P1】BUG-009 取消风暴：128 次 `model.failed(cancelled)` / 13 bots
- **影响**：全模型；`glm_glm_4_enchant_anvil` 24 次、`hy4_hy_2_bare` 18 次、`glm_glm_3_village` 14 次
- **证据**：114/128 紧邻 `guardian.incident`（`pre_hurt_melee` 30、`pre_hurt_creeper` 15、`pre_hurt_ranged` 16、`fresh_hurt` 49）；单次丢弃已生成 token 后重新请求
- **源码定位**：`harness/guardian.py` 抢占路径 → 取消在途生成；触发面 `guardian.py:246` `_detect_pre_hurt_trigger`
- **根因**：本身是设计选择，但被 BUG-003/004 的长工具（>4min）放大：工具越久，期间被抢占概率越高，已花的生成 token 全部作废
- **最小修复**：优先修 BUG-003/004；同时给 `pre_hurt_*` 增加 2~3s 去抖/同目标冷却，避免同一 creeper 反复触发取消

### 【P1】BUG-010 复活瞬间 `eat` 必然失败并二次致死
- **影响**：`hy4_hy_10_enchant_anvil`（同一死亡链）
- **证据**：`guardian.action.completed eat → {"ok":false,"error":{"code":"not_ready","message":"world is still loading (after login, respawn or dimension change)"}}`（94.8s），紧接着 `wrong_server`（111.2s），随后 `player_explosion` 死亡
- **源码定位**：`src/mc_agent/harness/guardian.py:573-581` `_do_eat` 直接 `use_item`，未检查 `status.ready` / 刚 `death_or_connection_changed`
- **最小修复**：`deaths or not status.ready` 时跳过进食动作并标记 `retry_after_ready`；`guardian` 轮询到 `ready` 再补一次，不把 `not_ready` 计入动作失败

### 【P1】BUG-011 生存反射里的瞬态网络抖动会打死整集
- **影响**：`glm_glm_3_village` EP5、`hy4_hy_8_nether` EP7 → `status=runtime_error`，`score_delta=0`；全队 5 次 `ReadTimeout`/`ReadError`
- **证据**：episode report `error="生存响应 subcortical_combat 的网络结果不确定：ReadTimeout"`（`../20261119-024632-126478.json`）
- **源码定位**：`harness/guardian.py:195-196` 把 `httpx.HTTPError` 包成 `RuntimeError`；`harness/agent.py:1614-1620` 原生 `httpx.HTTPError` 会判为可继续的 `network_error`，但被包装后的 `RuntimeError` 落入 `runtime_error` **终止整集**
- **最小修复**：`_action` 捕获后返回 `ok:false, error:{code:"network_uncertain"}`（或退避重试），不得抛 `RuntimeError`

### 【P1】BUG-012 `not_loaded` 被当致命错误，整集报废
- **影响**：`hy4_hy_2_bare` EP5 `bootstrap_iron_age`（`actor_requests=20`，`score_delta=0`）
- **证据**：episode report `error="[not_loaded] chunk not loaded"`
- **源码定位**：`harness/agent.py:209 _API_RETRYABLE_CODES` 不含 `not_loaded`；`agent.py:1618-1620` 把 `ApiError` 一律判 `runtime_error`；后端 `session.js:1367/1866/1917` 在区块未加载时返回 `not_loaded`
- **最小修复**：`not_loaded`/`chunk_not_loaded`/`world_not_ready` 纳入短退避重试（0.5~2s，最多 2~3 次），仍失败降级 `network_error`

### 【P1】BUG-001（已在册）启动崩溃：确认- `glm_glm_1_nether_fortress/stdout.log:11` `NameError: name 're' is not defined`，抛点 `scripts/l3_wild_endurance_runner.py:167`（`re.search`，文件缺 `import re`）；`glm_glm_2_nether_bastion`、`ds_ds_4_nether_fortress` 同因空跑
- 这是 **3/22 路完全无数据** 的唯一原因，优先级应高于其它表现层 Bug

---

## 3. P2 Backlog（不打断 P0/P1，已同步到 BUG_BOARD）

| # | 现象 | 证据/次数 | 归档理由 |
|---|---|---|---|
| P2-1 | `mc_move_to` 微距 `range=0.1` 反复 `movement_not_confirmed`（"已在 0.1m 内"） | 36 次 / 10 bots | 提示词/参数规范问题，非硬 Bug |
| P2-2 | `mc_move_to:path_not_found` 59 次 / 11 bots，短距失败后无自动 unstick | 59 次，15 次显式传了 `y` | 策略层：应引导模型先 `unstick`；KB 已有规则，需加强 |
| P2-3 | `mine_dfs(action="reset")` 空树时返回 `nothing_to_reset` 并继续劝重置 | 3 次 | 契约可用，只是提示冗余 |
| P2-4 | 对无配方物品调用 `craft_gear`（`wheat_seeds`、`shield` 缺料） | `no_recipe` 3 次 | 模型策略；可由 KB 提示 |
| P2-5 | `void_geofence_blocked`：模型朝 `(0,0)` 世界原点移动 | 4 次 / 2 bots | 提示词；地理围栏已正确拦截 |
| P2-6 | `mc_place` 连续 `no_support`/`blocked_by_self`/`place_failed(仍为 sugar_cane)` | 3 次 | 可加"先 dig 目标格"提示 |
| P2-7 | `attack_zero_hits` 对 45m 外的 glow_squid 空挥 20s | 3 次 | 策略：不可达目标应放弃 |
| P2-8 | 舰队共岛互伤：`player_explosion`/`spear` 致死 | `hy4_hy_10` 4 死、`hy4_hy_8` 3 死 | 场景/编队间距问题，非 Harness Bug |
| P2-9 | `find_blocks(water,radius=96)` 返回 `total:10000` | 1 次 | 数据量/性能小优化 |
| P2-10 | `out_of_reach_vertical` 桶交互（y 差 6.8/9.0） | 3 次 | 已有可执行提示，属策略 |
| P2-11 | `mine_dfs` 无目标时 `quota_reached/shaft_exhausted` 仍返回 `ok=true` | 10 次 | 语义优化：`ok` 与 `goal_met` 分离 |

---

## 4. 交接建议（省额度）

- **Slice A（#3）**：BUG-003 + BUG-004（deadline 透传，P0 级）
- **Slice B（#2）**：BUG-009 去抖、BUG-010 复活进食、BUG-011 反射网络抖动不可致命
- **Slice C（#4）**：BUG-002 进传送门、BUG-006 别名、BUG-007 配方预检、BUG-008 桶假阴性
- **Slice D2（#6）**：BUG-001 的 `import re` + `v2_fleet.py triage` 字段对齐（可直接参考 `scripts/d1_triage.py`）
- **Integrator（#1）**：BUG-012 `not_loaded` 可重试化（`agent.py` 未分配 Slice）
- 其余 P2 已归档，不需要任何人现在放下 P0/P1
