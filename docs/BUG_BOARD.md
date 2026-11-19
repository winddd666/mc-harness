# `v2-integrated` 6-Session 共享 P0/P1 Bug 看板 (`BUG_BOARD.md`)

> **唯一主线**：`/home/xzq/projects/mc/.worktrees/v2-integrated`（分支 `feat/v2-integrated`）
> **协作铁律**：
> 1. **只修 P0/P1 硬 Bug**（崩溃、死锁、取消风暴、超时穿透、工具契约/方块/GUI/跨维度硬故障），**不动课程先后顺序与分数权重等策略项**。
> 2. **RCON / 线上 Bot 单一入口**：只有 **#1 主 AGY** 操作 `nju-beast` 与 `v2_fleet.py launch`；其余 5 个会话全程纯离线工作（只读 `runs/` + 跑离线 `pytest`）。
> 3. **严禁跨文件修改**：每个会话只在自己的 Worktree 里改自己 Slice 内的文件，禁止运行 `uv pip install -e .` 污染公共 `.venv`。
> 4. **离线测试命令**：
>    `PYTHONPATH=<你的worktree>/src /home/xzq/projects/mc/.venv/bin/pytest <你的worktree>/tests -q`

---

## 角色与 Worktree 映射

| 会话 | Slice | Worktree 路径 | 分支 | 独占文件范围 |
|---|---|---|---|---|
| **#1 主 AGY** | Integrator | `/home/xzq/projects/mc/.worktrees/v2-integrated` | `feat/v2-integrated` | 总控、合入、RCON、`v2_fleet.py launch` |
| **#2 子 AGY** | Slice B | `/home/xzq/projects/mc/.worktrees/s-combat` | `fix/s-combat` | `harness/guardian.py`, `skills/combat/`, `skills/safety.py`, `tests/test_generation_guard.py` |
| **#3 Codex #1** | Slice A | `/home/xzq/projects/mc/.worktrees/s-mining` | `fix/s-mining` | `skills/mining_dfs.py`, `skills/tunnel.py`, `tests/test_mining_dfs.py` |
| **#4 Codex #2** | Slice C | `/home/xzq/projects/mc/.worktrees/s-skills` | `fix/s-skills` | `harness/mc_tools.py`, `skills/library.py`, `tests/test_skills_and_providers.py` |
| **#5 DeepSeek #1** | Slice D1 | `/home/xzq/projects/mc/.worktrees/s-triage` | `fix/s-triage` | 只读 `runs/`，输出根因/行号/最小失败单测至本看板 |
| **#6 DeepSeek #2** | Slice D2 | `/home/xzq/projects/mc/.worktrees/s-scenarios` | `fix/s-scenarios` | `scripts/l3_wild_endurance_runner.py`, `scripts/v2_fleet.py`, `kb/data_1_21_11.py` |

---

## 当前待处理 P0/P1 工单（来源：`r11_22bot` 22 路首飞，2026-11-19）

### BUG-001 | P0 | Slice D2（#6 DeepSeek #2）| 状态: FIXED (待 r12_22bot 实机验收，提交 `249bf1e` / `92163b0` / `26c3795`)
- **跨模型影响**：`3 bots` 启动崩溃（`glm_glm_1_nether_fortress`, `glm_glm_2_nether_bastion`, `ds_ds_4_nether_fortress`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/glm_glm_1_nether_fortress/stdout.log` (`NameError: name 're' is not defined` at `l3_wild_endurance_runner.py:167`)
- **现象与根因**：`select_nether_structure_site` 使用了 `re.search` 但未导入 `re`（抛点 `scripts/l3_wild_endurance_runner.py:167`，已由 D1 在 `glm_glm_1_nether_fortress/stdout.log:11` 复核）；同时 `nether_fortress`/`nether_bastion` 缺下界死亡自动回场补装逻辑，且 `v2_fleet.py triage` 解析 `wild-endurance-*.json` 分数/错误字段未对齐 `v2_triage.py`。
- **D1 字段对账**（`v2_fleet.py`）：`tool.finished`→应为 `tool.completed` 且 `ok` 在 `result` JSON 字符串内（:227/:243）；`final_score` 在 `curriculum_summary` 下（:208）；`episodes` 亦在 `curriculum_summary` 下（:211）。参考实现：`/home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_triage.py`。
- **验收标准**：补 `import re`、补下界死亡回场、`v2_fleet.py triage --wave r11_22bot` 能准确打印 22 路分数与 TopErr 榜单，通过 `py_compile` 与离线 `pytest`。

### BUG-002 | P0 | Slice C（#4 Codex #2）| 状态: FIXED (待 r12_22bot 实机验收，提交 `7d4a8ee` / `ef6a8ac`)
- **跨模型影响**：`1 bot / 15 次连续失败`（比赛模型 `nju_guitest_01_nether` EP02：`mc_skill_build_portal:unknown_fail×13` + `repeated_identical_failure_blocked×2`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/nju_guitest_01_nether/stdout.log`
- **现象与根因**：`build_nether_portal` 在特定地形/障碍下连续 13 次失败且未返回清晰可恢复错误码。
- **D1 精确根因（已定位到行）**：15 次连续失败全部为 `portal_ignited:true / entered_nether:false`（4x5 门在 `[6409,65,-6382]` 已建成点燃），期间 `move_to(y=67..70,range=0.0/0.1)` 反复 `stalled dist_left=0.7/1.6/2.6`，轨迹 `y=65→70.1（爬到门框顶）→64（unstick）`，**从未进入 y=66..68 门内**。代码 `src/mc_agent/skills/library.py:6803-6854 _walk_into_portal`：只发一次 `move_to(range=0.0)` 后仅轮询 `dimension`，从不校验实体是否位于 `nether_portal` 方块内；清障（:6966-6971）只清门内+门前两格，未清门框两侧/上方接近面，寻路绕顶后永久失败；`reused_existing_portal` 分支复用同一不可达门形成 540s 死循环。对照 `hy4_hy_8_nether` 同工具 `origin y=66` 一次成功。
- **建议最小修复**：`_walk_into_portal` 改为「走到门前 1 格 → `mc_control(forward+jump,~600ms)` 原子跨入 → 每轮校验 feet/head 所在方块为 `nether_portal`」，3 轮失败返回 `portal_enter_blocked`；清障扩展到 `bz-2` 与门框两侧各 1 格。
- **验收标准**：定位精确失败点（放置/点火/步入/地形清障）并修复 `library.py:build_nether_portal`，补离线回归单测并跑通全量 `pytest`。

### BUG-003 | P1（D1 建议升 P0：属"超时穿透"）| Slice A（#3 Codex #1）| 状态: OPEN
- **跨模型影响**：`20 bots`；`mine_dfs` 161 次调用中，显式声明 `max_duration_s` 的 106 次里 **86 次超过预算 2 倍**，中位 **249.7s**，最大 **655.0s**（预算 32s）
- **日志证据**：`hy4_hy_9_deep_water` → `climb_out max_duration_s=32` 实测 **654.98s**；`glm_glm_5_midgame` → `explore max_duration_s=60` 实测 242.9/312.6/383.9/454.2/530.1/600.7s；`hy4_hy_1_bare` → `max_duration_s=90` 实测 242/336/436/529/624s
- **源码定位**：`skills/mining_dfs.py:4638`（预算夹到 5~90s）、`:4559-4560`（climb 预算 90~120s）、`:2808-2818`（deadline 仅在每次 `dig_staircase` 之前检查，而单次 `dig_staircase` 可达 376s）、`:5198-5231`（`time_slice` 只在子步骤结束后判定，`time_slice_progress` 被当成功返回 119 次）
- **现象与根因**：预算是协作式的，底层 `dig_staircase`/`tunnel`/`_navigate_to_node` 不接收 `deadline`，单个子步骤即可吃掉全部预算；`climb_incomplete` 反复重试进一步放大。
- **验收标准**：把 `deadline` 透传进 `dig_staircase`/`tunnel`/`_navigate_to_node` 并逐 step 检查；超预算必须返回 `ok:false, requires_replan:true, stop_reason:"time_slice"`；补离线回归单测并跑通全量 `pytest`。

### BUG-004 | P1 | Slice C（#4 Codex #2）| 状态: FIXED (待 r12_22bot 实机验收，提交 `7d4a8ee` / `2626e4e`)
- **跨模型影响**：`6+ bots`；91 次 `mc_skill_staircase` 中 **71 次 > 60s**，最大 **376.3s**（steps=10 仅走 3 步 / `time_budget`）
- **日志证据**：`nju_nju_3_village` `staircase(steps=10)` 376.3s `steps_done=3`；`hy4_hy_6_midgame` `steps=6` 373.9s；`steps=10` 298.2s / 232.8s / 203.0s；`movement_not_confirmed×9`、`missing_step_support×7` 均在同一调用内串行重试
- **源码定位**：`skills/library.py:5480-5500`、`:5930-6000`（单步失败重试）、`:6066-6076`（stop_reason 归一）
- **现象与根因**：单步失败后的"换方向/垫脚/重试"没有单步墙钟上限，最坏 4 方向 × 多轮串行。
- **验收标准**：为 `dig_staircase` 增加 `deadline` 与 `per_step_timeout`（建议 8~12s），超时立即结构化返回；补离线单测并跑通全量 `pytest`。

### BUG-005 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：`4 bots` 高死亡/高频抢占（`hy4_hy_2_bare` 5 死、`hy4_hy_10_enchant_anvil` 4 死、`hy4_hy_8_nether` 3 死、`glm_glm_3_village` `guardian=19/5preempt`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_2_bare/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_10_enchant_anvil/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/glm_glm_3_village/stdout.log`
- **现象与根因**：排查上述 4 条轨迹中的连续死亡原因与 `guardian.py` 高频反射未脱险问题。
- **D1 证据补强**：死亡归因 `hy4_hy_2_bare` = `falling_stalactite`×1 + `mob_attack`×2 + 复活后被围；`hy4_hy_10_enchant_anvil` = `player_explosion`/`spear`（舰队共岛互伤，见 P2-8）+ 复活瞬间进食失败；`hy4_hy_8_nether` = `player_explosion`×2。全舰队 128 次 `model.failed(cancelled)` 中 114 次紧邻 `guardian.incident`，其中 `pre_hurt_melee 30 / pre_hurt_ranged 16 / pre_hurt_creeper 15`，取消风暴与 BUG-003/004 的超长工具有强相关。
- **验收标准**：修复对应 `guardian.py` / `combat/` / `safety.py` 生存反射硬漏洞，补离线单测并跑通全量 `pytest`。

### BUG-006 | P1 | Slice C（#4 Codex #2）| 状态: OPEN
- **跨模型影响**：**22 次 / 9 bots / 4 模型**（`hy4_hy_2_bare`、`hy4_hy_5_midgame`、`hy4_hy_7_village`、`hy4_hy_8_nether`、`nju_guitest_01_nether`、`ds_ds_3_village`…）
- **日志证据**：`find_blocks(types:["log","planks","bed","wool"])` → `bad_args: unknown block log`；`["shield"]`、`["flowing_water"]`、`["nether_rack"]`、`["cave"]`、`["leather"]`、`["sheep"]` 同类整批失败（22 次）
- **源码定位**：`harness/mc_tools.py:48-59 _normalize_find_block_types` 仅处理 `bed`，其余原样下发；`experiments/20261115-until-1900/backend_patched/src/session.js:1773` 任一未知名 `fail('bad_args')`
- **现象与根因**：别名表过窄 + 后端 fail-fast，模型把实体/物品名混入方块查询时整次调用归零（高频别名 `log` 都缺失）。
- **验收标准**：补别名（`log→*_log`、`planks→*_planks`、`grass→short/tall_grass`、`portal→nether_portal`、`nether_rack→netherrack`、`cave→cave_air`）；无法识别者剔除并继续，返回 `ignored_types`；全部失败才报错并给可执行示例；补离线单测。

### BUG-007 | P1 | Slice C（#4 Codex #2）| 状态: OPEN
- **跨模型影响**：**17 次 / 10 bots**（`hy4_hy_2_bare`、`hy4_hy_6_midgame`、`nju_guitest_01_nether`、`hy4_hy_7_village`…）
- **日志证据**：`craft_gear(stone_pickaxe)` 包内 `cobblestone:1` → `{"error":"missing_ingredients","message":"could not craft stone_pickaxe: server placed no result","example_recipe_needs":{"cobbled_deepslate":3,"stick":2}}`；`hy4_hy_6_midgame` 同一必失败合成连续等待 236.9s / 497.4s
- **源码定位**：`skills/crafting.py:10-22` 预检仅 7 个配方（无 stone_*/shield/石砖）；`skills/library.py:2826-2837` 未命中即放行；`experiments/.../backend_patched/src/session.js:2215-2219` 服务端把配方拒绝包装成 `missing_ingredients: ... server placed no result`
- **现象与根因**：预检表是手写子集，未覆盖配方把"缺料"伪装成"服务器异常"，模型无法换目标只能重试。
- **验收标准**：预检改为读真实配方或覆盖常用工具/盾牌/石砖；错误码区分 `missing_ingredients(missing={...})` 与 `server_recipe_rejected`；补离线单测。

### BUG-008 | P1 | Slice C（#4 Codex #2）| 状态: OPEN
- **跨模型影响**：4 bots / 10 次，关键 1 条实际已成功
- **日志证据**：`hy4_hy_7_village` `mc_interact(item=bucket, lava)` → `{"ok":false,"result":{"before":"lava","after":"lava","changed":false,"used":{"bucket":1},"gained":{"lava_bucket":1}},"error":{"code":"interact_no_effect"}}`
- **源码定位**：`harness/mc_tools.py:1158-1171`——只要 `changed is False` 就强改 `ok=false`，忽略 `used`/`gained`
- **现象与根因**：以"方块是否变化"为唯一判据；桶装流体时方块可能被同 tick 回填/读到旧快照。
- **验收标准**：仅当 `changed is False` 且无 `used` 且无 `gained` 才判失败；补离线单测覆盖 `gained=lava_bucket`。

### BUG-009 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：**128 次 / 13 bots**（`glm_glm_4_enchant_anvil` 24、`hy4_hy_2_bare` 18、`glm_glm_3_village` 14、`hy4_hy_3_iron`/`hy4_hy_8_nether`/`hy4_hy_7_village` 各 12-13）
- **日志证据**：114/128 紧邻 `guardian.incident`（`fresh_hurt 49 / pre_hurt_melee 30 / pre_hurt_ranged 16 / pre_hurt_creeper 15`），每次取消丢弃已生成 token 后整请求重发
- **源码定位**：`harness/guardian.py:246 _detect_pre_hurt_trigger` → 抢占取消在途生成
- **现象与根因**：设计上抢占正确，但被 BUG-003/004 的超长工具（>4min）放大——工具越久，被抢占概率越高，浪费越大。
- **验收标准**：优先随 BUG-003/004 收敛；`pre_hurt_*` 增加 2~3s 去抖/同目标冷却；离线单测覆盖同目标连续触发只取消一次。

### BUG-010 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：`hy4_hy_10_enchant_anvil`（同一死亡链）
- **日志证据**：复活后 `guardian.action.completed eat` → `not_ready: world is still loading (after login, respawn or dimension change)`（94.8s），紧接着 `wrong_server`（111.2s），随后 `player_explosion` 死亡
- **源码定位**：`harness/guardian.py:573-581 _do_eat` 直接 `use_item`，未检查 `status.ready` 或刚发生 `death_or_connection_changed`
- **现象与根因**：复活/换维度窗口内进食必然失败，且被计为动作失败，不重试。
- **验收标准**：`deaths or not status.ready` 时跳过进食并标记 `retry_after_ready`；`ready` 后补一次，不把 `not_ready` 计失败；补离线单测。

### BUG-011 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：`glm_glm_3_village` EP5、`hy4_hy_8_nether` EP7 直接整集 `runtime_error`；全队 5 次 `ReadTimeout`/`ReadError`
- **日志证据**：episode report `error = "生存响应 subcortical_combat 的网络结果不确定：ReadTimeout"`，`status = runtime_error`，该集 `score_delta=0`
- **源码定位**：`harness/guardian.py:195-196` 把 `httpx.HTTPError` 重新包成 `RuntimeError`；`harness/agent.py:1614-1620` 中**原生** `httpx.HTTPError` 会被判为可继续的 `network_error`，但被包装后的 `RuntimeError` 落入 `runtime_error` 分支**终止整集**
- **现象与根因**：一次瞬态网络抖动发生在生存反射里，就把整个 episode 打死；`guardian` 的包装绕过了 agent 的传输层异常分类。
- **验收标准**：`_action` 捕获 `httpx.HTTPError` 后返回 `ok:false, error:{code:"network_uncertain"}` 或带退避重试，不再抛 `RuntimeError`；补离线单测（mock 抛 ReadTimeout 必须不终止 episode）。

### BUG-012 | P1 | Slice Integrator（#1）/ 归属待定 | 状态: OPEN
- **跨模型影响**：`hy4_hy_2_bare` EP5 `bootstrap_iron_age` 整集 `runtime_error`（`actor_requests=20`，`score_delta=0`）
- **日志证据**：episode report `error = "[not_loaded] chunk not loaded"`
- **源码定位**：`harness/agent.py:209 _API_RETRYABLE_CODES` 不含 `not_loaded`；`agent.py:1618-1620` 把 `ApiError` 一律判 `runtime_error` 终止；`experiments/.../backend_patched/src/session.js:1367/1866/1917` 在区块未加载时返回 `not_loaded`
- **现象与根因**：`not_loaded` 是毫秒级可恢复状态（区块加载完成即可），却按致命的 `runtime_error` 结算，直接报废整集。
- **验收标准**：把 `not_loaded`/`chunk_not_loaded`/`world_not_ready` 纳入可重试集合（短退避 0.5~2s，最多 2~3 次），仍失败再降级为 `network_error` 而非 `runtime_error`；补离线单测。

---

## P2 Backlog（不抢 P0/P1；KB/脚本类由 #6 随时消化，核心代码类由 #2/#3/#4 在各自 P0/P1 清零后顺延处理）

> 来源：Slice D1 `/home/xzq/projects/mc/.worktrees/s-triage/docs/D1-r11-triage.md`（`r11_22bot` 22 路轨迹）。以下均为策略/提示/非阻塞小优化，不丢弃。

- **P2-1** `mc_move_to` 微距 `range=0.1` 反复 `movement_not_confirmed`（提示"已在 0.1~2m 内"）：36 次 / 10 bots。模型参数规范问题，建议 KB 强调微调站位用 `range>=0.5` 或不传坐标的 `mc_place`。
- **P2-2** `mc_move_to:path_not_found` 59 次 / 11 bots，短距失败后不自动 `unstick`，其中 15 次显式硬传 `y`。KB `data_1_21_11.py:682-683` 已有规则，需在提示/上下文层加强。
- **P2-3** `mine_dfs(action="reset")` 空间树为空时 `nothing_to_reset` 后仍建议 reset：3 次。提示冗余。
- **P2-4** 对无合成配方物品调用 `craft_gear`（`wheat_seeds` 等）：`no_recipe` 3 次。模型策略，交由 KB 提示。
- **P2-5** `void_geofence_blocked`：模型朝 `(0,0)` 世界原点移动：4 次 / 2 bots。围栏已正确拦截，属提示词问题。
- **P2-6** `mc_place` 连续 `no_support` / `blocked_by_self` / `place_failed(目标仍是 sugar_cane)`：3 次。可加"先 dig 目标格"提示。
- **P2-7** `attack_zero_hits` 对 45m 外 glow_squid 空挥 20s：3 次。不可达目标应放弃。
- **P2-8** 舰队共岛互伤：`player_explosion`/`spear` 致死（`hy4_hy_10` 4 死、`hy4_hy_8` 3 死）。属 22 路编队间距/站点隔离问题，非 Harness Bug。
- **P2-9** `find_blocks(water, radius=96)` 返回 `total:10000`：1 次。数据量/性能小优化（限制返回上限）。
- **P2-10** `out_of_reach_vertical` 桶交互（y 差 6.8/9.0）：3 次。已有可执行提示，属策略层。
- **P2-11** `mine_dfs` 未达成目标时仍返回 `ok=true`（`quota_reached`/`shaft_exhausted`/`time_slice_progress`）：约 130 次。建议 `ok` 与 `goal_met` 分离，避免模型误判进度。
- **P2-12** `island_recon_sweep` 24 次 `objective_verified=false`：该 target 无 `advancement_id`，`curriculum.py:220-228` 必然落到 `return False`。仅影响报表字段，请勿用它做晋级门槛（进度看 `material_progress/chunks_after`）。

---

## 场景需求队列（Scene Requests — 由 #1~#5 提单，#6 DeepSeek #2 按单实现）

> 任何会话需要特定实机复现/验收环境时，按以下 5 行模板追加需求单；`#6` 实现为 `l3_wild_endurance_runner.py --stage <name>` 并接入 `v2_fleet.py` 后标为 `READY_FOR_RUN`，由 `#1 主 AGY` 合入上机。
>
> ```markdown
> ### SCENE-XXX | 状态: OPEN | 提单人: #X | stage名: <name>
> - **地形/结构要素**：(如 locate fortress / fill 3x3 岩浆池 / 混合有业+无业村民)
> - **初始背包与成就**：(...)
> - **死亡拉回规则**：(如设 spawnpoint 或下界死亡回传补装)
> - **核心验证目标**：(验证哪个 Bug 修复或哪个成就链路)
> ```


