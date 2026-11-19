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
- **D1 实机复核（r12 19:26 起）**：三路下界 bot 均正常启动并持续调用工具（`glm_glm_1_nether_fortress`/`glm_glm_2_nether_bastion`/`ds_ds_4_nether_fortress` 的 `stdout.log` 无 Traceback），启动崩溃已消除；分数待收官。

### BUG-002 | P0 | Slice C（#4 Codex #2）| 状态: FIXED (待 r12_22bot 实机验收，提交 `7d4a8ee` / `ef6a8ac`)
- **跨模型影响**：`1 bot / 15 次连续失败`（比赛模型 `nju_guitest_01_nether` EP02：`mc_skill_build_portal:unknown_fail×13` + `repeated_identical_failure_blocked×2`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/nju_guitest_01_nether/stdout.log`
- **现象与根因**：`build_nether_portal` 在特定地形/障碍下连续 13 次失败且未返回清晰可恢复错误码。
- **D1 精确根因（已定位到行）**：15 次连续失败全部为 `portal_ignited:true / entered_nether:false`（4x5 门在 `[6409,65,-6382]` 已建成点燃），期间 `move_to(y=67..70,range=0.0/0.1)` 反复 `stalled dist_left=0.7/1.6/2.6`，轨迹 `y=65→70.1（爬到门框顶）→64（unstick）`，**从未进入 y=66..68 门内**。代码 `src/mc_agent/skills/library.py:6803-6854 _walk_into_portal`：只发一次 `move_to(range=0.0)` 后仅轮询 `dimension`，从不校验实体是否位于 `nether_portal` 方块内；清障（:6966-6971）只清门内+门前两格，未清门框两侧/上方接近面，寻路绕顶后永久失败；`reused_existing_portal` 分支复用同一不可达门形成 540s 死循环。对照 `hy4_hy_8_nether` 同工具 `origin y=66` 一次成功。
- **建议最小修复**：`_walk_into_portal` 改为「走到门前 1 格 → `mc_control(forward+jump,~600ms)` 原子跨入 → 每轮校验 feet/head 所在方块为 `nether_portal`」，3 轮失败返回 `portal_enter_blocked`；清障扩展到 `bz-2` 与门框两侧各 1 格。
- **验收标准**：定位精确失败点（放置/点火/步入/地形清障）并修复 `library.py:build_nether_portal`，补离线回归单测并跑通全量 `pytest`。
- **D1 离线复核**：`7d4a8ee` 已按根因加入门前 3 格净空清理、`get_block` 接触判定、`control` 脉冲与 `portal_*` 结构化错误；新增 `test_portal_entry_clears_approach_and_confirms_dimension` 覆盖 r11 的"脚低一格→跳跃入门"场景。全量离线 `pytest tests` = **345 passed, 1 skipped, 3 xfailed**（xfail 为 BUG-006/007 的 TDD 复现）。r12 待实机。

### BUG-003 | P1 | Slice A（#3 Codex #1）| 状态: FIXED（`eea50d7`+`7f87334`，但引入 P0 回归 BUG-018）
- **D1 r12 实测**：`mine_dfs` 单次中位 35.0→**7.5s**、max 174.5→85.0s，预算穿透已消除，但**过度收紧**导致 124 次 0 进展 `time_slice`（见 BUG-018）。
- **跨模型影响**：`20 bots`；156 次 `mine_dfs` 单次耗时（start→finish 差值）中位 **35.0s**、p90 **91.7s**、max **174.5s**；显式带预算的 106 次里 **6 次 > 2× 用户预算**
- **日志证据（D1 口径更正：旧数字误把 run 相对 `elapsed` 当单次耗时）**：`hy4_hy_9_deep_water` `climb_out max_duration_s=32` → **174.5s**（`surface_reached`），另有 131.9/115.2/107.1s 均 `climb_incomplete`；`hy4_hy_8_nether` `explore max_duration_s=80` → **127.0s**（`no_progress`）；`ds_ds_1_bare` `explore 90s` → 95.8s；`hy4_hy_4_iron` `explore 32s` → 96.0s
- **源码定位**：`skills/mining_dfs.py:4638`（预算夹到 5~90s）、`:4559-4560`（`climb_out` 预算 `min(120,max(max_duration_s,90))`，**32s 请求被静默抬到 90s**，是差额主因）、`:2808-2818`（deadline 仅在每次 `dig_staircase` 前检查，单步可再吃 20~90s，故最坏 174.5s≈内部 90s 上限的 1.9×）、`:5198-5231`（`time_slice_progress` 被当成功返回 119 次）
- **现象与根因**：预算是协作式的，只在下钻一层检查；`climb_out` 隐式抬升预算，与工具契约 `max_duration_s` 不一致。
- **验收标准**：`deadline` 透传进 `dig_staircase`/`tunnel`/`_navigate_to_node` 并逐 step 检查；确需更长预算时返回 `budget_effective_s`，禁止静默抬升；`time_slice_progress` 附 `goal_met:false`；补离线回归单测并跑通全量 `pytest`。

### BUG-004 | P2（D1 复核降级，见 P2-13）| Slice C（#4 Codex #2）| 状态: FIXED (待 r12_22bot 实机验收，提交 `7d4a8ee` / `2626e4e`)
- **跨模型影响**：`6+ bots`；`mc_skill_staircase` 66 次调用中位 **17.9s**、仅 **5 次 > 60s**、max **92.3s**
- **D1 口径更正（重要）**：原条目里的 `376.3s / 373.9s / 298.2s` 是 **run 相对 `elapsed`**，不是单次耗时（同 BUG-003）。真实单次耗时没有"十几倍爆炸"；`steps_done=3 / time_budget` 仍说明地形边界会提前收工，但不是延迟故障。
- **保留价值**：`dig_staircase` 增加 `deadline/per_step_timeout` 的硬化本身仍正确（防御未来回归），可继续保留该提交；后续按 P2-13 跟踪真实停止原因分布（`movement_not_confirmed 9 / missing_step_support 7 / fluid_or_bedrock 8`）。
- **验收标准**：r12 复核 `staircase` 单次 p90/max 不优于 r11 则回滚该硬化并重定位。

### BUG-005 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：`4 bots` 高死亡/高频抢占（`hy4_hy_2_bare` 5 死、`hy4_hy_10_enchant_anvil` 4 死、`hy4_hy_8_nether` 3 死、`glm_glm_3_village` `guardian=19/5preempt`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_2_bare/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_10_enchant_anvil/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/glm_glm_3_village/stdout.log`
- **现象与根因**：排查上述 4 条轨迹中的连续死亡原因与 `guardian.py` 高频反射未脱险问题。
- **D1 r14 证据**：`hy4_hy_9_deep_water` **10 次 trident 死亡**（全部集中在 `(-6345,62,-7620)` 一带深水），`escape_water`/`raise_shield` 均未脱险；该机 r12/r13/r14 死亡 3/9/10，呈恶化趋势 → 见 BUG-023。
- **D1 r13 证据**：全队死亡 incident **19→42**（`mc.notable_events` 去重 32 次：`mob_attack 11 / trident 9 / arrow 5 / lava 4 / fireball 2 / player_explosion 1`）。其中 **9 次 trident 全在 `hy4_hy_9_deep_water`**：`escape_water` 返回 ok=true 后仍被 burst（health 15.5→2.9 连击）打死，无水下反击/上浮冲刺。
- **D1 证据补强（r11）**：死亡归因 `hy4_hy_2_bare` = `falling_stalactite`×1 + `mob_attack`×2 + 复活后被围；`hy4_hy_10_enchant_anvil` = `player_explosion`/`spear`（舰队共岛互伤，见 P2-8）+ 复活瞬间进食失败；`hy4_hy_8_nether` = `player_explosion`×2。全舰队 128 次 `model.failed(cancelled)` 中 114 次紧邻 `guardian.incident`（`pre_hurt_melee 30 / pre_hurt_ranged 16 / pre_hurt_creeper 15`），另付 80×~4s≈290s 强制冷却（见 BUG-009）。
- **验收标准**：修复对应 `guardian.py` / `combat/` / `safety.py` 生存反射硬漏洞，补离线单测并跑通全量 `pytest`。

### BUG-006 | P1 | Slice C（#4 Codex #2）| 状态: FIXED（`c34a895`，D1 已离线验证；待 r13 实机）
- **跨模型影响**：**22 次 / 9 bots / 4 模型**（`hy4_hy_2_bare`、`hy4_hy_5_midgame`、`hy4_hy_7_village`、`hy4_hy_8_nether`、`nju_guitest_01_nether`、`ds_ds_3_village`…）
- **日志证据**：`find_blocks(types:["log","planks","bed","wool"])` → `bad_args: unknown block log`；`["shield"]`、`["flowing_water"]`、`["nether_rack"]`、`["cave"]`、`["leather"]`、`["sheep"]` 同类整批失败（22 次）
- **源码定位**：`harness/mc_tools.py:48-59 _normalize_find_block_types` 仅处理 `bed`，其余原样下发；`experiments/20261115-until-1900/backend_patched/src/session.js:1773` 任一未知名 `fail('bad_args')`
- **现象与根因**：别名表过窄 + 后端 fail-fast，模型把实体/物品名混入方块查询时整次调用归零（高频别名 `log` 都缺失）。
- **验收标准**：补别名（`log→*_log`、`planks→*_planks`、`grass→short/tall_grass`、`portal→nether_portal`、`nether_rack→netherrack`、`cave→cave_air`）；无法识别者剔除并继续，返回 `ignored_types`；全部失败才报错并给可执行示例；补离线单测。
- **D1 离线复核**：`_normalize_find_block_types(["log","shield","oak_log"])` → `['oak_log','birch_log',…]`（`log` 展开、`shield` 剔除）；全量 `pytest tests` = 393 passed / 1 skipped / **2 xpassed**（即 BUG-006 的两条 TDD 复现已转绿），仅剩 BUG-007 的 1 条 xfail。

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
- **D1 复核**：`ef6a8ac` 的条件与建议完全一致；r12 前半段 `interact_no_effect` 10→2，剩余 2 次均无 `used/gained`，属真实无效交互。

### BUG-009 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：**128 次 / 13 bots**（`glm_glm_4_enchant_anvil` 24、`hy4_hy_2_bare` 18、`glm_glm_3_village` 14、`hy4_hy_3_iron`/`hy4_hy_8_nether`/`hy4_hy_7_village` 各 12-13）
- **日志证据**：114/128 紧邻 `guardian.incident`（`fresh_hurt 49 / pre_hurt_melee 30 / pre_hurt_ranged 16 / pre_hurt_creeper 15`），每次取消丢弃已生成 token 后整请求重发；另 `model.cancel_cooldown_wait` **80 次 × ~4s = 290.1s 纯睡眠**
- **源码定位**：`harness/guardian.py:246 _detect_pre_hurt_trigger` → 抢占取消在途生成
- **现象与根因**：抢占本身正确，但 `pre_hurt_*` 对同一目标反复触发，每次取消既丢 token 又强制 ~4s 冷却；与工具时长无关（D1 已更正：不存在 >4min 的单次工具）。
- **验收标准**：`pre_hurt_*` 增加 2~3s 去抖/同目标冷却；取消后的 4s 冷却仅在真实受伤后生效；离线单测覆盖同目标连续触发只取消一次。
- **D1 r12 实测（未修复）**：取消 **137 次**、`cancel_cooldown_wait` **272.6s**；`guardian.incident` 179 次中 `pre_hurt_*` 占 63 次（melee 29 / creeper 27 / ranged 7）。与 r11 基本持平，需 Slice B 处理。

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
- **D1 r12 实测**：仍未修复，`hy4_hy_10_enchant_anvil` EP3 再次整集 `runtime_error`。

### BUG-012 | P1 | Slice Integrator（#1）/ 归属待定 | 状态: OPEN
- **跨模型影响**：`hy4_hy_2_bare` EP5 `bootstrap_iron_age` 整集 `runtime_error`（`actor_requests=20`，`score_delta=0`）
- **日志证据**：episode report `error = "[not_loaded] chunk not loaded"`
- **源码定位**：`harness/agent.py:209 _API_RETRYABLE_CODES` 不含 `not_loaded`；`agent.py:1618-1620` 把 `ApiError` 一律判 `runtime_error` 终止；`experiments/.../backend_patched/src/session.js:1367/1866/1917` 在区块未加载时返回 `not_loaded`
- **现象与根因**：`not_loaded` 是毫秒级可恢复状态（区块加载完成即可），却按致命的 `runtime_error` 结算，直接报废整集。
- **验收标准**：把 `not_loaded`/`chunk_not_loaded`/`world_not_ready` 纳入可重试集合（短退避 0.5~2s，最多 2~3 次），仍失败再降级为 `network_error` 而非 `runtime_error`；补离线单测。
- **D1 r12 实测**：**未修复且放大**——`hy4_hy_9_deep_water` 单机 4 集（EP2/4/5/7）均因 `[not_loaded] chunk not loaded` 报废；已并入 BUG-020 统一处理。

### BUG-013 | P1 | Slice A（#3 Codex #1）| 状态: FIXED（`fbd9548`，r13 已实测拦截）
- **跨模型影响**：r12 前 3 分钟即出现 `3 bots / 5 次 repeated_identical_failure_blocked + 2 次 time_slice`：`glm_glm_1_nether_fortress`、`glm_glm_2_nether_bastion`、`ds_ds_4_nether_fortress`（BUG-001 修复后才首次跑起来的三路下界 bot）
- **日志证据**：`mine_dfs(action="explore", target="diamond", target_y=15, max_duration_s=60)` 在下界坐标 `[0.5,52,795.3]` / `[6.4,43,-810.6]` / `[800.5,60,796.5]` 反复 `time_slice` → 触发防卡死拦截（`repeat_count` 2/3）
- **源码定位**：`skills/mining_dfs.py:186-260 ORE_DFS_PROFILES` 仅编码主世界矿种；`_resolve_target_profile`（:668-703）**不检查维度**；runner 返回的 `diamond_ore/deepslate_diamond_ore` 在下界根本不存在，引擎只能空挖 netherrack 到 `time_slice`
- **现象与根因**：模型在 `nether_fortress`/`nether_bastion` 场景把"挖钻石"当成通用目标，`mine_dfs` 无维度可行性校验，导致 3 路各自空转 2~3 次、每次 60~90s。触发提示原文（r12 `run.started.task`）："钻石镐在下界Y8-22挖ancient_debris；床或TNT炸开更快。"——模型把"钻石镐"误读成"挖钻石"，且工具未兜底。
- **最小修复**：`run()` 入口读取 `status.dimension`；当下界/末地时，若 profile 的 `target_blocks` 不属于该维度，立即返回 `bad_target_for_dimension` + 该维度可用目标（下界：`ancient_debris`/`quartz`/`gold`）与建议 `target_y`，不要开挖；补离线单测（mock 维度=the_nether + target=diamond）
- **验收标准**：r13 下界三路不再出现"下界挖钻石"，且 `mine_dfs` 对不可能目标 0 次实际挖掘。
- **D1 r13 复核**：r13 出现 5 次 `stop_reason=bad_target_for_dimension`，提示「当前位于下界(the_nether)，钻石矿石仅存在于主世界(overworld)…DFS 可改采 ancient_debris (Y=15)、quartz (Y=48)、gold (Y=64)」，且不再实际空挖。通过。
- **D1 r12 收官实测**：`glm_glm_1_nether_fortress` 6 次调用中 3 次 `diamond@15`（4 次被拦）、`glm_glm_2_nether_bastion` 4 次 diamond、`ds_ds_4_nether_fortress` 7 次 diamond（12/15/20 层）、`hy4_hy_8_nether` 3 次 diamond——5 路下界 bot 全部中招。`fbd9548 fix(mining): reject ore targets unavailable in the current dimension` 已实现修复，待 r13 验收。

### BUG-018 | **P0 回归** | Slice A（#3 Codex #1）| 状态: FIXED（`/2` 已合入 `fb3b7a4`，r15 实测通过）
- **触发提交**：`eea50d7 fix(mining): bound DFS slices and resume underground escape routes`（为修 BUG-003 引入 `_BudgetSession`）
- **跨模型影响**：**18 bots / 154 次 `mine_dfs:unknown`**（`hy4` 82、`deepseek` 31、`nju` 22、`glm` 19），其中 124 次消息为"本轮时间预算已用尽"；`mine_dfs` 单次中位 **35.0s→7.5s**，调用数 161→241
- **分数回归（r11→r12）**：`ds_ds_2_iron` **95→20（-75）**、`hy4_hy_4_iron` **95→20（-75）**；两路都把 55~62 个 actor 请求耗在 `shiny_gear` 的反复 4s `time_slice` 上（r11 同目标 6~9 请求即完成）
- **精确根因**：`src/mc_agent/skills/mining_dfs.py:344`
  `request_timeout = min(float(kwargs.get("timeout_s") or 45.0), (remaining - 0.15) / 8)`
  —— 把剩余切片除以 8 当作"还要做 8 次调用"，导致默认 32s 切片下**每次子调用上限仅 3.92s**，短于 r12 `move_to` 中位 5.1s / p90 25.7s；第一次 move/dig 就超时 → `_MiningDeadline` → `stop_reason=time_slice`，0 进展返回
- **D1 离线复现**：`scripts/d1_budget_check.py`（本 worktree，只读 v2 源码）
  ```
  slice_budget  move_to_timeout  dig_timeout  verdict
         31.5s            3.92s         3.92s  STARVED
         59.5s            7.42s         7.42s  STARVED
         89.5s           11.17s        11.17s  OK
  ```
- **最小修复**：不要除以 8（deadline 已在每次调用前后检查）。建议
  `request_timeout = min(timeout_s or 45.0, max(6.0, remaining - 0.5))`；
  另给 `dig/move_to` 单项下限 6~8s。修后重跑 `d1_budget_check.py` 必须 exit 0。
- **验收标准**：r13 `mine_dfs` 单次中位回到 25~40s、`time_slice` 0 进展率 <10%、`ds_ds_2_iron`/`hy4_hy_4_iron` 分数不低于 r11 的 95。
- **D1 r15 复核（通过）**：`mine_dfs` n=284，**中位 27.9s / p90 58.8s / max 86.3s**；0 进展 `time_slice` 仅 **15/284（5.3%）**，`time_slice_progress` 190 次。预算回归已消除。
- **D1 r14 复核**：r14（含 `/2` 的本地树）`mine_dfs` n=196，**中位 18.9s / p90 54.2s / max 78.6s**，0 进展 `time_slice` **19/196（9.7%）**，已接近验收线；但 v2-integrated **HEAD 仍是 `/8`**，请 Codex #1 尽快提交 `/2` 并 rebase，避免下一波换树后又退回。
- **D1 r13 复核**：s-mining 未提交改动已把 `/8` 改为 `/2`（`attempt_budget = (remaining - 0.15) / 2`）。r13 实测 `mine_dfs` n=265，**中位 20.5s / p90 51.8s / max 84.7s**，0 进展 `time_slice` **44/265（16.6%）**——较 r12（124/241）大幅收敛，但**仍未达标**；且 `/2` 为未提交状态，需 Codex #1 提交后再验收。

### BUG-019 | P1 | Slice A（#3 Codex #1）/ Slice C（#4）| 状态: FIXED（r13 验证：56→11）
- **跨模型影响**：**17 bots / 56 次** `mine_dfs:repeated_identical_failure_blocked`（r11 仅 1 次），另有 `staircase:repeated_identical_failure_blocked` 8 次
- **日志证据**：工具返回 `time_slice` 且消息为"本轮时间预算已用尽，已保留矿井与回撤路径；**可用相同 action 继续**"，模型照做后立刻被防卡死拦截（`repeat_count=2/3`），0.0s 返回
- **源码定位**：防卡死拦截在 `harness/agent.py` 的重复失败检测；`time_slice` 文案（`mining_dfs.py` 的 slice-timeout 分支，`7f87334`）却邀请相同参数重试
- **现象与根因**：工具契约与 Harness 防循环规则自相矛盾；17 路各浪费 2~4 个 actor 请求
- **最小修复**：`time_slice` 返回 `suggested_next`（如加大 `max_duration_s`、先 `mc_move_to` 换 16 格外、或 `action='climb_out'`），文案禁止"相同 action 继续"；或在防卡死判定中豁免 `time_slice` 同参续跑 1 次
- **验收标准**：r13 `mine_dfs:repeated_identical_failure_blocked` ≤ 5 次且 0 次由 `time_slice` 文案直接触发
- **D1 r13 复核**：`mine_dfs:repeated_identical_failure_blocked` **56→11（17→4 bots）**；`time_slice` 文案已改为「本轮在时间限制内未确认进展，请检查当前位置和障碍后再选择下一步。」，不再诱导同参重试。通过。

### BUG-020 | P1 | Slice Integrator（#1）| 状态: FIXED（待 r15 实测验证）
- **跨模型影响**：r12 **12 个 episode 直接报废**（`runtime_error` 10 + `disconnected` 2），其中 9 个是瞬时故障
- **日志证据**：
  - `[not_loaded] chunk not loaded` ×4（全部在 `hy4_hy_9_deep_water` EP2/4/5/7）
  - Qwen/Cloudflare `HTTP 520/522/525` ×4（`nju_movetest_01_bare` EP3、`nju_nju_3_village` EP2/3、`nju_guitest_01_nether` EP2）——**525 既不在 `RETRYABLE_HTTP_STATUSES` 也不在 `is_transient_llm_error_text`**
  - `[not_connected] bot not connected` ×1、`生存响应 subcortical_combat … ReadTimeout` ×1（= BUG-011）
- **源码定位**：`harness/model.py:36`（缺 525/526）、`model.py:653`（`standard_max_attempts=3`，NJU 网关 5xx 连续 >10s 即耗尽）、`harness/agent.py:209/1618-1620`（`not_loaded`/`not_connected` 未纳入可重试，直接 `runtime_error`）
- **现象与根因**：瞬时 provider/连接故障被按"整集失败"结算，浪费该集全部剩余预算（r12 共 12 集；r11 仅 3 集）
- **最小修复**：① 525/526 加入两份清单；② `not_loaded`/`not_connected` 短退避重试后再判定；③ 重试耗尽时该集标 `paused/transient` 并允许重新排队，不写 `runtime_error`
- **验收标准**：r13 瞬时故障导致的整集报废 ≤ 2，且无 `525` 直接落 `runtime_error`
- **D1 r14 复核**：`runtime_error` 4、`disconnected` 2，均为 `not_loaded`/`not_connected`（r14 进程早于 `26f7213`，未含修复）。待 r15 验证 `not_loaded` 重试是否生效。
- **D1 r13 复核**：整集 `runtime_error` **10→2**、`disconnected` **2→0**，Qwen/CF 5xx 不再打死整集；剩余 2 集均为 `[not_loaded] chunk not loaded`（`hy4_hy_9_deep_water` EP1/EP3）。**保留 OPEN**：仅剩 BUG-012 的 `not_loaded` 分支。

---

### BUG-021 | P1 | Slice D2（#6 DeepSeek #2）| 状态: OPEN（r13 实测）
- **影响**：`nju_guitest_01_nether` 在 r13 **整路 0 事件、0 分数**（22 路只剩 21 路）
- **日志证据**：`runs/r13_22bot/nju_guitest_01_nether/stdout.log`
  `DeathLoopCooldownError: [death_loop_cooldown] bot is in death loop cooldown, 129s remaining`
  抛点 `scripts/l3_wild_endurance_runner.py:797 bot.connect(respect_reconnect_interval=False)` → `bot.py:192-204`
- **现象与根因**：上一波该 bot 死亡进入 129s 冷却，runner 启动时强制忽略重连间隔，异常直接终止进程，且没有等待重试
- **最小修复**：捕获 `DeathLoopCooldownError`，按 remaining 睡眠后重试（最多 2~3 次）；或启动探测到冷却时先等待并打印 ETA
- **验收标准**：r14 起 22/22 路均有 `*.events.jsonl` 与 summary；冷却场景下单路最多延迟 130s 启动而非丢失
- **D1 r14 复核**：`ds_ds_4_nether_fortress` 再次因 `DeathLoopCooldownError: 120s remaining` 整路丢失。修复提交 `26f7213` **已包含** try/except 睡眠重试，但 r14 的 runner 进程加载于 20:14（早于该提交 20:18），跑的是旧代码。
- **附加要求（发布纪律）**：`launch` 前打印 `git rev-parse HEAD` 并与运行树比对；或在 runner 启动日志写入 commit，避免"修复已合但实机仍跑旧码"。

### BUG-022 | P1 | Slice A（#3 Codex #1）| 状态: FIXED（r15 验证：铁阶段 100 分）
- **跨模型影响**：`ds_ds_2_iron`（95→20→25）、`hy4_hy_4_iron`（95→20→20）；两路把 25~40 个 actor 请求耗在 `shiny_gear`
- **日志证据（r13 `ds_ds_2_iron`）**：`explore` 每次 17~27s 稳定下潜 y=129→55（12 次 `time_slice_progress`，属正常），到 **y=47 触发 `shaft_exhausted`**，随后 `return_collar → no_collar_recorded`、`reset → nothing_to_reset`、`explore → needs_surface_climb`，再由 `climb_out` 从 y=56 爬到 101 反复 `time_slice`，直到 episode 超时
- **源码定位**：`skills/mining_dfs.py:5132-5162`（深处无法回撤时强制 `needs_surface_climb`）、`mining_dfs.py:5214-5230`（回撤失败同样强制爬升）；`shaft_exhausted` 在目标层上方 ~100 格（y=47 vs target -54）提前判定
- **现象与根因**：下潜切片 × episode 调用预算不足以到达目标层；`shaft_exhausted` 后引擎拒绝在深处续挖，要求回地表，而 `climb_out` 又需多次调用——形成「下潜→耗尽→爬升→超时」死循环
- **最小修复**：① `shaft_exhausted` 时若当前 y 距 target_y >10 且镐可用，允许就地开新分支（`offset/reopen`）而非强制回地表；② `climb_out` 支持跨调用续爬（保存进度）并返回 `next_action="climb_out"`；③ 提示模型为深目标使用 60~90s 切片
- **验收标准**：r14 `ds_ds_2_iron`/`hy4_hy_4_iron` 至少一路拿到 `mine_diamond`，分数 ≥60；`needs_surface_climb` 导致的 0 进展调用 ≤2/路
- **D1 r14 复核**：`ds_ds_2_iron` 20、`hy4_hy_4_iron` 20，未改善。
- **D1 r15 复核（通过）**：同一 iron 场景由 `hy4_hy_2_iron` 承接，拿到 **100 分**（此前 iron 槽 20~25），diamond 链路恢复；说明 BUG-018 的预算修复即 BUG-022 的主要解。

### BUG-023 | P1 | Slice B（#2 子 AGY）+ D2（场景）| 状态: OPEN（r14 实测）
- **影响**：`hy4_hy_9_deep_water` 在 r12/r13/r14 死亡 **3/9/10**，10 次全部为 `trident`，分数长期 25~40
- **日志证据**：死亡坐标集中在 `(-6345.3,62.9,-7614.5)` ~ `(-6368.5,62.6,-7648.5)`；每次事故 `escape_water ok=true` 但仍在 2~3 秒内被 5+ 次 trident 连击（health 15.5→2.9）打死；`raise_shield` 仅出现 2 次
- **源码定位**：`harness/guardian.py` 深水分支只做 `escape_water` 位移，没有"脱离水域到岸/上浮换气 + 盾挡 + 反击 drowned"的组合；`skills/safety.py` 水下脱困优先级低于持续受击
- **最小修复**：① 深水受击时优先 `mc_move_to` 最近干燥陆地/向上冲出水面（而非原地小幅闪避）；② 连续 3 次受击且仍在水中 → 强制上岸并设 30s 不再下水；③ 有盾时先 `equip_shield`+`raise_shield` 再退
- **场景侧（D2）**：`deep_water` 出生点距岸 ≤8 格，或保证 20 格内有可站立浅滩，避免刷新即被 drowned 围杀
- **验收标准**：r15 该路 trident 死亡 ≤2，分数 ≥80
- **发布状态（r16 前核对）**：修复 `e680b1f`（盾优先上岸 + 30s 不再下水）**已在 HEAD**，r16 可直接验收。
- **D1 r16 早段复核（正向）**：`hy4_hy_9_deep_water` 已执行 **12 次 `escape_water`、0 死亡**；`hy4_hy_10` 6 次、0 死亡（r15 该路 1 次 trident 死、r14 为 10 次）。门禁 `BUG-023 deep_water trident deaths ≤2` 当前 PASS，待收官确认。

### BUG-024 | P1 | Slice C（#4 Codex #2）| 状态: FIXED（`7dbcdb1` + `_dig_pillar_up` no_support 重试；r16 实测 46→1）
- **跨模型影响**：**45 次 / 6 bots**（`glm_3`、`hy4_4`、`hy4_6`、`nju_guitest`、`hy4_2`、`hy4_9`），占 r15 `staircase` 失败的首位（55 次 `missing_step_support` 中 45 次是 `no_support`）
- **日志证据**：`mc_skill_staircase(direction=*, vertical="up", steps=6)` → `stop_reason=missing_step_support`，`last_error={"code":"no_support","message":"no solid neighbour block to place against"}`；四个方向均发生
- **源码定位**：`src/mc_agent/skills/library.py:5688-5698` 在跳跃中对 `[bx,by,bz]` 执行 `place`；异常处理只对 `place_failed` 重试（`:5681-5688`），遇到 `no_support` 立即 `break`，随后 `:5696-5698` 归为 `missing_step_support`
- **现象与根因**：上阶梯在自挖竖井里垫脚，目标格四周已被挖空，没有可附着面 → 服务端拒绝放置；换 filler 材质无用（错误码不是 `missing_item`），于是整次 staircase 失败
- **后端契约（已核对）**：`session.js:1964-1981` 的 `place` 会自动从 6 个邻面找实心参考块；`no_support` = 6 邻面全空。未传 `x/y/z` 时会走 `autoPlaceSite()`（3 格内找有实心地面的空格，`session.js:1966-1969`）。
- **最小修复（按可行性排序）**：① `no_support` 时改用 **无坐标 `place(item=filler)`** 走 `autoPlaceSite`，成功后再 `mc_move_to` 站上去；② 或在目标格旁 `dig` 1 格侧袋制造附着面后重放；③ 仍失败再返回 `missing_step_support`，并给 `next_action="staircase(direction=..., vertical='down')"` 等替代
- **D1 趋势**：`no_support` 按波次 16(r12)→3(r13)→9(r14)→**46(r15)**，不是单次提交回归，而是新场景/竖井上阶梯使用量放大了既有缺陷
- **D1 代码复核（HEAD，已定位到函数）**：新增的 `no_place_face → _dig_pillar_up` 兜底位于 `_dig_staircase` 的 `v_key=="up"` 分支（`library.py:6310-6332`）。但 r15 的 46 次报错来自 **`_dig_pillar_up` 自身**（定义 `library.py:5492`，jump-place 在 `:5673` `place(item=filler,pos=[bx,by,bz])`，只重试 `blocked_by_self`，无 6 面实心检查）。
  → 修复点应是：在 `_dig_pillar_up` 的 `place` 之前做同样的 `has_place_face` 判定；无面时改为"向侧面 dig 1 格（制造附着面）"或"无坐标 place 走 autoPlaceSite"，而不是直接判 `missing_step_support`。
- **D1 成本量化（r15）**：46 次 no_support 直接墙钟仅 **122.7s**（avg 2.7s，失败很快），但每次都要模型重新规划 → **消耗 46 个 actor 轮次**；受影响排序：`nju_guitest_01_nether 51.7s`、`glm_3_village_trade 25.1s`、`hy4_4_cave_combat 21.7s`、`hy4_6_nether_bastion 17.2s`。
- **验收标准**：r16 `staircase:missing_step_support`（no_support）≤5 次；上阶梯成功率 ≥80%

### BUG-025 | P1 | Slice A（#3 Codex #1）| 状态: FIXED（`3d4ac1e`，D1 复现脚本已转绿；待 r16 实机）
- **跨模型影响（r15 收官）**：**39 次 / 10 bots**（`ds_ds_1_bare 16`、`hy4_hy_7_village 6`、`ds_ds_4 4`、`nju_guitest 3`、`nju_movetest 2`…）；`ds_ds_1_bare` 最终仅 **20 分 / 9 集**，`mine_dfs` 消耗 1265s
- **日志证据（ds_ds_1_bare）**：
  - `mine_dfs` → `stop_reason=pickaxe_low`，提示"当前材料可立刻合 1 把石镐（现有石料 **391**、木棍折算 **2**）"
  - 实际背包：`cobblestone 169 + cobbled_deepslate 223`，`acacia_planks **1**`，`stick **0**`，**无任何原木**
  - `craft_gear(stone_pickaxe)` → `missing_ingredients`（`missing_materials={"stick":4}`）；`craft_gear(stick,4)` → "需要至少 2 块木板，当前仅 1" —— 死循环 4 轮
- **源码定位**：`src/mc_agent/skills/mining_dfs.py:848-853`
  `sticks_available = sticks + 2 * (planks + 4 * logs)`
  —— 把 **1 块木板折算成 2 根木棍**，但原版配方是 **2 木板 → 4 木棍**，单块木板实际产出 0 木棍
- **现象与根因**：`pickaxe_low` 的"可立刻合成"判断高估木棍，诱导模型反复调用注定失败的 `craft_gear`；同时缺"没木头时先 `harvest_logs`"的兜底
- **最小修复**：`sticks_available = sticks + 4 * (planks // 2) + 8 * logs`；当 `n == 0 and stone >= 3` 时，提示改为"先去砍 1 棵树补木板（`mc_skill_harvest_logs(target_logs=1)`）再合成"
- **D1 离线复现**：`/home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_stick_check.py`（exit 1）
  ```
  1 plank / 0 stick / 391 stone (r15 ds_ds_1_bare)      got 1  want 0  sticks_available 2  WRONG
  2 planks -> 4 sticks (enough for 1)                   got 1  want 1  OK
  4 planks -> 8 sticks (enough for 2)                   got 2  want 2  OK
  1 log -> 4 planks -> 8 sticks                         got 2  want 2  OK
  ```
  改成 `4 * (planks // 2) + 8 * logs` 后 5/5 通过；另建议给 `craft_gear` 的 `missing_ingredients` 补 `message`（当前为 `None`，模型看不到下一步）。
- **D1 运行中 bot 复核（r15 未收官 4 路）**：`ds_ds_1_bare` 命中 **9 次**完整死循环：`stone_pickaxe{missing stick:2/4}` → `stick{planks_or_log:1, 仅 1 木板}` → 再 `stone_pickaxe`；`nju_movetest_01_bare` 另有 2 次（`wooden_pickaxe` 缺 `any_planks:3`）。`no_support` 在 4 路中 **0 次**，说明 BUG-024 主要出现在竖井内上阶梯。
- **验收标准**：r16 `pickaxe_low → craft_gear(missing stick)` 连续循环 0 次；bare 三路 20 分钟内达到石器基建（≥10 分）
- **D1 离线复核（通过）**：HEAD 已改为 `sticks + 4*(planks//2) + 8*logs`（`3d4ac1e`）；`scripts/d1_stick_check.py` **5/5 OK，exit 0**（r15 的 1 木板 case 由 1→0）。r15 实机仍有 39 次旧行为，属修复前数据。

### BUG-026 | P1 | Slice B（#2 子 AGY）+ Slice D2（场景）| 状态: OPEN（r15 部分收官）
- **跨模型影响（r15 收官 21 路）**：死亡 **51 次**（`notable` 去重；incident 口径 57），r14 为 28；`model.failed(cancelled)` **141→247**，`cancel_cooldown_wait` **286.3s→454.2s**
- **D1 r16 复核（`d2ac1ab`+`b920f24` 已合入）**：死亡 incident **57→14**、`cancel_cooldown_wait` **454.2s→148.3s**、`cancelled` 247→约 80；门禁 PASS。待 r17 22/22 复核后关闭。
- **死因分布**：`mob_attack 23 / arrow 13 / fall 5 / player_explosion 3 / lava 3 / trident 1`
- **集中点**：`hy4_hy_8_nether 8`（arrow 3 + mob 4 + 爆炸 1）、`ds_ds_3_enchant_anvil 5`、`hy4_hy_6_nether_bastion 5`（fall 2 + lava 3）、`hy4_hy_1_bare 4`、`hy4_hy_4_cave_combat 4`、`hy4_hy_9_deep_water 4`
- **现象与根因**：新增 `cave_combat` / `brewing_alchemy` / `village_trade` 场景没有任何生存节奏适配（洞穴刷怪、远程骷髅、岩浆），`pre_hurt_ranged` 覆盖不足；死亡后重开又消耗 episode
- **D1 深度归因（r15 全量 49 死）**：
  - 按层级：**地表 42 死**（mob_attack 23 / arrow 13 / explosion 4 / fall 2 / in_fire 1 / trident 1），地下 7 死（fall 3 / lava 3 / mob 1）
  - **10/49 死前没有任何 `guardian.incident`**（反射未触发，`fresh_hurt` 时才记录到 `death_or_connection_changed`）
  - **0/49 死时手持盾牌**，`raise_shield` 全程仅 1 次；但 **17 路结束时背包仍有 shield+iron_sword**
  → 结论：不是没资源，而是"受击前不举盾、远程来箭不切掩体"；pre-hurt 覆盖率与举盾时机是主因
- **最小修复**：① 表面 `pre_hurt_ranged`/`pre_hurt_melee` 命中即 `equip_shield`+`raise_shield`（当前只在部分分支）；② 箭矢来源方向已知时先横移 2 格再举盾（盾只挡正面）；③ `cave_combat` 进入前检查火把/甲；④ 岩浆边强制 sneak+封边；⑤ 场景侧（D2）提供光照/安全出生区
- **发布状态（r16 前核对）**：修复提交 `b920f24` 在 `fix/s-combat` 分支（含 arrow→lateral_sidestep+盾、auto_equip_armor、lava edge guard、pre_hurt 去抖），但 **`8e8d7a` 级别合并后仍未进 `feat/v2-integrated` HEAD（3fb883e）**。因此 r16 预计 BUG-026 指标不改善；请 #1 在下一波前合入。
- **D1 运行中 bot 复核（r15 未收官 4 路）**：`hy4_hy_1_bare` 5 死（`mob_attack 4 + arrow 1`，死亡点 `y=6` 地下，无甲无火把）；`nju_movetest_01_bare` 2 死（`arrow 2`，地表 `y=62`）；`glm_glm_5_midgame` 1 死。
- **验收标准**：r16 单波死亡 ≤ 25；`arrow` 死亡 ≤ 4；`cancel_cooldown_wait` ≤ 200s

### BUG-027 | P1 | Slice C（#4）/ backend（#1 合入）| 状态: FIXED（`e65356d` 前置校验；残留寻路 → P2-24）
- **影响**：`glm_glm_3_village_trade` 在 r16 前 4 分钟内 **6 次 `mc_window_view → no_window`**（entity 81197/81539/81333 重复 2 轮），直接阻断交易目标；同期 `nju_nju_3_village_trade` 已成功交易
- **日志证据**：实体距离 **12.2m / 12.5m / 16.1m**（`mc_get_entities`），窗口调用耗时 2.5~30.2s 后返回 `{"code":"no_window","message":"villager did not open a GUI"}`
- **源码定位**：`experiments/20261115-until-1900/backend_patched/src/session.js:1352-1372 openTarget()`：`await goNear(s, e.position, 2.5)` **不检查返回值**，随后直接 `activateEntity(e)`；`:1396-1397` 只等 **2500ms** windowOpen。站在 12m 外右键村民不会开窗
- **现象与根因**：`goNear` 失败（被地形/其他村民碰撞箱卡住）后仍在超距位置激活实体 → 必然 `no_window`，且错误码误导为"村民不开口"
- **最小修复**：① `goNear` 后校验 `distanceTo(entity) <= 4`，否则返回 `out_of_reach`（带距离）并重试一次绕行；② 让 `openTarget` 对 entity 使用 `goNear(..., 2.5)` 的失败结果，必要时改走侧面/背后；③ 窗口等待 2500→4000ms
- **D1 r17 复核（通过）**：`e65356d` 已在 `mc_window_view` 前置距离校验。r17 首 10 分钟：`out_of_reach` **10 次**（带距离与建议）、`no_window` 降至 **3 次**、`mc_window_view ok` 6 次；模型收到 `out_of_reach` 后会补 `mc_move_to`，`nju_3` 已成功开窗并交易。残留是"走到村民身边仍被碰撞箱卡住"→ 归 P2-24。
- **D1 现状升级（r16，11 次 / 全部 glm_3）**：该 bot 在 12~16m 处连续调 `mc_window_view`，**期间没有任何 `mc_move_to`**，说明 `goNear` 静默失败后直接激活。建议同时加 Python 侧（`harness/mc_tools.py:mc_window_view`）前置校验：若实体距离 >4 或 `mc_get_entities` 拿不到，直接返回 `out_of_reach + 建议 mc_move_to(x,z)`，不必等后端修复；这样 Slice C 可先止血。
- **验收标准**：r17 `village_trade` 路 `no_window` ≤ 1，且首笔 `mc_skill_trade` 在 3 分钟内完成

### BUG-028 | **P0 崩溃** | Slice B（#2 子 AGY）| 状态: FIXED（`52b0d3f`；r17 已实测 0 复现）
- **影响**：`hy4_hy_6_nether_bastion` 在 r16 **整路进程退出**（`stdout.log` Traceback，无 summary）；触发条件=下界踩到岩浆
- **日志证据**：
  ```
  [受伤 health=12 by mob_attack] → guardian escape_lava 启动（run 85.0s）
  combat_env.py:1206 AttributeError: 'VoidGeofence' object has no attribute 'contains'
  run.finished（进程结束，整路丢失）
  ```
- **源码定位（HEAD）**：`src/mc_agent/skills/combat_env.py` **4 处**调用不存在的 API：
  `:1206`、`:1293`、`:1354`、`:1360` 的 `self.geofence.contains(...)`；`VoidGeofence`（`harness/geofence.py`）只有 `check_point` / `check_ray` / `distance_from_center` / `margin_remaining`
- **引入提交**：`e680b1f`（BUG-023 修复，新增 `_find_walkable_shore_target`）与 `3210e7c`（escape_lava 找岸边）——r15 未触发是因为当时没有"岸候选"路径
- **最小修复**：4 处统一改为
  `self.geofence is not None and self.geofence.check_point(cx + 0.5, cz + 0.5, action="lava_shore") is not None` → 跳过；
  或 `hasattr(self.geofence, "check_point")` 保护。补一个用真实 `VoidGeofence` 的离线单测（mock 世界含岩浆+岸边），确保 `escape_lava` 不再抛 `AttributeError`。
- **r16 实际战损（已确认 4/22 整路丢失）**：
  - `hy4_hy_6_nether_bastion` → `escape_lava` → `:1206`
  - `ds_ds_1_bare`、`glm_glm_5_midgame`、`nju_guitest_01_nether` → `escape_deep_water_to_shore` → `:1293`
  → 不只是"下界 5 路"，**任何落水/踩岩浆的 bot 都会崩**；r16 因此只有 18 路有 summary。
- **修复落地**：`52b0d3f` 用 `_point_allowed()` + `check_point` 替换全部 `.contains`，`d1_geofence_check.py` 已 **exit 0**。
- **D1 r17 实机复核（通过）**：r17 前 10 分钟 **878 次工具调用、0 Traceback**；`escape_water`/`escape_lava` 正常返回结构化结果；门禁 `startup crash == 0` PASS。
- **D1 一键回归门禁**：`/home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_geofence_check.py`（当前 exit 1，精确列出 4 行）
  ```
  FAIL: combat_env calls non-existent geofence API: ['contains']
    combat_env.py:1206 / :1293 / :1354 / :1360
  ```
- **验收标准**：`grep -r "geofence.contains" src/` 为 0；`d1_geofence_check.py` exit 0；r17 下界 lava 事故 0 崩溃、`escape_lava` 全部返回结构化结果

### BUG-029 | P1 | Slice C（#4 Codex #2）| 状态: OPEN（r17 实测，7/22 路超保留上限）
- **现象**：背包被圆石/深板岩/凝灰岩/安山岩/地狱岩长期塞满，挤占 10~14 个槽位（约 30~39%）
- **r17 收官数据（`owned_end`，cap=192/种）**：
  | bot | 石料家族 | 地狱岩 | 泥土 |
  |---|---|---|---|
  | `hy4_hy_2_iron` | **450**（cobble 287 + deepslate 163） | 0 | 42 |
  | `nju_nju_3_village_trade` | 309 | 0 | 77 |
  | `hy4_hy_9_deep_water` | 274 | 0 | 0 |
  | `ds_ds_2_cave_combat` | 220 | 0 | 16 |
  | `glm_glm_2_nether_bastion` | 52 | **783** | 0 |
  | `glm_glm_1_nether_fortress` | 55 | 218 | 0 |
- **源码根因（逻辑 Bug，非策略）**：`skills/library.py:1465-1467`
  ```python
  empty_now = _compute_empty_slots(inv)
  if empty_now >= min_free:
      return inv, []          # ← 提前返回
  ...
  cobble_over_cap = any(per_item_counts.get(n, 0) > 192 for n in _TRASH_TIER4_COBBLE)  # :1478，永远走不到
  ```
  "超过保留上限就清理"的判断被放在"空槽不足"的提前返回**之后**，所以只要还有 `min_free` 个空槽，超量圆石/地狱岩永远不会被丢。
- **次要问题**：清理只在 `craft/harvest` 路径调用（`_ensure_free_slots`），`mine_dfs`/`mine_blocks` 连续采集后没有触发点；r17 全程 **0 个 `dropped` 结果**。
- **最小修复**：
  1. 把 `cobble_over_cap`/per-item cap 判断移到提前返回之前：
     `if empty_now >= min_free and not over_cap: return inv, []`
  2. 增加"采集后软清理"：`mine_dfs`/`mine_blocks` 结束若 `cobble>192` 或 `netherrack>128` 或 `dirt>64`，静默 drop 到 cap（一次最多 12 个 drop op）
  3. 下界把 `netherrack` 的保留量降到 0~32（无建筑价值）
  4. 返回/事件里回报 `inventory_cleaned:{item,count}`，便于 D1 验收
- **验收标准**：r18 起 `owned_end` 中 cobble+deepslate ≤ 384、netherrack ≤ 128、dirt ≤ 64 的路 ≥ 20/22；`dropped` 事件 > 0；无 `inventory_full`

### BUG-030 | P1 | Slice A（#3 Codex #1）| 状态: OPEN（r17 `hy4_hy_2_iron` -80 根因）
- **影响**：`hy4_hy_2_iron` 100（r15）→ **20**（r17）；两集 `shiny_gear` 共 37 个 actor 请求、~1000s，全部耗在下潜
- **证据（逐调用 y 轨迹）**：
  - EP2：64→57→49→40→31，随后 `backtrack_blocked@11024,64,-10977`，`pos_after` **y=64（+37，回到地表）**，整段下潜作废；重启后 64→28→45→45→34→30，18 次调用结束仍在 y=30
  - EP3：30→21→12→1→-12→-23→-36→-47→**-54（第 17 次调用）**，随后 episode 超时——**只差 1~2 次调用就能开始找钻石**
- **源码定位**：
  - `skills/mining_dfs.py:4750-4760`（`budget = min(90, max_duration_s)`，默认 32s）；32s 切片 + `_BudgetSession` 的实际动作预算只能下潜 ~8 格/次，100+ 格需 14~18 次调用
  - `skills/mining_dfs.py` 的 `backtrack_blocked` 恢复会走 `_climb_out`/回井口，导致 `pos_after y` 大幅上升（本次 +37），且最终仍以 `time_slice_progress`（ok=true）返回，上层无法感知"深度进度丢失"
- **现象与根因**：深目标（diamond@-54）所需下潜被 episode 调用预算吃光；一次 backtrack 失败还会把已下潜的 37 格清零。**与背包/BUG-029 无关**（该 bot 无 `inventory_full`、无 `no_drops_collected`，gained 全部正常入包）
- **最小修复**：① `run()` 按 `|current_y - target_y|` 自动抬升切片（如 `min(90, 30 + 0.4*gap)`）并返回 `budget_effective_s`；② `backtrack_blocked` 时优先在当前深度开分支/侧移，不要回地表；③ 若恢复导致净上升 >8 格，返回 `progress_lost:true` 或独立 `stop_reason="depth_reset"`，让模型能换策略
- **验收标准**：r18 起 `shiny_gear` 集内 `mine_dfs` 首次到达 `target_y±3` 的调用数 ≤10；`depth_reset` 0 次；`hy4_hy_2_iron`（或 iron 槽）≥80 分

### BUG-031 | P1 | Integrator（#1，`harness/curriculum.py`）| 状态: FIXED（待 r19 长程实测验证）
- **影响**：`glm_glm_5_midgame` 45 分钟长程中连续 **8 集 `objective_verified=False`**（EP08~EP15），后半程 ~20 分钟基本空转；`scripts/long_run_diag.py` 命中 `LONG-002:stalled_streak(8_eps)`
- **证据（`r15_22bot/glm_glm_5_midgame` 逐集自述）**：
  - EP10：`story/lava_bucket` 扫 352 格无静止岩浆 → 失败；**EP11 立刻又派同一个 `story/lava_bucket`**（扫 640 格仍无岩浆）
  - EP12：`husbandry/make_a_sign_glow` 地表无发光鱿鱼 → 失败
  - EP13：找羊扫 40m 无羊 → 失败；**EP14 立刻又派找羊**（扫 224m 确认无羊）
- **根因**：`CurriculumSupervisor` 对环境稀缺/资源缺失导致 `objective_verified=False` 的目标冷却太短（或未区分“当地无资源”与一般执行失败），导致同一目标在连续两集里被反复派发
- **最小修复**：① 连续 `objective_verified=False`（尤其模型汇报“未找到/半径内无目标”）后对该目标施加 ≥3~4 集的退避冷却；② `r18` 起用 `scripts/long_run_diag.py --wave <wave>` 自动巡检长程停滞链
- **验收标准**：45m 长程 `max_unverified_streak <= 4`，且不出现连续两集派发同一个无资源目标

### BUG-032 | P1 | Slice A（#3）+ Integrator（#1）| 状态: OPEN（r15 45m 长程诊断）
- **影响**：`ds_ds_1_bare` 45 分钟长程中 EP05~EP08 连续 **4 集卡在地底断木断镐循环**（总计 ~20 分钟停在铁器前）
- **证据（`r15_22bot/ds_ds_1_bare`）**：
  - EP05 位于 `y=-53` 地下深处，背包无原木、无木棍、无工作台、无镐，空耗 14 轮；EP06 爬到 `y=32` 仍缺木棍；EP07 已烧出 7 块铁锭仍因缺木棍做不出铁镐；EP08 爬回地表 `y=47` 才开始补木
- **根因**：深层挖矿前缺少“至少携带备用原木/木棍”的前置检查，且 `mine_dfs` 检测到 `y < 20` 且无木料+无可用镐时未尽早引导返回地表补木
- **最小修复**：① `curriculum.py` 下矿前保底要求背包留足备用木料（或 `pre_ensure` 补木）；② `mining_dfs` 检测到无可用镐且无合成木料时在返回里显式建议 `action="climb_out"` 回地表砍树
- **验收标准**：45m `bare` 长程不再出现 `y < 0` 断木断镐跨 2 集以上停滞；配合 `BUG-029`（`r15` 长程 `nju_movetest_01_bare` 31/36 格 903 垃圾、`glm_glm_5_midgame` 36/36 格、`hy4_hy_1_bare` 34/36 格 950 垃圾，且 4 路全部 `containers=0`）一起在 `r18`/`r19` 验收

## P2 Backlog（不抢 P0/P1；KB/脚本类由 #6 随时消化，核心代码类由 #2/#3/#4 在各自 P0/P1 清零后顺延处理）

> 来源：Slice D1 `/home/xzq/projects/mc/.worktrees/s-triage/docs/D1-r11-triage.md`（`r11_22bot` 22 路轨迹）。以下均为策略/提示/非阻塞小优化，不丢弃。

- **P2-1** `mc_move_to` 微距 `range=0.1` 反复 `movement_not_confirmed`：`6f54b6f` 已把"已在 range 内"转为成功；r17 仍剩 10 次（2 次 `entity_id` 目标未覆盖、其余为 `status()` 失败或 range 被后端改写的边界）。建议转换逻辑扩展到 entity_id，并用 error 内 `pos` 兜底。
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
- **P2-13**（原 BUG-004 降级）`dig_staircase` 地形边界停止：`movement_not_confirmed 9 / missing_step_support 7 / fluid_or_bedrock 8 / cave_or_drop_ahead 4`；66 次调用中位 17.9s、仅 5 次 >60s、max 92.3s。属正常地形判定，已有可执行 hint；只需优化"换向/垫脚"提示与失败方向记忆，不占 P1。
- **P2-14** 下界 prompt 歧义：r12 场景任务文本"钻石镐在下界Y8-22挖ancient_debris"被模型读成"在下界挖钻石"（3 路复现）。建议改为"用钻石镐挖 ancient_debris（下界 Y8-22）；钻石在下界不存在"。与 BUG-013 的维度校验双保险。
- **P2-15** 长尾工具耗时（r12）：`mine_blocks` 单次 **300.6s**（obsidian×4，`temporarily_avoided:1/timeout`，0 产出）、`smelt` 102s（12→9）、`harvest_logs` 87~91s（部分产出）。`mine_blocks` 内部 `mine_deadline` 仅 45~90s（`library.py:2524`），但 `dig`（:2568）未传 `timeout_s`，单次服务器超时即可吃掉全预算。建议同 `_StairSession` 加硬 deadline。
- **P2-16** `mc_move_to` 在动作租约未释放时阻塞 ~101s 才返回 `busy: running move_to`（3 bots：`glm_glm_5_midgame`、`hy4_hy_7_village`、`hy4_hy_10_enchant_anvil`）。建议 `busy` 前置探测（<2s）或直接返回 `action_busy`。
- **P2-27** `mc_skill_brew` 参数契约（r16 早期 8 次调用）：`bottles:[{"item":"water_bottle","slot":0}]` → `bad_args`（未说明期望 schema，模型重复传）；`fuel_item=blaze_powder` → `missing_item`（未回报剩余数量）。建议 bottles 容忍 `item/slot/count` 三种写法，并在 `missing_item` 中返回库存计数。
- **P2-26** `mine_dfs` 的 `stop_reason` 被拼进整段诊断串，如 `backtrack_blocked@-11011,34,-11125(feet=air,head=air,from=-11011,33,-11126)`（r15 至少 5 次）。建议固定枚举 `stop_reason`，坐标/脚部方块放 `blocker` 字段，否则聚合统计与模型分支判断都不可靠。
- **P2-25**（课程观察，**不改顺序/权重**）`shiny_gear` 在同一 bot 连续 2 次 `timeout`（`glm_3` 28+22 calls、`hy4_4` 40+36 calls），`strike_limit=2` 仅在 `progress_made=False` 时累计；一旦拾到 1 颗钻石/换镐就会清零 strike，导致 20 分钟内反复选中。建议 D2 评估"仅按 gained 成就清零 strike"是否更合理。
- **P2-24** `mc_window_view` 对村民交易：`glm_glm_5_midgame` 10 次调用 **6 stalled + 3 no_window**，共 66.7s（单次 max 9.6s）。`stalled` 为"stuck on an entity hitbox"，建议接近村民时按实体占位偏移到侧面/背后再开窗，或直接 `mc_interact` 村民而非寻路贴脸。
- **P2-21** `mc_dig:timeout` 4→15 次 / 7 bots（r15）：`dig` 单次超时后模型重试同格。建议 `dig` 返回 `timeout` 时自动标记该格 15s 避开并给替代格。
- **P2-22** `mc_place:no_support` 6 次 / 5 bots（r15）：与 BUG-024 同源，原始 `mc_place` 也应给"先 dig 侧袋/换参考面"的可执行提示。
- **P2-23** `staircase:time_budget` 仍 36 次 / 8 bots（r15）：45s 硬帽的 churn 仍在，随 P2-20 一起评估。
- **P2-19** `harvest_logs` 新增 `time_budget` 早退（r14：18 次/6 bots，max 145.8→75.0s）：提示建议降 `count` 或先靠近目标 2 格，避免连续早退。
- **P2-20** `staircase` 45s 硬帽导致调用数 69(r11)→119(r13)→146(r14)，单次 max 92.3→46.0s；可能"省一次长调用、多花多次模型轮次"。建议改为 60~75s 或按 `steps` 缩放。
- **P2-18** `mc_skill_tunnel:missing_pickaxe` 15 次 / 3+bots（r13 新增）：镐子耗尽/未装备时直接失败。建议 tunnel 先自动换镐/合成石镐，再返回 `missing_pickaxe`。
- **P2-17（已修复）** 模型参数串位：`mc_move_to {"x":-7828,"y":-2814,"z":-2744}`（y 填成了 z）。越界保护已忽略该 y，但浪费一次 90.9s `path_not_found`。建议在参数校验层对 `|y|>320` 直接 `bad_args` 并提示。 

---

## 场景需求队列（Scene Requests — 由 #1~#5 提单，#6 DeepSeek #2 按单实现）

### SCENE-001 | 状态: FIXED（`7d5a571`，r16 已实测交易成功）| 提单人: #5 D1 | stage: village_trade / brewing_alchemy / cave_combat
- **核心问题**：三个新 stage 目前**只换出生点与背包**，没有注入 stage 目标；6 路实机全程仍在跑通用课程（`island_recon_sweep → shiny_gear`），场景分数无法反映交易/酿造/战斗能力。
- **证据（r15）**：`glm_3_village_trade` 40（ep3/4 均 `shiny_gear` timeout）、`nju_3_village_trade` 40、`glm_4_brewing_alchemy` 55、`hy4_5_brewing_alchemy` 70、`ds_2_cave_combat` 60、`hy4_4_cave_combat` 45；`glm_5` 的 10 次 `mc_window_view` 中 6 次 `stalled`（开村民窗被实体碰撞箱卡住）。
- **因果链（已定位）**：工具是**按任务文本关键词动态暴露**的（`harness/agent.py:129-132`、`harness/curriculum.py:796-799`：出现 "trade/交易/村民" 才加 `mc_skill_trade`，出现 "brew/酿" 才加 `mc_skill_brew`）。r15 的 stage 只换出生点/背包，任务文本仍是通用课程 → 1593 次工具调用中 **`mc_skill_trade` 0 次、`mc_skill_brew` 0 次、`mc_window_act` 0 次**，场景能力完全没被验证。
- **验收目标（追加）**：r16 起 `village_trade` 路至少出现 1 次 `mc_skill_trade`，`brewing_alchemy` 路至少 1 次 `mc_skill_brew`。
- **D1 r16 实测（通过）**：`nju_nju_3_village_trade` 首次 `mc_window_view(entity_id=2071)` 返回 `merchant` 窗口，随后 `mc_skill_trade(option=0, times=12)` **成功**（paper 24→emerald 1，max_uses 12）。`glm_glm_3_village_trade` 仍有 1 次 `no_window`（villager did not open a GUI），待观察是否复现 P2-24。
- **建议注入**（仅供参考，按 `#6` 设计为准）：
  - `village_trade` → 首集 `minecraft:adventure/trade`（已有 emerald 32 + wheat 32 + stick 64）
  - `brewing_alchemy` → 首集 `minecraft:nether/brew_potion`（已有 blaze_powder/nether_wart/glass_bottle）
  - `cave_combat` → 首集 `minecraft:adventure/kill_a_mob` + 存活 ≥180s（已有铁甲/弓/盾）
- **验收目标**：r16 起三 stage 的首集 target 与 stage 语义一致，分数可横向比较；`mc_window_view` stalled ≤1。

> 任何会话需要特定实机复现/验收环境时，按以下 5 行模板追加需求单；`#6` 实现为 `l3_wild_endurance_runner.py --stage <name>` 并接入 `v2_fleet.py` 后标为 `READY_FOR_RUN`，由 `#1 主 AGY` 合入上机。
>
> ```markdown
> ### SCENE-XXX | 状态: OPEN | 提单人: #X | stage名: <name>
> - **地形/结构要素**：(如 locate fortress / fill 3x3 岩浆池 / 混合有业+无业村民)
> - **初始背包与成就**：(...)
> - **死亡拉回规则**：(如设 spawnpoint 或下界死亡回传补装)
> - **核心验证目标**：(验证哪个 Bug 修复或哪个成就链路)
> ```


