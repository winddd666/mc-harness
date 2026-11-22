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

### BUG-005 | P1 | Slice B（#2 子 AGY）| 状态: FIXED / 并入 BUG-026（r11 时代死亡单，后续由 BUG-026 跟踪）
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
- **D1 r24 复核（预检已够用，剩“无 message”）**：8 次 craft 失败全部是真实缺料（`iron_pickaxe` 缺 iron_ingot+stick ×3、`diamond_pickaxe` 缺 diamond ×2、`stone_pickaxe` 缺 cobble+stick ×2、`oak_sign` 缺 planks ×1），**均已正确返回 `missing_materials`**；但其中 7 次 `message=None`，模型只能靠解析 map 自行推断。
  → 剩余只需在 `missing_ingredients` 时生成一句标准提示（如“缺 iron_ingot×3、stick×2：先 smelt raw_iron / 砍 1 棵树”）。

### BUG-008 | P1 | Slice C（#4 Codex #2）| 状态: FIXED（`ef6a8ac`；仅 changed=false 且无 used/gained 才判失败）
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

### BUG-010 | P1 | Slice B（#2 子 AGY）| 状态: FIXED（`dd274ba`；`retry_after_ready` + `eat_deferred`）
- **跨模型影响**：`hy4_hy_10_enchant_anvil`（同一死亡链）
- **日志证据**：复活后 `guardian.action.completed eat` → `not_ready: world is still loading (after login, respawn or dimension change)`（94.8s），紧接着 `wrong_server`（111.2s），随后 `player_explosion` 死亡
- **源码定位**：`harness/guardian.py:573-581 _do_eat` 直接 `use_item`，未检查 `status.ready` 或刚发生 `death_or_connection_changed`
- **现象与根因**：复活/换维度窗口内进食必然失败，且被计为动作失败，不重试。
- **验收标准**：`deaths or not status.ready` 时跳过进食并标记 `retry_after_ready`；`ready` 后补一次，不把 `not_ready` 计失败；补离线单测。

### BUG-011 | P1 | Slice B（#2 子 AGY）| 状态: FIXED（`dd274ba`；guardian 不再把 httpx 包成 RuntimeError，返回 network_uncertain）
- **跨模型影响**：`glm_glm_3_village` EP5、`hy4_hy_8_nether` EP7 直接整集 `runtime_error`；全队 5 次 `ReadTimeout`/`ReadError`
- **日志证据**：episode report `error = "生存响应 subcortical_combat 的网络结果不确定：ReadTimeout"`，`status = runtime_error`，该集 `score_delta=0`
- **源码定位**：`harness/guardian.py:195-196` 把 `httpx.HTTPError` 重新包成 `RuntimeError`；`harness/agent.py:1614-1620` 中**原生** `httpx.HTTPError` 会被判为可继续的 `network_error`，但被包装后的 `RuntimeError` 落入 `runtime_error` 分支**终止整集**
- **现象与根因**：一次瞬态网络抖动发生在生存反射里，就把整个 episode 打死；`guardian` 的包装绕过了 agent 的传输层异常分类。
- **验收标准**：`_action` 捕获 `httpx.HTTPError` 后返回 `ok:false, error:{code:"network_uncertain"}` 或带退避重试，不再抛 `RuntimeError`；补离线单测（mock 抛 ReadTimeout 必须不终止 episode）。
- **D1 r12 实测**：仍未修复，`hy4_hy_10_enchant_anvil` EP3 再次整集 `runtime_error`。

### BUG-012 | P1 | Slice Integrator（#1）/ 归属待定 | 状态: FIXED（`26f7213`；`_API_RETRYABLE_CODES` 已含 not_loaded/chunk_not_loaded/world_not_ready）
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

- **r78 门户极小分母复核**：零进展比例 **1/2**，两条都来自 NJU_3 EP12 的槽末 45.422s 集。第一条战斗抢占，第二条开始时集余 **7.282s**，外层预留 4s，实耗 **1.331s** 后 `time_slice/timeout_path=place`。原始比例超门，轨迹定位到槽末剩余预算与单次放置。 [槽末集限](../runs/r78_midgame_portal_40m/nju_nju_3_midgame/20261121-145942-648679.events.jsonl#L1) · [短切片](../runs/r78_midgame_portal_40m/nju_nju_3_midgame/20261121-145942-648679.events.jsonl#L152) · [外层夹限](../src/mc_agent/harness/mc_tools.py#L3070)
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
- **r51→r53 下界同八槽**：`ok=false, stop_reason=time_slice` 从 **7/49→9/35**，均超 10%。失败片中位约 **25.84s**，r53 范围 **25.83–28.27s**。HY4_7 EP01 两片均停在同一 Y64 站位，HY4_10 占 5 片。实测未重现历史约 4s 预算饥饿，原修复状态保留，另核对当前平台上的矿道入口。 [零位移首片](../runs/r53_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-215410-391904.events.jsonl#L173) · [次片](../runs/r53_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-215410-391904.events.jsonl#L198)
- **r54 下界小样本复核**：`time_slice` 失败 **2/6**，超 10%。全波仅六条矿道回执，保持原来预算优化与该比例失败的区别。 [实测波次](../runs/r54_nether_fortress_6bot/triage-r54_nether_fortress_6bot.md#L3)
- **r55 下界**：`time_slice` 失败 **2/18**（11.1%），仍略高于目标。HY4_7 EP02 的 18.4s 失败片是平台 reset。 [失败片](../runs/r55_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261120-235224-139051.events.jsonl#L352)
- **D1 r14 复核**：r14（含 `/2` 的本地树）`mine_dfs` n=196，**中位 18.9s / p90 54.2s / max 78.6s**，0 进展 `time_slice` **19/196（9.7%）**，已接近验收线；但 v2-integrated **HEAD 仍是 `/8`**，请 Codex #1 尽快提交 `/2` 并 rebase，避免下一波换树后又退回。
- **D1 r13 复核**：s-mining 未提交改动已把 `/8` 改为 `/2`（`attempt_budget = (remaining - 0.15) / 2`）。r13 实测 `mine_dfs` n=265，**中位 20.5s / p90 51.8s / max 84.7s**，0 进展 `time_slice` **44/265（16.6%）**——较 r12（124/241）大幅收敛，但**仍未达标**；且 `/2` 为未提交状态，需 Codex #1 提交后再验收。

- **r59 下界零进展门禁失守**：同五槽，失败片 **0/27→2/10**。两片均为 HY4_9 EP03，actor 申请 60s，实耗 **25.909／26.025s**，目标先 Y15 后 Y18，均挖 0 块且原地。旧回归的约 4s 子调用饥饿未复现，应从当前矿口、深度策略和子动作截止诊断。 [首片](../runs/r59_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-015327-959696.events.jsonl#L161) · [次片](../runs/r59_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-015327-959696.events.jsonl#L604) · [切片进度确认](../src/mc_agent/skills/mining_dfs.py#L6549)
- **r60 同五槽加重**：探矿失败 time_slice **2/10→7/17**，涉及 HY4_5／9 两个 EP03，18.311–26.209s。六条无可测位移，一条约 0.8m，五条明确挖 0，另两条缺挖掘计数字段。门槛继续失败，需核对当前阶段如何耗尽切片。 [HY4_5 原地失败](../runs/r60_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-023638-409410.events.jsonl#L185) · [HY4_9 首次](../runs/r60_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-022459-216493.events.jsonl#L412)

### BUG-019 | P1 | Slice A（#3 Codex #1）/ Slice C（#4）| 状态: FIXED（r13 验证：56→11）
- **跨模型影响**：**17 bots / 56 次** `mine_dfs:repeated_identical_failure_blocked`（r11 仅 1 次），另有 `staircase:repeated_identical_failure_blocked` 8 次
- **日志证据**：工具返回 `time_slice` 且消息为"本轮时间预算已用尽，已保留矿井与回撤路径；**可用相同 action 继续**"，模型照做后立刻被防卡死拦截（`repeat_count=2/3`），0.0s 返回
- **源码定位**：防卡死拦截在 `harness/agent.py` 的重复失败检测；`time_slice` 文案（`mining_dfs.py` 的 slice-timeout 分支，`7f87334`）却邀请相同参数重试
- **现象与根因**：工具契约与 Harness 防循环规则自相矛盾；17 路各浪费 2~4 个 actor 请求
- **最小修复**：`time_slice` 返回 `suggested_next`（如加大 `max_duration_s`、先 `mc_move_to` 换 16 格外、或 `action='climb_out'`），文案禁止"相同 action 继续"；或在防卡死判定中豁免 `time_slice` 同参续跑 1 次
- **验收标准**：r13 `mine_dfs:repeated_identical_failure_blocked` ≤ 5 次且 0 次由 `time_slice` 文案直接触发
- **D1 r13 复核**：`mine_dfs:repeated_identical_failure_blocked` **56→11（17→4 bots）**；`time_slice` 文案已改为「本轮在时间限制内未确认进展，请检查当前位置和障碍后再选择下一步。」，不再诱导同参重试。通过。

### BUG-020 | P1 | Slice Integrator（#1）| 状态: OPEN（r34 NJU 5xx 重试耗尽复现）

- **r79 官方 GLM_1 终版 8 集 `not_ready`**。新版保住终版，前八集均在明确死亡后 **0.2–0.4ms** 结束，actor 请求 0。石器七集免罚，第九集执行 14 请求并正常 `answered`。另两官方槽均 0 `not_ready`。BUG-020 的整集门仍超。 [第八集](../runs/official_r79_35m_glm1/GLM_1/20261121-205313-860321.events.jsonl#L5) · [第九集](../runs/official_r79_35m_glm1/GLM_1/20261121-205329-538873.events.jsonl#L320) · [官方终版](../runs/official_r79_35m_glm1/GLM_1/official-official_r79_35m_glm1-GLM_1-20261121-205140.json#L7)

- **r77 日出四槽三条 `runtime_error`**。HY4_3 EP10 与 HY4_4 EP18 均为 `not_connected`，HY4_3 EP13 为模型输出截断；另三条 `disconnected` 与一条 `network_error/chunk_not_loaded` 单列。四槽均写终版。 [未连接 EP10](../runs/r77_day12_real_drop_60m/hy4_hy_3_bare/20261121-204449-178532.events.jsonl#L95) · [未连接 EP18](../runs/r77_day12_real_drop_60m/hy4_hy_4_bare/20261121-210416-172785.events.jsonl#L9) · [截断](../runs/r77_day12_real_drop_60m/hy4_hy_3_bare/20261121-205705-956016.events.jsonl#L499)

- **r77 官方 GLM_1 仍有 7 集死后 `not_ready`**。加载版 `6263077`，早于课程免罚补丁。七集最后明确死亡到终态相隔 **0.2–185.9ms**，期间未见重生，四集 actor 请求为 0。MoveTest 和 5Ease 同波均无 `not_ready`，5Ease EP03 另有一条 MC API SSL `ReadError`，落 `network_error` 后 wrapper 继续并写终版。 [GLM_1 EP07](../runs/official_r77_40m_glm1/GLM_1/20261121-200955-882480.events.jsonl#L5) · [最长间隔 EP11](../runs/official_r77_40m_glm1/GLM_1/20261121-201257-948412.events.jsonl#L157) · [5Ease 网络终态](../runs/official_r77_40m_5ease/5Ease_Bot/20261121-200839-027237.events.jsonl#L9) · [actor 网络分类](../src/mc_agent/harness/agent.py#L1643)

- **`8df2f2a` 的等待分支由 `317a6c7` 修复**（BUG-089）。官方 r79 GLM_1 已记录同类 `not_ready` 集并继续开课。报废计数仍超门，程序续课和单集未就绪需各自验收。 [旧版 r82 加载指纹](../runs/r82_midgame_portal_30m/hy4_hy_9_midgame/20261121-203249-644593.events.jsonl#L1) · [新版实际又开课](../runs/official_r79_35m_glm1/GLM_1/20261121-205246-956282.events.jsonl#L3) · [课程分类](../src/mc_agent/harness/curriculum.py#L2746)

- **12:01 官方 r76 三槽缺终版**。GLM_1、5Ease、MoveTest 末条 actor 时间 **12:01:12–12:01:40**，当时为工具发起或限速等待。到 **12:03** 旧 PID 已退出，官方终版 **0/3**；四个同时运行的 Fleet 波仍有原 PID 与新事件。官方 r77 三个新进程在约 12:02 开课，时长改为 40 分钟。旧进程退出原因待外层回执。 [GLM_1 末条](../runs/official_r76_80m_glm1/GLM_1/20261121-195901-529871.events.jsonl#L81) · [5Ease 末条](../runs/official_r76_80m_5ease/5Ease_Bot/20261121-200041-544018.events.jsonl#L79) · [MoveTest 末条](../runs/official_r76_80m_movetest/MoveTest_01/20261121-195854-934878.events.jsonl#L187) · [新版开课](../runs/official_r77_40m_movetest/MoveTest_01/20261121-200217-673376.events.jsonl#L1)

- **11:05 六目录共 13 槽停写**：官方 r75 三槽、r76 裸装三槽、r75 日出四槽、r80 中期三槽，末条 actor 时间集中于 11:05:07–11:05:48。到 11:10 时旧 runner PID 均已退出，0/13 有终版，末条只有等待、工具开始或预准备。门禁未检出 runner traceback，停止原因待证。同期 r80 门户仍持续写盘。 [官方](../runs/official_r75_80m_movetest/MoveTest_01/20261121-190227-877950.events.jsonl#L203) · [裸装](../runs/r76_bare_real_drop_60m/nju_movetest_01_bare/20261121-190313-471279.events.jsonl#L488) · [日出](../runs/r75_day12_real_drop_60m/hy4_hy_4_bare/20261121-190518-883825.events.jsonl#L130) · [中期](../runs/r80_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-190546-648555.events.jsonl#L14) · [现有门禁](../../s-triage/scripts/d1_acceptance.py#L93)

- **07:05 北京时间跨波传输中断**：UTC **23:05:08.169–08.227**，r79 门户三槽、r75 裸装 MoveTest 和 r74 日出三槽共七条 Guardian `ReadTimeout`，首尾 **57.869ms**；七个 actor 在 **19.489–19.742** 落断连。同窗官方 MoveTest／GLM_1 有 `ConnectError` 并收尾当前 actor，5Ease 在 13.065s 见同码。邻档另有 `not_connected`。四波已结算的 11 个相关 episode 报告使用同一个配置 API 地址。Guardian 的首次失败记录包住状态与事件拉取，两者任一失败可进入三次失败门。源端故障未在本批日志暴露。 [r74 日出](../runs/r74_day12_real_drop_60m/hy4_hy_3_bare/20261121-150243-092978.events.jsonl#L134) · [r75 裸装](../runs/r75_bare_real_drop_60m/nju_movetest_01_bare/20261121-150300-413111.events.jsonl#L264) · [r79 门户](../runs/r79_midgame_portal_40m/nju_nju_3_midgame/20261121-150405-511034.events.jsonl#L358) · [官方 MoveTest](../runs/official_r74_80m_movetest/MoveTest_01/20261121-150346-042893.events.jsonl#L102) · [Guard 观察](../src/mc_agent/harness/guardian.py#L1472)

- **r73 官方 GLM_1 再超门**：三条 `not_ready`，EP08／09 各末死后 **0.330／0.355ms**；EP03 课前战斗抢占，actor 启动后约 **0.18s** 便失败，0 请求、无同集死亡回执。5Ease 同加载版 0 条。 [EP03 课前动作](../runs/official_r73_80m_glm1/GLM_1/20261121-132323-719387.events.jsonl#L3) · [EP03 终态](../runs/official_r73_80m_glm1/GLM_1/20261121-132323-719387.events.jsonl#L10) · [EP09 终态](../runs/official_r73_80m_glm1/GLM_1/20261121-133217-408311.events.jsonl#L10) · [口径](../src/mc_agent/harness/agent.py#L1646)

- **r72 两官方同槽改善**：r71→r72 的死后 `not_ready` 为 **9→1（5Ease）／7→1（GLM_1）**，本轮均低于 ≤2，外层重连 0。仅存两条末死至终态 **179.950／183.189ms**，期间无重生。r72 MoveTest 同时仍有四条，保留 OPEN。 [5Ease](../runs/official_r72_80m_5ease/5Ease_Bot/20261121-130700-873913.events.jsonl#L84) · [GLM_1](../runs/official_r72_80m_glm1/GLM_1/20261121-124847-254900.events.jsonl#L152)
- **r72 官方 MoveTest 终版**：EP07／08／19／23 共四条 `runtime_error/not_ready`，各自最后一次死亡回执至终态间隔 **0.382／0.362／213.960／0.282ms**，期间无重生。EP08 actor 请求 0。官方跑满 80 分钟、重连 0，超出单槽 ≤2 门槛。 [EP08 终态](../runs/official_r72_80m_movetest/MoveTest_01/20261121-114752-382255.events.jsonl#L10) · [EP19 终态](../runs/official_r72_80m_movetest/MoveTest_01/20261121-122358-838903.events.jsonl#L185) · [错误分类](../src/mc_agent/harness/agent.py#L1645)
- **r70 官方 5Ease 临时世界未就绪导致 10 集报废**：全部终态 `[not_ready]`，且每条均在同集明确死亡后 **0.2–188ms** 内结束、未记录重生。EP14／15／30／32／33 五集 actor 请求为 0。同官方 wrapper 最终 `completed`、重连 0，排除了整槽启动失败的解释。当前 actor `ApiError` 临时清单含 `world_not_ready`，此处回执为 `not_ready`。合并该错误口径并在重生未就绪期间走有界等待或课程重排，验收整集报废 ≤2。 [EP14 死亡](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-094356-917731.events.jsonl#L3) · [EP14 终态](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-094356-917731.events.jsonl#L5) · [EP31 活跃 actor 路径](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-103112-711817.events.jsonl#L150) · [错误分类](../src/mc_agent/harness/agent.py#L1645) · [官方终版](../runs/official_r70_80m_5ease/5Ease_Bot/official-official_r70_80m_5ease-5Ease_Bot-20261121-091803.json#L7)
- **r71 官方新版过程仍复现**：GLM_1 EP07 加载 `3d1a19c`，工具被护航抢占后出现明确死亡，转 `preparing`，**0.190s** 内落 `runtime_error/[not_ready]`。e3c349a 的战斗重生回执与 actor 尚未就绪的终态分类应分别复验。 [死亡](../runs/official_r71_80m_glm1/GLM_1/20261121-105118-741619.events.jsonl#L238) · [终态](../runs/official_r71_80m_glm1/GLM_1/20261121-105118-741619.events.jsonl#L241) · [actor 异常分类](../src/mc_agent/harness/agent.py#L1645)
- **r71 两官方终版**：同 r70 角色的 `not_ready` **10→9（5Ease）／2→7（GLM_1）**。r71 的16条都紧跟同集明确死亡，最后死亡到落盘 **0.171–190.073ms**，期间无重生回执。GLM_1 另有一集 ReadTimeout。两槽官方 `completed`、重连 0。 [5Ease 终态](../runs/official_r71_80m_5ease/5Ease_Bot/20261121-112419-977675.events.jsonl#L117) · [GLM_1 终态](../runs/official_r71_80m_glm1/GLM_1/20261121-113948-745566.events.jsonl#L312) · [分类](../src/mc_agent/harness/agent.py#L1645)
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
- **D1 r34 复现**：NJU 四路在波头共 **7/83 集**以 5xx `runtime_error` 收尾，终端 JSONL 为 **502×5、503×1、500×1**；GLM 和 HY4 无同类集。NJU_3 EP01 两次 502 退避各 20s，第三次 502 仍按整集失败，DS_2 EP02 的第三次为 503 `Loading model`。保持已有 5xx 重试清单，验证耗尽后进入可重排 `paused/transient`，原门槛整集报废 ≤2。 [NJU_3 EP01](../runs/r34_short/nju_nju_3_village_trade/20261119-230802-518258.events.jsonl#L7) · [DS_2 EP02](../runs/r34_short/nju_ds_2_cave_combat/20261119-230908-867093.events.jsonl#L25) · [模型重试](../src/mc_agent/harness/model.py#L674)

---
- **r46**：14/14 写出终版，actor `runtime_error` 与进程退出缺终版均为 0。Day 2 HY4_8 在 6 次明确死亡集中出现的最后三集，两次 `disconnected` 紧跟致死回执，另一集 `not_loaded` 归为 `network_error`。末次冷却 60s 大于所余 58s。 [死亡后断开](../runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-090654-015616.events.jsonl#L289) · [区块错误](../runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-091722-147672.events.jsonl#L158) · [再恢复预算](../runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/stdout.log#L300)

- **r57 裸装断连样本**：HY4_4 EP08 取岩浆桶遇三次明确死亡，最后一次箭伤后以直接 `ApiError(not_connected)` 结束。波末剩约 58s，重新连接需等 60s 死亡冷却，最终仍写汇总。加载版 `agent.py:1638–1650` 单列 watchdog 的 `RunDisconnected`，直接 `ApiError(not_connected)` 归 `runtime_error`；本案缺具体发出请求的 API 位置。复验直接断连与可恢复冷却的分类。 [末次死亡](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/20261121-012022-831993.events.jsonl#L143) · [集尾](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/20261121-012022-831993.events.jsonl#L145) · [波尾冷却](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/stdout.log#L466) · [异常分支](../src/mc_agent/harness/agent.py#L1638)

- **r60 裸装 MC API 个案**：HY4_4 EP05 铁器课尝试从箱中取 11 小麦，调用约 45s 后 `ReadTimeout`，记 runtime_error。该槽继续执行并写出终版，区别于要塞的整路 BUG-083。 [取箱起始](../runs/r60_day12_full_45m/hy4_hy_4_bare/20261121-024511-342493.events.jsonl#L24) · [集尾结果](../runs/r60_day12_full_45m/hy4_hy_4_bare/20261121-024511-342493.events.jsonl#L27)

- **r66 Day1/Day2 终版**：HY4_10 EP10／11 都有明确死亡后 `not_loaded`，按 `network_error` 收尾。EP12 再次在死亡后收直接 `not_connected`，仍归 `runtime_error`。HY4_3 EP04 一次脱困 20.9s 后无结构化回执并以 `MC API ReadTimeout` 报废。四槽均终版，无 provider 硬 429／502。 [HY4_10 EP10](../runs/r66_day12_full_45m/hy4_hy_10_bare/20261121-080355-501560.events.jsonl#L114) · [EP11](../runs/r66_day12_full_45m/hy4_hy_10_bare/20261121-080449-066529.events.jsonl#L150) · [EP12](../runs/r66_day12_full_45m/hy4_hy_10_bare/20261121-080605-969715.events.jsonl#L199) · [HY4_3 脱困](../runs/r66_day12_full_45m/hy4_hy_3_bare/20261121-073837-649621.events.jsonl#L416) · [错误归类](../src/mc_agent/harness/agent.py#L1645)
- **r68 裸装复现脱困传输异常**：HY4_5 EP01 首次石器集，最后一次 `mc_skill_unstick` 约 **9.4s** 后工具中止并抛 `MC API ReadTimeout`，12 次 actor 请求、0 分。下一集实际做出石器、+10，槽最终 2700.1s。与 r66 HY4_3 的脱困同一错误形态，具体哪一次内部 API 缺结构化记录。 [工具中止](../runs/r68_bare_real_drop_3bot/hy4_hy_5_bare/20261121-073250-294642.events.jsonl#L236) · [集尾](../runs/r68_bare_real_drop_3bot/hy4_hy_5_bare/20261121-073250-294642.events.jsonl#L239) · [复跑终版](../runs/r68_bare_real_drop_3bot/hy4_hy_5_bare/wild-endurance-r68_bare_real_drop_3bot_hy_5_bare-20261121-073241.json)

- **r61c／r62 运行停写，原因未证**：北京时间 20:03 前后，r61c 日出仍 0/4 终版，r62 要塞只有 HY4_1 的未就绪终版。六个无终版槽的本地 runner 已无匹配。多路原件停在工具开始或模型等待，缺 Python traceback。验收脚本依赖 traceback 的 runner abort 项为 0，本情况仍按缺终版记录。 [DS_2 最后工具开始](../runs/r61c_day12_full_45m/nju_ds_2_bare/20261121-035742-189492.events.jsonl#L534) · [r62 HY4_2 最后阶梯](../runs/r62_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-035647-174443.events.jsonl#L527)

### BUG-021 | P1 | Slice D2（#6 DeepSeek #2）| 状态: FIXED（`26f7213`；残余启动冷却超时单独立为 BUG-039）
- **影响**：`nju_guitest_01_nether` 在 r13 **整路 0 事件、0 分数**（22 路只剩 21 路）
- **日志证据**：`runs/r13_22bot/nju_guitest_01_nether/stdout.log`
  `DeathLoopCooldownError: [death_loop_cooldown] bot is in death loop cooldown, 129s remaining`
  抛点 `scripts/l3_wild_endurance_runner.py:797 bot.connect(respect_reconnect_interval=False)` → `bot.py:192-204`
- **现象与根因**：上一波该 bot 死亡进入 129s 冷却，runner 启动时强制忽略重连间隔，异常直接终止进程，且没有等待重试
- **最小修复**：捕获 `DeathLoopCooldownError`，按 remaining 睡眠后重试（最多 2~3 次）；或启动探测到冷却时先等待并打印 ETA
- **验收标准**：r14 起 22/22 路均有 `*.events.jsonl` 与 summary；冷却场景下单路最多延迟 130s 启动而非丢失
- **D1 r14 复核**：`ds_ds_4_nether_fortress` 再次因 `DeathLoopCooldownError: 120s remaining` 整路丢失。修复提交 `26f7213` **已包含** try/except 睡眠重试，但 r14 的 runner 进程加载于 20:14（早于该提交 20:18），跑的是旧代码。
- **附加要求（发布纪律）**：`launch` 前打印 `git rev-parse HEAD` 并与运行树比对；或在 runner 启动日志写入 commit，避免"修复已合但实机仍跑旧码"。

- **r61 启动准备超时**：r61 要塞 3 路和日出 4 路均为 not_ready，r61b 要塞重跑再 3 路未就绪。要塞报该 SSH 调用等待 30s，日出报 45s，均无 actor 轨迹。记录只定位启动准备步骤，不推断远端故障具体位置。 [要塞重试终版](../runs/r61b_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/wild-endurance-r61b_stronghold_end_40m_movetest_01_village_to_stronghold_end-20261121-031745.json#L13) · [日出终版](../runs/r61_day12_full_45m/nju_ds_2_bare/wild-endurance-r61_day12_full_45m_ds_2_bare-20261121-031650.json#L13) · [启动异常写汇总](../scripts/l3_wild_endurance_runner.py#L2175)

### BUG-022 | P1 | Slice A（#3 Codex #1）| 状态: FIXED（r15 验证：铁阶段 100 分）
- **跨模型影响**：`ds_ds_2_iron`（95→20→25）、`hy4_hy_4_iron`（95→20→20）；两路把 25~40 个 actor 请求耗在 `shiny_gear`
- **日志证据（r13 `ds_ds_2_iron`）**：`explore` 每次 17~27s 稳定下潜 y=129→55（12 次 `time_slice_progress`，属正常），到 **y=47 触发 `shaft_exhausted`**，随后 `return_collar → no_collar_recorded`、`reset → nothing_to_reset`、`explore → needs_surface_climb`，再由 `climb_out` 从 y=56 爬到 101 反复 `time_slice`，直到 episode 超时
- **源码定位**：`skills/mining_dfs.py:5132-5162`（深处无法回撤时强制 `needs_surface_climb`）、`mining_dfs.py:5214-5230`（回撤失败同样强制爬升）；`shaft_exhausted` 在目标层上方 ~100 格（y=47 vs target -54）提前判定
- **现象与根因**：下潜切片 × episode 调用预算不足以到达目标层；`shaft_exhausted` 后引擎拒绝在深处续挖，要求回地表，而 `climb_out` 又需多次调用——形成「下潜→耗尽→爬升→超时」死循环
- **最小修复**：① `shaft_exhausted` 时若当前 y 距 target_y >10 且镐可用，允许就地开新分支（`offset/reopen`）而非强制回地表；② `climb_out` 支持跨调用续爬（保存进度）并返回 `next_action="climb_out"`；③ 提示模型为深目标使用 60~90s 切片
- **验收标准**：r14 `ds_ds_2_iron`/`hy4_hy_4_iron` 至少一路拿到 `mine_diamond`，分数 ≥60；`needs_surface_climb` 导致的 0 进展调用 ≤2/路
- **D1 r14 复核**：`ds_ds_2_iron` 20、`hy4_hy_4_iron` 20，未改善。
- **D1 r15 复核（通过）**：同一 iron 场景由 `hy4_hy_2_iron` 承接，拿到 **100 分**（此前 iron 槽 20~25），diamond 链路恢复；说明 BUG-018 的预算修复即 BUG-022 的主要解。

### BUG-023 | P1 | Slice B（#2 子 AGY）+ D2（场景）| 状态: FIXED（`e680b1f`；`escape_deep_water_to_shore` + 30s 不再下水；r16/r17 trident 死亡 0）
- **影响**：`hy4_hy_9_deep_water` 在 r12/r13/r14 死亡 **3/9/10**，10 次全部为 `trident`，分数长期 25~40
- **日志证据**：死亡坐标集中在 `(-6345.3,62.9,-7614.5)` ~ `(-6368.5,62.6,-7648.5)`；每次事故 `escape_water ok=true` 但仍在 2~3 秒内被 5+ 次 trident 连击（health 15.5→2.9）打死；`raise_shield` 仅出现 2 次
- **源码定位**：`harness/guardian.py` 深水分支只做 `escape_water` 位移，没有"脱离水域到岸/上浮换气 + 盾挡 + 反击 drowned"的组合；`skills/safety.py` 水下脱困优先级低于持续受击
- **最小修复**：① 深水受击时优先 `mc_move_to` 最近干燥陆地/向上冲出水面（而非原地小幅闪避）；② 连续 3 次受击且仍在水中 → 强制上岸并设 30s 不再下水；③ 有盾时先 `equip_shield`+`raise_shield` 再退
- **场景侧（D2）**：`deep_water` 出生点距岸 ≤8 格，或保证 20 格内有可站立浅滩，避免刷新即被 drowned 围杀
- **验收标准**：r15 该路 trident 死亡 ≤2，分数 ≥80
- **发布状态（r16 前核对）**：修复 `e680b1f`（盾优先上岸 + 30s 不再下水）**已在 HEAD**，r16 可直接验收。
- **D1 r16 早段复核（正向）**：`hy4_hy_9_deep_water` 已执行 **12 次 `escape_water`、0 死亡**；`hy4_hy_10` 6 次、0 死亡（r15 该路 1 次 trident 死、r14 为 10 次）。门禁 `BUG-023 deep_water trident deaths ≤2` 当前 PASS，待收官确认。

### BUG-024 | P1 | Slice C（#4 Codex #2）| 状态: FIXED（`7dbcdb1` + `_dig_pillar_up` no_support 重试；r16 实测 46→1）

- **r78 门户 `no_support` 再超门**：严格口径 **8**。HY4_8 EP03 四个方向均在同一格 `[7811,75,2783]` 失败，NJU_3 EP05 三方向在另一固定格 `[-9984,81,3011]` 失败，另一条为 NJU_3 EP03。另有三条柱式 `place_failed/no_support`，未进这项门。 [四方向首条](../runs/r78_midgame_portal_40m/hy4_hy_8_midgame/20261121-142345-893612.events.jsonl#L161) · [同格末条](../runs/r78_midgame_portal_40m/hy4_hy_8_midgame/20261121-142345-893612.events.jsonl#L210) · [NJU_3 同格](../runs/r78_midgame_portal_40m/nju_nju_3_midgame/20261121-143844-150033.events.jsonl#L703) · [放置失败收口](../src/mc_agent/skills/library.py#L7647)
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

### BUG-026 | P1 | Slice B（#2 子 AGY）+ Slice D2（场景）| 状态: OPEN（r22 回潮：29 死 / 258s）

- **r76→r77 日出同四槽回潮**。明确死亡 **9→32**，HY4_3／4／10 各 **13／12／7**，DS_2 0。模型取消 **35→138**，日志计划的取消冷却 **55.0→206.1s**。整波分别超死亡与取消冷却门。 [r77 终版榜单](../runs/r77_day12_real_drop_60m/triage-r77_day12_real_drop_60m.md#L7) · [D1 门禁](../../s-triage/scripts/d1_acceptance.py#L213)

- **r79 门户开波三路同步断连候选**：三槽均为 EP01，Guardian `ReadTimeout` 在 UTC **23:05:08.197–08.227** 内出现，间差 **29.784ms**；actor 在 **23:05:19.489–19.742** 各自落断连。结算补读 **5／4／39** 条明确死亡，**4／3／38** 重生，未见独立连接变化回执。NJU_3 同时有模型传输 `ConnectError` 重试。死亡聚合无事件 ID，源端事件实际发生时刻不可由补读时刻反推。继续核对同时间窗其他运行及本波终版。 [HY4_8](../runs/r79_midgame_portal_40m/hy4_hy_8_midgame/20261121-150400-367510.events.jsonl#L135) · [HY4_9](../runs/r79_midgame_portal_40m/hy4_hy_9_midgame/20261121-150410-298003.events.jsonl#L151) · [NJU_3](../runs/r79_midgame_portal_40m/nju_nju_3_midgame/20261121-150405-511034.events.jsonl#L358) · [断连判定](../src/mc_agent/harness/agent.py#L1126)

- **r72 日出同四槽回潮**：r71→r72 明确死亡回执 **23→45**、重生 **21→42**，请求取消冷却 **178.9→242.1s**，两门超限。HY4_3／10 合计 **32** 条死亡，HY4_10 EP06 的 `not_connected` 随后补读死亡并断连结算，缺独立连接变化回执。 [死亡与断连](../runs/r72_day12_real_drop_60m/hy4_hy_10_bare/20261121-131136-097546.events.jsonl#L42) · [HY4_10 EP13](../runs/r72_day12_real_drop_60m/hy4_hy_10_bare/20261121-133438-063447.events.jsonl#L185) · [取消冷却](../src/mc_agent/harness/model.py#L899)

- **r72 官方两角色复验**：r71→r72 明确死亡 **47→43（5Ease）／69→11（GLM_1）**，r72 重生 **43／10**，官方均 80 分钟。5Ease 仍超 ≤25，GLM_1 通过。5Ease EP06 有 12 条热地板伤害和一例热地板致死。 [5Ease 终版](../runs/official_r72_80m_5ease/5Ease_Bot/official-official_r72_80m_5ease-5Ease_Bot-20261121-115855.json#L6) · [GLM_1 终版](../runs/official_r72_80m_glm1/GLM_1/official-official_r72_80m_glm1-GLM_1-20261121-115906.json#L6) · [致死](../runs/official_r72_80m_5ease/5Ease_Bot/20261121-121740-255689.events.jsonl#L268)
- **r71→r72 官方 MoveTest**：明确死亡 **6→42**、重生 **6→38**，本轮取消冷却请求等待 **117.5s**、外层重连 0。EP23 四死，最后一次前守护回复 `no_safe_retreat`、位移 0，随后同集先重生再死亡。 [零位移撤退](../runs/official_r72_80m_movetest/MoveTest_01/20261121-123159-214577.events.jsonl#L110) · [最后死亡](../runs/official_r72_80m_movetest/MoveTest_01/20261121-123159-214577.events.jsonl#L124) · [撤退判定](../src/mc_agent/skills/combat_env.py#L2028) · [官方终版](../runs/official_r72_80m_movetest/MoveTest_01/official-official_r72_80m_movetest-MoveTest_01-20261121-112252.json#L6)
- **r70→r71 同官方双角色**：5Ease **49→47** 明确死，GLM_1 **46→69**，r71 明确重生 **38／60**。新槽的计划取消冷却 **193.9／266.2s**，重连均 0。属于两份官方单槽。 [5Ease 终版](../runs/official_r71_80m_5ease/5Ease_Bot/official-official_r71_80m_5ease-5Ease_Bot-20261121-103837.json#L6) · [GLM_1 终版](../runs/official_r71_80m_glm1/GLM_1/official-official_r71_80m_glm1-GLM_1-20261121-103850.json#L6)
- **r70 日出四槽回潮**：同 r69 的一小时配置，明确死亡 **21→35**，r70 为近战 27／箭伤 6／玩家爆炸 2，31 条重生。两条 `disconnected` 分别为 HY4_4 EP09 的第四死和 HY4_10 EP11 的第三死，明确死亡回执至断连终态仅 **0.1–0.2ms**。终态前两槽均先记录 `ApiError/not_connected`，事件落盘最后补读死亡，两槽后来各以约 329／316 秒的间隔重新开集并写终版。请求取消的冷却开始合计约 **201.0s**，墙钟仍需结束回执。 [HY4_4 接口失败](../runs/r70_day12_real_drop_60m/hy4_hy_4_bare/20261121-111927-804548.events.jsonl#L383) · [HY4_10 接口失败](../runs/r70_day12_real_drop_60m/hy4_hy_10_bare/20261121-112703-876626.events.jsonl#L132) · [HY4_4 零位移](../runs/r70_day12_real_drop_60m/hy4_hy_4_bare/20261121-111927-804548.events.jsonl#L374) · [HY4_4 断连](../runs/r70_day12_real_drop_60m/hy4_hy_4_bare/20261121-111927-804548.events.jsonl#L386) · [HY4_10 断连](../runs/r70_day12_real_drop_60m/hy4_hy_10_bare/20261121-112703-876626.events.jsonl#L135) · [断连终态](../src/mc_agent/harness/agent.py#L1127)
- **r70→r71 官方 MoveTest 同 80 分钟**：明确死亡 **41→6**，r71 为近战 4、箭伤 1、摔落 1。EP04 占三死，六死各有重生回执；官方重连仍 0。两版与课程构成都变化，作为同角色终版对照。 [r71 官方终版](../runs/official_r71_80m_movetest/MoveTest_01/official-official_r71_80m_movetest-MoveTest_01-20261121-100121.json#L6) · [EP04 末次死亡与重生](../runs/official_r71_80m_movetest/MoveTest_01/20261121-101043-978900.events.jsonl#L148) · [重生控制](../src/mc_agent/harness/guardian.py#L1384)
- **r70 官方双槽死亡复验**：5Ease／GLM_1 各 **49／46** 明确死亡，分别 38／44 重生。5Ease EP31 有四次近战死，最终死后 `not_ready`。GLM_1 EP26 六死，14 个 actor 请求全部取消。两槽实际均为 `hy4-flash-free`。 [5Ease EP31 最后一死](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-103112-711817.events.jsonl#L147) · [GLM_1 EP26 末次](../runs/official_r70_80m_glm1/GLM_1/20261121-102915-075206.events.jsonl#L154) · [Guardian 断连观察](../src/mc_agent/harness/guardian.py#L1384)
- **r70 官方 MoveTest 再失败**：41 死，近战 32／箭伤 8／玩家爆炸 1，**35/41** 落在 EP10–24。EP23 六次近战死亡，前五次同位重生，末次从 12.33 血到 0.8 血期间撤退零位移并报无屋顶，随后死亡。59 次冷却的请求等待合计 **189.366s**。r71 官方 MoveTest 已加载 `0d8d3da`，待真正暴露时复验。 [撤退零位移](../runs/official_r70_80m_movetest/MoveTest_01/20261121-093429-678378.events.jsonl#L172) · [无屋顶](../runs/official_r70_80m_movetest/MoveTest_01/20261121-093429-678378.events.jsonl#L185) · [末次死亡](../runs/official_r70_80m_movetest/MoveTest_01/20261121-093429-678378.events.jsonl#L198) · [撤退判定](../src/mc_agent/skills/combat_env.py#L2028)
- **r69 官方 GLM_1 复验未过死亡门**：实跑模型 `hy4-flash-free`。43 条显式死亡，近战 32、箭伤 10、出世界 1。42 条集中于 EP15–28。EP18 在 146.761s 内六次近战死亡，22 个 actor 请求中 21 个取消。64 条取消冷却开始事件请求等待合计 **223.930s**，该等待可被抢占，实耗门缺结束回执。EP18 首死前无盾、掩体失败、撤退零位移，前五次重生同坐标。集内已触发提前反射且成功撤过 1.8m，随后重新陷入无安全退路。 [首次拒绝](../runs/official_r69_80m_glm1/GLM_1/20261121-085638-697822.events.jsonl#L13) · [提前反射](../runs/official_r69_80m_glm1/GLM_1/20261121-085638-697822.events.jsonl#L97) · [撤退拒绝分支](../src/mc_agent/skills/combat_env.py#L2028) · [EP18 首次死亡](../runs/official_r69_80m_glm1/GLM_1/20261121-085638-697822.events.jsonl#L26) · [EP18 末次死亡](../runs/official_r69_80m_glm1/GLM_1/20261121-085638-697822.events.jsonl#L224) · [EP25 冷却被抢占](../runs/official_r69_80m_glm1/GLM_1/20261121-090734-161037.events.jsonl#L60) · [可取消等待](../src/mc_agent/harness/model.py#L899)
- **r72／r69 日出加载版**：`0d8d3da` 含 `e160e7f`，危急箭压与撤退失败重查掩体的专测通过。待实际带箭压的零位移撤退、掩体结果及死亡回执。 [掩体检查](../src/mc_agent/harness/guardian.py#L2718) · [专测](../tests/test_generation_guard.py#L9405) · [r72 新版](../runs/r72_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-094530-854837.events.jsonl#L1)
- **跨模型影响（r15 收官 21 路）**：死亡 **51 次**（`notable` 去重；incident 口径 57），r14 为 28；`model.failed(cancelled)` **141→247**，`cancel_cooldown_wait` **286.3s→454.2s**
- **D1 过夜主轨（`overnight_12bot`，4h/12 路）**：**110 次 notable 死亡**（incident 144）：`ds_4_nether_fortress` **41**、`ds_3_enchant_anvil` 23、`ds_1_bare` 13；死因 `mob_attack 38 / lava 22 / arrow 15 / hot_floor 13（岩浆块）/ fall 7`。
  → 下界堡垒（ds_4）是重灾区：4h 41 死 ≈ 每 6 分钟 1 次；`hot_floor` 为新暴露原因。
- **过夜 hy4 审计轨（`hy4_audit_r26`，配置 2 小时）**有 **38 条明确死亡回执**。`hy4_6_nether_bastion` 的 **20 条分布在 11/20 集**，其中 11 条前一条可归因为岩浆伤害。`hy4_2_iron` 有 13 条。旧记录的 45 次 `in_lava` 反射与死亡回执没有一一对应，不能据此计算逃生成功率。 [堡垒课程汇总](../runs/hy4_audit_r26/hy4_hy_6_nether_bastion/curriculum-20261119-094409-012939.json)
- **D1 r18 收官（22 路）**：死亡 incident **37**（notable 去重 28：`mob_attack 12 / lava 6 / on_fire 4 / arrow 3 / trident 1 / drown 1 / wither 1`），**全部集中在 3 路下界 bot**（`ds_4` 7、`hy4_6` 6、`hy4_8` 4 = 17/28）；r19_short 13 死、冷却 205.6s。→ 当前主因已从"地表箭伤"转为**下界岩浆/着火**。
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
- **r41 Day 1 复验未过**：15 路 20 分钟有 **26** 条明确死亡回执，**39** 条混合 Guardian 事件。可由致命受伤回执关联的死因为近战 **13**、箭伤 **6**、玩家爆炸 **2**、火伤 **2**、摔落 **1**，另 **2** 条无明确死因。冷却 **140.4s** 达标。HY4_1 EP02 从 11 血到 4 血三次撤退被安全落脚判定拒绝，最终死于箭伤。GUITest EP04 也在受压时找不到安全撤退点。保留落脚检查，复验撤退被拒后的掩体或备选路径。 [HY4_1 EP02 最后拒绝](../runs/r41_day1_15bot/hy4_hy_1_bare/20261120-060611-164914.events.jsonl#L230) · [箭伤死亡](../runs/r41_day1_15bot/hy4_hy_1_bare/20261120-060611-164914.events.jsonl#L242) · [GUITest EP04](../runs/r41_day1_15bot/nju_guitest_01_bare/20261120-060912-316930.events.jsonl#L429)
- **r43 Day 1**：4 路进入课程，8 条显式死亡全在 HY4_1，均为箭伤。11 条 Guardian 混合事件、54.2s 取消冷却。HY4_1 EP04 连死两次，EP07 又两次，保持裸装弓箭退路验收。 [EP04 第一次](../runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L35) · [EP04 第二次](../runs/r43_day1_5bot/hy4_hy_1_bare/20261120-072235-246493.events.jsonl#L174)

- **r57 下界死亡门禁复核**：终版轨迹有 12 次明确死亡、18 次重生、0 条带类型连接变化、26 条 Guardian 混合告警。验收脚本已按本票标准以明确死亡核对 ≤25，取消冷却 153.2s 也在 200s 内。r41 仍有 26 次明确死亡，保持该波失败。 [r57 死亡样本](../runs/r57_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-010528-021498.events.jsonl#L122) · [验收脚本](../../s-triage/scripts/d1_acceptance.py)

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

### BUG-029 | P1 | Slice C（#4 Codex #2）| 状态: 部分修复（短波达标；4h 长程 8/12 超限，根因已定位）

- **正式赛补证，清掉的杂物沿原路又被捡起**。现场观察为满包后朝面前丢杂物，后续移动把杂物吸回。`5Ease_Bot` EP07 第一次回执报丢 36 圆石，沿原巷道折返期间 `dug=[]`、`gained={}`，下一次清理前的圆石仍为 228，随后再报丢 36。EP10 另有连续三条回执各报丢 110 圆石和 123 深板岩圆石，末两次站位相同。JSONL 没有这批实体的拾取 ID，重复清理已确认，实体复拾来自现场观察。`_ensure_free_slots` 只有调用方提供朝向才转头，常用清理入口未传方向；它以 `drop` 调用未抛错记录拟丢数量。 [EP07 首次](../../../runs/20261121-234628-872325.events.jsonl#L157) · [同巷折返](../../../runs/20261121-234628-872325.events.jsonl#L177) · [EP10 同站位](../../../runs/20261121-235709-070941.events.jsonl#L845) · [丢弃与复查](../src/mc_agent/skills/library.py#L2153) · [清理入口](../src/mc_agent/harness/mc_tools.py#L2662)
- **Slice C 接手，正式赛追加验收**。优先把低价值杂物存入附近可达的自有箱。没有箱时，在不挡回程的安全位置放随身箱，或用富余材料合箱后放置。放置与存入受当前动作预算限制，保留木材、装备、燃料和任务物品的现有保留量。无安全箱位时明确反馈，允许安全地把杂物丢在后续路线之外。用 36 格满包的 1×2 矿道复现，核对箱转移和腾位，再往返、拾矿，确认空位仍在且目标掉落能入包。报告存入数量、空位和触发的回退分支。容器调用兼容性见 BUG-097。 [存箱入口](../src/mc_agent/skills/library.py#L4553)

- **r78 裸装终包超门 1→2**。HY4_7 圆石 **222**；MoveTest 圆石 **247**、深板岩圆石 **261**；门为各 220.8。 [HY4_7 终包](../runs/r78_bare_real_drop_60m/hy4_hy_7_bare/wild-endurance-r78_bare_real_drop_60m_hy_7_bare-20261121-200837.json) · [MoveTest 终包](../runs/r78_bare_real_drop_60m/nju_movetest_01_bare/wild-endurance-r78_bare_real_drop_60m_movetest_01_bare-20261121-200817.json)

- **r77 日出 DS_2 终包超门**。四槽唯一，**576 深板岩圆石／269 圆石**，两项均超各 220.8 容差。 [DS_2 终包](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/wild-endurance-r77_day12_real_drop_60m_ds_2_bare-20261121-201001.json)

- **r77 裸装同三槽终包超限 2→1**。唯一超限为 HY4_5 **345 深板岩圆石**，同槽另有 180 圆石。 [HY4_5 终包](../runs/r77_bare_real_drop_60m/hy4_hy_5_bare/wild-endurance-r77_bare_real_drop_60m_hy_5_bare-20261121-190727.json#L53) · [门禁](../../s-triage/scripts/d1_acceptance.py#L202)

- **r74 裸装两槽终包超限**：HY4_7 **384 深板岩圆石、176 圆石**，MoveTest **300 圆石**。 [HY4_7](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/wild-endurance-r74_bare_real_drop_60m_hy_7_bare-20261121-132014.json) · [MoveTest](../runs/r74_bare_real_drop_60m/nju_movetest_01_bare/wild-endurance-r74_bare_real_drop_60m_movetest_01_bare-20261121-131954.json)

- **r77 中期终包两槽超限**：HY4_1 **320 圆石、192 深板岩圆石**，HY4_2 **281 深板岩圆石、160 圆石**。 [HY4_1](../runs/r77_midgame_real_drop_3bot/hy4_hy_1_midgame/wild-endurance-r77_midgame_real_drop_3bot_hy_1_midgame-20261121-131847.json) · [HY4_2](../runs/r77_midgame_real_drop_3bot/hy4_hy_2_midgame/wild-endurance-r77_midgame_real_drop_3bot_hy_2_midgame-20261121-131857.json)

- **r76 门户库存复验**：三槽一槽超限，NJU_3 终包 **307 圆石、191 深板岩圆石**。 [终包](../runs/r76_midgame_portal_40m/nju_nju_3_midgame/wild-endurance-r76_midgame_portal_40m_nju_3_midgame-20261121-125543.json)
- **r76 中期库存复验**：一槽超过库存门，HY4_1 终包 **349 深板岩圆石、294 圆石**。 [终包](../runs/r76_midgame_real_drop_3bot/hy4_hy_1_midgame/wild-endurance-r76_midgame_real_drop_3bot_hy_1_midgame-20261121-123652.json)
- **r75 门户库存复验**：三槽有一槽超限，NJU_3 终包 **404 深板岩圆石、169 圆石**。过门任务 EP04 长段循环探矿无黑曜石产出，确认计数与终包需要分别读。 [尾段探矿](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/20261121-122230-392784.events.jsonl#L1522) · [终包](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/wild-endurance-r75_midgame_portal_40m_nju_3_midgame-20261121-121405.json)

- **r74 门户**：HY4_8 终版地狱岩 **328**，超过 147.2 容差门，三槽中唯一超限。 [终包](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/wild-endurance-r74_midgame_portal_40m_hy_8_midgame-20261121-113154.json#L64) · [清包源码](../src/mc_agent/skills/library.py#L2052)

- **r71 裸装 60 分钟**：HY4_7 终包圆石 **260**，三槽中唯一超限。 [终包](../runs/r71_bare_real_drop_60m/hy4_hy_7_bare/wild-endurance-r71_bare_real_drop_60m_hy_7_bare-20261121-101003.json#L54) · [清包入口](../src/mc_agent/skills/library.py#L2052)

- **r69 日出 60 分钟**：HY4_10／HY4_4 终版圆石 **344／273**，**2/4** 槽超限。 [HY4_10 终包](../runs/r69_day12_real_drop_60m/hy4_hy_10_bare/wild-endurance-r69_day12_real_drop_60m_hy_10_bare-20261121-094915.json#L51) · [HY4_4 终包](../runs/r69_day12_real_drop_60m/hy4_hy_4_bare/wild-endurance-r69_day12_real_drop_60m_hy_4_bare-20261121-094905.json#L53) · [清包入口](../src/mc_agent/skills/library.py#L2052)

- **r72 门户延续超限**：HY4_8／NJU_3 终版深板岩 **320／352**，HY4_8 另有 228 圆石。**2/3** 槽超门。 [HY4_8 终包](../runs/r72_midgame_portal_40m/hy4_hy_8_midgame/wild-endurance-r72_midgame_portal_40m_hy_8_midgame-20261121-100821.json#L65) · [NJU_3 终包](../runs/r72_midgame_portal_40m/nju_nju_3_midgame/wild-endurance-r72_midgame_portal_40m_nju_3_midgame-20261121-100811.json#L62) · [清包入口](../src/mc_agent/skills/library.py#L2052)

- **r72 中期**：1/3 超限。HY4_1 终包深板岩 **256**，超过 220.8；EP05 末段以 DFS 找钻石同时持续回执深板岩。 [终包](../runs/r72_midgame_real_drop_3bot/hy4_hy_1_midgame/wild-endurance-r72_midgame_real_drop_3bot_hy_1_midgame-20261121-094524.json#L58) · [EP05 增量](../runs/r72_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-095957-754719.events.jsonl#L985) · [清包源码](../src/mc_agent/skills/library.py#L2052)

- **r71 门户**：深板岩终版 NJU_3 **303**、HY4_9 **235**，均超 220.8。NJU_3 另有圆石 235。NJU_3 EP04 的 34 个探矿切片累报产出 603 深板岩，按每次增量加总，终包以 `owned_end` 为准。 [NJU_3 终包](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/wild-endurance-r71_midgame_portal_40m_nju_3_midgame-20261121-092607.json#L59) · [HY4_9 终包](../runs/r71_midgame_portal_40m/hy4_hy_9_midgame/wild-endurance-r71_midgame_portal_40m_hy_9_midgame-20261121-092627.json#L65) · [清包入口](../src/mc_agent/skills/library.py#L2052)

- **r68 日出四槽**：HY4_3 终版圆石 **469**、DS_2 **311**，均超 220.8 门，**2/4** 超限。HY4_3 EP10 的铁器探索多次产圆石，末段找不到粗铁且回程耗尽余量。 [HY4_3 终包](../runs/r68_day12_real_drop_45m/hy4_hy_3_bare/wild-endurance-r68_day12_real_drop_45m_hy_3_bare-20261121-090238.json#L51) · [DS_2 终包](../runs/r68_day12_real_drop_45m/nju_ds_2_bare/wild-endurance-r68_day12_real_drop_45m_ds_2_bare-20261121-090228.json#L55) · [HY4_3 采块](../runs/r68_day12_real_drop_45m/hy4_hy_3_bare/20261121-092852-151315.events.jsonl#L329) · [清包入口](../src/mc_agent/skills/library.py#L2052)

- **r64→r65 Day1/Day2 同四槽**：圆石门禁超限槽 **1→3**，r65 的 HY4_3／HY4_4／HY4_10 终版圆石分别 **497／345／388**，均超 220.8 门槛。HY4_4 夜间做盾时背包圆石 192，终版再次累积到 345。 [HY4_3 终包](../runs/r65_day12_full_45m/hy4_hy_3_bare/wild-endurance-r65_day12_full_45m_hy_3_bare-20261121-063742.json#L58) · [HY4_4 当时背包](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-070237-818958.events.jsonl#L310) · [HY4_4 终包](../runs/r65_day12_full_45m/hy4_hy_4_bare/wild-endurance-r65_day12_full_45m_hy_4_bare-20261121-063752.json#L56) · [HY4_10 终包](../runs/r65_day12_full_45m/hy4_hy_10_bare/wild-endurance-r65_day12_full_45m_hy_10_bare-20261121-063802.json#L46)
- **现象**：背包被圆石/深板岩/凝灰岩/安山岩/地狱岩长期塞满，挤占 10~14 个槽位（约 30~39%）
- **r18 早期影响**：`inventory_full` **0 次**、`no_drops_collected` 7 次（`ds_2`/`ds_4`/`hy4_5`）、`partial_collection` 1 次；暂未造成硬失败，但槽位压力随挖矿持续累积（待收官用 `owned_end` 复核）
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
- **D1 r22 终验（短波通过）**：18/18 路 `owned_end` 检查（容差 15%）**gross-over-cap = 0**；`inventory_cleaned` 事件正常。
- **D1 过夜 4h 长程复核（未达标）**：`overnight_12bot` **8/12 路**超限：`ds_1` cobble **571**、`glm_2` netherrack **551**、`ds_2` 382+301、`nju_3` 320+256、`ds_3` 320、`glm_3` 300、`nju_movetest` 256、`glm_4` deepslate 247。
- **根因（精确）**：`_ensure_free_slots` 的 `pre_over_cap` 只决定"是否进入"；真正丢弃由 `plan_garbage_drops` 的 `need = max(0, min_free - empty_before)` 控制——**只要还有 ≥3 个空槽，need=0，一个都不丢**。所以"超 cap 但有空槽"的情况仍然完全不清理（4h 里 145 次 `inventory_cleaned` 只是空槽不足时的偶发清理）。
- **最小修复**：超 cap 时把 `need` 提升为"至少清到 cap"的槽数（如 `need = max(need, ceil((count-cap)/64))`），或给 `plan_garbage_drops` 增加 `enforce_caps=True` 参数。
- **D1 r20 复核（补丁已实机生效）**：`53835cf` 补上 `mine_dfs` 返回后的 `_ensure_free_slots(min_free=3)`；r20_short **已观测到 `inventory_cleaned` 事件（cobblestone 25）**。
- **D1 门禁口径**：`owned_end` 检查允许 **15% 堆叠粒度容差**（cobble 192→220、netherrack 128→147）。
- **r52 Day 2**：八个开跑槽有一路超过门禁，HY4_5 终版持有 **235** 深板岩圆石。EP05 同一 Y−54 站位两次各报告清理 43 个，后一次回执同时为 `gained={}`、`backtrack_blocked`。数量与原地复拾相容，现场缺少拾取事件来确定。 [首次清理](../runs/r52_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-214805-417222.events.jsonl#L161) · [第二次清理](../runs/r52_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-214805-417222.events.jsonl#L184) · [终版库存](../runs/r52_day2_wild_scout_9bot/hy4_hy_5_iron/wild-endurance-r52_day2_wild_scout_9bot_hy_5_iron-20261120-213213.json#L58)
- **r53 Day 1**：3 槽中 HY4_3 终版圆石 **267**，超容差门槛。EP06 三片先后报告清理 45、68、75 个圆石，配对净采集 38、21、7 个，最终高于限额。须核对清理后的实测库存及贴身丢弃后的拾取，回执目前仅证明清理操作和库存超限同时出现。 [末片清理](../runs/r53_day1_bare_3bot/hy4_hy_3_bare/20261120-221019-438185.events.jsonl#L151) · [终版圆石](../runs/r53_day1_bare_3bot/hy4_hy_3_bare/wild-endurance-r53_day1_bare_3bot_hy_3_bare-20261120-215401.json#L47)
- **r53 下界**：两路超过地狱岩容差 147，HY4_10 **253**、NJU_3 **592**，均从各自终版 `owned_end` 读取。 [HY4_10 终版](../runs/r53_nether_fortress_9bot/hy4_hy_10_nether_fortress/wild-endurance-r53_nether_fortress_9bot_hy_10_nether_fortress-20261120-215408.json#L59) · [NJU_3 终版](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/wild-endurance-r53_nether_fortress_9bot_nju_3_nether_fortress-20261120-215352.json#L61)
- **r56 要塞波**：HY4_1 EP04 起始深板岩圆石 **193**，14 条隧道回执合计报告新获 169 且均有清理回执，终版仍 **385**，另有圆石 178。该版本的隧道已接入清理，需确认丢弃后的实际库存与重新拾取。 [起始库存](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002551-640618.events.jsonl#L46) · [采集清理回执](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002551-640618.events.jsonl#L528) · [终版库存](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/wild-endurance-r56_stronghold_triangulate_3bot_hy_1_village_to_stronghold_end-20261121-000244.json#L66)
- **D1 r20_short 收官（17/18）**：仍有 **2 路**超限：`glm_3_village_trade` cobble **242**、`hy4_8_nether` netherrack **182**。
  → `53835cf` 只在 `mine_dfs` 返回后清；这两路的剩余石料来自 `mine_blocks`/`tunnel`/地形收集，没有触发点。
  → 剩余修复：把同一清理挂到 `mine_blocks`/`tunnel`/`harvest_logs` 返回路径，或在 episode 收尾统一清一次；并断言返回的 `dropped` 非空时才计为生效。
  → **已合入（23:47 `e60a8d5`）**：`989d757` 现为 HEAD 祖先，`grep _cleanup_inventory_after` = **4 处**；r22 进程加载于 23:48，**包含该修复**，可用 `owned_end` 验收。
- **D1 r20_long 实测（修复前）**：`hy4_hy_1_bare` cobbled_deepslate **493**、`ds_1_bare` cobble 228 —— 长任务堆料更严重，正是本补丁的目标场景。
- **D1 r19 实测（当时未达标）**：18 路中仍有 **3 路**超限：`nju_guitest 234 cobble`、`ds_2 261 cobble`、`glm_2 138 netherrack`（阈值 192/128）。
  → 说明 cap 判断已可达，但**清理仍只在 craft/harvest 时触发**；最后一次挖矿后没有调用点，残留无法清。
  → 剩余修复：`mine_dfs`/`mine_blocks` 结束（或 episode 收尾）加一次 `_ensure_free_slots(min_free=5)` 软清理；或把 `pre_over_cap` 检查挂到 `mine_dfs` 返回路径。
- **D1 代码复核（通过）**：`4161d8f` 已在 `_ensure_free_slots` 的提前 return **之前**计算 `pre_over_cap`（cobble/deepslate >192、netherrack >128、dirt >64），并改为 `if empty_now >= min_free and not pre_over_cap: return`——与建议一致。r19_short 起用 `owned_end` 与 `dropped` 事件验收。
- **r43 短波再现**：Day 2 终版 3/9 路超出 192/种上限及 15% 容忍，均为圆石深板岩，HY4_10 **293**、HY4_8 **296**、HY4_9 **332**。 [HY4_10](../runs/r43_day2_village_near_9bot/hy4_hy_10_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_10_day2_village_near-20261120-071631.json#L52) · [HY4_8](../runs/r43_day2_village_near_9bot/hy4_hy_8_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_8_day2_village_near-20261120-071627.json#L59) · [HY4_9](../runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_9_day2_village_near-20261120-071629.json#L63)
- **r44 Day 1**：HY4_2 和 HY4_3 终版分别仍有 **242／276** 圆石，两路超过上限与宽限。 [HY4_2](../runs/r44_day1_5bot/hy4_hy_2_bare/wild-endurance-r44_day1_5bot_hy_2_bare-20261120-074020.json#L52) · [HY4_3](../runs/r44_day1_5bot/hy4_hy_3_bare/wild-endurance-r44_day1_5bot_hy_3_bare-20261120-074022.json#L53)
- **r47 收官**：单项 192 加 15% 容差门禁超标 **Day 1 1/5、Day 2 4/9**。GuiTest 圆石 258；Day 2 HY4_4、HY4_6 圆石 257/420，HY4_9、NJU_3 深板岩圆石 267/344。GuiTest EP05 连续两条回执清理 66 圆石，第二条零挖掘、零收益、同位置，终版仍为 192+66。回收附近掉落是待直接拾取回执验证的分支。 [GuiTest 终版](../runs/r47_day1_5bot/nju_guitest_01_bare/wild-endurance-r47_day1_5bot_guitest_01_bare-20261120-095707.json#L48) · [同位置再次清理](../runs/r47_day1_5bot/nju_guitest_01_bare/20261120-101126-256146.events.jsonl#L360) · [HY4_6 终版](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/wild-endurance-r47_day2_full_day_scout_9bot_hy_6_iron-20261120-095717.json#L62)

- **r57 同四路裸装终版**：HY4_10 圆石 264，HY4_3 圆石 250＋深板岩圆石 239，DS_2 圆石 386。脚本按独立栈上限判 3 路超标；按看板合计 ≤384 也有 HY4_3 和 DS_2 两路明确超限。HY4_4 末次状态断连，`owned_end` 为空，末包库存未知。 [HY4_10 末包](../runs/r57_day1_day2_fast_4bot/hy4_hy_10_bare/wild-endurance-r57_day1_day2_fast_4bot_hy_10_bare-20261121-005215.json#L53) · [HY4_3 末包](../runs/r57_day1_day2_fast_4bot/hy4_hy_3_bare/wild-endurance-r57_day1_day2_fast_4bot_hy_3_bare-20261121-005205.json#L56) · [DS_2 末包](../runs/r57_day1_day2_fast_4bot/nju_ds_2_bare/wild-endurance-r57_day1_day2_fast_4bot_ds_2_bare-20261121-005200.json#L51)

### BUG-030 | P1 | Slice A（#3 Codex #1）| 状态: OPEN（r17/r18 实测；HEAD 仍未改）
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
- **D1 状态（22:50）**：`mining_dfs.py:4818` 仍是 `budget = max(0.0, min(90.0, max_duration_s))`，未修；r19_short 的 `mine_dfs` 零进展 10.8%，仍受影响。
- **D1 HEAD 核对（23:15）**：`mining_dfs.py:4818` 仍为 `min(90, max_duration_s)`，**未修**。
- **D1 r20 深度轨迹定量（口径更正，避免误导）**：
  - 统计：**11 个含深挖的 episode → 11 次首调用 + 39 次续调用**；续调用中只有 **1 次**在净上升 >15 格后重开（真"重启"极少）
  - 每片平均下潜 **11.5 格**、中位耗时 ~30s；从 y≈64 到 -54 需 **10~11 次调用（约 5 分钟）**，但 episode 还要分配给备料/战斗/其它目标，常在到达前超时
  - `hy4_hy_2_iron` 文件 2：`-17 → hurt → 67` —— **一次受伤/死亡把整段下潜清零**（唯一的重置样本）
  → 修复优先级：① **跨 episode 复用竖井**（新集/复活后先 restore shaft 并走到最深节点，不要重挖）；② 深目标给更长的单集时间或更少的中间目标；③ 单次预算自适应（原建议）只减少轮次开销，不改变总挖掘时间
- **D1 竖井树追踪（修正结论：树通常是保留的，归零是偶发）**：
  - `hy4_hy_1_bare`（r20_long）：`node_count 1→3→4→5→…→15`，**跨 episode 稳定保留**，中途 `surface_reached` 后仍继续下潜到 **y=-27** → 这是期望行为
  - `hy4_hy_2_iron`（r20_short）：树原本 1→7，但某次 `action=explore` 返回 `node_count=0`、位置 **y=64**；该调用参数里**没有 reset** → 树在两次调用之间被清（疑与死亡/复位有关），随后从地表重建
  - `hy4_hy_4_cave_combat`：树保留 19，但 `active_node_id` 回退到 16/9/5、位置在 y=39/51 → 有记忆却不回最深节点
  → 修复点：① 在清空 `nodes` 的所有分支发 `mine_dfs.tree_reset{reason}` 事件，先定位是谁清的；② `run()` 入口优先导航到 `active_node`/最深节点（而非 collar）；③ 不必假设"受伤必清树"
- **D1 r24 复核（埋点已落地）**：新增 `stop_reason="depth_reset"` 已生效，r24_short 出现 **3 次**并带净上升数据：
  | bot | pos_before → pos_after | 净上升 |
  |---|---|---|
  | `ds_2_cave_combat` | 106 → 125 | **+19** |
  | `hy4_2_iron` | -25 → -5 | **+20** |
  | `ds_4_nether_fortress` | 44 → 54 | **+10** |
  → 可量化"深度被清零"的代价；下一步仍需让 `explore` 在复位后**导航回 deepest node**，而不是从 collar 重挖。
- **D1 r20_short 复核（仍 0 命中）**：9 路共 **45 次** `target_y<=-40` 深挖，**0 次到达**；最深仅 `-22`（`hy4_4`），多路最深仍在 **y=60~74**。
  stop_reason 分布：`time_slice_progress 32`（有进展但太慢）、`None/False 7`、`shaft_exhausted 3`、`needs_surface_climb 2`、`bad_target_for_dimension 2`、`pickaxe_low 1`、`hurt 1`。
  → 主因是"每片只下潜几格 + 耗尽后回地表"，不是单次预算；"深度清零/无法续潜"比预算更致命。
- **D1 r18 扩大复核（问题比单机更广）**：8 路发起 `target_y<=-40` 的深挖，**0 路在任一调用中到达 target_y±3**：
  | bot | deep calls | 最深 y | 现象 |
  |---|---|---|---|
  | `hy4_hy_2_iron` | 23 | **-28** | 中途 48→41→**48** 回退；3 次 `time_slice` 0 收益 |
  | `nju_nju_3_village_trade` | 19 | 55 | 94→58 后 `shaft_exhausted`，被迫 `needs_surface_climb`→`surface_reached`，再从 97 重开 |
  | `hy4_hy_9_deep_water` | 17 | -28 | 在 **y=2 / y=-6 已挖到 diamond ×2**，仍被 `target_y=-54` 牵着继续下潜 |
  | `hy4_hy_10_deep_water` | 11 | 18 | 未有效下潜 |
  合计 ~82 次深挖调用 ≈ **38 分钟**，0 次到达目标层。
- **D1 过夜两波复核**：`overnight_12bot` **16 次**（`nju_guitest` 4、`ds_1` 4、`ds_2` 2、`glm_5` 2…）；`overnight_12bot_part2` **17 次**（`ds_1` 6、`nju_3` 5、`nju_movetest`/`nju_guitest`/`glm_2`/`glm_3`/`glm_5`/`ds_2` 各 1）。
  → 频次稳定在 **~4 次/小时/12 路**，instrumentation 正常；**行为修复（导航到最深已挖节点而不是从浅层重开）仍未实现**，`nju_3` 单路 5 次最严重。

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
- **D1 关系说明（r24）**：该单的“地底断木断镐循环”与 **BUG-038（climb 慢）/BUG-041（木镐硬岩）** 是同一现象的不同层面；本单负责**下矿前的木料保底**（curriculum 层），038/041 负责恢复路径（skills 层）。
- **验收标准**：45m `bare` 长程不再出现 `y < 0` 断木断镐跨 2 集以上停滞；配合 `BUG-029`（`r15` 长程 `nju_movetest_01_bare` 31/36 格 903 垃圾、`glm_glm_5_midgame` 36/36 格、`hy4_hy_1_bare` 34/36 格 950 垃圾，且 4 路全部 `containers=0`）一起在 `r18`/`r19` 验收

### BUG-033 | P1 | Slice D2（#6）+ Slice B（#2）| 状态: FIXED（`2bfe3c7`；r19 实测 0 线程异常）
- **影响**：`ds_ds_4_nether_fortress` 的 **scene-watchdog 线程直接死亡**（主进程继续跑）；该 bot 的场景回场/死亡环校验从此刻起静默失效
- **日志证据**：
  ```
  Exception in thread scene-watchdog-DS_4:
    l3_wild_endurance_runner.py:1155 run → :1075 _tick → bot.status()
    bot.py:256 status → guardian.py:1232 _wrapped_call
    raise ReflexPreempted(pending_trigger[0])   # Reflex preempted by tool_interrupted_hurt: blaze
  ```
- **源码定位**：
  - `harness/guardian.py:111` `class ReflexPreempted(BaseException)` —— **不是 Exception**
  - `scripts/l3_wild_endurance_runner.py:1150-1155` 与 `:1072-1075` 的 `except Exception` 都**捕获不到**它，线程直接退出
  - `guardian.py:1229-1232` 对任何 `call`（含只读 `status`/`method="GET"`）在 pending_trigger 时都抛抢占
- **现象与根因**：反射抢占信号只该打断"动作调用"，不应打断 watchdog 的只读状态轮询；且 `BaseException` 绕过了所有 `except Exception` 保护
- **最小修复**：① `_wrapped_call` 对 `method="GET"`/`status`/`inventory`/`events` **永不抛** ReflexPreempted；② 或仅在 actor 线程抛（`threading.current_thread()` 判定）；③ runner 侧 watcher 捕获 `BaseException`（至少显式 catch `ReflexPreempted`）并记录 `thread_health`，不要静默死
- **验收标准**：r19 `d1_acceptance` 的 `BUG-033 thread exceptions == 0`；watchdog 每 tick 有 heartbeat 日志
- **D1 r19 复核（通过）**：`2bfe3c7` 已在 `_wrapped_call` 中判定 `is_actor_thread` 与 `is_readonly_poll`（GET/READ_ONLY 直接放行），runner 侧也补了 `except BaseException`；r19_short **thread exceptions=0**。注：该提交信息写作 `BUG-031`，实际对应本单 BUG-033。

### BUG-034 | P1 | Slice B（#2 子 AGY）| 状态: FIXED（`PRE_HURT_RANGED_COOLDOWN_S 10→30`；r20 早期 70.4s）
- **影响**：r18 前 18 分钟共 **186 次取消 / 393.4s 冷却**，其中 `hy4_hy_4_cave_combat` **113 次 / 259.7s**（该路 66% 的取消、66% 的冷却）
- **日志证据**：`guardian.incident` 的 `pre_hurt_ranged` **74 次**（melee 仅 11、creeper 5）；cave_combat 竞技场里骷髅持续射击，每 ~10s 触发一次抢占 → 取消在途生成 → 强制冷却 → 模型重发
- **源码定位**：`harness/guardian.py:142` `PRE_HURT_RANGED_COOLDOWN_S = 10.0`；`_mark_pre_hurt_cooldown`（:391-414）只按"目标 id/类型"做 10s 去抖；持续火力下每 10s 仍取消一次
- **现象与根因**：去抖窗口对"持续远程压制"太短；且已举盾/已掩体时仍会取消生成（`status.blocking=True` 未作为豁免）
- **最小修复**：① 远程触发后进入 **30~45s combat stance**（举盾/掩体），期间**不再取消**已在途生成；② `status.blocking=True` 或已在 `dig_cover` 时豁免取消；③ 同目标冷却 10→30s
- **验收标准**：r19 `cancel_cooldown_wait` ≤200s；`pre_hurt_ranged` ≤25；cave_combat 路取消 ≤20
- **D1 r20_short 收官（通过）**：`cancelled 76 / wait 157.7s`（r19 205.6s、r18 653.2s），门禁 ≤200s PASS；`PRE_HURT_RANGED_COOLDOWN_S=30` 生效。
- **D1 r18 收官（22 路）**：`cancelled` **311 次**、`cancel_cooldown_wait` **653.2s**（门禁 200s），其中 `hy4_hy_4_cave_combat` **117 次**；r19_short 仍 205.6s。
- **D1 r18 分集证据（`hy4_hy_4`）**：
  | episode file | pre_hurt | cancels |
  |---|---|---|
  | `...061732` | 22 | **40** |
  | `...062244` | 23 | **40** |
  其余 3 集各 3~4 次；两次病态集里 `pre_hurt` 间隔中位 **12.1s**（≈ 10s 冷却 + 检测），骷髅距离 3.5~8.4m，`subcortical_combat` 每次都只是短打，未击杀/未换掩体。

### BUG-035 | **P0 崩溃** | Slice C（#4）+ Integrator（#1）| 状态: FIXED（`3aea714`，D1 离线复现已转绿；r21 待实机）
- **影响**：`hy4_hy_3_midgame` 在 r20_short **整路进程退出**（无 summary）；触发点=异步熔炼 world job 的收取阶段遇到网络抖动
- **日志证据**（`runs/r20_short/hy4_hy_3_midgame/stdout.log`）：
  ```
  curriculum.py:1813 run → world_jobs.collect_job(...)
  curriculum.py:384  collect_job → collect_smelt_batch
  ensure.py:443      → skills.smelt_collect
  library.py:4121    bot.call("window/view")  →  httpcore.ReadTimeout: timed out
  → 未捕获 → main:1603 退出（SystemExit）
  ```
- **源码定位**：`skills/library.py:4120-4122` 的 `window/view` 只 `except ApiError`，**没有捕获 `httpx.HTTPError`**；`harness/curriculum.py:384`、`:1813` 也没有兜底
- **现象与根因**：熔炼产物留在炉里不会消失，读取超时本应是"下次再收"；现在一次 ReadTimeout 就把整个 episode/进程打死
- **最小修复**：① `smelt_collect` 捕获 `httpx.HTTPError` → 返回 `{"ok": False, "deferred": True, "still_burning": True}`；② `collect_job` 对异常返回 `deferred` 并保留 job；③ `curriculum.run:1813` 把 `collect_job` 包在 `try/except (ApiError, httpx.HTTPError)` 里，失败不阻断恢复流程
- **D1 离线复现（最小单测）**：`/home/xzq/projects/mc/.worktrees/s-triage/scripts/d1_smelt_timeout_check.py`（当前 exit 1）
  ```
  FAIL: ReadTimeout escaped collect_job -> would kill the run
  ```
  用真实 `WorldJobRegistry` + 注入了 `{"pos":[0,64,0]}` 的 job，fake `skills.smelt_collect` 抛 `httpx.ReadTimeout`；修好后应返回 `deferred/still_burning` 并保留 job，脚本 exit 0。
- **验收标准**：r21 `d1_acceptance` 无 startup crash；`d1_smelt_timeout_check.py` exit 0；`smelt_collect` 的 ReadTimeout 0 进程退出；同 bot 下一集能回收炉内产物
- **D1 r21_short 实机复核（通过）**：17/18 路，**0 次 `smelt_collect` Traceback**；上一波因此丢掉的 `hy4_hy_3_midgame` 本波恢复到 **130 分**。
- **D1 r21 早期实机复核（正向）**：r21_short 全波 stdout **0 次 `smelt_collect` 报错**（唯一 Traceback 是 glm_1 的 `ensure_bot_ready` 超时，见 BUG-039）；说明 `collect_job` 的 deferred 兜底在真实运行中生效或至少未再触发同类崩溃。待 r21 收官确认。
- **D1 修复核对（23:27，通过）**：`3aea714` 在 `WorldJobRegistry.collect_job` 捕获 `(httpx.HTTPError, ApiError, TimeoutError, OSError)`，返回 `deferred/still_burning` 并把 `job.ready_at` 推后 10s；`d1_smelt_timeout_check.py` 现 **exit 0**：
  ```
  collect_job returned: {'ok': False, 'deferred': True, 'still_burning': True, ...}
  OK: timeout is deferred and the job is preserved
  ```
  待 r21 实机确认同类 Traceback 归零。
- **D1 触发链证据（r20_short）**：`hy4_hy_3_midgame/curriculum-*.json` 里 `active_world_jobs: 1` 且第 2 集 `use_lodestone` 状态为 `paused_for_world_job`；下一集 resuming 时 `collect_job` → `smelt_collect` 撞上 ReadTimeout。r20_short 全波仅这 1 个 active job（即命中率≈1/18 但有 job 的 bot 必中）。
- **触发面比想象更广（r20 实测）**：r20_short 全波 **0 次 `mc_skill_smelt` 调用**，崩溃的 job 来自 `pre_ensure`（0-LLM ensure）注册的熔炼任务；崩溃集 target 为 `minecraft:adventure/use_lodestone` / `minecraft:story/lava_bucket`（都需要烧铁）。→ 任何走 `pre_ensure → furnace job → 下一集 resume` 的 bot 都可能被打死。
- **D1 r20_short 复核（连续第 2 波复现）**：`hy4_hy_3_midgame` 再次在 `library.py:4121 smelt_collect` 因 `ReadTimeout` 整路退出；同一 bot、同一行，证明该 P0 稳定复现，不是偶发。

### BUG-036 | P1 | Slice D2（#6）+ Integrator（#1）| 状态: OPEN（r20_long 实测）
- **影响**：`nju_movetest_01_bare` 的 45 分钟长任务在 **425.7s** 就整体结束（`status=auth_error`），损失约 38 分钟预算
- **日志证据**：`runs/r20_long/nju_movetest_01_bare/wild-endurance-*.json`
  `"error": "[invalid_token] token not found or expired"`；`curriculum-*.json` 第 3 集 `target=None, status=auth_error`
- **现象与根因**：运行中 MC API token 被吊销（单 Token 互斥：另一处对同一 bot `login` 会立刻使旧 token 失效）。curriculum 在**开局边界**遇到 401 直接终止整轮，没有"用 TokenStore 重新登录并续跑"的恢复路径
- **最小修复**：① 每集开始前做一次轻量 `status` 探活；遇 `invalid_token` 用 `TokenStore` 里的凭据 `login` 刷新一次，再重试该集（失败才终止）；② `TokenStore` 缺失/过期时给出"需人工 login"的明确提示
- **附加（防呆）**：`v2_fleet.py launch` 启动前断言"同一 bot 名不得同时出现在两个运行中的 wave"（当前 r20_long 与其它 wave 共用 `MoveTest_01` 的 token 文件，一旦并发就会互相吊销）
- **验收标准**：r21 起 `auth_error` 导致的整轮中止 ≤1 且能自动续跑；`v2_fleet launch` 检测到并发同名 bot 时拒绝启动
- **r51 两波同时异常**：北京时间 13:31:10.262–11.093，9 路在 **0.831s** 内报 `invalid_token`。另一已跑槽 HY4_7 收 `wrong_server`／`not_connected`，全体 10 路受影响。8 个断连集、1 个 `runtime_error`、1 个超时；8 路断连均再次开集，10 路均有 completed 终版。r52 最早 stdout 创建于 13:32:03，晚于故障约 53s，现有证据无法定为下一波登录覆盖或服务器重启。 [首个鉴权失败](../runs/r51_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-212645-030849.events.jsonl#L911) · [HY4_7 异常](../runs/r51_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-213009-977723.events.jsonl#L126) · [恢复动作](../runs/r51_nether_fortress_9bot/hy4_hy_4_nether_fortress/20261120-213116-023603.events.jsonl#L8)

- **r67 村庄波 MoveTest_01**：EP05 第三次 actor 请求期间获 `invalid_token`，该集 `runtime_error`、0 分。EP06 再次开集并成功调用隧道，整路有 1500.1s 终版。保留鉴权失效原因待查，已有恢复执行的回执。 [鉴权失败](../runs/r67_village_trade_4bot/nju_movetest_01_village_trade/20261121-071950-773584.events.jsonl#L195) · [后续工具](../runs/r67_village_trade_4bot/nju_movetest_01_village_trade/20261121-072128-723236.events.jsonl#L210) · [终版](../runs/r67_village_trade_4bot/nju_movetest_01_village_trade/wild-endurance-r67_village_trade_4bot_movetest_01_village_trade-20261121-070623.json#L348)

### BUG-037 | P1 | Slice C（#4 Codex #2）| 状态: OPEN（r20_short 实测）
- **影响**：r20_short 的 `staircase:time_budget` **28 次 / 7 bots**，其中 **21 次 `steps_done=0`**（75% 完全无进展），单次中位 **15.3s**（≈ `per_step_timeout=15s`）
- **日志证据**：`glm_glm_1_nether_fortress 8 次`、`ds_ds_4 5 次`、`glm_glm_2 4 次`、`hy4_hy_7 4 次`；参数多为 `steps=12~20`，说明模型想走长阶梯，但**第一步**就超时
- **源码定位**：`skills/library.py:5887-5889` `dig_staircase(..., per_step_timeout: float = 15.0)`；`_StairSession.call` 在 `step_deadline` 耗尽时抛 `_StairDeadline` → 返回 `time_budget`（`library.py:1161-1205`），第 0 步没有豁免
- **现象与根因**：爬升/下探的第一步常需要"走到位 + 清 1~3 格 + 确认落点"，15s 在卡顿时不够；一旦超时就整次失败、模型再发一次，形成 0 进展循环
- **最小修复**：① 第 0 步使用总预算 45s（不套 per-step）；② 或 `per_step_timeout` 提到 20~25s；③ 返回里带 `steps_done=0` 的 `time_budget` 时给不同提示（换方向/unstick），避免模型原样重试
- **验收标准**：r21 `staircase:time_budget` 中 `steps_done=0` ≤5 次/波；`staircase` 调用总数下降 ≥20%
- **D1 r20_long 复核（不是首步问题，是总预算打长请求）**：`ds_ds_1_bare` 的 `bootstrap_iron_age` 集 13 次调用里 **6 次 `staircase:time_budget`**，但 `steps_done` 都是 **7~9**（不是 0）；模型请求 `steps` 中位 **40**，45s 总预算永远做不完。
- **D1 两波对比（两种模式必须分开修）**：
  | 波次 | `steps_done=0`（per-step 15s 饿死） | `steps_done>0`（45s 总预算打长请求） | 请求 steps 中位 |
  |---|---|---|---|
  | r20_short | **24** | 12 | 12 |
  | r20_long | **0** | 8（done 中位 8） | **40** |
  → 本单负责 0 步模式（per-step 15→20~25s 或第 0 步豁免）；>0 步模式见 P2-20（请求步数 clamp + 预算按 steps 缩放）。
- **D1 r24 复核（首步 starvation 与镐等级无关）**：`r24_short` 仍有 **9 次 `timeout_path=dig`**（其中 **5 次 `steps_done=0`**），但**没有出现"木镐"新文案** → 这些 bot 镐等级 ≥2，说明首步 15s 超时在**正常镐 + 硬方块（黑曜石/堡垒砖）**下同样发生。BUG-041 的"命名低镐"只覆盖 tier<2 子集，**第 0 步豁免仍是必须的**。
- **D1 r23 新证据（下界 ancient_debris/gold 0 进展）**：`nju_guitest` 在下界 y=54 连续 `explore(target=gold/ancient_debris, target_y=15)` 返回 `time_slice`，**pos_before == pos_after**、耗时 28~50s（预算 60~75s）；tree 里 `active_node 5` 挂着 `pending: stair_down south, steps=35` 且无 pruned 分支 → 内部 `dig_staircase` 在第一步就超时退出，分支未执行。r23 的 zero-slice 17.5%（10 次）全部是这一类。
  → 说明 BUG-037 的 per-step 15s 不只影响独立 `mc_skill_staircase`，也卡住 `mine_dfs` 内部阶梯；修复时需同时覆盖内部调用，并把"0 进展 + 分支 pending"归为 `branch_failed`（触发 relocate），而不是 `time_slice`。
- **D1 r22_long 死锁链（P1 实证，裸开卡铁器）**：`hy4_hy_1_bare` 在 y=39 地下：
  1. `craft_gear(stone_pickaxe)` → `missing {"stick":2}`（无木头）
  2. `harvest_logs` → "y=39 低于树木带，请先 staircase(up)"
  3. `staircase(north, up, steps=20/30)` → **time_budget ×11**（每片只 done 部分，模式二）
  4. `mine_dfs(iron)` → `insufficient_pickaxe_tier`（只有木镐）
  → 三步互为前置，整轮 24 分钟停在 10 分。**修好模式二即可直接解锁**：入口 clamp `steps<=10` 或预算 `min(90, 8+steps*5)`。
- **D1 过夜两波复核**：`overnight_12bot` **351 次** time_budget（342 `timeout_path=dig`、135 低镐文案、`steps_done` 中位 **1**）；`overnight_12bot_part2` **241 次**（225 dig、79 低镐、`steps_done` 中位 **2**、`dig_cell_avoided` 101）。
  → `steps_done` 中位 1→2 说明快速失败路径有效，但**仍无"软方块方向优先"**；`pillar_fallback` 在 part2 升到 **140**（r28 hy4 更高：73），已成为与 time_budget 并列的主要失败路径。
- **D1 r21 第三种模式（首步 movement_not_confirmed）**：`ds_ds_4_nether_fortress` 在下界 `[705,66,640]` 连续 `north/south` 都返回 `steps_done=0, dug_count=0, stop_reason=movement_not_confirmed`，但返回体里已有 `viable_directions=[south/east/west]`；模型却在对向反复试。
  → 建议：`steps_done==0 && dug_count==0` 时不返回泛化失败，而是自动 `unstick` 或改用 `viable_directions[0]`；同时把 `steps` 被 clamp 的事实显式回传（r22 已见 `steps_capped_from:20 / steps_clamped:true`，此项已满足）。

### BUG-038 | P1 | Slice A（#3 Codex #1）| 状态: OPEN（r20_long 实测，卡死裸开铁器）
- **影响**：`ds_ds_1_bare` 在 y=17 断镐后，**~250s 只爬升 10 格**（17→27）且仍无法补料；`bootstrap_iron_age` 因此超时，整轮停在 15 分
- **逐调用证据（r20_long `ds_ds_1_bare/20261119-071842`）**：
  ```
  pickaxe_low  y=20→17
  climb_out 53.0s 17→20 (climb_progress)
  climb_out 27.7s 20→20 (time_slice, 0 进展)
  climb_out 40.9s 20→20 (time_slice, 0 进展)
  climb_out 43.3s 20→25 (climb_progress)
  climb_out 25.6s 25→26 (climb_progress)
  staircase 25.8s 26→27 (time_budget)
  unstick   25.7s
  staircase 16.2s 27→27 (time_budget, 0 进展)
  → 下一集：craft_gear missing_ingredients + harvest_logs no_targets_available
  ```
- **源码定位**：`mining_dfs._climb_out`（`:2728+`）每片预算只够 3~5 格，且两次 `time_slice` 0 进展说明"沿自建巷道走"未生效、退化为重新开阶梯；`library.dig_staircase` 又叠加 BUG-037（首步/per-step）
- **最小修复**：① `_climb_out` 优先沿已建 corridor 的 waypoints 直达（walk 模式），不要在巷道里重新 dig；② 单次 climb 至少保证 **8~10 格/片**；③ `pickaxe_low` + 无木头时直接给"climb_out 到 y>=目标地表"的量化建议（当前 P2-30 只给了文字）
- **验收标准**：r21 地下断镐到回地表的调用数 ≤3、总耗时 ≤120s；`climb_out` 0 进展 `time_slice` 为 0

### BUG-039 | P1 | Slice D2（#6 DeepSeek #2）| 状态: OPEN（r21_short 实测）
- **影响**：`glm_glm_1_nether_fortress` 在 r21_short **启动即整路丢失**（0 summary）
- **日志证据**：
  ```
  l3_wild_endurance_runner.py:1572 st = ensure_bot_ready(bot, timeout_s=45.0)
  l3:1327 RuntimeError: bot did not become ready after 3 attempts (cooldown waited 186s)
  ```
- **源码定位**：`scripts/l3_wild_endurance_runner.py::ensure_bot_ready` —— `attempts=3` 是硬上限，**186s 就放弃**，而签名里的 `max_cooldown_wait_s=420.0` 并未真正用于循环条件
- **现象与根因**：bot 处于 >186s 的死亡冷却中（连续死亡/服务端未恢复）；函数在第 3 次尝试后直接 `raise`，把"可等待"变成整路崩溃
- **最小修复**：① 循环条件改为 `while waited < max_cooldown_wait_s`（attempts 只作为日志/软阈值）；② 仍失败时不要 `raise`，写一份 `status="not_ready"` 的 summary 并正常退出该 bot（保留其它 17 路）；③ 打印剩余冷却 ETA
- **验收标准**：r22 起 `ensure_bot_ready` 失败 0 进程 Traceback；单路最长等待 ≤420s，且等待失败只影响该路
- **D1 r21 早期复核**：`r21_short` 0/18 summary、1 Traceback（即本单）；其余 17 路正常写入 events，`thread exceptions=0`。
- **r51／r52 连续复现（同 HY4_6）**：两波均累计等待 **558s**、下一轮 60s 大于剩余 42s 时结算 `not_ready`，0 actor、0 进程崩溃。当前缺省预算 600s，超过本票验收写的 420s。日志每轮都写 60s，但源码把 API 缺 ETA 时的回退也写成 60s。先区分 `connect` 的服务端剩余、`status` 剩余与本地回退，并记录剩余量是否减少，才能裁定服务端冷却重置。 [r51 终版](../runs/r51_nether_fortress_9bot/hy4_hy_6_nether_fortress/wild-endurance-r51_nether_fortress_9bot_hy_6_nether_fortress-20261120-210116.json#L13) · [r52 启动日志](../runs/r52_day2_wild_scout_9bot/hy4_hy_6_iron/stdout.log#L11) · [r52 终版](../runs/r52_day2_wild_scout_9bot/hy4_hy_6_iron/wild-endurance-r52_day2_wild_scout_9bot_hy_6_iron-20261120-213215.json#L13) · [ETA 回退](../scripts/l3_wild_endurance_runner.py#L1674)
- **r53 第三次复现**：同一 HY4_6 再次累计等待 **558s**，以 `not_ready` 终结、0 集。其余八路正常落盘。 [冷却耗尽](../runs/r53_nether_fortress_9bot/hy4_hy_6_nether_fortress/stdout.log#L10) · [终版状态](../runs/r53_nether_fortress_9bot/hy4_hy_6_nether_fortress/wild-endurance-r53_nether_fortress_9bot_hy_6_nether_fortress-20261120-215400.json#L11)
- **r54 第四次复现**：同一 HY4_6 再次因下一次 60s 大于余量 42s，在累计 **558s** 后以 `not_ready` 收尾。4 个开跑槽正常落盘。 [冷却回执](../runs/r54_nether_fortress_6bot/hy4_hy_6_nether_fortress/stdout.log#L10) · [终版状态](../runs/r54_nether_fortress_6bot/hy4_hy_6_nether_fortress/wild-endurance-r54_nether_fortress_6bot_hy_6_nether_fortress-20261120-231204.json#L11)
- **r56 裸装第五次复现**：同一 HY4_6 改为主世界裸装，仍累计 558s 后 `not_ready`，终版开局集数 0。该问题已在两个不同阶段重复。 [冷却回执](../runs/r56_day1_day2_bare_5bot/hy4_hy_6_bare/stdout.log#L10) · [裸装终版状态](../runs/r56_day1_day2_bare_5bot/hy4_hy_6_bare/wild-endurance-r56_day1_day2_bare_5bot_hy_6_bare-20261121-001919.json#L11)

### r20_long / r21_short 终验摘要（D1，23:57）

- **r20_long 4/4**（0 Traceback）：`glm_5` 180（12 集）、`hy4_1` 40、`ds_1` 15、`nju_movetest` **0（auth_error，BUG-036）**；门禁仅 `BUG-029 over-cap 2` 失败（`hy4_1` cobbled_deepslate **493**、`ds_1` cobble 228）。
- **r21_short 17/18**：仅 `startup crash 1`（BUG-039 `glm_1`）；BUG-035 实机 0 复发；`over-cap 0`；deaths 9、冷却 111.7s、mine 0.90/6.6%。
- 分数：r20_short 1680 → r21_short 1690（17 vs 17）；若 `glm_1` 不丢（上波 160 分），本波约 **1850**。

- **r66 探索铁器槽**：`r66_expl_mem_hy6` 再次记录 HY4_6 等待 **558s／600s**，下一轮估计 60s 超出剩余 42s，终版 `not_ready`、0 集、0 actor。600s 循环中的 60s 仍可能是源码缺 ETA 时的回退值。 [等待日志](../runs/r66_expl_mem_hy6/hy4_hy_6_iron/stdout.log#L10) · [终版](../runs/r66_expl_mem_hy6/hy4_hy_6_iron/wild-endurance-r66_expl_mem_hy6_hy_6_iron-20261121-061609.json#L11) · [回退值](../scripts/l3_wild_endurance_runner.py#L1670)

### BUG-040 | P1 | Integrator（#1）+ Slice D2（#6）| 状态: 部分修复（`165e652` 已大幅收敛；仍有死亡回城残留）
- **影响**：NJU 两路被虚空围栏误判为"离岛"，`staircase`/`tunnel`/`move_to` 连续被拦截。r22_short 累计：`nju_3` **19 次**（分数跌到 **25**，r21 为 40）、`nju_guitest` **9 次**（本波 180）、`ds_3` 1 次（真实贴边）
- **证据**：
  ```
  nju_guitest: spawn=(6381.5, 66, -6415.5)   围栏 center=(797.6, -814.5)   target dist 7913m
  nju_nju_3  : spawn=(-5703.2, 63, 5728.0)  围栏 center=(-153.5, -385.5)   target dist 8257m
  ds_ds_3    : spawn=(-2974.5, 69, 9974.5)  围栏 center=同 spawn           正常（213.9m 属真实贴边）
  ```
- **源码定位**：runner 在 `l3_wild_endurance_runner.py:1663` 用 `center_x=sx, center_z=sz` 建围栏；但 `harness/agent.py:1234-1250` 的 ephemeral-HUD 自动回中逻辑会在 `dimension == overworld` 且 `dist(bot, center) > 400` 时**把中心改成 bot 当前坐标**。若此时场景 teleport 尚未生效（bot 还停在旧位置），围栏就被永久钉在旧坐标
- **现象与根因**：场景 teleport 与 HUD 自动回中竞态；NJU 网关较慢，teleport 未落地时被回中 → 之后场景内所有方向检查都判越界
- **最小修复**：① runner 显式传入的围栏加 `pinned=True`（或子类标记），`agent.py` 跳过自动回中；② 或仅当 `center == .env 默认值` 时才允许回中；③ `verify_scene_entry` 后断言 `dist(bot, geofence.center) < safe_radius`，不满足则按场景锚点重建
- **D1 r23 早期复核（大幅收敛）**：`165e652` 在每集开始时若 bot 已在 `geofence.radius` 外则重新锚定；r23 加载于 00:14（修复后），NJU 两路 **0 次拦截**（r22 为 9/19 次），移动 4/4、4/6 正常。
- **D1 r23 残留 1 例（死亡回城）**：`hy4_hy_8_nether` 第 3 集 **elapsed 238s**（非集首）出现一次 `void_geofence_blocked`：center=(972.4,350.5)、目标 (7795.5,2806)、距离 7251m。
  → 推断：bot 死亡后回世界出生点 (972,350)，HUD 自动回中把围栏钉在出生点；随后返回场景 (7795,2806) 时被拦。集首重锚救不了"集中死亡"。
  → 追加修复：① 死亡/重生时把围栏重锚到**场景锚点**而非重生点；② 或在 `_check_direction_geofence`/`mc_move_to` 检查前做惰性重锚（`dist(bot,center)>radius` 时以 bot 当前位置重锚）。
- **验收标准**：r23 起 NJU 两路 `void_geofence_blocked` = 0；`verify_scene_entry` 增加围栏一致性检查

### BUG-041 | P1 | Slice C（#4 Codex #2）| 状态: FIXED（`7b6e64d` 已合入；r24 加载于合并之后）
- **影响**：`hy4_hy_1_bare` 在 y=33~39 用 **木镐** 向上开阶梯，`staircase` 变成 **~30s/格**（8 次调用只前进 1~2 格），导致无法回到地表补木/补镐
- **日志证据**：
  ```json
  {"stop_reason":"time_budget","steps_done":1,"dug_count":4,"timeout_path":"dig",
   "dig_target":[-11054,36,-11051],"steps_capped_from":20,"steps_clamped":true,
   "error":{"code":"time_budget","message":"单步挖掘未在预算内确认…"}}
  ```
  同波 8 次：`north/east/south/west` 全部 `steps_done=1`，耗时 24~32s
- **源码定位**：`skills/library.py` 的 `dig_staircase` 每步 `dig` 走统一超时；未按"当前镐等级 vs 目标方块硬度"选择路径/软方块，也未在超时后提示"镐太低级"
- **现象与根因**：木镐挖 stone/deepslate 单块就要 ~25s；上爬阶梯全是硬岩 → 每步超时。这是 BUG-037（阶梯）与裸开死锁链的**真正瓶颈**
- **最小修复**：① 当 `highest_pickaxe_tier < 2` 时，上/下阶梯优先选择 `dirt/gravel/sand/netherrack` 等软方块方向（`_probe_viable_*_dirs` 增加 hardness 排序）；② `timeout_path=="dig"` 且 tier<2 时，错误信息直接给 `mc_skill_craft_gear(stone_pickaxe)` 或"换软土方向"；③ 同一格连续 2 次 dig 超时就标记 30s 避开
- **D1 成本量化（r22_long `hy4_hy_1`）**：整轮 **426s（7.1 分钟）** 耗在 `staircase`，其中两个 episode 分别 **282s / 144s**（约占该集 59% / 94%），只换来 y 33→43；同期 `mine_dfs` 654s。**7 个 episode / 32 分钟仍停在 10 分**（对照：同波 `glm_5` 在同一时长内拿到 195 分、13 个成就，含 `shiny_gear 60`）。
- **D1 代码复核（23:22 提交，符合建议）**：
  - `1bbeb4f`：dig 超时时读取 `session.pickaxe_tier`，tier<2 时明确报"当前镐等级 1（木镐）挖 X 约需 25s/格"，并给出 `craft_gear(stone_pickaxe)` / 走软方块 / `harvest_logs` / `unstick` 四条出路
  - `0264145`：同一 dig 格超时后加入 `_avoid_temporarily(..., "dig_timeout", 30s)`，并在下一次调用**快速失败**，避免连续烧 15~45s
  - 未包含"软方块方向优先"（建议项②），但错误文案已引导模型自行换向，可接受
  - **合并状态（已合入）**：`7b6e64d`（00:35）合并后 `grep low_tier_slow_dig` = 3；r24 进程加载于 00:37 → **r24 可实机复验**
- **D1 过夜实机复核（部分达成）**：`overnight_12bot` 中 **146 次** `staircase:time_budget`，其中 **143 次 `timeout_path=dig`**、**64 次触发"木镐/镐等级"新文案**、`dig_cell_avoided` 64 次 → **命名与快速失败已生效**；但 **`steps_done` 中位仍为 1**（请求中位 20 步），说明缺少"软方块方向优先"，仍每步超时。
  → 剩余修复即建议项②：`_probe_viable_*_dirs` 增加方块硬度排序，tier<2 时优先 dirt/gravel/sand/netherrack。
- **验收标准**：r23 裸开 `staircase` 单片 `steps_done` ≥3（木镐场景）；`timeout_path=dig` 的 1 步调用 ≤2/波

### BUG-042 | P1（P0 级影响）| Integrator（#1，`harness/model.py`）| 状态: OPEN（r22/r23 实测，单波丢 21 集）

- **r63–r70 新增长槽复核**：在有数值墙钟的 73 个终版槽中，19 个计划 2700s，分别来自 r63／64／65／66 日出及 r68 裸装。19 槽的终版 `runtime_error` 回执里，硬 429／502 导致的丢集为 **0**。仍缺 12 路约 4 小时配置。 [r66 HY4_10 终版](../runs/r66_day12_full_45m/hy4_hy_10_bare/wild-endurance-r66_day12_full_45m_hy_10_bare-20261121-072420.json) · [r68 HY4_5 终版](../runs/r68_bare_real_drop_3bot/hy4_hy_5_bare/wild-endurance-r68_bare_real_drop_3bot_hy_5_bare-20261121-073241.json)

- **r67 村庄 LC 双槽复现**：各 6 RPM，LC1／LC2 有 **4／5** 个硬 429，同请求均第八次失败。83 条 429 延后均 35s，79 条能跟下一实际尝试配对，确认退避 **2765 Bot·s／3000.2 Bot·s = 92.2%**，其余四条结束证据不足。四次试图交易均在 actor 首请求失败，未下发交易工具；同波 HY4_7、MoveTest 各真实交易一笔。全波 9 个硬 429 均来自 LC，MoveTest 9 次 429 延后均有后续响应。 [LC2 八次尝试](../runs/r67_village_trade_4bot/lc_lc_2_village_trade/20261121-071856-821092.events.jsonl#L17) · [LC1 首集](../runs/r67_village_trade_4bot/lc_lc_1_village_trade/20261121-070653-569155.events.jsonl#L14) · [MoveTest 交易](../runs/r67_village_trade_4bot/nju_movetest_01_village_trade/20261121-070633-745290.events.jsonl#L107)
- **影响**：`hy4_hy_1_bare` 的 11 个 episode 中 **5 个**因 provider 过载直接 `runtime_error`（EP5/6/9/10 = 429、EP8 = 502），整轮只剩 10 分
- **日志证据**：
  ```
  Qwen API 返回 HTTP 429：{"type":"server_error","message":"The inference service is overloaded. Please try again later."}  ×4
  流式 API 返回错误：{'type':'server_error','message':'Streaming response failed: [502]'}  ×1
  ```
- **源码定位**：`harness/model.py:574-578 _is_concurrency_limit_429` 只匹配 `concurrency` / `simultaneous`；Qwen 的 "inference service is overloaded" **不被识别** → 走 `standard_max_attempts=3` + 1~2s 退避，很快耗尽 → `RuntimeError` → 整集结束
- **现象与根因**：过载型 429 应等价于并发限流（更长退避、更多尝试），或至少在 curriculum 层把该集"暂停并重排"，而不是整集报废
- **最小修复**：① `_is_concurrency_limit_429` 增加 `overloaded` / `try again later` / `server_error` 关键词（复用 8 次 + 6~28s 退避）；② `curriculum` 对 `transient_api_error` 的集改为 `paused` 并重新入队（保留剩余预算）
- **D1 r23_short 扩大复核（严重）**：全波 **21 个 episode** 因 provider 过载报废 —— **18× 429 "inference service is overloaded"** + **3× 502**，波及 8 路（`hy4_7` 5、`hy4_6` 4、`hy4_2` 3、`hy4_9` 3…）。r23 总分 1765（r22 1845），`runtime_error` 门禁 21/2。
- **D1 r24 实时观察（严重：hy4 已丢 46% 集）**：`r24_short` 当前 **72 个 episode 中 33 个 `runtime_error`（46%）**，全部在 `hy4-flash-free`：`hy4_3` 7、`hy4_7` 7、`hy4_6` 4、`hy4_9` 4、`hy4_8` 3、`hy4_10` 3…；`http_retry` 100+、失败 429 18+。`r24_long` 仍 0（长任务请求频率低）。
  → **强烈建议 #1 立即暂停 hy4 squad 或切 provider**；同时修 `_is_concurrency_limit_429`。
- **D1 r24_short 分 bot 报废率（00:50，hy4 通道基本不可用）**：
  | bot | 总集 | runtime | 成功 |
  |---|---|---|---|
  | `hy4_6_nether_bastion` | 4 | **4（100%）** | 0 |
  | `hy4_7_village` | 9 | **8（89%）** | 0 |
  | `hy4_3_midgame` | 12 | **8（67%）** | 1 |
  | `hy4_10_deep_water` | 4 | 3（75%） | 0 |
  | `hy4_9_deep_water` | 5 | 4（80%） | 1 |
  | `hy4_8_nether` | 6 | 4（67%） | 2 |
  | `hy4_2_iron` | 3 | 2（67%） | 0 |
  对照 `glm_* / ds_* / nju_*`：**0 次** runtime_error，全部正常。
  → hy4 这 10 路当前贡献接近 0，继续跑只会消耗配额与时间。
- **D1 r24_short 最新（00:54）**：93 个 episode 中 **40 个 runtime_error（43%）**，4 分钟内又新增 7 个；`r24_long` 仅 `hy4_1` 1 个（长任务频率低）。**过载仍未缓解。**
- **D1 诊断（排除"长上下文导致过载"）**：失败集的 `actor_requests` 中位 **3**（max 30），成功集中位 **6**（max 45）→ 失败发生在 episode **开头**，不是上下文膨胀触发；按分钟统计 429/retry 从 16:37 持续到 16:51（峰值 16:48 共 10 次），属**通道级持续饱和**。
  → 结论：缩小上下文/减少历史不会缓解；只有暂停 hy4 或换通道有效。
- **D1 缓解方案已验证（`hy4_audit_r25`，01:44）**：**4 路 hy4 + rpm=6 + 20 分钟** → `retries 0 / failed429 0 / 502 0 / tracebacks 0`，全门禁 PASS。对照 r24_short 的 **10 路无 rpm 限制 → 40 集报废**。
  → 运维结论：**hy4 的安全并发是 4 路 + rpm=6**；`v2_fleet.py launch` 应把 hy4 squad 默认限制在该配置，或对 429 率做熔断。
- **D1 04:30 主轨复核（又一种 429 body）**：`overnight_12bot` 3 小时累计 612 次 retry、**6 次失败 429**，错误体为
  `{"type":"rate_limit_error","message":"Rate limit exceeded. Please try again."}` —— **既不含 `concurrency` 也不含 `overloaded`**，同样不被 `_is_concurrency_limit_429` 识别。
  → 修复关键词必须覆盖：`concurrency` / `simultaneous` / `overloaded` / `rate limit` / `rate_limit` / `try again later` / `server_error`。
- **D1 hy4 审计轨对照（决定性证据）**：
  - `hy4_audit_r25`（20min）：**0 retries / 0 429 / 0 502**
  - `hy4_audit_r26`（配置 2 小时）：0 429、1 502
  - `hy4_audit_r27`（**3 小时**、4 路、rpm=6、收官 **127 集**）：**0 retries / 0 429 / 0 502 / 0 Traceback**，`mine_dfs` 零进展 2.2%，明确死亡回执 22 条，cooldown 约 128s。 [收官报告](../runs/hy4_audit_r27/triage-hy4_audit_r27.md)
  → **结论定稿：hy4 的 429 与模型无关，纯粹是并发数；4 路 + rpm=6 在 3 小时长程下完全稳定。**
- **D1 06:52 精确定位（`overnight_12bot_part2`，Qwen 429 硬失败率 6×）**：
  - 失败率对照：第一波 `9/1079` = **0.08%**；第二波 `17/3488` = **0.49%**；重试率两波接近（8.0% vs 9.0%）→ **不是总量问题，是突发窗口**。
  - 时间分布：第一波 9 次散布在 19:33–20:53（UTC）；第二波 **15/17 集中在 22:23–22:33（10 分钟）**，期间主轨 12 路 + hy4 审计轨 4 路 = **16 并发**。
  - **每次硬失败的调用序列完全一致**：
    ```
    model.request → http_retry(attempt=1, wait=15.0, concurrency_limit=false)
                  → http_retry(attempt=2, wait=15.0, concurrency_limit=false)
                  → model.failed  Qwen API 返回 HTTP 429：{"type":"rate_limit_error",...}
                  → run.finished  runtime_error
    ```
    即 **2 次重试、共 30 秒退避后放弃**。
  - **源码根因（`model.py:652-653`）**：`standard_max_attempts = 3` / `concurrency_max_attempts = 8`。Qwen 的 `rate_limit_error` body 不含 `concurrency`/`simultaneous` → 落到 `standard` 分支 → **只有 3 次尝试**；并发分支有 8 次 + 6/10/15/20/22/25/28s 退避（约 126s）。Qwen 的限流窗口 ≥60s，30s 必然不够。
  - **最小修复（二选一即可，建议都做）**：① `_is_concurrency_limit_429` 增补 `rate_limit` / `rate limit` / `overloaded` / `try again later` / `server_error`；② 对**任意 429** 直接使用 `concurrency_max_attempts`（429 语义上就是"稍后重试"）。
  - **验收标准**：12 路 × 4h 波次中，`model.failed` 含 429 的集数 ≤2；单次 429 连续尝试 ≥6 次。
- **D1 07:37 追加：天然 A/B 对照证明"8 次尝试预算"就是解药**
  | provider | retry 事件 | `concurrency_limit` | 硬失败 429 |
  |---|---|---|---|
  | NJU（`nju_*`） | 16 | **true**（走 8 次 + 6~28s） | **0** |
  | Qwen（`glm_*`） | 527 | **false**（走 3 次 + 2~4s） | **26** |
  | DeepSeek（`ds_*`） | 0 | — | **0** |

  同一波次、同一时段、同样的 429 压力下：**被识别为并发限流的 NJU 一次都没死，未被识别的 Qwen 死了 26 次**。修复②（任意 429 用 8 次预算）即可直接消除这 26 次报废。
- **D1 07:37 追加：`Retry-After` 无抖动导致惊群（thundering herd）**
  - `overnight_12bot_part2` 545 次 retry 中 **259 次 `wait_s=15.0`**（服务端 `Retry-After: 15` 被原样采用）。
  - **51 个"惊群簇"**：≥3 个不同 bot 在 **2 秒内**同时重试（例：`21:50:40` glm_1/glm_3/glm_5 同秒；`21:53:55` glm_1/glm_4/glm_5 同秒）。
  - 对照：`overnight_12bot` 77 簇；hy4 4 路波次 **0 簇**（`hy4_audit_r26` 4 次 retry、`r27` 0 次）。
  - → **所有 bot 在同一秒醒来 → 再次撞限流 → 再次同秒重试**，形成自持的碰撞循环。修复：`_retry_delay_s` / `_concurrency_retry_delay_s` 对 `Retry-After` 结果加 **0.5~1.5× 抖动**（或每 bot 固定随机相位）。（`hy4_7` 4、`hy4_3/6/9` 各 3、`hy4_5/8` 各 2、`hy4_2/10` 各 1）；**glm / ds / nju 全部 0 次**。
  → 结论：过载是 **hy4 免费通道专属**，不是全局。**运维建议：暂停或降并发 hy4 squad（10 路），保留 glm/ds/nju**；同时尽快修 `_is_concurrency_limit_429`。
  → **运维建议：暂停新波次或切 provider，否则 r24 的分数会被 429 严重稀释**。
- **建议（运维）**：① 立即按上述修 `_is_concurrency_limit_429` 关键词；② `v2_fleet.py launch` 增加熔断：连续 429 率 >20% 时暂停新波次或降并发/换 provider；③ curriculum 对 `transient_api_error` 的集暂停重排而不是报废
- **验收标准**：r24 起长任务因 429/502 报废的集数 ≤1；`hy4_1`（或同类长任务）分数 ≥60
- **r31_long 复核**：3 路 2700s，429/502 报废 **0**。HY4_1 终版 **20/60**，EP01 唯一验证成交，后续多次重派铁器，EP03 为区块未加载，两个超时均无硬 429。12 路 4h 和持续重试仍待暴露。 [终版](../runs/r31_long/hy4_hy_1_bare/wild-endurance-r31_long_hy_1_bare-20261119-221907.json) · [EP10 终止](../runs/r31_long/hy4_hy_1_bare/20261119-230411-792716.events.jsonl#L10)
- **持续重试实证**：r36 DS_2 EP01 同一请求连续 5 次 429 退避，合计 **83.897s**，第 6 次完成。r33/r34 长波 429/502 报废均为 **0**，HY4_1 仍各 **20/60**。12 路 4h 样本仍缺。 [r36 DS_2 EP01](../runs/r36_short/nju_ds_2_cave_combat/20261119-235444-144417.events.jsonl#L293) · [r34 长波终版榜单](../runs/r34_long/triage-r34_long.md)

- **P2-40** 课程应按**实测 ROI** 动态加权：`shiny_gear`（61 分/100 请求）、`brew_potion`（87.2）、`use_lodestone`（74.9）应优先；`obtain_ancient_debris`（11.6）、`find_fortress`（5.9）应降权。建议在 `curriculum.py` 里用每波实测的 `score_delta / actor_requests` 更新 target 权重（滑动平均），而不是固定分数。
- **P2-37** `mine_dfs` 的 `bad_target_for_dimension` **80 次**（`diamond` **63**、`iron` 16、`coal` 1）：模型在**下界**要求挖钻石/铁。工具已返回错误但模型反复重试。修复：`bad_target_for_dimension` 时返回该维度可用矿种清单（下界：quartz/ancient_debris/gold/netherite_scrap）。
- **P2-38** `mc_skill_craft_gear` 的 `missing_ingredients` **652 次**（占 craft 调用 43%，成功仅 380）：`ds_4` 112、`glm_1` 107、`hy4_6` 88、`nju_3` 80；高频物品 `stone_pickaxe` 132、`wooden_pickaxe` 125、`diamond_pickaxe` 90、`oak_sign` 46。返回体已含 `required_materials`/`available_materials`/`missing_materials`，但模型仍重复调用。修复：返回 `suggested_next`（`smelt`/`gather`/`bootstrap_*`），同一 item 连续 2 次失败后加 90s 冷却。另有 `need_crafting_table` 185、`inventory_full` 69、`server_recipe_rejected` 26。
- **P2-39** `mc_move_to` 的 `stalled`（8s 无位移）**103 次**（move_to 87、dig 13、bind_bed 2、interact 1）：现有 `unstick` 提示有效，但缺少"stall 2 次自动 unstick"的自动兜底。
- **P2-36** `staircase:pillar_fallback` **自锁**：`no_place_face` → `_dig_pillar_up(1)` → 返回 `ok=True, requires_replan=False` + hint"从新高度重试" → 模型原地再调一次 → 再 `no_place_face`。
  - 实测：`overnight_12bot_part2` 140 次 pillar_fallback，**14 段连续 ≥3 次**，累计 **723s**；最长 `nju_guitest_01_nether` **连续 29 次**（188s，其间无任何其他工具调用）；`glm_2` 18 次（132s）。
  - `hy4_audit_r28`：73 次，**10 段 ≥3 次**，累计 **437s**；`hy4_6` 最长 10 次（134s）。
  - 修复（约 5 行）：同一 episode 内连续 `pillar_fallback` ≥2 时，返回 `requires_replan=True` 并建议 `mc_skill_unstick` / 水平换向；≥3 时直接给该 target 加 120s 冷却。
- **P2-35** `agent.py:1617-1621` 新增的 `network_error` 状态（`not_loaded`/`chunk_not_loaded`/`world_not_ready` 等）未被 `curriculum._is_transient_llm_error`（`:887` 仅接受 `status=="runtime_error"`）识别 → **纯传输抖动会累计 strike**。实测 1 例：`ds_1` EP13 `sleep_in_bed`，`[not_loaded] chunk not loaded`，9 次请求后 `status=network_error`。修复：`_is_transient_llm_error` 接受 `network_error`（或 `status in {"runtime_error","network_error"}`）。
- **P2-34** `overnight_12bot_part2 / ds_4_nether_fortress` EP9（`nether/get_wither_skull`）在 **连续两次死亡**（`mob_attack`，182s 内 health 20→0→重生→0）后以 `[not_connected] bot not connected` 结束，`status=runtime_error`，白烧 28 次 actor 请求。`not_connected` 本属 `agent.py:206 _TRANSIENT_CODES`，但该错误是在 run 循环收尾阶段抛出的，未被当作 transient → 整集报废。建议：run 循环对"死亡/掉线导致的 not_connected"执行一次 `mc_status` + 重连，再决定是否暂停该集。
- **P2-33** `max_tokens` 截断：过夜双轨共 **65 次** `finish_reason=length` 事件，其中 **64 次被 `_filter_complete_prefix_calls` 救回**（`length_recovered`），仅 `hy4_audit_r27` **1 次**因无完整工具调用而报废整集。分布：`nju_guitest_01_nether` 27、`nju_3_village_trade` 13、`hy4_2_iron` 9。
  → 影响已很小，建议仅对"零完整工具调用"的截断做一次自动重试（或该 slot 提高 max_tokens），优先级维持 P2。
- **P2-32**（评分口径，影响跨模型对比）场景 loadout **预授予成就会被计入 final_score**：`midgame` 在 setup 里 `advancement grant` 了 `mine_stone/upgrade_tools/smelt_iron/iron_tools/mine_diamond/shiny_gear`，合计 **95 分**。r24_long 的 `glm_5` 在 **elapsed=180s、EP1 仍 timeout** 时已显示 **115 分**（其中 95 分来自预授予）。
  建议：① episode 开始时做 advancement 基线快照，`final_score` 只计增量；或 ② 报告里单列 `baseline_points` 与 `earned_points`。否则"midgame 195 vs bare 10"不能直接当作能力差异。

- **r66 LC 新通道过程证据**：`longcat-2.5-free` 四槽各 6 RPM。22:55 点验时已记录 9 条硬 429 废集，四槽均有。LC1 EP03、LC2 EP04、LC3 EP03、LC4 EP04 均找到单一请求的七次 HTTP 429 延后，每次 35s，第八次仍 429 后 `runtime_error`。本地初始限速等待为 0；单请求历时约 251–256s。LC1 另有连续 429 后撞集时限的样本。源码当前把所有 429 放进最多八次的预算，耗尽后仍按运行错误收尾。复验需按 LC 通道调度并区分硬 429 与带 429 前因的集超时。整波数字等终版。 [LC1 七次延后](../runs/r66_lc_village_midgame_4bot/lc_lc_1_day2_village_near/20261121-064125-325784.events.jsonl#L8) · [LC2 耗尽](../runs/r66_lc_village_midgame_4bot/lc_lc_2_day2_village_near/20261121-064608-815971.events.jsonl#L105) · [LC3 耗尽](../runs/r66_lc_village_midgame_4bot/lc_lc_3_midgame/20261121-064016-656650.events.jsonl#L34) · [LC4 耗尽](../runs/r66_lc_village_midgame_4bot/lc_lc_4_midgame/20261121-063532-899055.events.jsonl#L37) · [重试预算](../src/mc_agent/harness/model.py#L664)

- **r66 LC 终版**：四槽各 6 RPM，**4/4、30 集、0 验证、13 硬 429**。LC1／2／3／4 分别 **3／3／2／5** 集硬失败，最后请求全部走满八次，前七次各 35s。另 12 个超时集都记录过 429。总 184 次 429 延后与 2 次其他重试，计划等待 6440 Bot·s，未扣取消或集截止中断。服务端具体配额规则未落盘。此波是 LC 通道，不影响过去五路 GLM 的实机验收。 [LC1 终版](../runs/r66_lc_village_midgame_4bot/lc_lc_1_day2_village_near/wild-endurance-r66_lc_village_midgame_4bot_lc_1_day2_village_near-20261121-063318.json) · [LC4 终版](../runs/r66_lc_village_midgame_4bot/lc_lc_4_midgame/wild-endurance-r66_lc_village_midgame_4bot_lc_4_midgame-20261121-063348.json) · [通道配置](../scripts/v2_fleet.py#L128)

- **LC 退避实耗下界**：据当集同请求的下一次重试、完成或 HTTP 失败回执，160 条 429 退避确定已等完且集内无重叠，共 **5600 Bot·s／7200.6 Bot·s（77.8%）**。另有 24 条已安排的退避缺完整结束证据。 [计时样本](../runs/r66_lc_village_midgame_4bot/lc_lc_1_day2_village_near/20261121-064125-325784.events.jsonl#L8) · [客户端等待](../src/mc_agent/harness/model.py#L703)

### BUG-043 | P1 | Integrator（#1，`harness/curriculum.py`）| 状态: OPEN（overnight_12bot 实测）
- **影响**：`ds_ds_2_cave_combat` 的 EP4/EP7/EP8 全部 `status=aborted, gate_reason=lost_iron_pickaxe`，其中 EP4 已消耗 **33 个 actor 请求**才发现无镐，EP7/EP8 各 1 次；三集零产出
- **日志证据**：`overnight_12bot/ds_ds_2_cave_combat/curriculum-*.json` 三集 `aborted`；report 内 `gate_reason=lost_iron_pickaxe`
- **源码定位**：`harness/curriculum.py:2235-2237` —— `shiny_gear/mine_diamond` 在 `highest_pickaxe_tier < 3` 且 `iron_ingot+raw_iron < 3` 时 `abort`。但：
  1. 判定发生在 episode **运行中**，EP4 白烧 33 次请求才触发；
  2. abort 后没有给该 target 加冷却/换目标，EP7/EP8 又被选中并立即 abort（strike 到 2 才冷却）
- **最小修复**：① 在 episode **开始时**（下矿前）做同样的镐/铁料检查，不满足直接跳过该目标；② `lost_iron_pickaxe` abort 后立即 `cooldowns[target]=+300s`，并把 `bootstrap_iron_age`/`smelt_iron` 提升为候选
- **D1 04:30 追加证据**：`ds_ds_1_bare` EP19 也出现一次 `lost_iron_pickaxe` abort（累计 4 次 abort，2 路）。
- **D1 06:51 追加证据**：`hy4_hy_9_deep_water` EP7（shiny_gear）同样 `lost_iron_pickaxe` abort → 已扩散到 3 路（`ds_2`、`ds_1`、`hy4_9`）。
- **验收标准**：r25 起 `lost_iron_pickaxe` abort ≤1 次/波；无"abort 后再选同一目标"的重复

### BUG-044 | P1 | Integrator（#1，`harness/agent.py` + `mc_tools`）| 状态: OPEN（过夜双轨实测）
- **影响**：过夜主轨 2 集 + hy4 审计轨 1 集因 **MC API（bot 后端）ReadTimeout** 整集报废
- **日志证据**：
  - `ds_ds_4_nether_fortress` EP11/EP12（`nether/ride_strider`）：`MC API 网络请求异常（ReadTimeout）：timed out`
  - `hy4_hy_2_iron` EP15（`make_a_sign_glow`）：同上
- **源码定位**：`harness/mc_tools.py:_call_json` 在 `httpx.HTTPError` 重试一次后仍失败时 `raise RuntimeError("MC API 网络请求异常…")`；`agent.py` 的 run 循环把 `RuntimeError` 归为 `runtime_error` 并终止该集（与 BUG-011 同类，但发生在 MC 后端而非 LLM）
- **最小修复**：① `_call_json` 对 `status/inventory/events` 等只读请求做 2~3 次短退避；② run 循环把"MC API 网络请求异常"识别为 `transient_api_error`（暂停重排该集），不要当致命错误
- **验收标准**：r25 起 MC API ReadTimeout 导致的整集报废 ≤1；只读请求 0 次直接抛 RuntimeError

### BUG-045 | P1 | Integrator（#1，`v2_fleet.py` / 调度层）| 状态: FIXED（r29 五路验收，r30/r31 复核）

- **D1 退避墙钟复算**。已进入下一次尝试的计划等待计入下界。上界按含已记录或可能未送达 429 重试的完整请求窗口计，每个请求最多一次。r29 GLM 为 **0.13–1.62%**，r30 短波/长波为 **2.21–3.81% / 1.42–3.14%**，r31 短波/长波为 **1.15–3.84% / 0.00–1.62%**，五波上界均 ≤5%。r29 DS_2 计划等 7.770s，重试转发与取消日志相隔 2.266s，实际等待无法从该间隔确定。 [r29 GLM 重试](../runs/r29_newmix/glm_glm_2_nether_bastion/20261119-210114-477304.events.jsonl#L43) · [取消示例](../runs/r29_newmix/nju_ds_2_cave_combat/20261119-210255-905851.events.jsonl#L152) · [复算表](../../s-triage/docs/D1-r11-triage.md#4hy-bug-045-将上界改为可能含退避的完整请求窗口)

- **r69 官方波归组复核**：`official_r69_80m_glm1` 使用 Bot 名 `GLM_1`，启动记录的模型为 `hy4-flash-free`。D1 优先读取逐集 `run.started.model`，早期没有模型字段才用目录名归组。复跑 r29 的 GLM 重试率 **0.44%**、退避占比 **0.13–1.62%**、全波硬 429 **0**。官方波仍缺终版。 [官方启动记录](../runs/official_r69_80m_glm1/GLM_1/20261121-075743-738230.events.jsonl#L3) · [D1 门禁](../../s-triage/scripts/d1_ticket_gates.py#L511)
- **影响**：`overnight_12bot_part2` 中 5 路 Qwen（`glm_*`）**retry 率 17~25%**，合计 **18.1% 墙钟**（glm_1 24.4%、glm_5 22.7%）耗在 429 退避上；对照 NJU **1.2~2.1%**、DeepSeek **0~0.2%**。
- **证据（2h50m 波，同一时段）**：
  | bot | 集 | 请求 | retry | retry% | 硬失败 429 | 退避墙钟占比 |
  |---|---|---|---|---|---|---|
  | `glm_1_nether_fortress` | 63 | 1018 | 213 | **17.3%** | 15 | **24.4%** |
  | `glm_5_midgame` | 79 | 950 | 201 | **17.5%** | 15 | **22.7%** |
  | `glm_2_nether_bastion` | 24 | 702 | 169 | 19.4% | 9 | 18.4% |
  | `glm_4_brewing_alchemy` | 34 | 516 | 135 | 20.7% | 8 | 14.7% |
  | `glm_3_village_trade` | 19 | 313 | 105 | 25.1% | 2 | 10.5% |
  | `nju_*`（3 路） | 81 | 1475 | 22 | **1.2~2.1%** | 0 | ≤0.5% |
  | `ds_*`（4 路） | 139 | 2627 | 1 | **0~0.2%** | 0 | 0% |
- **判读**：5 路 Qwen 的 retry 率**成比例一致**（与各 bot 请求量无关）→ **是共享 key/通道的配额饱和**，不是某个 bot 行为异常。`hy4`（另一通道，4 路 + rpm=6）同时段 **0 次 429**。
- **最小修复**：给 Qwen squad 加**每通道 RPM 上限**（参照 hy4 已验证的 `rpm=6`），或把 `glm` 并发从 5 降到 3；`v2_fleet.py launch` 应按 provider 配置 `rpm` 而不是全局 default。
- **验收标准**：Qwen 通道 retry 率 ≤5%、退避墙钟 ≤5%；单波 429 硬失败 ≤3。
- **实测验收**：`r29_newmix` 五路 GLM，各 10 RPM，按终版 Bot 墙钟重算的过夜第二波→r29 为 **16.87%→0.44%** 重试、**15.38%→0.13%** 退避、**52→0** 整波硬 429。r30_short/long 分别为 2.67%/2.21%/0、2.28%/2.15%/0，r31_short 为 2.30%/1.30%/0。 [频道 RPM](../scripts/v2_fleet.py#L101) · [r29 GLM2 EP01 重试](../runs/r29_newmix/glm_glm_2_nether_bastion/20261119-210114-477304.events.jsonl#L43) · [r31 GLM1 EP01 重试](../runs/r31_short/glm_glm_1_nether_fortress/20261119-215952-195509.events.jsonl#L303)

### BUG-046 | P1 | Integrator（#1，`scripts/l3_wild_endurance_runner.py` / 波次配置）| 状态: OPEN（过夜主轨实测）

- **r63–r70 终版增量**：计划时长有数值的 **73 槽**均为 1500–2700s。其中 2700s 的 **19/19** 槽启动均记录 `max_episodes=0`，实际 **2700.0–2715.8s**。与 r29–r62 的 18 槽合计 **37** 条 45 分钟达标证据。四小时验收仍缺终版。 [最短槽启动](../runs/r66_day12_full_45m/hy4_hy_10_bare/stdout.log#L3) · [最长槽终版](../runs/r63_day12_full_45m/hy4_hy_3_bare/wild-endurance-r63_day12_full_45m_hy_3_bare-20261121-050448.json)
- **影响**：`max_episodes=80` 是与 `duration_s` **独立的硬上限**，跑得快的 bot 会在**墙钟未到**时提前退出，剩余时间全部空转。
- **实测（`overnight_12bot_part2`，`duration_s=14100` / `max_episodes=80`）**：
  | bot | 用满集数 | 实际墙钟 | 剩余墙钟 | 结束时状态 |
  |---|---|---|---|---|
  | `glm_5_midgame` | 80/80 | **10355s（2h53m）** | **3745s（1h02m）空转** | `completed` |
  | `glm_1_nether_fortress` | 80/80 | **12433s（3h27m）** | 1667s（28m）空转 | `completed` |
  | 其余 10 路 | 28~56 | 12214~12939s | — | 正常跑到 deadline |
- **放大因素**：429 报废的集**同样消耗集数配额** —— `glm_1` 80 集中 **16 集**是 429 `runtime_error`（20%），`glm_5` **15/80**（19%）。即 Qwen 通道既浪费墙钟（18% 退避）又浪费集数槽位。
- **r28 无 429 的对照**：短预算目标让 `hy4_2_iron`、`hy4_5_brewing_alchemy` 分别在 8640s、7911s 达到 `50/50` 集，距 9000s 目标还有 360s、1089s。集数上限在通道无硬失败时依然会提前收尾。 [铁器收官](../runs/hy4_audit_r28/hy4_hy_2_iron/curriculum-20261119-145210-146844.json) · [酿造收官](../runs/hy4_audit_r28/hy4_hy_5_brewing_alchemy/curriculum-20261119-145213-868815.json)
- **副作用（评测口径）**：快 bot 被截断、慢 bot 跑满 → **跨 bot/跨波次分数不可比**（`glm_5` 名义 4h 实际只跑了 2h53m）。
- **最小修复**：① 波次按 `max_episodes = ceil(duration_s / 60)` 或直接 `None`（只靠 deadline）；② 429/502 等 `transient` 集**不占用**集数配额（或 `max_episodes` 只统计非 transient 集）。
- **验收标准**：4h 波次所有 bot 实际墙钟 ≥ 0.95 × `duration_s`；`elapsed_s/duration_s` 比值分布 ≥0.95。
- **D1 配置复核**：`165e792` 让 Fleet 默认传 `max_episodes=0`，runner 映射为无集数上限。r30_long→r31_long 同三路从上限 45 集切到 0，最短实跑仍为 2700.1s。r29 至 r62 有终版墙钟的 446 槽中，实际用 0 上限的 18 个 2700s 槽全部跑满，范围 2700.1–2747.3s。终版最大计划时长为 2700s，四小时验收待补。 [Fleet 默认值](../scripts/v2_fleet.py#L955) · [runner 映射](../scripts/l3_wild_endurance_runner.py#L2356) · [r30 GLM5 开局](../runs/r30_long/glm_glm_5_midgame/stdout.log#L2) · [r31 GLM5 开局](../runs/r31_long/glm_glm_5_midgame/stdout.log#L2) · [r31 GLM5 终版](../runs/r31_long/glm_glm_5_midgame/wild-endurance-r31_long_glm_5_midgame-20261119-221905.json#L90)

### BUG-047 | P1 | Integrator（#1，`harness/mc_tools.py` + `bot.py`）| 状态: OPEN（实体占位坐标单航点已实测，尾集与忙锁待复验）

- **同裸装三槽 r73→r74**：移动最大 **24.8→11.8s**，样本 **116→52**；集限峰 **0.049→0.077s**。新版混参三次均一跳，最长 **11.787s**。后端忙锁和 44 秒余量失败档无匹配样本。 [最长混参](../runs/r74_bare_real_drop_60m/hy4_hy_5_bare/20261121-132039-660332.events.jsonl#L74) · [新版集限峰](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/20261121-142018-399633.events.jsonl#L11)

- **坐标补丁真实失败分支通过**：r74 裸装 HY4_5 EP02 加载 `28a9838`，同 180s 环岛任务三次长距都带 `entity_id=0`，单航点移动 **6.586／11.787s**；最终 `path_not_found` 在 **0.0724s** 返回 `partial_move=true` 与重规划，整集 **67.814s** 收尾。该次失败发起仍余 145.971s，44s 档集尾和原后端忙锁继续验。 [加载和集限](../runs/r74_bare_real_drop_60m/hy4_hy_5_bare/20261121-132039-660332.events.jsonl#L1) · [长距失败回执](../runs/r74_bare_real_drop_60m/hy4_hy_5_bare/20261121-132039-660332.events.jsonl#L118) · [整集结束](../runs/r74_bare_real_drop_60m/hy4_hy_5_bare/20261121-132039-660332.events.jsonl#L310)

- **坐标预算补丁已合入**：`3fbdf12` 优先坐标，带集限时跳过旧多跳，失败状态读限为 1s。D1 的虚拟时钟回放和集成有／无起点位置夹具均通过。等待加载该版的同参数长距失败及集尾，原忙锁和护航 `status` 子门继续跟踪。 [坐标选型](../src/mc_agent/harness/mc_tools.py#L1214) · [状态读](../src/mc_agent/harness/mc_tools.py#L1356) · [集成夹具](../tests/test_skills_and_providers.py#L1834) · [D1 回放](../../s-triage/tests/test_d1_repro.py#L110)

- **r71 日出坐标移动越限 53.227s**：DS_2 EP02 加载 `ceba3a8`，两条环岛坐标移动都随模型参数带 `entity_id=0,y=0`，耗 **126.371／97.353s**，终态均 `path_not_found`。第二条发起时余 **44.133s**，180s 集限在 **233.227s** 才结束。坐标参数优先组成请求，但单航点检查另外要求 `entity_id is None`，模型占位值会绕过这道门。直达失败后状态读默认 45s，满足距离条件时启用未继承集截止的分段回退。模拟传输实测多发一跳，越限 13.588s。实机子请求时延待加回执。统一按坐标模式判断航点，状态读和回退共用余量，复验整集超量 ≤10s。 [离线夹具](../../s-triage/tests/test_d1_repro.py#L121) [加载和集限](../runs/r71_day12_real_drop_60m/nju_ds_2_bare/20261121-115440-072602.events.jsonl#L1) · [第二条工具开始](../runs/r71_day12_real_drop_60m/nju_ds_2_bare/20261121-115440-072602.events.jsonl#L66) · [整集终态](../runs/r71_day12_real_drop_60m/nju_ds_2_bare/20261121-115440-072602.events.jsonl#L69) · [单航点门](../src/mc_agent/harness/mc_tools.py#L1304) · [分段回退](../src/mc_agent/harness/mc_tools.py#L1397) · [内部跳点循环](../src/mc_agent/skills/library.py#L1934)

- **新传输和熔炉修复已合入**：`102fea5` 含 `f514f19`，Guard 的 `status` 把 HTTP 分阶段及一次套接字重试纳入共同余量。`e6f2a72` 停止超时挂起的熔炉交互再重试。两个离线入口均通过。等待加载新版后的 `combat_timeout endpoint=status` 和熔炉抛时对照。 [传输预算](../src/mc_agent/harness/guardian.py#L205) · [熔炉超时夹具](../tests/test_skills_and_providers.py#L2794)
- **`f514f19` 首条实机状态截止回执**：新版官方 r72 MoveTest EP06 `endpoint=status`，护航配对 **9.035s**，清理 **0.094s**，前段 **8.941s**，仍比核心声明 7.5s 多 **1.441s**。旧版同角色裸装 r72 EP05 **10.498s**（课前），r70 日出 DS_2 EP05 **15.428s**（actor）。已见新版截断结构化回执。场景和执行阶段不同，重试／水中暂停缺子计时，保留核心长尾门。 [新版全对](../runs/official_r72_80m_movetest/MoveTest_01/20261121-114456-299783.events.jsonl#L12) · [旧版 MoveTest](../runs/r72_bare_real_drop_60m/nju_movetest_01_bare/20261121-112732-013568.events.jsonl#L3) · [旧版 DS_2](../runs/r70_day12_real_drop_60m/nju_ds_2_bare/20261121-110655-738829.events.jsonl#L233) · [预算分段](../src/mc_agent/harness/guardian.py#L210)
- **战斗控制器和恢复宏共享预算已合入**：`ceba3a8` 含 `c3b0136`、`ff28f1a`。控制器取更紧外层期限，普通战斗恢复宏用同一 Guard 会话。r75 中期与 r72 官方双槽加载 `5b7fd9d62eca34fc`，恢复宏、进食、浅岸复查、窒息及传输定向夹具共 15 通过，待实机。 [控制器截止](../src/mc_agent/skills/combat_env.py#L2982) · [恢复宏夹具](../tests/test_generation_guard.py#L11098)
- **r75 中期两项实机接触**：HY4_2 EP04 新版 `use_item` 截止，总 **7.933s**、兜底 **0.427s**、清理前 **7.506s**。EP05 的隧道在余 **0.459s** 时调用，**0.0017s** 内返回 `episode_deadline_imminent`、零步，整集只超 **0.0485s**。末秒隧道入口通过，仍缺相同模式的 `status` 长尾对照和移动忙锁。 [护航诊断](../runs/r75_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-120541-223755.events.jsonl#L143) · [隧道拒绝](../runs/r75_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-123027-022488.events.jsonl#L427) · [隧道预算源码](../src/mc_agent/harness/mc_tools.py#L2966)

- **r71 裸装采木钳制分支实机通过**：HY4_5 EP18 加载 `bea8ba8`，含 `e6aff12`。实际 actor 余 **25.5644s** 启动采木，工具限额约 21.564s，配对 **20.5994s** 后返回 `time_budget`，集尾仅超 **0.0727s**。旧版官方 5Ease 剩 29.604s 执行 43.369s、超 13.775s。Bot、地块不同，此对照实证共享预算截断。极短入口拒绝、移动忙锁与末秒隧道仍待实机。 [新版采木配对](../runs/r71_bare_real_drop_60m/hy4_hy_5_bare/20261121-110659-020341.events.jsonl#L181) · [新版集尾](../runs/r71_bare_real_drop_60m/hy4_hy_5_bare/20261121-110659-020341.events.jsonl#L211) · [旧版](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-103642-535444.events.jsonl#L111) · [钳制源码](../src/mc_agent/harness/mc_tools.py#L2622)
- **r72 裸装短余量采木遭战斗抢占**：HY4_7 EP12 余 **9.863s** 发起采木，**3.899s** 后战斗反射开始，无该动作完成回执。外层工具 **10.068s** 返回通用中止文本，整集仅超 **0.210s**，过 ≤10s 门。该例仍待隔离采木内部和反射阶段的剩余预算。 [采木与抢占](../runs/r72_bare_real_drop_60m/hy4_hy_7_bare/20261121-121030-666805.events.jsonl#L37) · [集尾](../runs/r72_bare_real_drop_60m/hy4_hy_7_bare/20261121-121030-666805.events.jsonl#L42) · [采木入口](../src/mc_agent/harness/mc_tools.py#L2622)

- **r70 官方旧版采木尾集对照**：两路均加载 `494b133`，早于 `e6aff12`。5Ease EP36 剩 **29.604s** 起采木，耗 **43.369s**，集尾超 **13.775s**。GLM_1 EP30 剩 **35.071s** 起采木，耗 **43.049s**，集尾超 **7.988s**。两条都返回部分入包和技能时限，可用作新版剩余 30–35 秒档的对照。 [5Ease 配对](../runs/official_r70_80m_5ease/5Ease_Bot/20261121-103642-535444.events.jsonl#L111) · [GLM_1 配对](../runs/official_r70_80m_glm1/GLM_1/20261121-103720-547105.events.jsonl#L86) · [当前入口](../src/mc_agent/harness/mc_tools.py#L2622)

- **r70 门户中期重现采木越限**：HY4_9 EP10 加载 `db680f5`，预算 **92.8769s**。余 **8.5219s** 启动 `harvest_logs`，运行 **44.0378s**，技能自身 `time_budget` 回执含 `partial_error=mine_timeout`、真得 2 原木。集尾 **128.3997s**，超 **35.5228s**，该槽整波超计划 **35.6s**。和 5Ease EP03 一起覆盖入口余量低于 6 秒及仍可发起但必须钳制的两条路径。复验采木及清包共享集截止后集尾超量 ≤10s。 [版本和集限](../runs/r70_midgame_portal_40m/hy4_hy_9_midgame/20261121-092232-005556.events.jsonl#L1) · [工具配对](../runs/r70_midgame_portal_40m/hy4_hy_9_midgame/20261121-092232-005556.events.jsonl#L217) · [集尾](../runs/r70_midgame_portal_40m/hy4_hy_9_midgame/20261121-092232-005556.events.jsonl#L220) · [采木入口](../src/mc_agent/harness/mc_tools.py#L2622)
- **采木截止修复已合入**：`4c18a3c` 含 `e6aff12`。集成 9 条采木测试和 D1 两条实机余量夹具通过。r71 门户三槽加载 `c036413f85c8ad6b=4c18a3c`，待集尾真实采木和终版对照。移动忙锁及末秒隧道专项门仍待。 [D1 两个余量夹具](../../s-triage/tests/test_d1_repro.py#L110) · [新入口](../src/mc_agent/harness/mc_tools.py#L2622) · [r71 加载指纹](../runs/r71_midgame_portal_40m/hy4_hy_9_midgame/20261121-092634-275938.events.jsonl#L1)
- **r70→r71 门户终版对照**：同三角色 actor 集限最大超 **35.523→0.081s**，r71 三槽均 2400.1s 跑满。新版五条采木开跑时至少剩 **184.397s**，其中最长 **47.577s**。末秒采木本波无实际暴露，保持专项待验。 [新版集限峰](../runs/r71_midgame_portal_40m/hy4_hy_8_midgame/20261121-100134-423114.events.jsonl#L411) · [最少余量的采木](../runs/r71_midgame_portal_40m/hy4_hy_8_midgame/20261121-095749-845877.events.jsonl#L96) · [预算钳制](../src/mc_agent/harness/mc_tools.py#L269)
- **护航截止补丁已合入**：`0d8d3da` 含 `39eaca5`，主操作和常规退出共用 7.5 秒截止，超时兜底清理四个阶段另共用 1.5 秒。离线专测通过，r72 中期与 r69 日出 60 分钟均加载 `afd53dc361091229`。待可配对的真实 `combat_timeout` 回执。 [慢清理专测](../tests/test_generation_guard.py#L940) · [r69 日出加载版](../runs/r69_day12_real_drop_60m/nju_ds_2_bare/20261121-094851-381801.events.jsonl#L3)
- **r69 日出回执定位到清理之前**：HY4_3 EP06 加载新版，护航 `combat_timeout` 配对 **12.055s**，异常检测端点 `status`、核心预算 7.5s、清理实耗 **0.073s**。清理之前约 **11.982s**，轨迹缺内层逐调用计时。复验应量化主操作、允许单独计时的水中恢复和传输阶段。 [开始](../runs/r69_day12_real_drop_60m/hy4_hy_3_bare/20261121-100747-526928.events.jsonl#L7) · [截止诊断](../runs/r69_day12_real_drop_60m/hy4_hy_3_bare/20261121-100747-526928.events.jsonl#L8) · [清理边界](../src/mc_agent/harness/guardian.py#L455) · [独立水恢复](../src/mc_agent/harness/guardian.py#L649)

- **r69 官方另一条集尾路径**：5Ease_Bot EP03 加载 `f22bfc9`。`harvest_logs` 在余 **5.8975s** 时开跑，配对 **20.1520s** 后只返回中止文本，整集 **614.2655s／600s**。当时和当前入口均用独立 45 秒预算，工具后清包在采木的预算作用域外。日志只给出外层完成，具体阻塞内层未定位。采木和清包共享本集余量，复验相同余量的拒绝或截断，以及集限最大超 ≤10 秒。D1 离线夹具在旧入口复现 **614.2545s**。 [工具起止](../runs/official_r69_80m_5ease/5Ease_Bot/20261121-080156-112922.events.jsonl#L431) · [集尾](../runs/official_r69_80m_5ease/5Ease_Bot/20261121-080156-112922.events.jsonl#L434) · [采木入口](../src/mc_agent/harness/mc_tools.py#L2622) · [独立预算](../src/mc_agent/skills/library.py#L3562) · [离线夹具](../../s-triage/tests/test_d1_repro.py#L110)

- **r70 战斗传输截止暴露**：`2b422eb` 内 `08f3364` 实际返回两次 `mode=combat_timeout,reason=time_budget,requires_replan=true`，均 HY4_2，配对耗时 **7.929s／7.933s**。同三角色 r69→r70 `subcortical_combat` 普通护航样本 **20→47**，最大 **7.534→8.284s**，新版新增可见的有界故障回执。actor 移动忙锁和末秒隧道门仍缺暴露。 [首次截止](../runs/r70_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-083214-025673.events.jsonl#L461) · [再次截止](../runs/r70_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-085902-864680.events.jsonl#L208) · [护航截止源码](../src/mc_agent/harness/guardian.py#L455)

- **隧道候选 `637b18e` 已上后续波次**：围栏检查、原生隧道、探测和清理共用集截止。`r65_day12_full_45m` 与 `r67_expl_mem_3bot` 启动早于此补丁。`r66_day12_full_45m`、`r68_expl_mem_iron_3bot` 启动指纹 `328d1430f7830c51=b328836` 包含此提交，待有集尾隧道样本核对以及原票 busy／末十秒楼梯门槛。 [r68 启动](../runs/r68_expl_mem_iron_3bot/nju_ds_1_iron/20261121-072056-284230.events.jsonl#L1) · [r66 Day1 启动](../runs/r66_day12_full_45m/nju_ds_2_bare/20261121-072356-368358.events.jsonl#L3)
- **D1 末十秒隧道子门**：用同集调用完成时间减预算，复跑 r66 HY4_1 EP06 报工具超 **33.289s**、集尾超 **33.295s**。`637b18e` 的临界拒绝、挖掘中断、清理中断和底层探测 **4 条离线夹具通过**。后续须有终版末十秒隧道才能关闭此分支。 [工具回执](../runs/r66_expl_mem_hy1/hy4_hy_1_iron/20261121-065115-374234.events.jsonl#L168) · [actor 夹具](../tests/test_skills_and_providers.py#L2977) · [原生隧道夹具](../tests/test_mining_dfs.py#L4381)
- **移动 `busy` 的剩余根因**：以 `b328836` 的 `McToolset` 和 `BotSession` 离线复现短距坐标移动。预检 0.1s 认为空闲，随后首个 `/move_to` 立即报 `busy`，`/stop` 报未释放。逻辑已格式化 `busy` 再进入通用失败观察，额外走一次使用 **45s** 默认读超时的 `GET /status`。模拟该 GET 卡住时，最终仍返回 `busy`，总耗 **45.3s**。收尾剩余仅 6s 也同耗，超 **39.3s**。当前实机基线仍是 r34 HY4_6 EP03 的长 `busy`。最小修复是在租约未释放的坐标分支格式化后立即返回；复验已知 `busy` ≤20s、集尾超限 ≤10s，预检和已确认释放后的重试各保留原用途。 [实机旧忙锁](../runs/r34_short/hy4_hy_6_nether_bastion/20261119-232351-607318.events.jsonl#L251) · [额外状态读取](../src/mc_agent/harness/mc_tools.py#L1347) · [默认超时](../src/mc_agent/bot.py#L35)
- **候选 `aa6c2d5` 已随 `0a68fc8` 合入**：最终忙锁立即返回，保留已确认的租约释放重试与围栏元数据。D1 两条再现加集成三条租约夹具 **5 项通过**。r68 中期两个已启动槽加载早于合入，忙锁实机仍须等待新指纹。 [修复返回](../src/mc_agent/harness/mc_tools.py#L1347) · [D1 临界夹具](../../s-triage/tests/test_d1_repro.py#L74)
- **r56 的末秒采矿穿透**：HY4_4 裸装 EP05 在 600s 集限只余 **0.904s** 时启动 `mc_skill_mine_blocks(coal_ore)`，单次回执 `dig_timeout_s=15.0`、0 产出，调用实耗 **15.498s**，终态超限 **14.600s**。加载版 `mc_skill_mine_blocks` 直接调用带默认 45s 的技能，没有 `_clamp_tool_budget`；相邻熔炼入口已有护栏。最小修复是在这个入口拒绝极短余量，并把余量贯穿扫描、单次挖掘和清理。验收复用余约 0.9s 的调用，确认不发生 dig、集尾不超门槛。 [调用开始](../runs/r56_day1_day2_bare_5bot/hy4_hy_4_bare/20261121-003259-118933.events.jsonl#L431) · [单次挖掘超时](../runs/r56_day1_day2_bare_5bot/hy4_hy_4_bare/20261121-003259-118933.events.jsonl#L432) · [终态](../runs/r56_day1_day2_bare_5bot/hy4_hy_4_bare/20261121-003259-118933.events.jsonl#L434) · [工具入口](../src/mc_agent/harness/mc_tools.py#L2683) · [默认预算](../src/mc_agent/skills/library.py#L3890)
- **影响**：**阻塞型工具调用不受 episode 预算约束**，单次可超出集时限 100s 以上；`move_to` 的 `busy` 预检对"僵尸租约"无效，33 次各白等 ~102s。
- **证据 1（unstick 超预算）**：`ds_ds_1_bare` 一次 `mc_skill_unstick` **404.4s**，结果 `错误：工具执行中止，未取得正常结果。`，随后 `run.finished 运行总时限已到（300.0s）` → **超出集预算 104s**。
- **证据 2（move_to busy 白等）**：`overnight_12bot_part2` 中 **33 次** `mc_move_to` 耗时 **102.2~103.1s**，全部返回
  ```
  {'code': 'busy', 'message': '动作锁仍被 其他动作 占用，move_to 未执行；请先脱困或等待 1~2 秒后重试'}
  ```
  涉及 `ds_3`、`glm_2`、`glm_5` 等多路。
- **源码定位**：
  - `mc_tools.py:1004-1024` 已有 `status` 预检（r12 修复），但当 `/status.busy` 为空、服务端租约仍被占时**预检通过**，随后 `_call_json("move_to")` 阻塞。
  - `bot.py:34 timeout: float = 45.0` + `bot.py:120 for attempt in range(2)` → 单次 `call` 最长 ~90s；`unstick`/`stop`/`status` 等控制类调用沿用该默认值。
  - episode 时限只在**调用之间**检查，工具内部阻塞期间不会被打断。
- **最小修复**：① 控制类工具（`unstick`/`stop`/`status`/`move_to` 预检）显式传 `timeout_s=min(15, remaining_budget)`；② `move_to` 遇到 `busy` 时先 `/stop` 再重试一次，仍 busy 立即返回（不要等 90s）；③ `unstick` 内部总时长上限 30s，超时即返回 `ok=False, requires_replan=True`。
- **验收标准**：单工具调用 p99 ≤ 60s；`unstick` 单次 ≤ 30s；`move_to` 返回 `busy` 的耗时 ≤ 20s；`elapsed_s` 不超过 `budget_timeout_s + 10`。
- **r29–r31 残余**：r29 HY4_5 EP05 的 actor `unstick` 在 45s 自报预算下阻塞 **622.934s**，未离开水中。r30 HY4_8 EP06 的 `move_to:busy` 仍达 **102.079s**。r31 的 `unstick` max **45.031s**，且 GUITest EP04 在集预算只剩约 21s 时进入同步采木，耗时 **80.558s**，整集超预算 **59.539s**。检查新版本是否把 actor 剩余预算贯穿采木的扫描和破块分支。 [r29 HY4_5 EP05](../runs/r29_newmix/hy4_hy_5_brewing_alchemy/20261119-210552-217591.events.jsonl#L110) · [r30 HY4_8 EP06](../runs/r30_short/hy4_hy_8_nether/20261119-214925-046294.events.jsonl#L190) · [r31 GUITest EP04](../runs/r31_short/nju_guitest_01_nether/20261119-221356-261049.events.jsonl#L747) · [采木入口](../src/mc_agent/harness/mc_tools.py#L2172) · [调度完成后检查集时限](../src/mc_agent/harness/agent.py#L1522)
- **r32_short 复核**：`unstick` max **45.031→28.036s**（r31→r32），该子门禁通过。GLM2 EP01 在 1200s 集预算还剩 **27.57s** 时启动 90s `mine_dfs(explore)`，实耗 **87.16s**，整集超出 **59.603s**。保持 OPEN，采矿入口同样需要贯穿外层剩余时间。 [GLM1 EP01 unstick](../runs/r32_short/glm_glm_1_nether_fortress/20261119-222148-738485.events.jsonl#L429) · [GLM2 EP01 最后调用](../runs/r32_short/glm_glm_2_nether_bastion/20261119-222150-480408.events.jsonl#L310) · [采矿入口](../src/mc_agent/harness/mc_tools.py#L2381)
- **r31_long/r33_short 复核**：脱困上限分别为 **12.102s、28.041s**，而全工具仍有采矿长尾。r31 MoveTest EP09 的最后一次 `mine_dfs(explore)` 耗时 **85.974s**，整集超出分配预算 **43.362s**。r33 NJU_3 EP04 在 540s 预算将尽时启动 85s 探矿，耗时 81.1s，整集超出 **54.930s**。`9ebf316` 的采矿 55s 和外层剩余预算钳制已合入，待加载它的新波次复验。 [r31 MoveTest EP09](../runs/r31_long/nju_movetest_01_bare/20261119-225100-136198.events.jsonl#L478) · [r33 NJU_3 EP04](../runs/r33_short/nju_nju_3_village_trade/20261119-224635-843085.events.jsonl#L267) · [钳制实现](../src/mc_agent/harness/mc_tools.py#L2502)
- **r34_short 复验**：已加载 `9ebf316`，工具 p99 **86.285→59.223s**、`unstick` max **29.396s**，两项达标。HY4_6 EP03 两次 `move_to:busy` 仍阻塞 **102.606s、约101.8s**，第二次在集预算只剩约 6s 时启动，最终超预算 **95.916s**。需验证动作租约预检与后端请求的短预算。 [HY4_6 首次 busy](../runs/r34_short/hy4_hy_6_nether_bastion/20261119-232351-607318.events.jsonl#L251) · [第二次与超时](../runs/r34_short/hy4_hy_6_nether_bastion/20261119-232351-607318.events.jsonl#L294) · [busy 预检](../src/mc_agent/harness/mc_tools.py#L1159)
- **r32_long/r35_short 复验**：新采矿钳制之外仍有独立长调用。r32 HY4_1 EP01 在 300s 预算下采石 14 个耗时 **74.430s**，全工具 p99 超限，整集超 **40.361s**。r35 HY4_10 EP09 约余 7s 时发起采木，耗时 **90.4s**，整集超 **83.504s**。r35 早于移动时限修复 `7f3da7c`。 [r32 采石](../runs/r32_long/hy4_hy_1_bare/20261119-230752-142336.events.jsonl#L208) · [r35 采木](../runs/r35_short/hy4_hy_10_enchant_anvil/20261119-234654-160737.events.jsonl#L69) · [采木／采石入口](../src/mc_agent/harness/mc_tools.py#L2333)
- **r36–r39 复验**：r36/r37 多个直接移动在约 **25.1s** 受控停止，分段移动仍出现 **85.900s／68.483s** 长尾。分段路径没有共享外层截止。r38/r39 工具 p99 **52.164s／52.129s**，r38 整集最大超 **2.097s**，r39 仍有一次 `mc_attack` 在只余约 0.2s 时启动并运行 **13.3s**，整集超 **13.057s**。最近两短波无 `busy` 回执，20s 该项待专门暴露。 [短移动止损](../runs/r36_short/hy4_hy_10_enchant_anvil/20261120-000316-533427.events.jsonl#L197) · [r36 分段长尾](../runs/r36_short/hy4_hy_6_nether_bastion/20261120-000416-692883.events.jsonl#L40) · [r39 攻击超时](../runs/r39_short/hy4_hy_10_enchant_anvil/20261120-011658-949353.events.jsonl#L147) · [分段调用](../src/mc_agent/harness/mc_tools.py#L1298)
- **r40 复验**：工具 p99 **51.450s**，脱困最大 **15.113s**，两项通过。HY4_3 EP07 在 540s 集预算只剩 **3.579s** 时启动 `mc_skill_mine_blocks`，运行 **59.979s**，整集超 **56.406s**。同集此前 `mine_dfs` 已收缩到约 1s。采块入口仍使用独立时限，需接入整集截止。移动 `busy` 仍缺暴露。 [采块开始](../runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-052644-245586.events.jsonl#L359) · [整集收尾](../runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-052644-245586.events.jsonl#L362) · [采块入口](../src/mc_agent/harness/mc_tools.py#L2362) · [独立时限](../src/mc_agent/skills/library.py#L2580)
- **r41 复验**：工具 p99 **90.071s**，23 个超过 60s 的配对调用中，**20** 个是采木。HY4_9 EP04 回到地表后，集预算仅余 **4.829s** 时启动熔炼，执行 **94.349s**，整集超 **89.529s**。入口沿用熔炼默认 95s 等待时限，继续跟踪，将采木和熔炼都接入整集截止。 [采木 p99 样本](../runs/r41_day1_15bot/hy4_hy_8_bare/20261120-060119-277958.events.jsonl#L133) · [HY4_9 启动熔炼](../runs/r41_day1_15bot/hy4_hy_9_bare/20261120-060915-293692.events.jsonl#L309) · [熔炼入口](../src/mc_agent/harness/mc_tools.py#L2371) · [默认等待时限](../src/mc_agent/skills/library.py#L4247)
- **r43 实测仍有超集时限**：Day 1/2 工具 p99 **51.305/51.889s**，脱困最大 **11.883/15.301s**。Day 2 HY4_9 EP04 在 **8.927s** 余量下启动正北六步上楼梯，单次 **27.269s**，整集超 **18.287s**。actor 的 `mc_skill_staircase` 没有把本集截止传到已有 `dig_staircase(deadline=...)`。最小改动是正向、pillar 和自动重试共享同一个集截止，验收覆盖本次 8.9s 余量。另有 DS_1 EP03 矿道封堵式撤退 **79.655s**，走 BUG-072。移动 `busy` 仍无样本。 [楼梯启动](../runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-073353-791901.events.jsonl#L306) · [集尾](../runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-073353-791901.events.jsonl#L309) · [actor 楼梯入口](../src/mc_agent/harness/mc_tools.py#L2604) · [已有技能截止](../src/mc_agent/skills/library.py#L6554)
- **r44 Day 1 再现楼梯越界**：GuiTest EP05 在 `budget_timeout_s=310.300`、只剩 **2.753s** 时启动 `steps=50`，已钳为 10 步但实际运行 **36.090s**。集尾 **343.643s**、超 **33.343s**，与 r43 近村为同一外层截止传递缺口。 [楼梯开始](../runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.events.jsonl#L450) · [集尾](../runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.events.jsonl#L453) · [集预算原件](../runs/r44_day1_5bot/nju_guitest_01_bare/20261120-075635-392125.json)
- **r44 Day 2**：工具 p99 **51.588s**、脱困 max **28.037s**。HY4_10 EP03 在 540s 集限剩 **8.222s** 时启动矿道撤退，实际 **18.929s**，整集超 **10.713s**。楼梯 `76020c6` 和撤退 `fa4cd15`、`cfde702` 均在 r44 进程启动之后合入。 [撤退起点](../runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074538-967229.events.jsonl#L289) · [集尾](../runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074538-967229.events.jsonl#L292)
- **r46 终版**：Day 1／2 p99 **51.911／51.554s**，整集最大超额 **3.572／0.229s**，通用门槛通过。末 10s 启动楼梯和移动 `busy` 缺样本，保持待验。 [Day 1 最大值](../runs/r46_day1_5bot/nju_movetest_01_bare/20261120-090001-392391.events.jsonl#L226) · [Day 2 最大值](../runs/r46_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-092014-538120.events.jsonl#L10)
- **r47 终版**：Day 1/2 工具 p99 **51.435/51.342s**，脱困最大 **7.914/9.884s**，集超预算最大 **0.032/0.145s**。三次在最后 10s 发起楼梯均在预算内返回，最大超额 **−2.510/−5.910s**。移动 `busy` 没有暴露，整票继续 OPEN。 [MoveTest 集尾](../runs/r47_day1_5bot/nju_movetest_01_bare/20261120-101654-485630.events.jsonl#L130) · [HY4_6 集尾](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-100313-044535.events.jsonl#L310)
- **r48→r49**：r48 附魔、下界分别超集预算 **10.967／10.293s**。HY4_3 EP07 仅余约 13.1s 仍启动 24.098s 的 `mc_dig`；HY4_6 EP04 余约 11.7s 发起约 22s 的金靴合成。r49 裸装/要塞门禁最大 **2.105／−0.001s**，但这四波均早于 `b926a47` 的 actor 挖掘和制作预算修复。待同路径加载复验，`move_to:busy` 仍无暴露。 [挖掘](../runs/r48_enchant_5bot/hy4_hy_3_enchant_anvil/20261120-103730-952225.events.jsonl#L416) · [合成](../runs/r48_nether_fortress_9bot/hy4_hy_6_nether_fortress/20261120-103850-267446.events.jsonl#L98)
- **r50 加载 `b926a47`**：同九槽下界集尾超额 **10.293→0.065s**，工具 p99 **51.452s**、脱困最大 **22.817s**；末地集尾 **0.112s**。下界配对 47 次挖掘、43 次制作，最小开工余量 **80.518／167.610s**，r48 的余十余秒长操作条件没有重现。`move_to:busy` 仍待验。 [下界集尾](../runs/r50_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-205702-433214.events.jsonl#L85) · [末地集尾](../runs/r50_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-205013-814729.events.jsonl#L916)

- **r57 裸装及 r58 上线**：同四槽集尾超额 **14.600→0.091s**。r57 最接近集尾的 `mine_blocks` 仍有 **96.992s**，另有阶梯在约余 1.57s 时直接拒绝施工。r58 日出和下界实机指纹 `815fe6b7f2bca974=fb73751`，含 `f645655` 的采块余量钳制；D1 离线重跑 3 条通过。保留 OPEN，待 r58 实测末秒采块及 busy。 [r57 最近采块](../runs/r57_day1_day2_fast_4bot/hy4_hy_3_bare/20261121-005211-755066.events.jsonl#L264) · [阶梯拒绝](../runs/r57_day1_day2_fast_4bot/hy4_hy_10_bare/20261121-010436-698952.events.jsonl#L341) · [r58 加载版](../runs/r58_day12_sunrise_village_4bot/nju_ds_2_bare/20261121-012426-316026.events.jsonl#L3) · [预算入口](../src/mc_agent/harness/mc_tools.py#L2683) · [夹具](../tests/test_skills_and_providers.py#L564)

- **r59 日出新版首次压缩采块**：DS_2 EP01 开工只余 **29.746s**，采块实耗 **24.778s**、入包 1 圆石，返回时仍余 **4.968s**。加载版在采块入口保留 4s，实际短于默认 45s 的预算已暴露。r56 的余 0.904s 拒绝路径及移动 busy 仍缺实机。 [开工余量](../runs/r59_day12_sunrise_village_4bot/nju_ds_2_bare/20261121-015914-761595.events.jsonl#L322) · [采块结果](../runs/r59_day12_sunrise_village_4bot/nju_ds_2_bare/20261121-015914-761595.events.jsonl#L323) · [入口钳制](../src/mc_agent/harness/mc_tools.py#L2687)

- **r66 HY4_1 末秒隧道**：`r66_expl_mem_hy1` EP06 集预算约 73.6s，`mc_skill_tunnel` 在集内 67.495s 启动，剩约 6.0s。工具自身耗 **39.394s**，回 `time_slice`、6 步，集尾 **106.895s**，门禁超 **33.295s**。actor 入口只限步数，库包装未传可选 `deadline`，底层因此使用自身 35s 期限。为 actor 传集的绝对截止，单次调用也按余量约束，接近截止时核对 6s 的夹具与整集最多超 10s。 [启动](../runs/r66_expl_mem_hy1/hy4_hy_1_iron/20261121-065115-374234.events.jsonl#L167) · [工具完成](../runs/r66_expl_mem_hy1/hy4_hy_1_iron/20261121-065115-374234.events.jsonl#L168) · [集尾](../runs/r66_expl_mem_hy1/hy4_hy_1_iron/20261121-065115-374234.events.jsonl#L170) · [入口](../src/mc_agent/harness/mc_tools.py#L2945) · [底层期限](../src/mc_agent/skills/tunnel.py#L35)

### BUG-048 | P1 | Integrator（#1，`harness/geofence.py` + 工具层）| 状态: OPEN（过夜两波实测）

- **r68 中期 NJU_3**：主世界传送门课 EP07／08 近同一岛边站位，累计 **25 条围栏拒绝**（14／11），全槽 **30 条**。两集朝内楼梯还有 10 次 `fluid_or_bedrock`，EP07 能采 10 圆石但外侧搭桥仍被拒，预准备一直差 6 黑曜石与打火石。围栏约束有效，失败反复进入模型，仍缺可达的向内撤离路线。 [首次拒绝](../runs/r68_midgame_portal_3bot/nju_nju_3_midgame/20261121-075627-454701.events.jsonl#L132) · [内侧阻塞](../runs/r68_midgame_portal_3bot/nju_nju_3_midgame/20261121-075627-454701.events.jsonl#L783) · [EP08 末条](../runs/r68_midgame_portal_3bot/nju_nju_3_midgame/20261121-080439-892102.events.jsonl#L903)
- **影响**：`void_geofence_blocked` **不触发**"重复失败保护"，因为模型每次换一个略有差异的坐标 → 单 bot 连续被拦 **129~154 次**，每次都是一轮完整 LLM 往返。
- **实测（两波）**：
  | 波次 | bot | 被拦次数 | 不同坐标数 | 距离范围 | safe_radius |
  |---|---|---|---|---|---|
  | `overnight_12bot` | `nju_guitest_01_nether` | **154** | **118** | 554.5m ~ **12927.4m** | 212.0m |
  | `overnight_12bot_part2` | `glm_4_brewing_alchemy` | **129** | **110** | 212.2m ~ 254.4m | 212.0m |
  | `overnight_12bot_part2` | 其余 5 路 | 17 | — | — | — |
  | `hy4_audit_r28` | `hy4_9_deep_water` | 5 | — | — | — |
  | `hy4_audit_r27` | `hy4_hy_2_iron` | **126，集中在 EP02–12** | — | — | — |
- **两种形态**：
  1. **"差一点"型（`glm_4`）**：目标 212.2~254.4m，仅超出 0~42m —— 意图合理（去挖看见的矿），但围栏只拒绝不引导；`glm_4` 本波 **145 分（-50 vs wave1）**，129 次被拦 ≈ 8~12 分钟。
  2. **"离谱坐标"型（`nju_guitest`）**：目标 554m~**12.9km**（最远 12927m），同一坐标重复 14 次 —— 明显是幻觉坐标/未换算的绝对坐标，工具层未做"距离明显异常"识别。
- **源码定位**：`geofence.py:88-106 check_point` 已返回 `distance` / `safe_radius` / `center` / 中文提示，但：① 只**拒绝**不**钳制**；② 无按 target 的累计计数（`repeated_identical_failure_blocked` 按"完全相同的参数"匹配，坐标差 0.5 格就绕过了）。
- **最小修复**：
  1. `distance <= safe_radius * 1.25` 时**自动钳制**到安全边界内（把目标投影到半径 212m 上）并继续执行；
  2. 距离 > `safe_radius * 3` 时直接返回 `requires_replan=True` + "坐标疑似错误，请用 mc_status 的 pos 重新定位"；
  3. 每 episode 对 `void_geofence_blocked` 累计 ≥3 次 → 强制 `requires_replan=True` 并给该 target 加 120s 冷却。
- **验收标准**：单 bot 单波 `void_geofence_blocked` ≤5；`glm_4` 类场景分数回到 ≥195。

### BUG-049 | **P0（规划锁死）** | Integrator（#1，`harness/curriculum.py`）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **影响**：Bot 身处下界（`the_nether`）死亡丢镐后，规划器不看当前维度，把主世界 `bootstrap_stone_age` / `bootstrap_iron_age` 锁死在最高优先级，导致整局后半程几乎 100% 空转。
- **轨迹实据**：`overnight_12bot_part2/glm_glm_1_nether_fortress`（80 集）中 **`bootstrap_stone_age` 0/52 + `bootstrap_iron_age` 0/23 = 75/80 集（94% 集数全废）**；下界不存在主世界橡木、圆石与铁矿石。
- **源码定位**：`harness/curriculum.py` 选目标逻辑未按 `cur_dim == "the_nether"` 过滤主世界 `bootstrap_*` 剧本。
- **最小修复**：当 `cur_dim == "the_nether"` 时，禁止派发主世界 `bootstrap_stone_age` / `bootstrap_iron_age`（或仅在存在黑石/下界木时走下界自救分支，或先回主世界门）。
- **验收标准**：下界场景（`nether_fortress` / `nether_bastion`）4 小时长测中主世界 `bootstrap_stone/iron` 派发次数为 `0`。
- **r48 维度切换样本**：HY4_9 EP01 以 `not_connected` 结束，runner 捕捉 `wrong_dimension(overworld)` 并开始拉回下界。EP02 在这个时段选中 `bootstrap_stone_age`，预准备随后因目标区块未加载而终止，0 actor 请求。终版共 1 次对应派发，证据指向回程维度交接窗口；主世界 bootstrap 直接在稳定下界重复派发未观察到，4h/丢镐门禁仍待实测。 [主世界拉回](../runs/r48_nether_fortress_9bot/hy4_hy_9_nether_fortress/stdout.log#L49) · [派发与失败](../runs/r48_nether_fortress_9bot/hy4_hy_9_nether_fortress/20261120-103735-195754.events.jsonl#L1)
- **r54 维度交接复现**：HY4_8 EP04 岩浆死亡后回主世界，日志有 `wrong_dimension(overworld)` 拉回。EP05 仍派 Day 1 石器，预准备报 `not_ready`，14 次生成全部被下界岩浆逃生取消。终版背包验证为真、加分 0；属于已有装备验收。模型开始执行时任务已过时，日志缺选中当刻的维度，故无法确认稳定下界错误派单。现有选择逻辑只在主世界或状态缺省时启用该目标；先把恢复后的维度与装备快照用于派单，再按原标准验收。 [死亡与重生](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-233420-211102.events.jsonl#L738) · [维度拉回](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/stdout.log#L1027) · [派单原件](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-234117-609756.events.jsonl#L3) · [终版验证](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/wild-endurance-r54_nether_fortress_6bot_hy_8_nether_fortress-20261120-231214.json#L229) · [石器目标判定](../src/mc_agent/harness/curriculum.py#L1381)

- **r58 运行中复现维度交接**：HY4_8 EP01 过程中捕捉主世界重生，集尾恢复入下界。EP02 仍派 `bootstrap_stone_age`，第一批护航动作在下界反复救岩浆，集尾验证为真、加分 0。源码候选仍要求 `cur_dim == "overworld"`，选中当刻的状态未留在回执，归入维度交接证据。 [拉回下界](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/stdout.log#L207) · [EP02 实际派单](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-012759-392518.events.jsonl#L1) · [下界脱岩浆失败](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-012759-392518.events.jsonl#L85) · [选择器维度条件](../src/mc_agent/harness/curriculum.py#L1449)

- **r58 终版两次交接**：另有 HY4_8 EP06，前一集已验证烈焰棒，但死亡回主世界，又在下界跑了石器，预准备被反射抢占。再次恢复物资后按已有镐验收，0 增分。r57 下界石器派发 1 次、验证 0，r58 两次均被验收，均加 0。 [EP05 死亡](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-013745-338680.events.jsonl#L402) · [EP06 派单](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-014120-043634.events.jsonl#L1) · [EP06 终版](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/wild-endurance-r58_nether_fortress_5bot_hy_8_nether_fortress-20261121-012119.json#L255)

- **r59 终版一处交接**：同 HY4_8 EP01 已验证要塞，后死亡回主世界。恢复入下界后仍派 EP02 石器，14 次 actor 请求消耗在反复液态岩浆救援，`limit_reached` 且验证为假。 [死亡后拉回](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/stdout.log#L27) · [派单](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-015400-066825.events.jsonl#L1) · [集尾](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-015400-066825.events.jsonl#L142)

- **r61 交接再现**：HY4_8 EP03／05／07 与 HY4_9 EP06 共 3 次石器、1 次铁器，另有 HY4_8 EP06 侦察。两条石器／铁器机验为真、加分皆 0。HY4_8 反复有主世界重生，恢复阶段有解码错误、60s 子进程超时，也有成功恢复。选中时的状态未写入日志，待同一 trace 关联派单与实际维度。 [石器任务](../runs/r61_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-031624-901704.events.jsonl#L1) · [恢复超时](../runs/r61_nether_fortress_5bot/hy4_hy_8_nether_fortress/stdout.log#L761) · [终版课程](../runs/r61_nether_fortress_5bot/hy4_hy_8_nether_fortress/wild-endurance-r61_nether_fortress_5bot_hy_8_nether_fortress-20261121-025615.json#L82)

- **r63 下界维度交接复验**：HY4_8 EP05 的 `bootstrap_stone_age` 预准备被岩浆救援抢占，终版 `objective_verified=true`、0 actor、0 增分。中断回执里的「5 原木、1 石镐」来自原备料请求。紧邻的 EP04 末段空包，EP06 首次 Guardian 库存已持钻石镐并有整套下界补给。源码刷新库存后按镐阶短路，离线回放证明空包加旧进度不能验收，钻石镐可以。现有证据支持场景补装造成的零操作镐阶验收，派单维度和 EP05 集尾实际镐种缺快照。复验时记录派单维度和集尾镐阶，并为场景补装标记来源。 [EP05 中断](../runs/r63_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-050403-056945.events.jsonl#L5) · [EP06 补给快照](../runs/r63_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-050423-644174.events.jsonl#L8) · [缺口回执](../src/mc_agent/harness/curriculum.py#L2318) · [刷新库存并短路](../src/mc_agent/harness/curriculum.py#L2365)

- **r64 同型复现**：HY4_9 EP05 的石器预准备被岩浆抢占，0 次 actor、0 分、机器验证为真；下一集第一份 Guardian 库存已持钻石镐。派单当时的维度仍缺直接记录。 [EP05 中断](../runs/r64_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-053858-337030.events.jsonl#L5) · [EP05 终版](../runs/r64_nether_fortress_5bot/hy4_hy_9_nether_fortress/wild-endurance-r64_nether_fortress_5bot_hy_9_nether_fortress-20261121-052442.json#L224) · [EP06 钻石镐](../runs/r64_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-053919-147104.events.jsonl#L8)

### BUG-050 | **P0（后 3h 停滞主因）** | Integrator（#1，`harness/curriculum.py`）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **影响**：缺少硬前置道具/群系门控 + 固定 20 分钟冷却（`cooldown_s = 1200s`）导致 4 小时内冷却反复到期（最多 12 次），波及全部 16 路长程，吃掉后半程 50%~75% 集数。
- **轨迹实据**：
  - 无鞍却派 `nether/ride_strider`：`ds_4` **0/13**、`glm_2` **0/6**、`hy4_6` **0/4**；
  - 无钻石镐却派 `obtain_ancient_debris` / `obtain_crying_obsidian`：`ds_4` **0/12 + 0/10**、`nju_guitest` EP03；
  - 无苍白花园/深暗之域/村庄/烈焰棒却反复强派：`glm_5`（手握 10 颗钻石+钻镐钻剑）被派 `lava_bucket` **0/14**、`make_a_sign_glow` **0/8**、`heart_transplanter` **0/7**；`hy4_9` `kill_mob_near_sculk_catalyst` **0/4** + `heart_transplanter` **0/4**；`glm_4`/`hy4_5` `cure_zombie_villager` 0/3~0/4、`follow_ender_eye` 0/3；
  - 模型在 `final_answer` 中已多次明确输出“已扫描 8k~32k 格确认本岛无苍白橡木/无村民/无鞍，不可达，建议切换”，Harness 完全未感知。
- **最小修复**：
  1. 硬前置门控：`ride_strider` 需背包有 `saddle`；`obtain_ancient_debris` / `obtain_crying_obsidian` 需有 `diamond_pickaxe`/`netherite_pickaxe`；`follow_ender_eye` 需有 `ender_eye` 或（`blaze_rod`/`blaze_powder` + `ender_pearl`）；`heart_transplanter` / `kill_mob_near_sculk_catalyst` / `cure_zombie_villager` 未在 WorldMemory 观测到对应结构/实体前禁止盲派；
  2. 冷却改为指数退避：第 1 次失败 20m → 第 2 次 60m → 第 3 次 ≥3h（10800s），若模型明确回报环境阻断/无目标则直接跳级至长冷却。
- **验收标准**：4 小时长测中单个无进展非主线目标重复派发次数 ≤3；后 2 小时不再出现 `0/14`、`0/8`、`0/7` 式空转。
- **D1 全量 ROI 复核（6 波 1,581 集，逐 target 统计）**：**18 个 target 成功率 0%，合计 494 集（31.2% 集数）/ 8,078 请求 / 0 分**：
  | target | 集数 | 请求 |
  |---|---|---|
  | `husbandry/make_a_sign_glow` | **116** | 2,402 |
  | `nether/ride_strider` | 57 | 1,581 |
  | `husbandry/breed_an_animal` | 51 | 665 |
  | `adventure/heart_transplanter` | 47 | 448 |
  | `nether/obtain_crying_obsidian` | 34 | 826 |
  | `island_recon_sweep` | 30 | 229 |
  | `adventure/kill_mob_near_sculk_catalyst` | 28 | 270 |
  | `nether/distract_piglin` | 25 | 344 |
  | `husbandry/tame_an_animal` / `fishy_business` | 18 / 18 | 149 / 233 |
  | `husbandry/allay_deliver_item_to_player` | 17 | 116 |
  | `nether/get_wither_skull` | 10 | 409 |
  | 其余 6 个（sniffer_egg / cure_zombie_villager / ride_a_boat_with_a_goat / trade_at_world_height / silk_touch_nest / find_bastion） | 53 | 406 |
- **ROI 排名（分/100 请求）**：`brew_potion` **87.2**（78% 成功）、`use_lodestone` **74.9**、`shiny_gear` **61.0**（1,245 分，占全部得分 61%）、`kill_a_mob` 46.2；而 `obtain_ancient_debris` 仅 **11.6**（91 集 / 3,237 请求 / 375 分）、`find_fortress` **5.9**。
  → **`shiny_gear` + `brew_potion` + `use_lodestone` 三项贡献了绝大部分分数，而 18 个 0% 目标吃掉了 1/3 集数**。
- **D1 离线复现（r48 实际版本 `198c32a`）**：缺少其他可派候选时，选择器移除最早到期的冷却再递归。下界有钻镐、已完成 `shiny_gear`、只剩堡垒目标的夹具中，`now=1` 仍会派发冷却截止 1200 或 10800 的 `find_fortress`。显式排除该目标后，现有空闲探索回退正常选中。应在无可用目标时保留未到期冷却，并在阶段有可做任务时继续派发已就绪目标。实机冷却被提前解除须同时核对累计冷却和再次派发两个事件。 [到期判断](../src/mc_agent/harness/curriculum.py#L1343) · [回退移除冷却](../src/mc_agent/harness/curriculum.py#L1638) · [指数写入](../src/mc_agent/harness/curriculum.py#L2538) · [离线夹具](../../s-triage/tests/test_d1_repro.py#L165)
- **r49 SCENE-003 另一路冷却绕过**：HY4_1 EP02 的终版记录 `cooled_down=true`，仅余 133s 的 EP03 重新派同一 `follow_ender_eye`。场景覆盖选择器直接返回首个未完成成就，未走常规候选和冷却判断；这是常规无候选回退夹具之外的另一分支。 [EP02 冷却／EP03 派发](../runs/r49_stronghold_end_5bot/hy4_hy_1_village_to_stronghold_end/curriculum-20261120-104503-004529.json#L57) · [场景覆盖](../scripts/l3_wild_endurance_runner.py#L1866)

- **r60 维度前置仍缺**：HY4_8 EP08 在下界实际接到获取三叉戟、找溺尸的主世界目标，返回空扫描。通用候选池仍为非下界目标保留 tier 3，只降优先级。末集只分到约 9.6s，样本证明实际派单，不足以推算长期损失。 [任务](../runs/r60_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-025429-850084.events.jsonl#L1) · [实际下界状态](../runs/r60_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-025429-850084.events.jsonl#L33) · [通用维度降权](../src/mc_agent/harness/curriculum.py#L1678)

### BUG-051 | P1 | Integrator（#1，`harness/curriculum.py`）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **影响**：规划器高度/位置失明——Bot 挖矿留在地下 `y < 45` 时，连续派发 8 轮短预算的地表任务，导致每集全用来爬楼梯、连地表都见不到就超时。
- **轨迹实据**：`overnight_12bot/glm_glm_4_brewing_alchemy` EP08 挖钻后停在 `y=23`，EP15~EP30 连续 17 集被派 `sleep_in_bed` / `breed_an_animal` / `plant_seed`（8 轮预算），EP19 光从 `y=23` 爬到 `y=29` 就耗光全集；`hy4_2` 在 `y=38` 被派 `make_a_sign_glow`。
- **最小修复**：选定地表专属目标（`plant_seed`、`breed_an_animal`、`sleep_in_bed`、`tame_an_animal`、`trade`、`island_recon_sweep`）前检查 `cur_pos[1] < 50 and cur_dim == "overworld"`，先执行独立返地表步（或 `mine_dfs(climb_out)`）再开地表集。
- **验收标准**：`y < 45` 状态下不再直接开启 8 轮地表短任务；地表任务开局时 Bot 位于地表或已带足够爬升前置。

### BUG-052 | P1 | Slice C（#4）+ Integrator（#1）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **影响**：地底“缺 1 块木板/工作台”或“36 格杂物满包”导致高进展 Bot 硬卡 5~9 集（30~60 分钟）。
- **轨迹实据**：
  - `ds_ds_1_bare` EP25~33 手握 192 圆石 + 3 木板 + 4 木棍，只差 1 块木板合工作台，在 `y=40` 徒手挖石（25s/格）硬爬 9 集；
  - `nju_nju_3_village_trade` EP11 已挖齐 4 颗钻石，却因缺 3 块木板卡在 `y=6` 合不出钻石靴；
  - `glm_glm_4_brewing_alchemy` EP43~45 已挖 13 粗铁、烧出 4 铁锭，却因 36 格塞满杂物导致 `craft_gear` 连续报 `inventory_full`，模型只能逐格放圆石腾位，连卡 5 集。
- **最小修复**：
  1. `curriculum.py` 下矿（`shiny_gear`/`mine_diamond`/`bootstrap_iron_age`）前 `pre_ensure` 硬保底：`crafting_table >= 1` 且 `any_log >= 4`（或等效木板 ≥16）；
  2. `library.py:craft_gear` 遇到 `inventory_full` 时自动按优先级丢弃 1~2 格低价值杂物（`rotten_flesh`、`seeds`、`granite`、`diorite`、`andesite`、`tuff`、`dirt`、超量 `cobblestone`）腾出空位后再合成。
- **验收标准**：4 小时长测中 `craft_gear:inventory_full` 归零；不再出现“已有 4 钻石/4 铁锭却因缺工作台或满包卡 ≥2 集”。

### BUG-053 | P1 | Integrator（#1）+ Slice D2（#6）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **r77 门户确认一次入下界**：HY4_8 EP04 先靠近主世界黑曜石区，随后收到重生位置，护航状态明确为下界，课目标 +30 已验证。过程未调用建门，缺明确切维度的移动回执，现成入口的具体跨越动作待核。六次过门派发中一条验证。 [主世界移动](../runs/r77_midgame_portal_40m/hy4_hy_8_midgame/20261121-134543-530372.events.jsonl#L183) · [下界状态](../runs/r77_midgame_portal_40m/hy4_hy_8_midgame/20261121-134543-530372.events.jsonl#L197) · [验证](../runs/r77_midgame_portal_40m/hy4_hy_8_midgame/20261121-134543-530372.events.jsonl#L221)
- **r75 门户见既有门复用工具回执**：HY4_8 EP06 工具发现并复用已点亮门，靠近超时、仍在主世界。EP07 随后获得 +30 过门验证，末次坐标来源为重生回执，终局下界；缺明确的移动切维度回执。NJU_3 EP04 仍缺六黑曜石，34 次探井配对 **1593.320s**，没有黑曜石入包，1800s 集限。 [复用尝试](../runs/r75_midgame_portal_40m/hy4_hy_8_midgame/20261121-122700-174958.events.jsonl#L829) · [EP07 重生与验证](../runs/r75_midgame_portal_40m/hy4_hy_8_midgame/20261121-123552-409796.events.jsonl#L661) · [NJU_3 缺料门](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/20261121-122230-392784.events.jsonl#L285) · [工具入口](../src/mc_agent/harness/mc_tools.py#L2300)

- **r74 门户再次真实穿门**：HY4_8 EP05 缺六黑曜石，`move_to` 明示 `dimension_changed`，随后进度验证 +30 分，终局 `the_nether`。没有建门或点火调用，符合利用既有门。五次过门派发只有这一活体验证，另两个角色仍在主世界。 [缺料](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/20261121-114024-897912.events.jsonl#L2) · [跨维度](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/20261121-114024-897912.events.jsonl#L187) · [验证](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/20261121-114024-897912.events.jsonl#L223)

- **r73 门户同三角色实机跨维度 0→1**：HY4_8 EP04 开集仍缺六黑曜石，之后移动到 `[7795.5,67,2806.5]`，相邻挖掘被抢占后实坐标 `[971.5,56,350.5]`，集尾 `goal_satisfied`、进度与 +30 分均确认，整槽终版 `the_nether`。操作序列没有放置、点火或建门调用，符合走入既有门。另两槽仍在主世界，4 小时长程门待验。 [缺料](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/20261121-105733-217864.events.jsonl#L2) · [最后主世界移动](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/20261121-105733-217864.events.jsonl#L348) · [位置突变](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/20261121-105733-217864.events.jsonl#L370) · [整槽维度](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/wild-endurance-r73_midgame_portal_40m_hy_8_midgame-20261121-105005.json#L43) · [课程验证入口](../src/mc_agent/harness/curriculum.py#L2500)

- **r71 同三门户角色**：派发 **7→6**，仍无建门与切维度。三个终包均有 4 黑曜石。NJU_3 EP04 预准备缺 6 黑曜石和打火石后，实际 34 次 DFS 探矿配对 **1540.933s**，新增 9 粗铁和 5 钻石，无黑曜石入包；目标集 1800s 超时。 [前置缺口](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2) · [后段探矿](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2448) · [课程材料门](../src/mc_agent/harness/curriculum.py#L746)

- **r70 门户 40 分钟三槽**：七条过门课实际建门和跨维度均为 0，三槽终版均在主世界。HY4_9 EP05 有一次明确的制作打火石入包，报告仍为四黑曜石。对照 r69 两个可比终版角色，过门派发 **4→5**、实际跨维度 **0→0**，运行时长 30→40 分钟。 [打火石合成](../runs/r70_midgame_portal_40m/hy4_hy_9_midgame/20261121-090257-037167.events.jsonl#L877) · [该集报告](../runs/r70_midgame_portal_40m/hy4_hy_9_midgame/20261121-090257-037167.json) · [课程前置](../src/mc_agent/harness/curriculum.py#L746)

- **r66→r67 中期同三槽**：加载 `32a6bff` 后，过门课 **0→5 集**、获入下界验证 **0→2 槽**。HY4_8 EP05 和 HY4_9 EP04 均 +30，watchdog 确认 `the_nether`。五条课无搭建／点火工具回执，HY4_9 后续看到已点亮的门并再次进入。NJU_3 两集预备阶段均缺 6 黑曜石与打火石、90 个 actor 请求仍未建门。三条后续堡垒课均未验，4 小时 250 分门槛仍缺。 [HY4_8 下界](../runs/r67_midgame_portal_3bot/hy4_hy_8_midgame/stdout.log#L650) · [HY4_9 下界](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/stdout.log#L223) · [NJU_3 缺料](../runs/r67_midgame_portal_3bot/nju_nju_3_midgame/20261121-072736-309950.events.jsonl#L2) · [课程提权](../src/mc_agent/harness/curriculum.py#L1657)
- **r67 找门前的空转**：HY4_8 EP04／05 与 HY4_9 EP04，12 次点挖超时合计 **361.765s**。每次所挖坐标均有此前黑曜石扫描，七条复合挖矿又耗约 **105.5s**，确认挖掘或入包均 0。现课程优先让缺料者凑黑曜石、后备材料具备才提示建门，任务文案缺先搜索可使用传送门的分支。复验应区分已有可用门与确需建门的落点。 [HY4_8 重试](../runs/r67_midgame_portal_3bot/hy4_hy_8_midgame/20261121-072013-481973.events.jsonl#L905) · [HY4_9 采块零产出](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-071710-307864.events.jsonl#L199) · [过门剧本](../src/mc_agent/harness/curriculum.py#L740)
- **r68 可对照两槽**：HY4_8／NJU_3 同 `midgame`、1800s，过门派发 **4→5**、真实切维度 **1→0**。HY4_8 两集六次实际黑曜石复合采块仍零入包，NJU_3 在岛边反复被围栏与流体阻隔，合计五条均缺建门点火。第三槽 HY4_9 启动崩溃，见 BUG-085。 [HY4_8 采块](../runs/r68_midgame_portal_3bot/hy4_hy_8_midgame/20261121-080403-617415.events.jsonl#L664) · [NJU_3 缺料](../runs/r68_midgame_portal_3bot/nju_nju_3_midgame/20261121-075627-454701.events.jsonl#L2)
- **r69 `midgame_real_drop` 三槽**：八次过门派发、零入下界，DS_1 EP04 和 HY4_1 EP06 各调一次建门工具，均因当前 **4 黑曜石、1 打火石** 被材料门立即拒绝。需要增加能取得余 6 黑曜石或找到可用门的路线证据。 [DS_1 建门材料门](../runs/r69_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-080259-101154.events.jsonl#L366) · [HY4_1 建门材料门](../runs/r69_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-080458-735914.events.jsonl#L988) · [工具校验](../src/mc_agent/harness/mc_tools.py#L2289)
- **影响**：开局现成场景资源未被优先收割，且预授成就与物理维度脱节，锁死主世界满装 Bot 的下界高分主线。
- **轨迹实据**：
  - `ds_ds_3_enchant_anvil` 出生在附魔台 + 书架 + 30 级经验平台，EP01 却先被派 `island_recon_sweep` 跑出平台掉水被溺尸击杀，30 级经验瞬间清零，后续 `enchant_item` 0/7；
  - `glm_glm_4_brewing_alchemy` / `hy4_hy_5_brewing_alchemy` 开局预授 `story/enter_the_nether`，但 Bot 站在主世界；1 小时穿满钻甲到 195 分后，规划器误以为去过下界，后 3 小时再也不派建门进下界（`195→195→195→195`）。
- **最小修复**：
  1. `enchant_anvil` 切片同 `brewing_alchemy`/`village_trade` 一样加开局专属目标（优先附魔，不先跑 `island_recon_sweep`）；
  2. 规划器判断“能否走下界高分线”看 `cur_dim == "the_nether"` 或已知传送门，而非仅看 `enter_the_nether` 成就位；主世界满钻装后若尚未在下界，主动派建门进下界。
- **验收标准**：`enchant_anvil` 开局 2 集内完成首次附魔；主世界满钻装 4h 长测能主动进入下界继续推进至 ≥250 分。
- **r48 开局分支实测**：附魔场景五槽 EP01 全部 `goal_satisfied`，工具核对钻石剑重新入包、实际附魔为锋利 I，等级 30→29。首集均无环岛派发。满钻装主世界主动进下界与 4h ≥250 分仍需同条件实机。 [HY4_1 附魔回执](../runs/r48_enchant_5bot/hy4_hy_1_enchant_anvil/20261120-102002-047133.events.jsonl#L24) · [MoveTest 附魔回执](../runs/r48_enchant_5bot/nju_movetest_01_enchant_anvil/20261120-101957-760568.events.jsonl#L47)
- **r52 同三槽**：HY4_1／2／3 均在 EP01 取回附魔钻石剑并验证，锋利 I 两把、亡灵杀手 I 一把。HY4_2 EP06 确认举盾弹反再得 15 分。同组三路从 **3→4** 集验证，7 集寻村均未验证。主世界无建门派单或跨维度；20 分钟波未覆盖满钻主动进下界与长测门槛。 [HY4_1 附魔](../runs/r52_enchant_3bot/hy4_hy_1_enchant_anvil/20261120-213215-641448.events.jsonl#L15) · [HY4_2 举盾](../runs/r52_enchant_3bot/hy4_hy_2_enchant_anvil/20261120-214606-252651.events.jsonl#L204) · [终版验证](../runs/r52_enchant_3bot/hy4_hy_2_enchant_anvil/wild-endurance-r52_enchant_3bot_hy_2_enchant_anvil-20261120-213205.json#L242)

### BUG-054 | P1 | Integrator（#1）+ Slice C（#4）| 状态: OPEN（16 路过夜轨迹全量复盘）
- **影响**：`husbandry/plant_seed` 与 `adventure/sleep_in_bed` 看似简单却长期接近 0% 成交，白烧大量集数。
- **轨迹实据**：
  - `plant_seed` 全场 16 路约 **0/39**（`glm_4` 0/6、`glm_5` 0/8、`glm_3` 0/8、`ds_2` 0/8、`hy4_2` 0/6、`hy4_9` 0/5、`hy4_5` 0/4）：预算仅 8 轮，打不到高差草丛拿种子、没来得及找水边耕地即结束；
  - `sleep_in_bed` 全场仅 **2/25**（`glm_5` 1/6、`glm_4` 0/6、`ds_2` 0/6、`nju_guitest` 0/6）：模型已成功绑定床位，但**白天点床只设重生点、不触发睡觉成就**，导致反复重派。
- **最小修复**：
  1. `sleep_in_bed` 增加时间门控：仅在傍晚/夜晚（`not is_day` 或 `time_of_day >= 12000`）派发，白天跳过；
  2. `plant_seed` 提高预算至 `medium`（14 轮）或仅在背包已有 `wheat_seeds`/`carrot`/`potato`/`beetroot_seeds` 或地表近距有 `short_grass`/`wheat` 时派发。
- **验收标准**：`sleep_in_bed` 白天派发次数为 `0`、夜间成功率 ≥60%；`plant_seed` 成交率从 0% 提升至 ≥50%。

### BUG-055 | P1 | Integrator（#1，`harness/curriculum.py`）| 状态: OPEN（16 路过夜轨迹全量复盘，关联 P2-36）
- **影响**：`mc_skill_staircase` 成功率仅 17.2% 且 `pillar_fallback` 返回 `ok=True, requires_replan=False` 形成同工具自锁（`nju_guitest_01` 29 连击白烧 188s，`part2` 累计白烧 723s），叠加 `bootstrap_stone_age` 反复回档，严重拖慢长程推进。
- **轨迹实据**：`overnight_12bot_part2` 1180 次 `staircase` 仅 203 次 `completed`（17.2%），`pillar_fallback` 140 次；`ds_1`/`nju_3` 各耗 23~24 集重打 `bootstrap_stone/iron`。
- **最小修复**：`pillar_fallback` 连续 ≥2 次返回 `requires_replan=True` 并建议水平换向/`unstick`；≥3 次给当前方向/target 加 120s 冷却（详见 `D1-r11-triage.md` 4bw 节）。
- **验收标准**：`pillar_fallback` 连续 ≥3 段降至 0；单次 `bootstrap_stone_age` 平均 ≤2 集完成。
- **r47 Day 2 已完成集复核**：`hy4_hy_4_iron` EP03 寻村时东向斥候原地 `stalled`，随后三次东向 `staircase(up)` 连续 `pillar_fallback`，均 `requires_replan=false`，XZ `(-7823.5,-2780.5)` 不变、Y127→130；下一次下楼梯因 `cave_or_drop_ahead` 拒绝。属于同一防自锁门禁，后续复验增加高柱起点。 [第一次](../runs/r47_day2_full_day_scout_9bot/hy4_hy_4_iron/20261120-100127-264626.events.jsonl#L80) · [第二次](../runs/r47_day2_full_day_scout_9bot/hy4_hy_4_iron/20261120-100127-264626.events.jsonl#L98) · [第三次](../runs/r47_day2_full_day_scout_9bot/hy4_hy_4_iron/20261120-100127-264626.events.jsonl#L119) · [下楼梯失败](../runs/r47_day2_full_day_scout_9bot/hy4_hy_4_iron/20261120-100127-264626.events.jsonl#L141)

- **r57 要塞起点复现**：MoveTest_01 EP01 在无法水平前进后连续 **33 次** `pillar_fallback`，均 `requires_replan=false`。从 Y141 垫到 Y175，XZ 只变约 1m，下一集垫到 Y205 后出现缺施工材料与落脚失败。全波 14 次斥候 0 位移、0 估点。 [首条垫高](../runs/r57_stronghold_end_3bot/nju_movetest_01_village_to_stronghold_end/20261121-003457-597348.events.jsonl#L281) · [第 33 条](../runs/r57_stronghold_end_3bot/nju_movetest_01_village_to_stronghold_end/20261121-003457-597348.events.jsonl#L1205) · [末包地形困顿](../runs/r57_stronghold_end_3bot/nju_movetest_01_village_to_stronghold_end/20261121-005057-518776.events.jsonl#L772) · [回执分支](../src/mc_agent/skills/library.py#L7568)
- **r59 冷却分支实测**：HY4_1 EP01 在连续垫柱后返回 pillar_fallback_cooldown、requires_replan=true。随后尝试三次水平移动和脱困，仍在近原点；又显式发起 vertical=pillar，稍后恢复自动垫柱。分支已触发，仍需终版比较原受影响 MoveTest 槽的重复峰值和离开卡点。 [冷却回执](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-015338-434449.events.jsonl#L336) · [显式垫柱](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-015338-434449.events.jsonl#L428) · [冷却实现](../src/mc_agent/skills/library.py#L7513)
- **r59 终版对照**：同名 MoveTest 的自动垫柱 **39→0**、移动斥候 **0/14→12/12**，出生点仍有约 8.06m 水平和 5 格高度变化。全波 16 次自动垫柱仍全在 HY4_1。按楼梯调用序列仍有一段 5 次；计入中间其他完成工具后最长 2。冷却能迫使换招，显式垫柱和换列仍可能延续卡点，保持 OPEN 观察同柱压力。 [冷却及换招](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-015338-434449.events.jsonl#L336) · [MoveTest 启动](../runs/r59_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/stdout.log#L2)

### BUG-056 | P0 | Integrator（#1，`harness/guardian.py` + `skills/library.py`）| 状态: FIXED（Guardian 浮水门禁 r29/r31 达标）

- **r82 门户实机见水域抢占与腾空交接**。HY4_9 EP07 的采矿和解卡各被 `water_surface` 抢占，均 `in_water=false, on_ground=false`，护航仍保留未确认。边界浮水之后另一次返岸回执 `already_dry`。本波指定槽 11 集有浮水、0 单请求超时，浮水两条峰 5.328s。 [采矿被抢占](../runs/r82_midgame_portal_30m/hy4_hy_9_midgame/20261121-205053-977883.events.jsonl#L125) · [解卡被抢占](../runs/r82_midgame_portal_30m/hy4_hy_9_midgame/20261121-205053-977883.events.jsonl#L161) · [随后干岸](../runs/r82_midgame_portal_30m/hy4_hy_9_midgame/20261121-205053-977883.events.jsonl#L190) · [空中交接](../src/mc_agent/harness/guardian.py#L4405)

- **r82 中期三次干岸复试，两次出水回执、一条仍湿**。HY4_2 EP04 首次仍湿，`shore_bank_retry` 复试 **2.658s** 后干；EP13 先报仍湿，复试 **1.672s** 返回 `shore_landing` 和 `in_water=false`；EP12 仍湿后下一轮成功落脚。浮水 12 条峰 4.054s。 [EP04 复试](../runs/r82_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-200011-245584.events.jsonl#L676) · [EP12 仍湿](../runs/r82_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-202544-451893.events.jsonl#L141) · [EP13 落脚](../runs/r82_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-202828-907501.events.jsonl#L409) · [复试分支](../src/mc_agent/skills/combat_env.py#L2560)

- **r81 门户登岸复核实机确认**。加载 `b39ac26` 的 HY4_8 在两个 episode 返回三条 `shore_landing_unconfirmed` 与两条 `water_escape_failed`。EP02 解卡拒绝确认后，另一次护航返岸记录 `shore_landing` 与 `in_water=false`。全波 `boundary_escape_water` 共 10 条，峰 **4.031s**。危险水域抢占采掘及指定槽浮水仍待同类样本。 [解卡拒绝](../runs/r81_midgame_portal_40m/hy4_hy_8_midgame/20261121-195158-480460.events.jsonl#L171) · [后续确认](../runs/r81_midgame_portal_40m/hy4_hy_8_midgame/20261121-195158-480460.events.jsonl#L264) · [落脚复核](../src/mc_agent/harness/guardian.py#L4168)

- **r73 日出实机干岸落脚**：HY4_4 承担 73 条浮水中的 72 条，波峰 **8.076s** 为 `network_uncertain/ReadTimeout`。同角色六条 `return_to_dry_bank` 已配对，EP02 **6.7741s** 确认离水，另五条仍湿。该波加载 `e7b9b74`，含岸沿落脚及浅岸复试补丁。浮水时长门通过，岸边重复失败继续跟踪。 [成功落脚](../runs/r73_day12_real_drop_60m/hy4_hy_4_bare/20261121-140357-991822.events.jsonl#L143) · [仍湿样本](../runs/r73_day12_real_drop_60m/hy4_hy_4_bare/20261121-142302-430745.events.jsonl#L318) · [浮水峰](../runs/r73_day12_real_drop_60m/hy4_hy_4_bare/20261121-142302-430745.events.jsonl#L356) · [落脚确认](../src/mc_agent/skills/combat_env.py#L2763)

- **r78 浮水有一条末秒右截尾**：HY4_2 EP13 在 +83.516s 开始，+87.137s 整集超时，无同调用结束回执，观察窗 **3.621s**。已配对的 15 条最大 **5.505s**，该波完整时长门保留待定。 [开始](../runs/r78_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-144032-418758.events.jsonl#L154) · [终态](../runs/r78_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-144032-418758.events.jsonl#L156)

- **r73 官方 MoveTest 单请求原始门有一条槽末预算样本**：EP26 实载 **1.651930s**，单请求，无工具或护航动作，**1.743s** 结束。机械计数 1 超出零门，轨迹定位为整槽截止削减最后一课预算。四条实际浮水的峰值 **2.834s**。 [起跑预算](../runs/official_r73_80m_movetest/MoveTest_01/20261121-140401-919828.events.jsonl#L1) · [终态](../runs/official_r73_80m_movetest/MoveTest_01/20261121-140401-919828.events.jsonl#L9) · [槽末预算](../src/mc_agent/harness/curriculum.py#L2529)

- **r69 官方波角色兼容**：D1 识别单跑目录 `MoveTest_01` 与 Fleet 的 `nju_movetest_…`。官方波已出现同名角色的 `boundary_escape_water` 回执，终版仍待。 [边界浮水回执](../runs/official_r69_40m_movetest/MoveTest_01/20261121-082139-424652.events.jsonl#L194) · [D1 门禁](../../s-triage/scripts/d1_ticket_gates.py#L485)
- **r70 近岸复试过程回执**：加载 `2b422eb` 的 DS_1 EP03，前一条移动未确认，`boundary_escape_to_shore` 走 `shore_bank_retry → move_to_shore → shore_landing`，配对耗时 **2.513s**，最终 `ok=true,in_water=false`。浮水三项仍用各自命名的事件作终版验收。 [动作起点](../runs/r70_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-082728-331195.events.jsonl#L280) · [干岸回执](../runs/r70_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-082728-331195.events.jsonl#L281) · [复试源码](../src/mc_agent/skills/combat_env.py#L2390)
- **r67 日出确认浮水耗时和出水结果**：53 条 `boundary_escape_water` 最大 **12.558s**，11 条确认出水、40 条仍湿、2 条未确认落脚。HY4_4 EP09 有八条浮水全仍湿、九条寻岸五次成功，actor 多次重新目标到同段水下。最新 `dc6674a` 的三条干岸回撤夹具通过，r68 日出与 r71 中期已加载 `494b133`，待真实 `return_to_dry_bank`。 [EP01 最慢干岸](../runs/r67_day12_real_drop_45m/hy4_hy_4_bare/20261121-081607-708375.events.jsonl#L117) · [EP09 水下动作](../runs/r67_day12_real_drop_45m/hy4_hy_4_bare/20261121-084604-411190.events.jsonl#L273) · [新夹具](../tests/test_generation_guard.py#L8496)
- **r70 门户目标角色门通过**：HY4_9 的 10 集包含实际浮水，没有单请求超时。整波 17 次浮水最大 **4.554s**。HY4_8 EP05 两条已带 `shore_bank_retry`，分别耗 **5.535s／6.312s**，两次仍湿。等待加载后续干岸和浅滩补丁的同类回执。 [复试一](../runs/r70_midgame_portal_40m/hy4_hy_8_midgame/20261121-090337-616529.events.jsonl#L144) · [复试二](../runs/r70_midgame_portal_40m/hy4_hy_8_midgame/20261121-090337-616529.events.jsonl#L253) · [浮水上限样本](../runs/r70_midgame_portal_40m/hy4_hy_8_midgame/20261121-090705-517663.events.jsonl#L260)
- **r70 裸装近岸复试成功**：HY4_5 EP08 浮水 **4.030s** 后仍湿，稍后找岸复查以 **1.575s** 确认出水。r71 门户同三角色浮水 **17→8**、最大 **4.554→4.030s**，HY4_9 目标角色门通过，仍无 `return_to_dry_bank` 回执。 [裸装成功](../runs/r70_bare_real_drop_60m/hy4_hy_5_bare/20261121-093442-038671.events.jsonl#L65) · [r71 最大浮水](../runs/r71_midgame_portal_40m/hy4_hy_9_midgame/20261121-093241-377066.events.jsonl#L327) · [复试实现](../src/mc_agent/skills/combat_env.py#L2390)
- **r72 中期干岸回撤首次确认**：HY4_2 EP05 加载 `0d8d3da`，浮水 **4.027s** 尚湿，后续寻岸 **3.848s** 实际走入 `return_to_dry_bank`。寻路未确认后跳上岸沿，最终 `shore_landing`，`ok=true,in_water=false`。 [浮水](../runs/r72_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-095717-213409.events.jsonl#L274) · [干岸落脚](../runs/r72_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-095717-213409.events.jsonl#L276) · [缓存干岸判定](../src/mc_agent/skills/combat_env.py#L2463)
- **r73 中期新版两次干岸回撤未成功**：加载 `3d1a19c`，HY4_1 EP04 经两次浮水后分别选中两个缓存岸点。寻岸配对 **5.760／6.331s**，均返回 `move_to_shore_failed → shore_lip_jump` 后仍湿，末位置分别距目标约 **6.3／24.8m**。缓存岸点有效性门通过，尚需确认实际可回到该岸。 [第一次岸点](../runs/r73_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-103250-915321.events.jsonl#L134) · [第二次岸点](../runs/r73_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-103250-915321.events.jsonl#L218) · [缓存岸点校验](../src/mc_agent/skills/combat_env.py#L2482)
- **停滞岸点记忆已合入**：r74 门户加载 `ceafbfd` 含 `ccf10a4`，会记下仍湿、没有接近或卡在唇沿的岸点，后一次在同命和附近站位另选候选。离线重选回归通过，待两个连续寻岸及真实出水。 [停滞条件](../src/mc_agent/skills/combat_env.py#L2748) · [重选夹具](../tests/test_generation_guard.py#L10886) · [新版加载](../runs/r74_midgame_portal_40m/hy4_hy_9_midgame/20261121-113210-275282.events.jsonl#L1)
- **一句话**：`guardian._escape_water` → `skills.escape_water()` **没有任何时间预算**，找不到岸时会连续跑 **499~561 秒**，直接吃穿整个 episode 预算（300~600s）。
- **影响（最严重的一条）**：**19 个 episode 被单个 reflex 动作整集打死**，每集 0 产出：
  | bot | 波次 | 集 | 每次阻塞 | 占该 bot 总集数 |
  |---|---|---|---|---|
  | `nju_movetest_01_bare` | `overnight_12bot` | **EP11–EP20（连续 10 集）** | **499.0~499.3s** | **10/21 = 48%** |
  | `hy4_hy_9_deep_water` | `hy4_audit_r27` | **EP11–EP19（连续 9 集）** | **560.4~560.6s** | 9/19 = 47% |
- **逐集复核**：两段共 19 集。`r27` EP13/EP16 各出现两次约 560.6s 的浮水失败，即 19 集里共 21 条≥300s 的这类回执。 [NJU EP11](../runs/overnight_12bot/nju_movetest_01_bare/20261119-104250-680466.events.jsonl#L23) · [HY4 EP13](../runs/hy4_audit_r27/hy4_hy_9_deep_water/20261119-132625-966581.events.jsonl#L42) · [HY4 EP16](../runs/hy4_audit_r27/hy4_hy_9_deep_water/20261119-140421-425711.events.jsonl#L49)
- **全量统计（6 波）**：`boundary_escape_water` **557 次**，合计 **16,895s = 4.69 小时**；分布：中位 1.4s，但 **>30s 113 次、>60s 35 次、>120s 24 次、>300s 22 次**，max **560.6s**。对照 `boundary_escape_to_shore` 387 次仅 1,395s（均值 3.6s）—— 说明**慢的不是"上岸"，而是"浮水"这条路径**。
- **源码定位**：
  - `guardian.py:1872` `self._action("boundary_escape_water", lambda: self._escape_water(status), ...)`
  - `guardian.py:294 _escape_water()` → 直接 `self._skills().escape_water()`，**不传任何 deadline**
  - `skills/library.py:4803 escape_water()` 内部是四层嵌套循环（`range(4)` / `range(2,16)` / `range(10)` / `range(2,9)` + `time.sleep(0.25)`），**没有 `deadline` / `max_duration_s` 参数**（对比同文件 `unstick(max_duration_s=45.0)` 已有预算机制）
- **耗时定位**：日志确认耗时集中在一次 `boundary_escape_water`，源码中有固定次数的探测、破方块和移动回退，缺少贯穿这些步骤的墙钟预算。现有回执没有每次后端请求的耗时，499s/560.6s 的分项还需埋点确认。
- **最小修复**：
  1. `escape_water(*, max_duration_s: float = 20.0)`，内部所有循环共享一个 `deadline`，超时返回 `{"ok": False, "partial": True, "in_water": true, "next": "pillar_or_dig_up"}`；
  2. `_escape_water` 传 `max_duration_s=min(20, remaining_episode_budget)`；
  3. 连续 2 次"浮水失败"→ 改走**垂直脱困**（`pillar up` / 向上挖 2 格）而不是继续沿岸搜索。
- **验收标准**：`boundary_escape_water` p99 ≤ 25s、max ≤ 40s；`nju_movetest` / `hy4_9` 的 `req=1 且 timeout` 集数为 **0**。
- **实测验收**：`hy4_audit_r27→r29_newmix` 的 `boundary_escape_water` p99/max **560.644s→20.053s**（51→76 次），指定 Bot 单请求超时 **7→0**。r31_short 24 次 p99/max **20.029s**，指定 Bot 单请求超时 **0**，HY4_9 EP01 在 **5.8s** 内浮水成功。独立的 actor `unstick` 墙钟残余继续跟踪 BUG-047。 [r29 超时回执](../runs/r29_newmix/hy4_hy_10_enchant_anvil/20261119-210319-676548.events.jsonl#L147) · [r31 HY4_5 EP04 max](../runs/r31_short/hy4_hy_5_brewing_alchemy/20261119-220436-415275.events.jsonl#L178) · [r31 HY4_9 EP01 成功](../runs/r31_short/hy4_hy_9_deep_water/20261119-220016-062292.events.jsonl#L74)
- **r35 单请求例外**：指定 Bot 单请求超时原始门禁计 1。HY4_9 EP04 在全波余 **1.424s** 时开集，仅模型超时，整集无工具及 Guardian 浮水动作。该波配对浮水最大 **15.826s**，另有 HY4_9 EP01 一个浮水动作距收尾约 2.21s 开始，缺结束回执，时长暂不可判定。 [EP04 全部事件](../runs/r35_short/hy4_hy_9_deep_water/20261119-234719-299605.events.jsonl#L9) · [EP01 浮水开始](../runs/r35_short/hy4_hy_9_deep_water/20261119-233220-885224.events.jsonl#L114)
- **r44 Day 2 复验**：21 条 Guardian 浮水配对，p99/max **8.370s**。指定槽 HY4_9 有真实浮水暴露，4 集中没有仅一请求的超时。该子门槛本波通过。 [指定槽浮水](../runs/r44_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-074226-462662.events.jsonl#L89)
- **r46**：Day 2 浮水 p99/max **20.275s**，仍过 25/40s。HY4_9 EP05 原始单请求超时门禁数 1，实际只有集末 **2.512s** 预算，无涉水或工具调度。保留此计数作门禁失败，轨迹不支持浮水导致超时。 [最慢浮水](../runs/r46_day2_wild_scout_9bot/hy4_hy_4_iron/20261120-090012-090783.events.jsonl#L137) · [HY4_9 集尾](../runs/r46_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-092014-538120.events.jsonl#L10)
- **r49**：裸装边界浮水最长 9.059s，末地场景最长 13.896s。裸装 HY4_9 EP06 仅分到 1.290s 后模型超时，门禁原样记 1，原始回执无工具或 Guardian 浮水动作。末地场景有指定槽浮水暴露且 0 单请求超时。 [波末请求](../runs/r49_day1_bare_9bot/hy4_hy_9_bare/20261120-110502-382891.events.jsonl#L11) · [末地场景最长浮水](../runs/r49_stronghold_end_5bot/nju_movetest_01_village_to_stronghold_end/20261120-104400-405972.events.jsonl#L1845)
- **r59 要塞三项实测通过**：14 条浮水 p99／最大 **17.532s**。MoveTest 真实涉水并有浮水回执，三集中单请求超时 0。EP02 读到暂离水但悬空时正确返回落脚未确认，EP03 配对一条 9.063s。状态保持 FIXED。 [MoveTest 出水未着地](../runs/r59_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-021350-128742.events.jsonl#L634) · [另一次浮水](../runs/r59_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-022657-635248.events.jsonl#L62) · [全波峰值](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-015348-145054.events.jsonl#L671)

### BUG-057 | P1 | Integrator（#1，`harness/guardian.py`）| 状态: OPEN（r78 单集 16 伤，门槛 10）

- **r78 已含后续热地板补丁**：同 DS_1 EP07 单集 **24→16**，边界过后再接近黑曜石仍受伤，最后一条工具前护航明确 `stepped_off`。新旧均超 ≤10，归因死保持 0。 [首次热伤](../runs/r78_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-142204-327526.events.jsonl#L75) · [边界尝试](../runs/r78_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-142204-327526.events.jsonl#L388) · [离格](../runs/r78_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-142204-327526.events.jsonl#L782)

- **r73 官方旧版 MoveTest 水下热地板死亡**：EP11 先三段回撤到 Y33，两次找岸返回仍湿，第一条热伤后再进水恢复，累计 22 条热伤并在 Y37 致死。该槽加载 `8964fee`，早于 `0f1e23c` 的水下热地板下蹲。 [首次热伤之前上岸失败](../runs/official_r73_80m_movetest/MoveTest_01/20261121-132126-098686.events.jsonl#L146) · [明确死亡](../runs/official_r73_80m_movetest/MoveTest_01/20261121-132126-098686.events.jsonl#L159) · [新版水下站姿夹具](../tests/test_generation_guard.py#L8931)

- **r77 同 DS_1 新版仍单集 24 条**：EP07 加载 `28a9838`，已包含课前 `45c3418`。本次是 actor 反复接近黑曜石区域，边界下蹲成功后仍有热伤，两次楼梯零位移，最后东向上两阶。最低热伤 HP 17.4，归因死亡 0。课前临界血量致死与本次移动／挖掘复入需分别复验。 [首批伤](../runs/r77_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-133224-439233.events.jsonl#L132) · [下蹲](../runs/r77_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-133224-439233.events.jsonl#L603) · [后段最低热伤](../runs/r77_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-133224-439233.events.jsonl#L968) · [最后楼梯](../runs/r77_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-133224-439233.events.jsonl#L1014)

- **r72 官方 5Ease 明确未离热地板**：EP06 首伤至致死回执间约 **60.246s**，同集 12 条。至少两次已下蹲仍 `no_safe_hot_floor_exit`，三向楼梯缺支撑，末次移动因死亡结束。与 r76 课前低血入口、DS_1 离格后复入分开验收。 [首伤](../runs/official_r72_80m_5ease/5Ease_Bot/20261121-121740-255689.events.jsonl#L84) · [离格失败](../runs/official_r72_80m_5ease/5Ease_Bot/20261121-121740-255689.events.jsonl#L200) · [致死](../runs/official_r72_80m_5ease/5Ease_Bot/20261121-121740-255689.events.jsonl#L268)

- **r76 同 DS_1 复验**：EP07 热地板 **11** 条，确认离格后继续受伤；EP11 是另一生命，课前被抢占触发撤退，0.301 血、零位移，开集后 3.852s 记录明确 `hot_floor` 死亡。三槽加载 `4913e9e`。之后合入的 `45c3418` 将关键主世界课前阶段纳入岩浆落脚检查，四条离线测试通过，等待实机。 [EP07 离格](../runs/r76_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-125723-434183.events.jsonl#L560) · [再次受伤](../runs/r76_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-125723-434183.events.jsonl#L702) · [EP11 课前](../runs/r76_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-130927-981104.events.jsonl#L3) · [致死](../runs/r76_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-130927-981104.events.jsonl#L15) · [补丁入口](../src/mc_agent/harness/guardian.py#L3426)

- **r75 同角色热地板回潮到 14**：DS_1 EP07 三次 `stepped_off`，其中两次同一落点，另一次撤退落点未确认。后续采集/移动仍会触发热地板，归因死亡 0。 [第一次离格](../runs/r75_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-120750-309638.events.jsonl#L365) · [落点不安全](../runs/r75_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-120750-309638.events.jsonl#L422) · [第二次同落点](../runs/r75_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-120750-309638.events.jsonl#L754) · [末次离格](../runs/r75_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-120750-309638.events.jsonl#L938) · [落地验证](../src/mc_agent/skills/combat_env.py#L847)

- **r74 同角色主世界继续复现**：DS_1 EP02 **11** 条热地板伤害，无归因死亡。两次潜行回执后续伤仍出现，11 条报告 HP 均为 **19.3**，均没有 `stepped_off`。现有同格净 HP 判定不能识别这种续伤，轨迹没有已缓存 HP，离线同格夹具另可复现。 [首次潜行](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-111121-472224.events.jsonl#L119) · [后续批量伤害](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-111121-472224.events.jsonl#L133) · [再次潜行](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-111121-472224.events.jsonl#L299) · [续伤源码](../src/mc_agent/harness/guardian.py#L1177)

- **r73 主世界不同续伤路径**：DS_1 EP08 **17** 伤、0 归因死亡。攻击前安全离格实证一次，普通护航随后三次也返回 `stepped_off`，前两次落回近同一安全格。离格之间重新就近找圆石和移动，后续又收到热地板伤害。此前 r72 的净血量门仍需复验。本集另需在离格后识别相同岩浆路径并改变采块目标。 [攻击前离格](../runs/r73_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-105438-261391.events.jsonl#L121) · [再次采块](../runs/r73_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-105438-261391.events.jsonl#L232) · [再次离格](../runs/r73_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-105438-261391.events.jsonl#L243) · [最后离格](../runs/r73_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-105438-261391.events.jsonl#L416) · [落脚验证](../src/mc_agent/skills/combat_env.py#L847)

- **r72 主世界重新暴露**：DS_1 EP07 有 **14** 条 `hot_floor` 伤害，超过单集 ≤10，归因死亡 **0**。一次边界潜行成功，首批 8 条随后批量读出，后来仍有 6 条。两次移动被续伤阻断，`unstick` 无安全落点，三向楼梯零步，最后向西下六步才实际离开。轨迹确认落地、无水和液态岩浆，未直接记录脚下方块名。同格续伤当前仅看净 HP 降幅，回血会掩盖新事件，D1 离线夹具复现。将同格的新结构化伤害纳入续伤判定，先复查脚下，再沿用安全落脚验证。另核对 `mine_blocks` 的实时监控未开启。 [潜行](../runs/r72_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-100706-951120.events.jsonl#L207) · [四次脱困前的拒绝](../runs/r72_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-100706-951120.events.jsonl#L285) · [实际离开](../runs/r72_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-100706-951120.events.jsonl#L372) · [续伤判定](../src/mc_agent/harness/guardian.py#L1177) · [工具监控范围](../src/mc_agent/harness/guardian.py#L120) · [落脚验证](../src/mc_agent/skills/combat_env.py#L847) · [D1 夹具](../../s-triage/tests/test_d1_repro.py#L136)
- **一句话**：站在**岩浆块（hot_floor）**上持续掉血时，guardian 把 `hot_floor` 归入 `NON_COMBAT_HURT_SOURCES` **压制了 `fresh_hurt`**，只有 HP ≤ 6 才升级，且升级后的 `emergency_bunker` / `flee` 在下界岩浆区**必然失败**（`unsafe_open_side` / `no_safe_retreat`）。
- **实测（6 波）**：
  - `hot_floor` 伤害事件 **2,129 次**，涉及 **58 个 episode**
  - **因 hot_floor 死亡 34 次**：`ds_4_nether_fortress` **25**、`glm_2_nether_bastion` **7**、`hy4_6_nether_bastion` 2
  - 典型片段（`glm_2` EP1）：`[受伤 health=19 by hot_floor]×10` → HP 20→13.3，同一 tick 内 `emergency_bunker=unsafe_open_side`、`flee=no_safe_retreat`，然后继续掉血
- **源码定位**：`guardian.py:52-72 NON_COMBAT_HURT_SOURCES` 含 `hot_floor`；`guardian.py:744-755` 用它算出 `non_combat_only_hurt` → `fresh_hurt` 不加；`guardian.py:790` 只有 `critical_non_combat = non_combat_only_hurt and hp_cur <= 6.0` 才触发动作。**全程没有"离开该方块 1~2 格"这个最直接的反射**。
- **分支复核**：近处有敌人时，`hot_floor` 仍可能成为 `fresh_hurt`。这会走通用对敌反射；两种分支都缺少针对脚下岩浆块的脱离动作。不能把近敌样本也归因于 HP ≤6 的门槛。 [Guardian 受伤分类](../src/mc_agent/harness/guardian.py)
- **最小修复**：新增 `hot_floor` 专属反射：检测到 `hurt.source == "hot_floor"` → `mc_move_to` 到相邻**非岩浆块**（1~3 格）或 `posture jump` 跳出；给该方块坐标加 60s `block_avoid`；不必等 HP ≤6。
- **验收标准**：单集 `hot_floor` 伤害 ≤10 次；`hot_floor` 死亡 ≤3 次/波。
- **r31 残余**：已加载下蹲修复的 GLM2 EP02 受伤 **50** 次。连续 **42** 次 `hot_floor_sneak` 读回 `sneaking=false`，每次触发重规划并取消生成，坐标未变。EP03 首批回执在同一坐标记录 `health=0 by hot_floor`、空原因死亡和复活，自动原因计数 0 漏掉此例。最小追加修复是在下蹲回执失败后限次，切换到验证脚下已离开岩浆块的短移路径。 [EP02 首次失败](../runs/r31_short/glm_glm_2_nether_bastion/20261119-221753-374008.events.jsonl#L72) · [EP02 收尾](../runs/r31_short/glm_glm_2_nether_bastion/20261119-221753-374008.events.jsonl#L529) · [EP03 死亡回执](../runs/r31_short/glm_glm_2_nether_bastion/20261119-221849-499250.events.jsonl#L11) · [下蹲反射](../src/mc_agent/harness/guardian.py#L588)
- **r33_short 复验失败**：GLM2 EP01 已加载 `e2144d7`，5 次下蹲都读回成功，故未进入只在下蹲失败时启用的短移。末段 20s 攻击零命中后收到同批 **33/52** 次岩浆块伤害，`mc_attack` 在工具执行期间免反射抢占。检查在连续掉血或下蹲被后续动作解除时的脱离门槛。 [首次下蹲](../runs/r33_short/glm_glm_2_nether_bastion/20261119-224439-075002.events.jsonl#L144) · [末段伤害](../runs/r33_short/glm_glm_2_nether_bastion/20261119-224439-075002.events.jsonl#L329) · [下蹲回执分支](../src/mc_agent/harness/guardian.py#L615)
- **r39_short 实测验收**：已加载驻足岩浆检测，单集伤害峰值从 r33 的 52 降至 **4**，整波 **0** 起岩浆块死亡。GUITest EP03 的 `hot_floor_exit_before_attack` 在 0.4s 内返回 `ok=true`，同次攻击因岩浆地板被拒绝。此前 r38 无对应暴露。 [r39 伤害](../runs/r39_short/nju_guitest_01_nether/20261120-010509-472786.events.jsonl#L938) · [攻击前脱离](../runs/r39_short/nju_guitest_01_nether/20261120-010509-472786.events.jsonl#L1118) · [代码入口](../src/mc_agent/harness/guardian.py#L666)
- **r48 回归**：下界堡垒 5 集共 62 条岩浆块伤害，HY4_6 EP01 **33**、HY4_5 EP02 **11**，0 死亡可归因岩浆块。HY4_6 的长攻击后同批到账 10 次；恢复潜行但站位不变，末段再到 6 次。攻击前离开岩浆块因无安全出口失败，攻击执行中免 Guardian 中断，应复验安全退路被拒后禁止持续站在伤害块展开长攻击。 [长攻击、潜行与伤害](../runs/r48_nether_fortress_9bot/hy4_hy_6_nether_fortress/20261120-102011-565234.events.jsonl#L807) · [无法安全离开](../runs/r48_nether_fortress_9bot/hy4_hy_6_nether_fortress/20261120-102011-565234.events.jsonl#L957) · [集末伤害](../runs/r48_nether_fortress_9bot/hy4_hy_6_nether_fortress/20261120-102011-565234.events.jsonl#L1072) · [潜行逻辑](../src/mc_agent/harness/guardian.py#L692)
- **r50 门禁通过**：与 r48 同九槽，单集伤害峰 **33→3**，岩浆块归因死亡 **0→0**。r50 延长到 30 分钟，出生点布局也重新隔离；本条按门槛通过更新状态，仍用 r48 原件保留贴地高压回归锚点。 [r50 伤害回执](../runs/r50_nether_fortress_9bot/hy4_hy_9_nether_fortress/20261120-205722-925188.events.jsonl#L31)
- **r51 再现**：DS_1 EP04 收 16 条伤害。楼梯首次触发潜行成功后，数次撤退仍被拒；末段在 Y75、XZ 约 `(2017.4,1883.6)` 附近四次楼梯无实际确认并持续掉血，最后才爬到 Y79。原来的攻击前搬离分支覆盖不到这条施工链。另在 DS_1 EP03，同一事件串明确 `health=0 by hot_floor` 紧邻死亡及重生。门禁旧版只读死亡文本原因漏记，本波应归因 1 死，仍在 ≤3 的死亡门槛内。 [潜行成功](../runs/r51_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-211448-535160.events.jsonl#L618) · [重复楼梯伤害](../runs/r51_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-211448-535160.events.jsonl#L1054) · [致死三联回执](../runs/r51_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-210756-123043.events.jsonl#L931)
- **r52 Day 2**：DS_1 EP03 峰值 3 条、全波 0 起岩浆块归因死亡，均在门槛内。r51 的下界施工站位和新版本防护仍待 r53 下界同位置复验。 [伤害回执](../runs/r52_day2_wild_scout_9bot/nju_ds_1_iron/20261120-213833-041153.events.jsonl#L83)
- **r53 下界仍失败**：同 r51 八个开跑槽、同出生点。单集岩浆块伤害峰 **16→22**，全波 **28→51**，归因死亡 **1→0**。NJU_3 EP03 三次楼梯被抢占改为成功潜行，结束仍在同一坐标，持续掉血。一次尝试移动时已在 range=5 内，完全无位移。潜行成功即返回，当前脱离只在潜行失败或特定攻击前成立。复验施工、移动时潜行是否持续，并核对连续掉血后是否离开足下方块。 [首次伤害](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-221749-264411.events.jsonl#L879) · [无位移](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-221749-264411.events.jsonl#L938) · [同坐标楼梯被抢占](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-221749-264411.events.jsonl#L968) · [潜行短路](../src/mc_agent/harness/guardian.py#L702)
- **r54 候选已加载**：`8792469` 含 `c8b45fb` 和 `8ffdc0d`，增加长战斗宏岩浆块监听，同一脚下方块已潜行却继续掉血时尝试验证落脚后短移。D1 离线复跑新增两组夹具通过。r54 首批为主世界要塞／末地场景，下一次下界施工实机仍须核对持续掉血后是否离开接触格。 [实机版本](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L1) · [持续伤害分支](../src/mc_agent/harness/guardian.py#L758) · [短移夹具](../tests/test_generation_guard.py#L6213) · [长工具抢占夹具](../tests/test_generation_guard.py#L6457)
- **r54 同四个下界锚点终版**：r53 总岩浆块伤害中有 22 条来自这四路，r54 为 **0**，也无潜行或持续掉血防护动作，门禁记 PENDING。HY4_8 EP01 同锚点证实一次液态岩浆逃生确认着地，属于另一条恢复分支。 [r53 同组暴露](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-221749-264411.events.jsonl#L879) · [r54 液态岩浆着地](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-231222-082716.events.jsonl#L18)
- **r56 持续伤害分支首次真实暴露**：NJU_3 EP01 两次阶梯始终停在 `(-418,51,-1883.8)`，首次边界潜行成功，第二次仍返回 `sneaking=true` 但续伤触发后的脱离因 `no_safe_hot_floor_exit` 失败，随后 HP 19.5→19.1。确认新版候选已命中，仍需终版计数与有安全落点时离格的正例。 [潜行成功](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-001742-397963.events.jsonl#L546) · [脱离与续伤](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-001742-397963.events.jsonl#L571) · [安全落点检查](../src/mc_agent/skills/combat_env.py#L724)
- **r56 终版双门槛通过**：岩浆块伤害峰 **8≤10**，归因死亡 **0≤3**，两集共 11 条。参照同一 NJU_3 槽 r53 EP03 的 22 条伤害，两波出生点一致，实际施工站位有别。r56 EP01 短移拒绝后，actor 约 102s 后主动垫圆石，再用阶梯从 `(-418,51,-1883.8)` 到 `(-416.5,54,-1883.6)`，首次可证离格。EP02 也拒绝短移，随后滑入液态岩浆，逃出后死于燃烧。保留安全出口缺失的残余，FIXED 依据两项数值门槛和新分支实测。 [r53 对照](../runs/r53_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-221749-264411.events.jsonl#L879) · [actor 放圆石](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-001742-397963.events.jsonl#L730) · [离格](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-001742-397963.events.jsonl#L751) · [EP02 后续伤亡](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-002424-340910.events.jsonl#L703)

- **r57 主世界复验**：HY4_2 EP03 有 6 条岩浆块伤害，0 岩浆块归因死亡。两次潜行成功，攻击前脱离因 `unsafe_landing` 失败。保持双阈值通过与无落脚点的残余。 [首次伤害](../runs/r57_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261121-005134-587629.events.jsonl#L282) · [攻击前脱离拒绝](../runs/r57_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261121-005134-587629.events.jsonl#L340)

- **r57 下界回归**：同五槽峰值 **8→14**、伤害总量 **11→27**、岩浆块归因死亡 **0→0**，重开单集门禁。NJU_3 EP02 攻击前短移 1.41m 且返回成功。EP04 两次续伤短移均返回 `unsafe_landing`，紧接着均读取 `on_ground=false`，最后由 actor 楼梯确认上升。复验短移着地确认和未着地后换安全退路，单集仍按 ≤10、全波归因死亡 ≤3 验收。 [攻击前短移](../runs/r57_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-005432-799248.events.jsonl#L706) · [EP04 首次伤害](../runs/r57_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-010225-714536.events.jsonl#L141) · [二次落脚未确认](../runs/r57_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-010225-714536.events.jsonl#L392) · [楼梯上升](../runs/r57_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-010225-714536.events.jsonl#L419) · [续伤短移](../src/mc_agent/harness/guardian.py#L867)

- **r58 下界重新通过双门槛**：同五个名义锚点，峰 **14→7**、总 **27→8**、归因死亡 **0→0**。NJU_3 EP01 两次潜行读回成功，波内缺续伤后尝试短移的实机回执。此前回归的未着地短移链仍待复验，保持 OPEN。 [潜行](../runs/r58_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-012112-089498.events.jsonl#L1183) · [成批岩浆块伤害](../runs/r58_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-012112-089498.events.jsonl#L1263)

- **r59 同五槽再过数值门槛**：岩浆块峰 **7→5**、总 **8→5**，归因死亡 0。NJU_3 EP01 两次下蹲，仍缺实际短移离格样本。另有液态岩浆救援 **59→44**、确认着地 **3→2**、超预算 **23→9**、落脚未确认 **2→10**。HY4_8 EP07 实测跨桥后仍在空中，正确返回失败。 [岩浆块受伤](../runs/r59_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-015758-386899.events.jsonl#L87) · [跨桥后仍腾空](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-021429-192469.events.jsonl#L725) · [桥面落脚保护](../src/mc_agent/skills/combat_env.py#L1551)
- **r60 数值门槛通过**：岩浆块峰 **5→7**、总 **5→9**、明确归因死亡 0。五次潜行依旧缺确认离格。液态岩浆救援含边界 **46→37**、确认着地 **2→8**、落脚未确认 **11→5**，其中三条跨桥后下蹲确认已着地。7 条新预算事件全部记录终点 dig，加载版只在最后的岸缺口施工直接调用 dig。已合入 1a41f83 给开槽单次挖掘分时并保留跳跃时间，待新波。 [岩浆块伤害](../runs/r60_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-023634-859374.events.jsonl#L699) · [实际跨桥](../runs/r60_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-023926-594219.events.jsonl#L297) · [预算终点](../runs/r60_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-022439-449941.events.jsonl#L25) · [挖后预留](../src/mc_agent/skills/combat_env.py#L1620)
- **r61 运行中开槽样本**：HY4_9 EP03 16 次岩浆救援带开槽单次挖掘超时标记，共 53.419s，16 次均还在岩浆。当前已真实暴露 1a41f83 的分段挖掘限时，后续要核对失败岸的换向与是否完成跳岸。 [首条](../runs/r61_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-025954-778263.events.jsonl#L544) · [末条](../runs/r61_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-025954-778263.events.jsonl#L679)

- **r61 终版**：岩浆块峰 **7→9**、归因死仍 0。液态岩浆含边界 **37→103** 次，整段 6.5s 触顶 **7→0**；新单次开槽超时 **48 个标记、42 条救援**，均 HY4_9、均未确认成功。落脚未确认 **5→11**，明确岩浆死 **5→7**。EP03 重生后只有黑曜石、缺镐和可搭桥方块，16 次开槽失败跨集续发；EP04 补装后实际搭桥着地。复验同一岸向开槽失败后跳过该路径，以及无合适材料时的恢复。 [开槽重复](../runs/r61_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-025954-778263.events.jsonl#L544) · [岩浆块峰](../runs/r61_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-030507-451289.events.jsonl#L388) · [单次预算](../src/mc_agent/skills/combat_env.py#L1620)

- **r62 下界回归**：峰 **9→29**、总 **13→43**、明确归因死亡仍 0。NJU_3 EP03 两次续伤后，潜行读回为真，横向脱离都返回 `no_safe_hot_floor_exit`。二次拒绝之间，进食被续伤打断。本案实际走到了离格兜底，所有候选未通过安全探测。补此分支的安全垂直离格或保留平台的回退，并用落地且脚下非岩浆块的状态验收。液态岩浆另计，含边界回调 **103→33**、确认离浆 **6→11**、落脚未确认 **11→9**，开槽超时 **48→4**。r62 四条均在 HY4_8，回调最终返回已离浆。延后开槽和换泳向仍缺实机回执。 [NJU_3 岩浆块伤害](../runs/r62_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-042822-090404.events.jsonl#L1458) · [第二次无安全出口](../runs/r62_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-042822-090404.events.jsonl#L1465) · [安全出口探测](../src/mc_agent/skills/combat_env.py#L770) · [HY4_8 开槽超时后离浆](../runs/r62_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-042149-339253.events.jsonl#L118)

- **r63 下界**：岩浆块峰 **29→14**，归因死亡 0，NJU_3 EP01 有续伤后短移落脚失败，后来攻击前护航验证离格。液态岩浆开槽延后 **48**、泳向滞留 **2** 条标记，均首次在实机出现。HY4_8 EP04 同一水平站位三次开槽超时后的延后回调由 4.637s 降至 0.295s，仍未离浆。该波加载 `7165713`，后续 `d4ccb4f`／`63106a7`／`b83ba71` 的跳过黑曜石、读取结果细分及岸向排除需新波复验。 [落脚失败](../runs/r63_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-045226-551022.events.jsonl#L855) · [攻击前离格](../runs/r63_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-045226-551022.events.jsonl#L924) · [开槽超时](../runs/r63_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-050120-399129.events.jsonl#L8) · [延后回执](../runs/r63_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-050120-399129.events.jsonl#L17) · [泳向滞留](../runs/r63_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-045537-584987.events.jsonl#L521)

- **r64 下界**：岩浆块峰／总 **14／14→29／55**，归因死亡 0。液态岩浆开槽超时 **35→0**，`lava_notch_hard_block` **181** 次，均 HY4_9，同槽 171 次救援只确认 2 次离浆。全波确认离浆 **4→4**，落脚未确认 **17→83**。EP09 单集 42 次救援、22 次落脚未确认、0 次确认脱离，岸边数个相邻落点反复落回。r64 加载 `fcd634f`，已覆盖黑曜石跳过，仍需验证上岸落脚及连续未移动时的取消抑制。 [岩浆块首条](../runs/r64_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-052418-034391.events.jsonl#L530) · [落脚未确认](../runs/r64_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-055226-450350.events.jsonl#L7) · [末次岸边尝试](../runs/r64_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-055226-450350.events.jsonl#L424) · [硬块跳过](../src/mc_agent/skills/combat_env.py#L1748)

### BUG-058 | P1 | Integrator（#1，`harness/mc_tools.py` + 模型提示）| 状态: OPEN（航点统一登记实证通过，单集 ≤3 行为门待稳定）

- **r82 门户 30 分钟波无路峰／总 6/21**。HY4_8 EP04 六次均为直达。第一次显式 Y，第二次同 XZ 去掉 Y，之后换目标仍失败，统一计数未能阻止本集超门。HY4_8 EP02 另有两次地表 `scout_route_cooldown` 实机回执。 [首条](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/20261121-203846-280036.events.jsonl#L181) · [第六条](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/20261121-203846-280036.events.jsonl#L529) · [探索冷却](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/20261121-203400-931965.events.jsonl#L70)

- **r81 门户单集 4，移动计数止于 3**。HY4_9 EP06 三次移动和一次提桶 `mc_interact` 均为 `path_not_found`，后三条瞄同一水源 XZ。动作内的交互无路未进入移动计数，第三次移动才得到 `requires_replan`，整集原门为 4、波总 9。 [交互无路](../runs/r81_midgame_portal_40m/hy4_hy_9_midgame/20261121-195836-061369.events.jsonl#L864) · [第三次移动](../runs/r81_midgame_portal_40m/hy4_hy_9_midgame/20261121-195836-061369.events.jsonl#L934) · [移动登记](../src/mc_agent/harness/mc_tools.py#L1458)

- **r76 日出峰／总 9/44**。HY4_10 EP12 八次失败航点集中在约 **2.2×5.2 格** 的同一片区域，虽轮换远目标，Bot 只在两处邻近站位。第八次返回后 **5.049s**，对邻格发一直达并记为第九次。该直达已过 5s 且整格索引相异，故不命中现有航点转直达保护。下一步复验短段失败区域和朝岛心可用落点，单集门仍为 ≤3。 [首航点](../runs/r76_day12_real_drop_60m/hy4_hy_10_bare/20261121-200142-666715.events.jsonl#L60) · [末航点](../runs/r76_day12_real_drop_60m/hy4_hy_10_bare/20261121-200142-666715.events.jsonl#L809) · [直达入场](../runs/r76_day12_real_drop_60m/hy4_hy_10_bare/20261121-200142-666715.events.jsonl#L847) · [冷却条件](../src/mc_agent/harness/mc_tools.py#L1335)

- **r78→r80 门户同组无路峰／总 8/70→4/7**。NJU_3 EP08 在 Y4 尝试直达骷髅附近。第二条即收到 `requires_replan`，之后两次向西各掘 8 步，南侧目标 XZ 仍然相同，两次直达再次无路。回执计数推进至 4，单集超门 1。换站位和开挖已经发生，后续路径仍未对准目标，继续保留单集行为门。 [第二条](../runs/r80_midgame_portal_40m/nju_nju_3_midgame/20261121-194109-074676.events.jsonl#L142) · [第二次隧道](../runs/r80_midgame_portal_40m/nju_nju_3_midgame/20261121-194109-074676.events.jsonl#L211) · [第四条](../runs/r80_midgame_portal_40m/nju_nju_3_midgame/20261121-194109-074676.events.jsonl#L242) · [重规划回执](../src/mc_agent/harness/mc_tools.py#L1458)

- **r77→r78 门户同组无路峰／总 2/4→8/70**：HY4_8 EP04 八次无路全是直达，计数 1→8，最大样本不触及航点转直达冷却条件。r78 加载 `935a579`，全波无 `move_route_cooldown` 回执。 [第一条](../runs/r78_midgame_portal_40m/hy4_hy_8_midgame/20261121-142602-446031.events.jsonl#L19) · [第八条](../runs/r78_midgame_portal_40m/hy4_hy_8_midgame/20261121-142602-446031.events.jsonl#L662) · [冷却适用条件](../src/mc_agent/harness/mc_tools.py#L1335)

- **r78 中期已加载冷却，峰仍 5**：HY4_1 EP07 首个失败航点距最终目标 **5.43m**，随后一直达带 Y，另一直达瞄原目标，二者都不属于同格更窄子集；同局部还有两条换位后的无路。整波未产出 `move_route_cooldown` 实机回执。 [首条航点](../runs/r78_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-142054-525097.events.jsonl#L385) · [直达再次无路](../runs/r78_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-142054-525097.events.jsonl#L444) · [冷却条件](../src/mc_agent/harness/mc_tools.py#L1335)

- **航点转直达冷却已合入**：`ce80857` 对同维度、同起点、失败半径子集的直接重试拦截 5s，返回 `move_route_cooldown`。放宽半径、位置变化及到期均可重新提交。r73 日出和 r78 中期加载 `3c9771e42238a690=e7b9b74`，待目标行为曝光。r73 官方 MoveTest 仍早于该补丁，EP18 八条坐标寻路失败。 [冷却判断](../src/mc_agent/harness/mc_tools.py#L1335) · [离线夹具](../tests/test_skills_and_providers.py#L1917) · [EP18 末次无路](../runs/official_r73_80m_movetest/MoveTest_01/20261121-133842-075095.events.jsonl#L267)

- **r76 门户峰／总 3/3→4/18**：HY4_9 EP04 同一航点重复两条，改直达得第 3 条，约 419 秒后另一落点航点记第 4 条。统一登记与重规划工作，单集行为门仍超 1。 [重复航点](../runs/r76_midgame_portal_40m/hy4_hy_9_midgame/20261121-130413-750233.events.jsonl#L53) · [直达第 3 条](../runs/r76_midgame_portal_40m/hy4_hy_9_midgame/20261121-130413-750233.events.jsonl#L72) · [后续第 4 条](../runs/r76_midgame_portal_40m/hy4_hy_9_midgame/20261121-130413-750233.events.jsonl#L701)

- **r73 裸装验证探索路线冷却**：HY4_5 EP12、EP13 两次 `scout_route_cooldown` 分别 **0.428／0.323s**。同角色 EP10 仍有三条坐标无路和三个同站位探索无路，峰 6，波总 104。坐标计数仅三条，探索路径另记；冷却命中已有实机证据，整集门未过。 [EP12 冷却](../runs/r73_bare_real_drop_60m/hy4_hy_5_bare/20261121-124420-563209.events.jsonl#L173) · [EP13 冷却](../runs/r73_bare_real_drop_60m/hy4_hy_5_bare/20261121-124644-482972.events.jsonl#L122) · [EP10 第三条探索无路](../runs/r73_bare_real_drop_60m/hy4_hy_5_bare/20261121-123759-633318.events.jsonl#L242) · [冷却键](../src/mc_agent/skills/library.py#L1575)
- **r74→r75 同三门户角色达门**：无路峰／总 **4/12→3/3**。HY4_8 EP07 两航点和一直达共用计数 **1→2→3**，第三条给重规划。另一个日出配置 r71 同样包含统一登记，HY4_3 EP04 仍峰 **4**。 [航点 1](../runs/r75_midgame_portal_40m/hy4_hy_8_midgame/20261121-123552-409796.events.jsonl#L21) · [航点 2](../runs/r75_midgame_portal_40m/hy4_hy_8_midgame/20261121-123552-409796.events.jsonl#L58) · [直达 3](../runs/r75_midgame_portal_40m/hy4_hy_8_midgame/20261121-123552-409796.events.jsonl#L88) · [日出峰值集首条](../runs/r71_day12_real_drop_60m/hy4_hy_3_bare/20261121-120416-069807.events.jsonl#L50) · [航点登记](../src/mc_agent/harness/mc_tools.py#L1490)

- **r73 门户旧版分段对照**：峰／总 **3/3→4/13**。进入下界后的 HY4_8 EP06 四条坐标无路，末两条同 XZ 修改 range 3→5，仍无路。该波加载 `b8d06fd`，早于 `e84272c`。 [首条](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/20261121-110929-304625.events.jsonl#L109) · [同点重试](../runs/r73_midgame_portal_40m/hy4_hy_8_midgame/20261121-110929-304625.events.jsonl#L917)

- **分段航点登记修复已合入**：`102fea5` 含 `e84272c`。离线真实入口将 32m 航点和直达失败写入同一集历史，夹具通过。待实际航点失败、历史计数与单集峰 ≤3 同时验收。 [集成夹具](../tests/test_skills_and_providers.py#L1832)
- **r74 门户通过航点登记专项，行为门仍失败**：HY4_8 EP09 前三条直达依次 **1／2／3**，后一次航点回执明示 **4**，含同集换路提示。HY4_9 EP07 另一条航点正确从 **1** 开始。峰／总 **4/13→4/12**，单集 ≤3 仍未通过。 [第三条直达](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/20261121-120909-584794.events.jsonl#L289) · [航点登记为第四](../runs/r74_midgame_portal_40m/hy4_hy_8_midgame/20261121-120909-584794.events.jsonl#L351) · [另集从一开始](../runs/r74_midgame_portal_40m/hy4_hy_9_midgame/20261121-115318-010531.events.jsonl#L22) · [新登记](../src/mc_agent/harness/mc_tools.py#L1481)

- **r73 中期单集峰 8**：同 r72 三角色峰／总 **3/8→8/33**。HY4_2 EP04 八条全为坐标移动，含两条 32m 航点失败，之后仍回到相近目标邻域。斥候冷却无直接回执，复验直达与分段失败统一登记和换路。 [首条](../runs/r73_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-103331-383913.events.jsonl#L47) · [航点](../runs/r73_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-103331-383913.events.jsonl#L281) · [末条](../runs/r73_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-103331-383913.events.jsonl#L790) · [坐标失败登记](../src/mc_agent/harness/mc_tools.py#L1415)

- **r69 日出单集门回退**：同 r68 四角色无路峰／总 **3/33→6/36**。HY4_3 EP04 有三次 `move_to` 和一次 `dig` 指向同一 XZ，其中两次不传 Y，另有一次换邻近坐标和一次 `craft_gear` 无路。新斥候冷却无回执。复验同位置的跨工具重试是否触发换路。 [首条](../runs/r69_day12_real_drop_60m/hy4_hy_3_bare/20261121-100029-466314.events.jsonl#L61) · [同点挖掘](../runs/r69_day12_real_drop_60m/hy4_hy_3_bare/20261121-100029-466314.events.jsonl#L134) · [末次回执](../runs/r69_day12_real_drop_60m/hy4_hy_3_bare/20261121-100029-466314.events.jsonl#L258) · [坐标失败登记](../src/mc_agent/harness/mc_tools.py#L1415)

- **r67 中期过门三槽**：无路峰／总 **7／8**。HY4_9 EP06 七条无路全部在下界，首两条对同一 XZ 换填 Y，后续五条换目的地仍无路。近邻失败统计逐条见回执。进入下界后需要以可落脚通路规划替代轮换远点。 [首条](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-072808-164375.events.jsonl#L132) · [末条](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-072808-164375.events.jsonl#L431)
- **斥候候选 `80a3dab` 已随 `0a68fc8` 合入**：给原地无路的方向加 180s 冷却，按维度、站位 2m、步长 0.5m 匹配，保留未试方向。r67 的七次下界坐标 `mc_move_to` 为另一入口，复验分别看斥候真实请求和全票单集失败阈值。 [斥候冷却](../src/mc_agent/skills/library.py#L1574) · [近邻与换位夹具](../tests/test_skills_and_providers.py#L3198)
- **r69 加载后的实机残余**：中期实掉三槽无路峰／总 **4/9**，HY4_2 EP05 的四条为两次点挖、两次坐标移动，均在新斥候冷却之外。终版三槽未记录 `scout_route_cooldown` 的实机回执。 [首个点挖](../runs/r69_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-080654-479764.events.jsonl#L56) · [首个坐标移动](../runs/r69_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-080654-479764.events.jsonl#L608)
- **r70 同三个中期角色**：单集峰／总 **4/9→5/22**。HY4_1 EP10 五条全为坐标移动，第二和第三条同一 XZ 改传 Y，第四和第五条在同一目标邻域。新斥候冷却无实机回执。 [同一 XZ 首次](../runs/r70_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-084511-435312.events.jsonl#L407) · [同一 XZ 再试](../runs/r70_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-084511-435312.events.jsonl#L426) · [末次无路](../runs/r70_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-084511-435312.events.jsonl#L887)

- **r67 探索三槽**：无路峰／总 **4／15**。HY4_2 EP02 三次斥候各试正向及两条窄角，随后一次步行也失败。EP03 同站位附近再出现三次斥候无路。一次保护应覆盖相同受阻邻域内曾尝试的路线，留独立机动手段脱离该站位。 [EP02 首条](../runs/r67_expl_mem_3bot/hy4_hy_2_iron/20261121-065531-768950.events.jsonl#L18) · [步行无路](../runs/r67_expl_mem_3bot/hy4_hy_2_iron/20261121-065531-768950.events.jsonl#L168) · [EP03 首条](../runs/r67_expl_mem_3bot/hy4_hy_2_iron/20261121-065750-378055.events.jsonl#L19)

- **r65 Day1/Day2**：无路峰／总 **6／14**。DS_2 EP06 三次在 Y59–60 斥候无路，向上施工后再试，北向一次实际走 29.68m 才失败；原位再试北向被保护门拦一次，末次无路发生在另一站位。峰值混有换过位置后的失败，保护门已有触发证据。 [初始无路](../runs/r65_day12_full_45m/nju_ds_2_bare/20261121-065753-250126.events.jsonl#L55) · [进展后失败](../runs/r65_day12_full_45m/nju_ds_2_bare/20261121-065753-250126.events.jsonl#L359) · [重复拦截](../runs/r65_day12_full_45m/nju_ds_2_bare/20261121-065753-250126.events.jsonl#L440)
- **一句话**：`mc_move_to` 的 `path_not_found` 是**全量最大的错误码**，且模型会在"可达中转点"和"不可达目标"之间反复横跳。
- **实测（6 波）**：**1,137 次**（其中 `mc_move_to` **1,099**、`mc_dig` 31、`mc_place` 4），分布在 **446 个 episode**（占 1,581 集的 28%）；**50 个 episode ≥5 次**，最多 13 次。按 bot：`hy4_6_nether_bastion` **237**、`glm_1_nether_fortress` **151**、`glm_4_brewing_alchemy` 88、`nju_3` 73、`glm_2` 73。
- **典型片段（`hy4_6` r28 EP1，45 请求 0 分）**：
  ```
  move_to(228,205,range=4) -> ok
  move_to(232,205,range=4) -> path_not_found   # 目标就在 4 格外
  move_to(228,205,range=4) -> ok               # 退回中转点
  move_to(232,205,range=4) -> path_not_found   # 再撞
  ```
  目标 `ancient_debris` 在墙内/岩浆后，`find_blocks radius=96` 每次都返回"total 2"，模型知道位置但到不了。
- **根因**：`path_not_found` 只说明"没路"，**不告诉模型"该怎么改"**（换中间点？挖过去？放弃？）；`repeated_identical_failure_blocked` 只拦完全相同的参数，坐标差 1~4 格就绕过。
- **最小修复**：① `path_not_found` 连续 2 次且目标距离 <8m → 自动改 `mc_skill_mine_blocks` / `mc_dig` 朝目标掘进；② 同一目标累计 3 次失败 → `requires_replan=True` + 该坐标 120s 冷却；③ 返回 `suggested: {"mode":"dig_toward","target":[x,y,z]}`。
- **验收标准**：单 episode `path_not_found` ≤3；单波总量 ≤300。
- **r33_short 复验**：已加载 `090ad3d`，单集峰值 **7→4**（r32→r33），整波 **19→28**，仍未过单集门槛。GLM1 EP01 第三次 `move_to` 正确触发重规划，第四条失败来自 `mc_dig`。HY4_3 EP06 则连续收到四次 `dig_toward` 提示，前两次后挖掘和楼梯仍把模型带回同一片区域，第四次失败后才通过隧道离开。 [GLM1 第三次](../runs/r33_short/glm_glm_1_nether_fortress/20261119-224438-456095.events.jsonl#L184) · [GLM1 第四次](../runs/r33_short/glm_glm_1_nether_fortress/20261119-224438-456095.events.jsonl#L566) · [HY4_3 隧道](../runs/r33_short/hy4_hy_3_midgame/20261119-225345-169801.events.jsonl#L410) · [保护分支](../src/mc_agent/harness/mc_tools.py#L1261)
- **r35_short 下界复验**：HY4_8 EP03 共 **10** 次寻路失败，9 次收到重规划提示，三次尝试的隧道全部在首步把下方实测 `netherrack` 拒为 `unsafe_or_unknown_floor`。整波 11 条同类拒绝中，5 条下方为空气，另 6 条来自四个 Bot 的 `netherrack` 或 `basalt`，均落在 `tunnel.GROUND` 的缺口。最小修复应让确认坚实的下界方块通过地板集合，继续运行流体、坠落和未知地形检查。 [HY4_8 拒绝](../runs/r35_short/hy4_hy_8_nether/20261119-233433-043228.events.jsonl#L334) · [HY4_6 玄武岩拒绝](../runs/r35_short/hy4_hy_6_nether_bastion/20261119-233212-680176.events.jsonl#L455) · [地板集合](../src/mc_agent/skills/tunnel.py#L11) · [地板判定](../src/mc_agent/skills/tunnel.py#L131)
- **r43 短波门禁**：Day 1 单集峰值 **2**、全波 **3**，Day 2 **3／5**，均通过本波阈值。仍等同类下界路径压力验收。 [Day 2 峰值 EP03](../runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-072726-669575.events.jsonl#L47)
- **r44 Day 2 回潮**：全波 **10**，HY4_5 EP02 单集 **4** 超门槛，三条来自 `mc_skill_scout_surface`，一条来自 `mc_move_to`。重规划需跨斥候和直接坐标移动保留失败上下文。 [首条斥候失败](../runs/r44_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-074143-250000.events.jsonl#L19) · [直接移动失败](../runs/r44_day2_wild_scout_9bot/hy4_hy_5_iron/20261120-074143-250000.events.jsonl#L163)
- **r46**：Day 1 峰 **4／全波 7**，HY4_3 EP03 中两个近目标先后反复失败，第二个目标经过挖隧道和脱困后仍 `path_not_found`。Day 2 峰 **2／全波 4**。 [第一个近目标](../runs/r46_day1_5bot/hy4_hy_3_bare/20261120-090237-438331.events.jsonl#L78) · [第二个目标末次重试](../runs/r46_day1_5bot/hy4_hy_3_bare/20261120-090237-438331.events.jsonl#L314)
- **r47**：Day 1 单集峰 **4／全波 13**，Day 2 **3／6**。GuiTest EP03 从 Y55 起点尝试远距离 XZ 航点，脱困拒绝和浅爬后仍重复相同西向目标 `[6310,-6535]`。32m 分段回执提前返回，跳过路径失败历史与定向掘进提示；复验覆盖该分支。单集目标继续未过。 [分段提前返回](../src/mc_agent/harness/mc_tools.py#L1320) · [失败历史](../src/mc_agent/harness/mc_tools.py#L1403) [首次西向失败](../runs/r47_day1_5bot/nju_guitest_01_bare/20261120-100322-253643.events.jsonl#L79) · [重复西向失败](../runs/r47_day1_5bot/nju_guitest_01_bare/20261120-100322-253643.events.jsonl#L139)
- **r48／r49**：r48 下界 **8/34**，HY4_7 EP05 两次重试 `(-346,-292)`、两次重试 `(-333,-235)`，调整范围或去掉 Y 仍指向相同分段航点。r49 裸装 **2/9**，末地场景 **0/0**，场景不同且两波均未加载 `643f2f1` 的下界斥候。 [反复分段](../runs/r48_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-102911-540461.events.jsonl#L639)
- **r50**：下界单集峰／全波 **8／34→7／82**。HY4_9 EP03 同一 XZ `(3556,1790)` 返回三次 `path_not_found`，先传 XYZ 后去掉 Y 仍反复。末地峰／整波 **2／3**。 [同目标重试](../runs/r50_nether_fortress_9bot/hy4_hy_9_nether_fortress/20261120-204428-525944.events.jsonl#L737)
- **r51**：同名下界波单集峰／总量 **7／82→8／86**。仅 HY4_7 EP02 就有 5 次对 `(1512,3141)` 的失败，改变 Y 和 range 仍多次返回相近中间航点。 [连续失败](../runs/r51_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-210358-106863.events.jsonl#L106) · [集后段重试](../runs/r51_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-210358-106863.events.jsonl#L696)

- **r53 下界**：路径错误峰／总 **8／86→7／83**，峰值仍超 3。HY4_7 EP01 七次失败中三次目标同为 `(1512,3141)`；分段移动在报错回执处直接返回，未进入普通寻路失败累计与定向掘进提示。四个隧道方向另外均因黑曜石地板受阻。复验分段和普通寻路能否共用同目标失败保护。 [首个分段失败](../runs/r53_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-215410-391904.events.jsonl#L288) · [第三个同目标失败](../runs/r53_nether_fortress_9bot/hy4_hy_7_nether_fortress/20261120-215410-391904.events.jsonl#L649) · [分段提前返回](../src/mc_agent/harness/mc_tools.py#L1329) · [失败累计](../src/mc_agent/harness/mc_tools.py#L1412)
- **r54 下界同四锚点**：失败峰／总 **7／45→8／58**，仍超过单集阈值。HY4_5 EP03 失败后改过坐标和工具，随后仍返回同片堡垒平台多次无路。需按原点／目的地对照是否真正离开失败邻域。 [首次失败](../runs/r54_nether_fortress_6bot/hy4_hy_5_nether_fortress/20261120-232043-545240.events.jsonl#L40) · [末段再次无路](../runs/r54_nether_fortress_6bot/hy4_hy_5_nether_fortress/20261120-232043-545240.events.jsonl#L880)
- **r55 同四锚点**：失败全组 **58→47**，单集峰 **8→10**，仍超 3。HY4_8 烈焰棒 EP03 对 Y24 的堡垒砖无路，第二次已有重规划，随后隧道地板拒绝、楼梯遇岩浆，十次失败全部进入失败历史，集尾战斗报无目标。加上新槽 HY4_9，全波 62 条。 [首次高差失败](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-000100-494729.events.jsonl#L71) · [岩浆阻断](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-000100-494729.events.jsonl#L292) · [末次无路](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-000100-494729.events.jsonl#L953) · [战斗无目标](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-000100-494729.events.jsonl#L1021)
- **r56 同五锚点**：无路峰／总 **10／62→7／52**，峰值门槛仍失败。移动未确认 21→42，需同时看重新定位和 Y 轴可达性。HY4_9 EP06 首个无路后继续多次失败。 [HY4_9 首次](../runs/r56_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-003615-260986.events.jsonl#L68)

- **r57 要塞波**：无路峰／总 **6／21**。HY4_1 EP04 门框房 Y−15，首个站位也在 Y−15。首两次直走失败，后续在走廊水平改路，整集 6 条无路、0 挖通尝试或填眼。末段另有两条字符串坐标调用未取得有效位移回执。 [第一条直达失败](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L72) · [第六条无路](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L342)

- **r57 下界同五槽**：无路峰／总 **7／52→7／58**。HY4_9 EP05 首次隔 9 格高差找目标，随后收到重规划。集末失败历史仍到 7，近邻重复 5。 [首个无路](../runs/r57_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-010311-573592.events.jsonl#L345) · [重规划](../runs/r57_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-010311-573592.events.jsonl#L572) · [历史计数](../runs/r57_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-010311-573592.events.jsonl#L913)

- **r58 下界再验**：无路峰／总 **7／58→6／66**。HY4_5 EP03 六次无路，其中两次对同一矿点 XZ 分别带 Y 和省略 Y 重试，随后隧道地板和下行楼梯均拒绝。 [首个无路](../runs/r58_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-012831-363597.events.jsonl#L104) · [末段再次无路](../runs/r58_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-012831-363597.events.jsonl#L798)

- **r59 下界**：无路峰／总 **6／66→7／53**。HY4_5 EP02 对 `(-2272,3930)` 失败三次，对 `(-2260,3944)` 失败两次，改 Y 或范围后又在原平台报无路。 [第一轮失败](../runs/r59_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-020034-576283.events.jsonl#L40) · [两组反复](../runs/r59_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-020034-576283.events.jsonl#L354) · [最后无路](../runs/r59_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-020034-576283.events.jsonl#L899)

- **r59 要塞**：峰／总 **8／30**，HY4_1 EP03 含两次对同一远位坐标的分段无路，以及一次斥候无路。 [首条失败](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020828-570948.events.jsonl#L138) · [第二次分段](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020828-570948.events.jsonl#L487)

- **r60 下界**：无路峰／总 **7／53→9／76**，最差 HY4_9 EP03。地板类隧道失败 **24/28→35/37**，同时过去的楼梯缺支撑 **30→11**。 [最差集首条无路](../runs/r60_nether_fortress_5bot/hy4_hy_9_nether_fortress/20261121-022459-216493.events.jsonl#L88)

- **r61 下界**：无路峰／总 **9／76→7／56**，单集仍失败。 [最差集首条](../runs/r61_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-031245-993166.events.jsonl#L54)

- **r62 下界**：无路峰／总 **7／56→6／55**，单集仍超标。HY4_7 EP06 先后尝试两段路线，集尾超时。6 次无路中有 2 次为 32m 航点失败，带 `requires_replan` 但缺失败计数，末次回执显示 4。`mc_move_to` 先返回航点回执，绕过后面的失败历史登记。让航点和直达失败经同一处登记，保留航点位置，复验六条是否连续记满，以及单集 ≤3 的行为门槛。 [首个航点失败](../runs/r62_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-044040-323035.events.jsonl#L215) · [末次失败](../runs/r62_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-044040-323035.events.jsonl#L717) · [航点提前返回](../src/mc_agent/harness/mc_tools.py#L1333) · [失败历史登记](../src/mc_agent/harness/mc_tools.py#L1415)

- **r63 要塞**：峰／总 **10／25**。HY4_2 EP02 有多个失败段。按日志，尝试直达、楼梯与斥候，仍有原地重复失败保护和一批战斗抢占。须按原门槛复验单集 ≤3。 [首条失败](../runs/r63_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-042946-659807.events.jsonl#L246) · [重复拦截](../runs/r63_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-042946-659807.events.jsonl#L705)

### BUG-059 | P1 | Integrator（#1，`skills/mining_dfs.py`）| 状态: OPEN（深回程仍失败，井口局部达标与实际出洞需分别确认）

- **D1 集尾切片门禁更新**。回程 `time_slice` 落在剩余预算夹限窗口、回执显示距井口目标超 2 格且同一 actor 到总集限时，末段仍未完成可直接判 FAIL。后续工具、护航动作或生命周期变化出现时保留人工复核。回放 **39** 个终版波，只有 r77 日出从候选变为 **1 个确认失败**。r78 裸装仍按候选处理，轨迹已证实此前到达井口。 [r77 末片](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/20261121-205838-706786.events.jsonl#L452) · [r78 井口到达](../runs/r78_bare_real_drop_60m/hy4_hy_7_bare/20261121-205741-909353.events.jsonl#L546) · [夹限](../src/mc_agent/harness/mc_tools.py#L3070) · [D1 门禁](../../s-triage/scripts/d1_ticket_gates.py#L344)

- **r78 裸装排除一条深回程超时归因**。HY4_7 EP14 三次 `surface_reached` 到 Y60／68／68。末次到达后重置矿树，做四次移动，最后一次因集限被拒。该集是取材和反复下探后的超时，深回程归因 **0/1**。整波推进／到达仍为 **6/3**。 [末次到达](../runs/r78_bare_real_drop_60m/hy4_hy_7_bare/20261121-205741-909353.events.jsonl#L546) · [重置](../runs/r78_bare_real_drop_60m/hy4_hy_7_bare/20261121-205741-909353.events.jsonl#L569) · [末次移动](../runs/r78_bare_real_drop_60m/hy4_hy_7_bare/20261121-205741-909353.events.jsonl#L674)

- **r77 日出确认深回程超时 1/1**。DS_2 EP09 共 15 次 `climb_out` 合 **625.085s**，前 14 次推进 Y−41→50，目标 Y98，最后一段零进展。距集限余 **6.21s** 开始的最后一段耗 **0.106s**，回 `time_slice`，随后 688.507s 超时，仍差 48 格。波内推进／到达 **14/1**，净降 0/16。 [首段](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/20261121-205838-706786.events.jsonl#L60) · [最后推进](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/20261121-205838-706786.events.jsonl#L424) · [末片](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/20261121-205838-706786.events.jsonl#L452) · [终态](../runs/r77_day12_real_drop_60m/nju_ds_2_bare/20261121-205838-706786.events.jsonl#L460)

- **r76 日出回程 14/10、净降 1/28**。HY4_3 EP05 从 Y20 上到 25，下一片 `time_slice/timeout_path=dig` 结束在 Y22，目标 Y50。改北向楼梯时遇流体，再换隧道遇落脚不安全，以 `answered` 未验证收尾。深回程超时候选 0／4。 [首段](../runs/r76_day12_real_drop_60m/hy4_hy_3_bare/20261121-192614-801283.events.jsonl#L296) · [净降](../runs/r76_day12_real_drop_60m/hy4_hy_3_bare/20261121-192614-801283.events.jsonl#L315) · [流体](../runs/r76_day12_real_drop_60m/hy4_hy_3_bare/20261121-192614-801283.events.jsonl#L357) · [中断回执](../src/mc_agent/skills/mining_dfs.py#L5422)

- **r77 裸装回程 16/2**。八条推进来自 HY4_5 与 MoveTest 两集 Day 2 寻村的地下回程，出洞均未确认。HY4_5 EP06 四段 Y14→42，井口目标 Y49，后续朝北的台阶被围栏拦截，换南和东也耗时，300s 集超时。MoveTest EP06 四段 Y16→48，目标 Y62，下一片和次集开头均 `climb_blocked`。关联既有 BUG-051，后续需区分派单是否改进与回程本身的可达性。 [HY4_5 末次推进](../runs/r77_bare_real_drop_60m/hy4_hy_5_bare/20261121-193209-147212.events.jsonl#L61) · [围栏拒绝](../runs/r77_bare_real_drop_60m/hy4_hy_5_bare/20261121-193209-147212.events.jsonl#L81) · [MoveTest 同集回程阻断](../runs/r77_bare_real_drop_60m/nju_movetest_01_bare/20261121-193236-516465.events.jsonl#L105)

- **r81 中期净降 1/5**。HY4_2 EP04 三段回程 Y−55→−46→−48→−35，目标 Y24，配对共约 **149.4s**。第二段 `time_slice/timeout_path=dig`，目标挖掘点为 `[10956,-46,-10884]`。两条 `surface_reached` 均其他集就地到本井目标，汇总进度／到达 **2/2** 掩盖该深回程未完成。EP04 为 `answered` 且未验证，深回程超时门本波没有触发。 [首段](../runs/r81_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-191657-103991.events.jsonl#L799) · [净降](../runs/r81_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-191657-103991.events.jsonl#L816) · [末段](../runs/r81_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-191657-103991.events.jsonl#L832) · [中断回执](../src/mc_agent/skills/mining_dfs.py#L5422)

- **r73 官方 5Ease 又见下降却回执达标**：EP01 开始 Y21、目标 Y21，工具 **41.1s** 结束于 Y19，`surface_reached`。源码容忍目标下 2 格。后续采木不可达，终态未验证，回程净降门 **1/4**。 [净降回执](../runs/official_r73_80m_5ease/5Ease_Bot/20261121-131907-753366.events.jsonl#L259) · [局部判定](../src/mc_agent/skills/mining_dfs.py#L3200)

- **r74 裸装回程净降 1/16**：HY4_7 EP03 中等铁课 Y26→25，`time_slice`，目标 Y52；余 3.123s 的最后回程被拒。深课 EP08 回到 Y61、目标 Y54，随后继续下探，最后被拒的是 `explore`，1080s 的粗筛候选归为采矿尾部。回程进度／到达 **9/6** 仍超门。 [净降](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/20261121-132424-527442.events.jsonl#L396) · [深课到达](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/20261121-135547-504568.events.jsonl#L490) · [再下探](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/20261121-135547-504568.events.jsonl#L515) · [末次采矿余量门](../runs/r74_bare_real_drop_60m/hy4_hy_7_bare/20261121-135547-504568.events.jsonl#L779)

- **r77 同波回程切片 8/1**：HY4_1 EP04 在 Y−32 开始，四段到 Y63，目标 Y56。重开探井后再从 Y15 三段到 Y49，仍低于目标，下一次课程机会使集尾进入 `paused`。需要把深课超时门和切片耗用分别验收。 [首次井口到达](../runs/r77_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-132552-213004.events.jsonl#L349) · [第二次末段](../runs/r77_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-132552-213004.events.jsonl#L567) · [暂停终态](../runs/r77_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-132552-213004.events.jsonl#L570)

- **r72 日出铁课回程失败**：DS_2 EP05 先挖到 Y16，五段回撤到 Y63，目标 Y103，回撤配对 **122.9893s**。余 **2.108s** 的最后调用拒绝，600s 结束。一般回程进度／到达 **11/1**，正式深课筛选本轮无样本。 [首段](../runs/r72_day12_real_drop_60m/nju_ds_2_bare/20261121-130754-465144.events.jsonl#L453) · [末段](../runs/r72_day12_real_drop_60m/nju_ds_2_bare/20261121-130754-465144.events.jsonl#L525) · [余量门](../runs/r72_day12_real_drop_60m/nju_ds_2_bare/20261121-130754-465144.events.jsonl#L543) · [集尾](../runs/r72_day12_real_drop_60m/nju_ds_2_bare/20261121-130754-465144.events.jsonl#L551)

- **r73 裸装深课回到井口后集限**：HY4_7 EP02 三段 Y−54→−21→11→49，工具目标 Y35，配对合计 **155.8075s**，回井口时余 **12.541s**。随后采木无目标，最后楼梯入门拒绝，集限 1080s。粗筛候选的本次井口回程已完成，开阔地表仍未确认。 [深井首段](../runs/r73_bare_real_drop_60m/hy4_hy_7_bare/20261121-122112-596487.events.jsonl#L551) · [局部到达](../runs/r73_bare_real_drop_60m/hy4_hy_7_bare/20261121-122112-596487.events.jsonl#L591) · [末秒楼梯](../runs/r73_bare_real_drop_60m/hy4_hy_7_bare/20261121-122112-596487.events.jsonl#L629) · [井口目标](../src/mc_agent/skills/mining_dfs.py#L3220)
- **r75 门户一条深课超时候选已排除回程归因**：NJU_3 EP04 的 `climb_out` 在 **42.478s** 从 Y56 到 Y60，收到 `surface_reached`。随后重开下探，最后一个 `explore` 在 Y−20，1800s 结束。末段耗时来自重复探井，深回程行为仍缺另一次有效暴露。 [回到目标高度](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/20261121-122230-392784.events.jsonl#L158) · [末次探井](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/20261121-122230-392784.events.jsonl#L1522) · [终态](../runs/r75_midgame_portal_40m/nju_nju_3_midgame/20261121-122230-392784.events.jsonl#L1530)

- **r74 门户深课确认回程失败 1/2**：HY4_9 EP12 附魔课九个探井切片后 reset 要求回地表，两次回撤 Y−53→−26，目标 Y55。余 **1.879s** 的第三次回撤被拒，集尾 **599.211s** 超时。全波进度／到达 **5/1**，净降 **0/6**。 [reset](../runs/r74_midgame_portal_40m/hy4_hy_9_midgame/20261121-120211-036329.events.jsonl#L294) · [末段](../runs/r74_midgame_portal_40m/hy4_hy_9_midgame/20261121-120211-036329.events.jsonl#L335) · [余量门](../runs/r74_midgame_portal_40m/hy4_hy_9_midgame/20261121-120211-036329.events.jsonl#L370) · [深井入口](../src/mc_agent/skills/mining_dfs.py#L3209)

- **r72 裸装深课回程遭水顶截断**：MoveTest EP16 两段 Y14→32，第三段 `climb_blocked`，目标 Y66。两次解卡报 `unsafe_water_roof`，换楼梯后多次遇流体，最后 Y22 被近战击杀，Y80 仅重生回执。+188.277s 以集限退出，回程本身未达。 [阻断](../runs/r72_bare_real_drop_60m/nju_movetest_01_bare/20261121-120757-945636.events.jsonl#L95) · [水顶](../runs/r72_bare_real_drop_60m/nju_movetest_01_bare/20261121-120757-945636.events.jsonl#L109) · [死亡与重生](../runs/r72_bare_real_drop_60m/nju_movetest_01_bare/20261121-120757-945636.events.jsonl#L351) · [回程源码](../src/mc_agent/skills/mining_dfs.py#L3209)

- **r74 `surface_reached` 实为本井口高度门**：DS_1 EP07 从 Y−39 回到 −20，距本次目标 Y−18 两格，工具给 `surface_reached`。随后仍在负高度找材料和继续楼梯，最后已知 Y−16，394.925s 超时。源码把井口上界减 16 当目标。深回撤超时候选按工具门已到，真实地表可达性需地形或开阔落点实证。 [首次推进](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-114312-400750.events.jsonl#L338) · [工具达标](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-114312-400750.events.jsonl#L380) · [末次 Y−16](../runs/r74_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-114312-400750.events.jsonl#L600) · [井口基准](../src/mc_agent/skills/mining_dfs.py#L3218)

- **r71 裸装二次回程停于门槛外**：HY4_7 EP09 初次在 Y52 达标，二次回程 13 段配对 **549.417s**，先有 `timeout_path=dig` 导致 Y27→26，最后 Y51，目标 Y54，仍差一格才进容差。末次余量门拒绝，`answered` 且验证为假。全波进度／到达 **27/6**、净降 **1/35**。 [第一次达标](../runs/r71_bare_real_drop_60m/hy4_hy_7_bare/20261121-104728-302383.events.jsonl#L170) · [净降](../runs/r71_bare_real_drop_60m/hy4_hy_7_bare/20261121-104728-302383.events.jsonl#L302) · [末次位置](../runs/r71_bare_real_drop_60m/hy4_hy_7_bare/20261121-104728-302383.events.jsonl#L514) · [集尾](../runs/r71_bare_real_drop_60m/hy4_hy_7_bare/20261121-104728-302383.events.jsonl#L568) · [到达口径](../src/mc_agent/skills/mining_dfs.py#L3200)

- **r72 门户净降并耗尽回程**：HY4_8 EP05 五段 `climb_out` 内，先从 Y−31 到 −23，后一次挖掘 `time_slice` 从 Y−23→−25。改用楼梯与隧道，绕过两次流体判定，最终到 Y24，目标 Y42。末次楼梯因余量拒绝，+507.443s 超时。全波进度／到达 **3/0**，净降 **1/5**，人工核定唯一深课回程耗尽集限。复验超时出口净降及回撤换路能否达地表。 [首次](../runs/r72_midgame_portal_40m/hy4_hy_8_midgame/20261121-103932-286640.events.jsonl#L33) · [回撤净降](../runs/r72_midgame_portal_40m/hy4_hy_8_midgame/20261121-103932-286640.events.jsonl#L111) · [末次实际爬升](../runs/r72_midgame_portal_40m/hy4_hy_8_midgame/20261121-103932-286640.events.jsonl#L305) · [集尾](../runs/r72_midgame_portal_40m/hy4_hy_8_midgame/20261121-103932-286640.events.jsonl#L336) · [回程逻辑](../src/mc_agent/skills/mining_dfs.py#L3209)

- **r71 门户深课人工核验**：NJU_3 EP04 回程要求在 +1736.365s 首现，末次实际回撤 **27.523s**，Y−50→−44，目标 Y82。之后只有同一次模型请求的三条 429 重试，+1800.024s 集限，直到收尾均无第二次回撤。回程未完成与退避同时发生。全波进度／到达 **2/0**，两条深课中一条回程未完成即到集限。 [回撤提示](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2713) · [末次回撤](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2756) · [三次重试](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2762) · [集尾](../runs/r71_midgame_portal_40m/nju_nju_3_midgame/20261121-093550-660037.events.jsonl#L2767) · [回程源码](../src/mc_agent/skills/mining_dfs.py#L3209)

- **r68 日出普通铁器回程**：对照 r67 同四角色，进度／到达 **3/0→10/2**、三连击 **1→2**、净降 **0/12**。HY4_3 EP10 三段 **104.720s**，Y16→37／目标81。DS_2 EP09 四段 **171.179s**，Y25→66／目标108，随后一次 429 退避消耗余量。两集均超时且欠目标高度，深课专项无实际暴露。 [HY4_3 末段](../runs/r68_day12_real_drop_45m/hy4_hy_3_bare/20261121-092852-151315.events.jsonl#L432) · [DS_2 末段](../runs/r68_day12_real_drop_45m/nju_ds_2_bare/20261121-094037-366066.events.jsonl#L541) · [DS_2 集尾退避](../runs/r68_day12_real_drop_45m/nju_ds_2_bare/20261121-094037-366066.events.jsonl#L547) · [到达判定](../src/mc_agent/skills/mining_dfs.py#L3200)

- **r71 中期深集实败 1/2**：HY4_1 EP13 入口只剩 **55.7858s**。+15.9635s 的探矿回执要求回撤，+23.1228s 开始，耗 **24.3570s**，Y6→13，目标 Y34。末次只余 **3.1660s** 被拒，+55.9338s 超时。全波进度／到达 **3/3**、净降 **0/6**，深回程门仍失败。复验波尾深集派发时的回撤余量。 [真实集限](../runs/r71_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-094246-048172.events.jsonl#L1) · [推进](../runs/r71_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-094246-048172.events.jsonl#L121) · [拒绝](../runs/r71_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-094246-048172.events.jsonl#L138) · [波尾预算钳制](../src/mc_agent/harness/curriculum.py#L2529)

- **r69 官方普通铁器回程**：GLM_1 实际模型 `hy4-flash-free`，进度／到达 **16/0**，三连击 **3 集**。EP06 Y15→26→34→35，目标 Y58，末次 `climb_out` 被余量门拒绝，599.640s 时 `answered`，目标验证为假。普通铁器课缺深课暴露，该现象按未完成回程记录。 [首段](../runs/official_r69_80m_glm1/GLM_1/20261121-081411-496473.events.jsonl#L281) · [末段](../runs/official_r69_80m_glm1/GLM_1/20261121-081411-496473.events.jsonl#L326) · [余量拒绝](../runs/official_r69_80m_glm1/GLM_1/20261121-081411-496473.events.jsonl#L349) · [集尾](../runs/official_r69_80m_glm1/GLM_1/20261121-081411-496473.events.jsonl#L361) · [DFS 余量门](../src/mc_agent/harness/mc_tools.py#L2991)

- **r68 探索同三槽**：`climb_progress/surface_reached` **5/1→1/2**。HY4_2 EP05 首次回撤 Y29→23，`time_slice`、`method=interrupted`，随后 Y23→55 达标。再次下探后受敌怪抢占，第二次回程最终也到 Y59，随后进井、受击、撤退和浮水，最后超时。该深挖候选确认回程完成，净降门 **1/4** 仍失败。 [净降回执](../runs/r68_expl_mem_iron_3bot/hy4_hy_2_iron/20261121-073645-791746.events.jsonl#L51) · [首次到达](../runs/r68_expl_mem_iron_3bot/hy4_hy_2_iron/20261121-073645-791746.events.jsonl#L73) · [第二次到达](../runs/r68_expl_mem_iron_3bot/hy4_hy_2_iron/20261121-073645-791746.events.jsonl#L380) · [优先旧路](../src/mc_agent/skills/mining_dfs.py#L3228)
- **r69 中期三槽**：进度／到达 **3/2**，三连击一集，净降 0/5。深回程候选 HY4_1 EP07、DS_1 EP08 均先到地表门槛再下探，人工核定回程遇集限 **0/2**。 [HY4_1 到达](../runs/r69_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-081556-310680.events.jsonl#L163) · [HY4_1 再下钻石井](../runs/r69_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-081556-310680.events.jsonl#L196) · [DS_1 到达](../runs/r69_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-081519-478615.events.jsonl#L76)
- **r67 日出普通铁器集**：DS_2 EP10 先用六段 DFS 采到煤、零新增粗铁。找不到最近树节点且 reset 要求回地表，三次回撤共 **154.293s**，Y31→43→55→70，最后位置回执 Y71，目标 Y89，随后遇战斗并在原集超时。全波进度／到达 **3/0**，净降 **0/3**，深课样本缺失。 [找不到节点](../runs/r67_day12_real_drop_45m/nju_ds_2_bare/20261121-084620-188339.events.jsonl#L288) · [第一段回撤](../runs/r67_day12_real_drop_45m/nju_ds_2_bare/20261121-084620-188339.events.jsonl#L344) · [末段回撤](../runs/r67_day12_real_drop_45m/nju_ds_2_bare/20261121-084620-188339.events.jsonl#L402) · [集尾](../runs/r67_day12_real_drop_45m/nju_ds_2_bare/20261121-084620-188339.events.jsonl#L421)
- **r69 裸装深回程实败 1/2**：HY4_7 EP08 首次回到 Y50（目标 Y52 的容差内），后续十次 DFS 零钻石，再从 Y−46 触发回撤，剩约 28 秒。最后一次实跑 Y−46→−33，目标 Y48，下一次因剩余不足 6 秒被余量门拒绝，整集超时。另一深课 HY4_5 EP08 回程三段 Y−26→15，目标 Y19，因 `lost_iron_pickaxe` 中止。全波进度／到达 **13/3**，净降 **0/18**。在真实深回撤已打开时复验末次目标是否到达，避免同集早期到达掩盖末段失败。 [首次到达](../runs/r69_bare_real_drop_3bot/hy4_hy_7_bare/20261121-085446-662461.events.jsonl#L66) · [第二次回撤触发](../runs/r69_bare_real_drop_3bot/hy4_hy_7_bare/20261121-085446-662461.events.jsonl#L309) · [余量门](../runs/r69_bare_real_drop_3bot/hy4_hy_7_bare/20261121-085446-662461.events.jsonl#L345) · [丢镐集尾](../runs/r69_bare_real_drop_3bot/hy4_hy_5_bare/20261121-085334-928547.events.jsonl#L88) · [到达判定](../src/mc_agent/skills/mining_dfs.py#L3200)

- **r67 探索三槽**：五条进度全部在 DS_1 EP05，**228.865s** 的回程从 Y−53 到 Y69，末次报告 `method=interrupted,reached=false`，高度已超过 Y56。中断处理仅在未标为入水／岩浆时发 `climb_progress`，候选阻断字段缩至落地、存活、就绪，回执缺这三个终态值。最后一次调用因余量不足 6s 跳过，整集 420.064s 超时。 [第一段](../runs/r67_expl_mem_3bot/nju_ds_1_iron/20261121-071202-802321.events.jsonl#L179) · [最后一段](../runs/r67_expl_mem_3bot/nju_ds_1_iron/20261121-071202-802321.events.jsonl#L299) · [末次拒绝](../runs/r67_expl_mem_3bot/nju_ds_1_iron/20261121-071202-802321.events.jsonl#L335) · [到达判定](../src/mc_agent/skills/mining_dfs.py#L3200) · [中断分支](../src/mc_agent/skills/mining_dfs.py#L5429)

- **r65 Day1/Day2**：回程进度／到达 **23/6→6/1**、三连击 **3→2**、净降 **0/7**。HY4_4 EP08 从 Y16 四段至 Y52，抵达目标 Y54 的两格容差后开始斥候。EP10 三段约 142.836s 从 Y12 到 Y45，未到目标 Y54；末次被余量门阻止，集尾 `answered` 且验证假。深回程超时门本波为 0，该集另记为回程未完成。 [EP08 到达](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-070853-205065.events.jsonl#L67) · [EP10 末段](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-071720-647314.events.jsonl#L180) · [EP10 集尾](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-071720-647314.events.jsonl#L280)
- **一句话**：`mine_dfs(action="climb_out")` **回不到地表**：单集内连续 `climb_progress` 最多 16 次，深挖后必然在回程超时。
- **实测（6 波）**：
  - `climb_progress` **400 次** + `climb_blocked` 40 次，而 `surface_reached` 仅 **102 次**（成功率 ~20%）
  - **58 个 episode 出现 ≥3 次连续 climb_progress**，累计 **13,113s = 3.6 小时**
  - 最长：`glm_4` 一段 **10 连击 839s**、`nju_guitest` **16 连击 424s**、`nju_movetest` 8 连击 431s
- **为什么爬不出来（量化）**：331 个 `climb_progress` 样本 → **每次中位 +5 格 / 29.3s ≈ 7 秒/格**。`nju_guitest` EP37 从 **y=-48** 开始，16 次调用 458s 只到 **y=1**，仍未到地表（collar-16≈48）→ 需要 ~96 格 × 7s ≈ **672s > 600s 集预算**。**数学上不可能完成**。
- **源码定位**：`mining_dfs.py:3022 _climb_out(deadline, max_attempts=40)`，每次 `staircase(up,1)` 都要 `get_block + dig + place + 位置确认` 3~4 次 HTTP（≈7s/格）。
- **最小修复**：① `explore` 下潜深度按 episode 剩余预算**上限钳制**（`max_depth = min(target_y, start_y - remaining_s/7*0.6)`）；② `climb_out` 用"垂直垫高"（每格 1 次 place，≈1~2s/格）替代阶梯；③ 集预算剩余 <35% 时**强制回程**（在 `mine_dfs` 内部触发，不等模型）。
- **验收标准**：`climb_progress`/`surface_reached` ≤ 1:1；`≥3 连击` 的 episode ≤5/波；深挖集不再因回程超时。
- **r31_short 残余**：`climb_progress/surface_reached` 为 **9/7**。HY4_2 EP03 的采矿树从 29 节点归零，后续四次回程共 **107.74s**，Y 从 −54 到 −40，仍距目标 Y36 甚远。四集超时初筛里另有两集启动回程太晚且未完成，一集已到地表但越过预算。 [HY4_2 EP03 树归零](../runs/r31_short/hy4_hy_2_iron/20261119-221109-705383.events.jsonl#L269) · [第四次回程](../runs/r31_short/hy4_hy_2_iron/20261119-221109-705383.events.jsonl#L368) · [回程实现](../src/mc_agent/skills/mining_dfs.py#L3069)
- **r32_short 复核**：回程比 **0/3**，三条深挖超时候选均在集时限前进入 `surface_reached`。HY4_2 EP02/03 随后再次下探，GUITest EP03 在地表开始下一次生成后超时。仍待加载新回程修复的对照。 [HY4_2 EP02](../runs/r32_short/hy4_hy_2_iron/20261119-222315-781768.events.jsonl#L90) · [HY4_2 EP03](../runs/r32_short/hy4_hy_2_iron/20261119-223301-232412.events.jsonl#L332) · [GUITest EP03](../runs/r32_short/nju_guitest_01_nether/20261119-222430-023563.events.jsonl#L1329)
- **r31_long/r33_short 复验失败**：回程比分别 **13/3、11/6**。r31 GLM5 EP08 三次回程耗时 164.929s，从 Y−55 到 −31，目标 Y−12，最终超时。r33 GLM3 EP04 在约 44s 余量下执行 83.776s 回程，到 Y49/59，越过集时限。r33 四条候选中有两条带未完成回程，另两条已返回地表后再下探。 [r31 GLM5 EP08](../runs/r31_long/glm_glm_5_midgame/20261119-223935-867850.events.jsonl#L176) · [r33 GLM3 EP04](../runs/r33_short/glm_glm_3_village_trade/20261119-224742-727958.events.jsonl#L141) · [r33 DS2 EP04](../runs/r33_short/nju_ds_2_cave_combat/20261119-225515-225063.events.jsonl#L217)
- **r38→r39**：回程比 **11/5→4/6**。r38 HY4_2 EP02 从 Y−12 连续六次到 Y31，仍距目标 Y51，随后超时。r39 三个深挖超时初筛全部已到地表后再下探，≥3 连击集为 **1**。r39 短波过门槛，更深的 r38 型回程暴露待新版本长波。 [r38 第六次回程](../runs/r38_short/hy4_hy_2_iron/20261120-004253-882042.events.jsonl#L411) · [r39 HY4_9 EP03 归地表](../runs/r39_short/hy4_hy_9_deep_water/20261120-011527-319926.events.jsonl#L114) · [回程入口](../src/mc_agent/skills/mining_dfs.py#L3137)
- **r40 复验**：回程比 **3/1**，超过门槛。HY4_2 EP02 已走到 Y52 后再次探矿才超时。HY4_3 EP08 在波末仅分到 **92.32s**，战斗抢占了首次爬升，后续只到 Y23，最后一次回程因余量不足被拒，收尾时未完成回程。 [HY4_2 回到地表](../runs/r40_hy4_combat/hy4_hy_2_iron/20261120-051923-764129.events.jsonl#L564) · [HY4_3 末次拒绝](../runs/r40_hy4_combat/hy4_hy_3_midgame/20261120-053640-739752.events.jsonl#L184)
- **r42 Day 2 复验**：回程比 **8/3**。DS_1 EP03 连续 5 次回程共约 120s，从 Y−26 到 Y4，距目标 Y21 仍差 17 格。下一集延续矿道并到达回程阈值，随后再下探。其余两条深挖超时候选均已回到地表后继续采矿。 [首次回程超时](../runs/r42_day2_village_9bot/nju_ds_1_iron/20261120-063526-419884.events.jsonl#L361) · [下一集完成回程](../runs/r42_day2_village_9bot/nju_ds_1_iron/20261120-064426-706446.events.jsonl#L102)
- **r43 Day 2 复验**：回程比 **8/3→2/2**，≥3 连击集 **2→0**。唯一的深挖回程超时候选 NJU_3 EP04 在 +87.1s 已 `surface_reached`，随后继续五片单独的 `explore` 才到时限。实测两集有深挖回程，未见回程致超时。 [回到地表](../runs/r43_day2_village_near_9bot/nju_nju_3_day2_village_near/20261120-073021-002663.events.jsonl#L89) · [再次开井](../runs/r43_day2_village_near_9bot/nju_nju_3_day2_village_near/20261120-073021-002663.events.jsonl#L132) · [集尾](../runs/r43_day2_village_near_9bot/nju_nju_3_day2_village_near/20261120-073021-002663.events.jsonl#L376)
- **r44 Day 2**：回程 **1/1**、连续三次集 **0**。唯一深挖回程候选 HY4_6 EP03 在 +389.3s 已 `surface_reached`，随后开井三片，末次不足 6s 被拒后才到集限。 [回程完成](../runs/r44_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-074558-153223.events.jsonl#L178) · [末次拒绝](../runs/r44_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-074558-153223.events.jsonl#L267)
- **r45 回程回潮**：有终版的 Day 2 轨迹回程比 **4/2**，DS_1 EP04 先两次探矿失败和一次地下 reset 被拒，随后自 Y−28 开始，三片只达 Y9／目标 Y47，还差 **38 格**。末次在只余约 3s 时被拒，确属回程未完成。其他两个带回程的超时集（HY4_8、NJU_3）均先返回地表再继续。 [DS_1 末次推进](../runs/r45_day2_wild_scout_9bot/nju_ds_1_iron/20261120-085117-337871.events.jsonl#L218) · [到集限](../runs/r45_day2_wild_scout_9bot/nju_ds_1_iron/20261120-085117-337871.events.jsonl#L256)
- **r46**：Day 2 比 **6/1**，三条深挖候选有两条真实未回地面。HY4_7 EP04 连同参数 `climb_out` 先 Y−9→21，接着 **21→15→−3**、损失 24 格且分别报 `time_slice`，目标 Y61。首次与第一次倒退仍是同一单节点树，次次倒退后才出现 24 个节点。优先定位回程重入为何接受下行路径，给连续调用保留已验证高度和可继续回程的状态。DS_1 EP04 到 Y30／目标48 停滞。HY4_10 EP03 四片推进后已回到目标容差内，再下井时超时。 [首次上升](../runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L293) · [倒退回执](../runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L340) · [回程候选](../src/mc_agent/skills/mining_dfs.py#L3167)
- **候选修复与验收**：集成 `16df57e` 在回程时跳过低于当前站位的现役矿口与低位归档矿口。`50adb01` 在初次推进小于剩余爬升且预算低于 55s 时建议下一片 55s。现役、归档两条防倒退夹具及继续预算夹具均过。D1 新门禁回放 r46 Day 2 为 **2/12** 条 `climb_out` 净降高，第一条失败在 HY4_7 EP04 L314。首次爬到 Y21 的回执和第一次退回 Y15 的回执均保留同一个现役矿口 `(-7877,-9,2666)`，低于重入站位 30 格。旧版入口有该 ID 即尝试走回矿口，现场具备修复所拦条件。新版实机还需出现低位矿口分支，并核对净降、回程比、真实回程超时。 [现役及归档夹具](../tests/test_mining_dfs.py#L2443) · [继续预算夹具](../tests/test_mining_dfs.py#L2548) · [r46 倒退](../runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-091401-945732.events.jsonl#L314)
- **r47 已加载 `16df57e`、`50adb01`**：Day 1/2 净降高 **0/3、0/12**，Day 2 回程比 **6/1→9/2** 仍超门槛。三个深挖超时候选中，一条确为回程未完。HY4_6 EP03 四片共 **114.576s**，Y−2→7→9→10→10，距目标 Y59 仍差 49 格，实际收到 55s 提示后第四片因集尾只执行约 2.5s。EP04 延续自 Y10 出地表容差至 Y57。实机三次重入时同一现役矿口为 Y15，起始 Y16、28、39，分别继续升至 Y28、39、50。HY4_6 和 HY4_10 的 EP04 都在已回地面后另开探矿才超时。 [低位现役矿口与连续爬升](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-101213-237538.events.jsonl#L76) [提示](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-100313-044535.events.jsonl#L263) · [末次回程](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-100313-044535.events.jsonl#L287) · [下一集回地表](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-101213-237538.events.jsonl#L125) · [HY4_10 回地表](../runs/r47_day2_full_day_scout_9bot/hy4_hy_10_iron/20261120-100512-901208.events.jsonl#L98)
- **r48／r49**：r48 下界回程比 **1/0**、净降 **0/2**，DS_1 EP02 到 Y17 后目标 Y57，反复被流体或基岩挡住至超时。r49 裸装回程比 **9/4**、净降 **0/16**，唯一深挖候选 NJU_3 EP04 七片从 Y15 升到 Y40／目标 Y44，后两片无推进，最终在同一站位被重复失败保护判停。两轮都有真实未完成回程。 [DS_1 阻断](../runs/r48_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-102706-380097.events.jsonl#L1104) · [NJU_3 第七片](../runs/r49_day1_bare_9bot/nju_nju_3_bare/20261120-105731-751977.events.jsonl#L329) · [重复失败保护](../runs/r49_day1_bare_9bot/nju_nju_3_bare/20261120-105731-751977.events.jsonl#L419)
- **r50**：下界回程比 **1/0→1/1**，净降 **0/3**。NJU_3 EP02 从 Y15 爬到 Y21、目标 Y48，后续爬升同高 `climb_blocked`，本集仍确认为未返回而超时，保持 OPEN。 [阻断回执](../runs/r50_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-205332-322572.events.jsonl#L286)
- **r51 同槽实机通过**：回程比 **1/1→2/4**、≥3 连续进度集 **0**、净降 **0/6**、深挖回程超时 **1/2→0/3**。NJU_3 与 r50 同从 Y15 向目标 Y48，r50 停 Y21，r51 两片 **15→32→53**，回执到达。HY4_8 同波也从 Y15 回到目标 Y37 容差。 [NJU_3 回到地表](../runs/r51_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-211130-521338.events.jsonl#L147) · [HY4_8 回程](../runs/r51_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-210128-432821.events.jsonl#L662)
- **r52 Day 2 回程比失守**：HY4_10 EP05 在 Y−54 探矿遇 `shaft_exhausted`，距 277.850s 集限仅 **16.961s** 才首次发起回程。8.801s 内升至 Y−53，目标 Y78。下一次仅余 **5.811s**，被 6s 的开工门槛拒绝。终态 `answered`，所以状态门禁报 0 个回程超时，但原始证据确认未完成回程。该集仍保持净升高；下一轮复验需要在预算尚够时触发回程，并逐条核对深挖集的终止时站位。 [集预算](../runs/r52_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-214756-483939.events.jsonl#L3) · [首次回程](../runs/r52_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-214756-483939.events.jsonl#L203) · [重试拒绝](../runs/r52_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-214756-483939.events.jsonl#L228) · [终态](../runs/r52_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-214756-483939.events.jsonl#L262) · [回程实现](../src/mc_agent/skills/mining_dfs.py#L3171)
- **r53 Day 1**：回程比 **6/2** 仍失败，净降 **0/8**。HY4_3 EP04 四片爬升 Y24→100，在 +593.026s 已 `surface_reached`，之后再次探矿被集尾护栏拒绝，超时发生在回地表后。HY4_2 EP03 仅返回 Y18，EP04 延续后到达地表，再下挖时近战死亡。两条起始高度都未进入深挖超时门槛的样本范围。 [HY4_3 回地表](../runs/r53_day1_bare_3bot/hy4_hy_3_bare/20261120-220016-573157.events.jsonl#L370) · [HY4_2 跨集续爬](../runs/r53_day1_bare_3bot/hy4_hy_2_bare/20261120-220642-668999.events.jsonl#L109)
- **r53 下界**：四项通过，回程比 **0/1**，三连击集 0、净降 0/1、深挖回程超时 0/1。仅一条完整回执，HY4_8 EP01 用 **16.228s** 从 Y44 升到 Y48，进入目标 Y50 的容差。HY4_4 EP01 另有两次回程被实际岩浆救援抢占，第二次死亡，未计入具备起终坐标的回程。主世界状态仍 OPEN。 [完整回程](../runs/r53_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-215412-442389.events.jsonl#L567) · [岩浆抢占与死亡](../runs/r53_nether_fortress_9bot/hy4_hy_4_nether_fortress/20261120-215404-309252.events.jsonl#L230)

- **r55 下界通过四项**：回程比 **0/3**、三连击 0、净降 **0/3**、深回程超时 **0/2**。HY4_8 EP01 两次分别从 Y41→45、Y30→42，目标 Y40；第二条由目标下方起步。HY4_7 EP02 早已在回程门槛上方，原地返回 `surface_reached`。主世界长回程仍待复验。 [HY4_8 第一条](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261120-234425-383810.events.jsonl#L301) · [HY4_8 第二条](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261120-234425-383810.events.jsonl#L482) · [HY4_7 已在阈值上方](../runs/r55_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261120-235224-139051.events.jsonl#L112)
- **r56 裸装**：回程比 **6/6**、三连击集 1、净降 **0/13**。深回程超时无样本，仍缺 r52 Y−54 的同条件复验；同轮下界无 `climb_out`。 [裸装三连击样本](../runs/r56_day1_day2_bare_5bot/hy4_hy_3_bare/20261121-003435-071379.events.jsonl#L220) · [下界无井口返程](../runs/r56_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261121-003405-365963.events.jsonl#L86)

- **r57 裸装回归**：回程比 **6/6→5/1**，净降 **0/7**，三连击 0，深回程未暴露。HY4_10 EP06 已花 481s 挖矿，首条回程约余 116s，从 Y16 两片到 Y27，随后一片几乎无余量，集尾 Y27 超时。需在选矿井与退出时机上留正向爬升预算。 [首次进度](../runs/r57_day1_day2_fast_4bot/hy4_hy_10_bare/20261121-010436-698952.events.jsonl#L280) · [第二片](../runs/r57_day1_day2_fast_4bot/hy4_hy_10_bare/20261121-010436-698952.events.jsonl#L301) · [集尾](../runs/r57_day1_day2_fast_4bot/hy4_hy_10_bare/20261121-010436-698952.events.jsonl#L349) · [回程判定](../src/mc_agent/skills/mining_dfs.py#L3162)

- **r58 下界四门槛通过**：回程进度／达到 **2/4**，三连击 0，净降 0/6，深回程超时 0/1。NJU_3 EP03 三片 Y15→25→58→60，最后回执确认超过目标 Y48。主世界 r57 同票仍未过。 [首次上升](../runs/r58_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-012740-971475.events.jsonl#L832) · [实际到达](../runs/r58_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-012740-971475.events.jsonl#L881)

- **r59 主世界回程比再失败**：进度／达到 **3/3→3/2**，三连击 0，净降 0/5，深回程缺。 [HY4_10 EP05 回程](../runs/r59_day12_sunrise_village_4bot/hy4_hy_10_bare/20261121-021027-777971.events.jsonl#L182)

- **r60 主世界回程比仍失败**：三次进度／零次到达，净降 0/4。DS_2 EP07 首次真实在 Y16 接到日出任务，回程 Y16→20→33 后阻塞，改用楼梯仍卡支撑。EP09 从 Y26→35 后受波末截止。 [首次派单](../runs/r60_day12_full_45m/nju_ds_2_bare/20261121-030135-415499.events.jsonl#L1) · [卡住回执](../runs/r60_day12_full_45m/nju_ds_2_bare/20261121-030135-415499.events.jsonl#L61) · [末次回程](../runs/r60_day12_full_45m/nju_ds_2_bare/20261121-031515-000911.events.jsonl#L19)

- **r62 主世界深回程实证**：进度／到达 **14/2**，三连击 2 集，净降 **0/19**。HY4_10 EP09 余 **159.827s** 才从 Y−54 开始回撤，入口目标 Y48。三片实际共 **127.335s**，分别到 Y−27、−24、−23。第四片零进展，换阶梯命中刚记录挖掘超时的避让格，末段仍在 Y−23，810s 到期。需在深挖阶段给按剩余高差估计的撤离留时，并在重复卡格时切换回原矿道或安全退路。验收保留回程比 ≤1:1、深回程致超时 0。 [首片](../runs/r62_day12_full_45m/hy4_hy_10_bare/20261121-044646-438094.events.jsonl#L316) · [第三片](../runs/r62_day12_full_45m/hy4_hy_10_bare/20261121-044646-438094.events.jsonl#L357) · [避让挖掘格](../runs/r62_day12_full_45m/hy4_hy_10_bare/20261121-044646-438094.events.jsonl#L398) · [回程部分进度](../src/mc_agent/skills/mining_dfs.py#L5322)

- **r63 主世界**：进度／到达 **14/2→7/4**，三连击 **1 集**，净降 **0/11**，深回程超时 **1→0**。唯一深回程 HY4_3 EP09 的目标 Y80，五片均为进度。先 Y36→64，后一次回到 Y35 再爬，最后一片到 Y71；集尾 808.8s 为 `answered`，缺该集的 `surface_reached` 回执。保持原门槛开放。 [首片](../runs/r63_day12_full_45m/hy4_hy_3_bare/20261121-053411-758050.events.jsonl#L149) · [再次爬升](../runs/r63_day12_full_45m/hy4_hy_3_bare/20261121-053411-758050.events.jsonl#L331) · [最后进度](../runs/r63_day12_full_45m/hy4_hy_3_bare/20261121-053411-758050.events.jsonl#L375)

- **r65 下界回程失败**：HY4_8 EP03 的 `climb_out` 步行从 Y17 到 Y44，目标 Y33，却为 `reached=false`，下一片同站位 `climb_blocked`，随后独立阶梯到 Y50。本波进度／到达 **1/0**、净下降 0/2，深回程超时 0。高度以外还需要就绪、落脚及避开流体，前两条回执缺这些判据的实际值。 [回程进度](../runs/r65_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-061150-917834.events.jsonl#L713) · [回程阻塞](../runs/r65_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-061150-917834.events.jsonl#L734) · [判定源码](../src/mc_agent/skills/mining_dfs.py#L3162)

- **r64 主世界回程**：进度／到达 **7/4→23/6**，净降 0/30，深回程超时 0。DS_2 EP06 的普通铁器课 11 次回程调用共 **435.127s**，从 Y27 到 Y151，回程终态已达，仍挤占 600s 集的大部分时间。本集超时不属于深回程超时门槛。HY4_3 EP09 的深回程两段到 Y85，正常达标。 [DS_2 首段](../runs/r64_day12_full_45m/nju_ds_2_bare/20261121-061531-010755.events.jsonl#L122) · [到达](../runs/r64_day12_full_45m/nju_ds_2_bare/20261121-061531-010755.events.jsonl#L335) · [深回程到达](../runs/r64_day12_full_45m/hy4_hy_3_bare/20261121-062202-963594.events.jsonl#L497)

- **r66 DS_1 石层据点回程目标不一致**：寻村 EP03 任务要求到 Y≥60，`climb_out` 的井口目标为 Y22，在 Y22、零爬升直接返回 `surface_reached`。随后三次地表斥候合计只走 0.30m。全波 059 数值为 0/1，接受的是井口回程，未证实从地下出地表。复验 Day 2 地表逃出时以该任务给定的地表要求核对。 [派单目标](../runs/r66_expl_mem_ds1/nju_ds_1_iron/20261121-062107-503924.events.jsonl#L1) · [零爬升达标](../runs/r66_expl_mem_ds1/nju_ds_1_iron/20261121-062107-503924.events.jsonl#L19) · [第三次斥候](../runs/r66_expl_mem_ds1/nju_ds_1_iron/20261121-062107-503924.events.jsonl#L180) · [井口目标](../src/mc_agent/skills/mining_dfs.py#L3182)

### BUG-060 | P2 | Integrator（#1，`harness/mc_tools.py`）| 状态: FIXED（r65 下界实测攻击预检，过期 0、超大扫描 0）
- **一句话**：实体类目标（发光鱿鱼/动物）会陷入"实体 id 失效 → 重新扫描 → 再失效"的循环。
- **实测**：`no_such_entity` **43 次**（`nju_guitest` 12、`glm_5` 10、`glm_3` 8、`glm_4` 5）；`mc_get_entities` 共 3,493 次，其中 **118 次 radius ≥ 4096**（最大 65,536）。典型（`glm_5` EP39 `make_a_sign_glow`）：radius 256→512→…→65,536 连发 9 次，然后 `move_to(entity_id)` 两次 `no_such_entity`，最后摔死，集满 22 请求 0 分。
- **最小修复**：① `mc_get_entities` 对 `radius>512` 直接拒绝并提示"实体不在渲染距离内，请换目标"；② `move_to(entity_id)` 失败时返回"该实体已消失，请改用坐标或换目标"，并对该类型加 60s 冷却。
- **验收标准**：`no_such_entity` ≤5/波；`radius≥4096` 的 get_entities 为 0。
- **长波仍失败**：`r30_long→r31_long` 过期 ID **1→1**，超大扫描 **5→5**。r31 GLM5 EP06 从 64 倍增到 65536，超大半径段仍反复拿到同一批近处实体。r33 短波为 0/0。集成树 `a896f86` 已补扫描上限回执和半径倍增中断，需加载它的长波验证。 [r31 GLM5 EP06](../runs/r31_long/glm_glm_5_midgame/20261119-223015-242650.events.jsonl#L89) · [扫描上限](../src/mc_agent/harness/mc_tools.py#L200)
- **同场景验收**：加载 `a896f86` 的 `r32_long` 过期 ID **1**、超大扫描 **0**（r31 长波为 **1/5**），最大请求半径降到 256。GLM5 EP06 同为发光告示牌，出现两次 64m 空扫描后返回换区提示，此后扫描半径最高 96。r35 短波为 **3/0**，也过门槛。 [r32 GLM5 EP06 换区提示](../runs/r32_long/glm_glm_5_midgame/20261119-231713-963826.events.jsonl#L176) · [过期 ID](../runs/r32_long/glm_glm_5_midgame/20261119-231713-963826.events.jsonl#L247)

- **r64 要塞实测**：同三槽 `no_such_entity` **4→7**，超过阈值 5。七次均为 `mc_attack` 且 ID 各异，超大扫描为 0。MoveTest EP01 与 EP04 分别在扫描后的 24.5s、19.6s 请求攻击。HY4_1 EP03 则在一次状态回执的 4.7s 后攻击失败。`mc_attack` 的预检只覆盖近期已观测且 >32m 的实体，已有过期目标重规划仍需覆盖攻击入口。复验保留 ≤5 条和 0 次半径 ≥4096。 [MoveTest EP01 扫描](../runs/r64_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-050406-518549.events.jsonl#L122) · [攻击失败](../runs/r64_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-050406-518549.events.jsonl#L209) · [HY4_1 EP03 攻击失败](../runs/r64_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-052028-300275.events.jsonl#L298) · [攻击预检](../src/mc_agent/harness/mc_tools.py#L2008)

- **修复候选**：`9ba4658` 已合入，补查过时的攻击目标，并为目标在复核后消失复用旧 ID 提示。相关离线测试 **7 项通过**，r64 要塞启动早于此提交，待后续波次加载后复验。 [攻击复核](../src/mc_agent/harness/mc_tools.py#L2008) · [离线夹具](../tests/test_skills_and_providers.py)

- **r65 下界实测**：5/5 终版均加载 `ef66856`，11 次攻击、1 次实体移动，后端 `no_such_entity=0`，超大扫描 0。NJU_3 EP05 的攻击返回新预检 `target_not_visible` 与 `requires_replan=true`，要求重新查询实体。`d1_compare.py r64_nether_fortress_5bot r65_nether_fortress_5bot` 已跑，同下界前后两条原始门槛均为 0，新增分支实际触发一次。r64 要塞的同场对照缺加载补丁的终版。 [新预检回执](../runs/r65_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-061700-582549.events.jsonl#L917) · [预检源码](../src/mc_agent/harness/mc_tools.py#L2020)

### BUG-061 | P1 | Integrator（课程工具授权）+ Slice C（交互回执）| 状态: OPEN（过夜 6 波复核）

- `fishy_business` 共 **0/18 集验证通过**，分布在 4 条轨迹。18 集中 `mc_use_item` 调用为 **0**。`part2/ds_ds_1_bare` EP14 已合成鱼竿，对 `level=0` 水格调用 `mc_interact` 返回 `ok=true, changed=false`，空目标调用还返回 `bad_args`。EP37 在开阔水边复现，四次交互均无抛竿/钓获回执。 [EP14](../runs/overnight_12bot_part2/ds_ds_1_bare/20261119-150646-372249.events.jsonl#L99) · [EP37](../runs/overnight_12bot_part2/ds_ds_1_bare/20261119-171154-936339.events.jsonl#L131)
- 同一代码指纹离线复现：`_infer_extra_tools_for_advancement("minecraft:husbandry/fishy_business", "持fishing_rod对开阔水域使用，钓上1条鱼。")` 只返回 `mc_interact`，`COMPACT_CORE_TOOLS` 没有 `mc_use_item`。实际抛竿、等待和收竿由后端的 `use_item(fishing_rod)` 执行。 [课程分配](../src/mc_agent/harness/curriculum.py#L792) · [工具集](../src/mc_agent/harness/mc_tools.py#L98) · [后端钓鱼分支](../experiments/20261115-until-1900/backend_patched/src/session.js#L2348)
- `follow_ender_eye` 也触发同一授权缺口。对实际 KB 文案调用推断器返回 `()`；`r28/hy4_5` EP26 明确报缺投掷入口，EP25/50 又有独立的合成材料缺口。持眼时能否观测飞行方向仍待验证。 [EP26](../runs/hy4_audit_r28/hy4_hy_5_brewing_alchemy/20261119-163706-486842.events.jsonl) · [KB 文案](../src/mc_agent/kb/data_1_21_11.py#L900)
- 验收时为适用目标公开对应工具并给到准确工具名。对鱼竿走错入口给可执行的改用提示。用持竿、岸边、静水的回归检查真实抛竿回执与目标验证，再用持眼夹具检查投掷和可观测结果。钓鱼轨迹中的缺工作台、积水矿井问题继续关联 BUG-032/052/056。

### BUG-062 | P1 | Slice C（交互）+ Slice D2（酿造水源）| 状态: OPEN（过夜主轨第二波）

- `part2/glm_glm_4_brewing_alchemy` EP01/02 对 `level=1` 或 `level=8` 的水位 **共 13 次**使用玻璃瓶。回执均为 `ok=true, changed=false`，没有 `used/gained`，酿造台瓶槽仍空，随后 `missing_item`。EP03 是独立的 429。第一波同 Bot 的对照回执明确 `used.glass_bottle=1`、`gained.potion=1`，再把 `potion` 放进瓶槽后成功。 [第二波 EP01](../runs/overnight_12bot_part2/glm_glm_4_brewing_alchemy/20261119-133151-986552.events.jsonl#L30) · [第二波 EP02](../runs/overnight_12bot_part2/glm_glm_4_brewing_alchemy/20261119-133259-566434.events.jsonl#L32) · [第一波成功对照](../runs/overnight_12bot/glm_glm_4_brewing_alchemy/20261119-092305-340302.events.jsonl#L35)
- BUG-008 的库存变化判定只覆盖桶类。玻璃瓶仍会把无效交互报成功，目标没得到换水格提示。回执应确认玻璃瓶消耗与水药瓶入包，或明确提示改找可达水源。场景侧核查出生点给出的水格是否可交互。P2-27 继续跟踪 `mc_skill_brew` 的参数说明。 [交互判定](../src/mc_agent/harness/mc_tools.py#L1353)
- 验收覆盖无变化且无物品增减、方块不变但确实装水两路。真实酿造开局可在前两集得到水药瓶并将其投进酿造台。

### BUG-063 | P2 | Slice D1 + Integrator（评测口径）| 状态: FIXED（已合入，r31 Fleet 采用新口径）

- Fleet 的 `Deaths` 同时为 `guardian.incident/death_or_connection_changed` 和含 `死亡` 的 `mc.notable_events` 各加一。连接变化可以进入前者，真实死亡也可能两处都出现。同一消息里的多条死亡只会在后者加一。`r26/hy4_hy_6_nether_bastion` 榜单为 **51**，JSONL 有 **20 条明确死亡回执**，分布在 11 集。 [计数代码](../scripts/v2_fleet.py#L569) · [收官榜单](../runs/hy4_audit_r26/triage-hy4_audit_r26.md)
- 验收时区分死亡、重生和连接变化。有事件 ID 时按 Bot 与 ID 去重，缺 ID 时展示回执口径。离线夹具覆盖单次死亡同时出现在两个事件、仅掉线、同一消息两次死亡。
- D1 用只读回放复现 `hy4_audit_r26/hy4_hy_6_nether_bastion` 的 **51 = 31 条混合 Guardian 事件 + 20 条含死亡的消息**。新 Fleet 输出为 **20 次明确死亡（全部无源 ID）**、**22 次重生或出生回执**、**0 条显式连接变化**、**31 条混合事件**。旧日志的连接变化总数未知。离线夹具通过双发、掉线、同批两死、`×N` 和跨 Bot 去重。 [EP02 混合事件](../runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1017) · [EP02 死亡与重生回执](../runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1020) · [D1 报告](../../s-triage/docs/D1-r11-triage.md#4ca-bug-063-fleet-死亡口径与-r26-对账)
- `r31_short` 的真实 Fleet 导出已显示对应列。GUITest 分别计 **5** 条无 ID 死亡、**7** 条重生或出生、**8** 条混合 Guardian 事件。 [r31 榜单](../runs/r31_short/triage-r31_short.md#L24)

- **r67 跨维度复验**：中期过门波明确死亡 **2**、重生或出生文本 **5**。其中 HY4_8 首次过门、HY4_9 首次及第二次入下界的三条 `[已重生]` 都关联已确认维度切换，死亡计数保持 2。明确死亡为 HY4_9 岩浆和 NJU_3 跌落。`mc_tools.py` 将 `respawn`／`spawn` 渲染为同一文案，该列按原始回执读。 [HY4_8 跨维度](../runs/r67_midgame_portal_3bot/hy4_hy_8_midgame/20261121-073118-159591.events.jsonl#L226) · [HY4_9 入下界](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-071710-307864.events.jsonl#L259) · [实际岩浆死亡](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-072808-164375.events.jsonl#L489) · [回执渲染](../src/mc_agent/harness/mc_tools.py#L1167)
- **r68 裸装补给回执**：三槽明确死亡 **3**，stdout 有 **4** 条 watchdog `death_return/restock=True`。watchdog 可由出生点附近包量骤降推断死亡，清包和恢复按场景执行。回执用于确认干预发生，和明确死亡分开记，HY4_5 多出的一次补给缺对应明确死亡回执。 [HY4_5 补给回执](../runs/r68_bare_real_drop_3bot/hy4_hy_5_bare/stdout.log#L706) · [Watchdog 推断](../scripts/l3_wild_endurance_runner.py#L1445) · [场景清包](../scripts/l3_wild_endurance_runner.py#L1207)

### BUG-064 | **P1（Day 1 砍树效率与 `BUG-047` 残余慢调用）** | Slice C（#4，`skills/library.py`）+ Integrator（`harness/mc_tools.py`）| 状态: FIXED（`28052f1` 已合入，r43 开集四路 31 次采木均 <60s，待 15 路门禁）

- **一句话**：`mc_skill_harvest_logs`（底层 `_dig_target_blocks`）在树林/地形遮挡下会反复撞同一批不可达原木空转 **90 秒（0 块入包）**，且单次 deadline 高达 90s，既直接拖垮 Day 1 砍树效率（`MoveTest_01` 连撞 4 次耗时 6 分钟+），又是全队 `>=60s` 慢调用榜与 `BUG-047` 集超时最后的残余源头。
- **实测（`r41_day1_15bot`，5 NJU + 10 HY4 = 15 并发 `bare` 20m）**：
  - `mc_skill_harvest_logs:time_budget` 居全波第 1：**8 bots / 26 次**，10 条慢调用中 **8 条为 90.1–107.7s 的 `mc_skill_harvest_logs`**。
  - 典型空转：`hy4_hy_1_bare` 连续 2 次 **90.1s**、`dug_count=0, gained={}`；`nju_movetest_01_bare` 全波调 10 次砍树仅入包 **1 块原木**；`hy4_hy_3_bare` 耗时 **87.8s** 挖了 9 块仅入包 2 块（平均 44s/块）。 [r41 榜单](../runs/r41_day1_15bot/triage-r41_day1_15bot.md)
- **源码定位**：[library.py](../src/mc_agent/skills/library.py#L2580-L2734) `_dig_target_blocks`：
  1. **无连续失败熔断 + 避让仅 15s**：循环 `for _ in range(count * 3)`（`target_logs=16` 为 48 轮），遇 `path_not_found`/`out_of_reach` 仅避让 **15s**（L2690），单次服务端寻路失败耗 5–10s，15s 后同一批不可达原木解除避让被反复重选，直到耗尽 90s。
  2. **木头 deadline 过大且未受单集剩余预算钳制**：L2580 `mine_deadline = max(45.0, min(90.0, count * 12.0))` 允许单次 `harvest_logs` / `mine_blocks` 吃满 90s，击穿 `BUG-047 <= 60s` 与集尾余量保护。
  3. **未优先砍脚平齐低处树干**：排序未强化低处树干优先，先够高处原木导致掉落物卡在树叶/树顶捡不到。
- **最小修复**：
  1. `_dig_target_blocks` 增加 **`consecutive_failures >= 4` 立即早停**（返回 `requires_replan=True` 换树/换位），并将 `path_not_found`/`no_path`/`out_of_reach` 避让时长从 `15s` 提至 **`60s`**；
  2. 下调木头/采块 `mine_deadline` 上限至 **`40–45s`**，并新增可选 `max_duration_s` 参数由 [mc_tools.py](../src/mc_agent/harness/mc_tools.py) 接入 `_clamp_tool_budget` 受 `episode_deadline_mono` 钳制；
  3. 砍树候选排序优先脚平齐的低处树干（`y` 贴近 `bot_y`），提高单次入包率。
- **验收标准**：`mc_skill_harvest_logs` 离线单测覆盖“连续 4 次不可达 12s 内早停”与“低处树干优先”；实机 15 并发 Day 1 波次中 `harvest_logs >= 60s` 慢调用为 **0**，0 产出 90s 空转为 **0**，`BUG-047 episode elapsed - budget max <= 10s`。
- **发布与复验**：r42 Day 1 仍在旧 Python 指纹，工具 p99 **91.048s**，HY4_3 EP01 的第二次采木耗时约 91s、整集超 **57.027s**。`28052f1` 后续已合入，待新版本 Day 1 复验。 [r42 采木](../runs/r42_day1_5bot/hy4_hy_3_bare/20261120-063104-637104.events.jsonl#L77)
- **r42→r43 Day 1**：配对采木 **16→31**、最长 **91.048→45.027s**、≥60s **8→0**、90s 零原木保持 **0**。r43 四路实际开集且按时结束，另 1 路 `not_ready`。外层工具 p99 **51.305s**，19 条完整 actor 时限回执最大余量仍有 **0.399s**。 [最慢采木](../runs/r43_day1_5bot/nju_movetest_01_bare/20261120-071622-437822.events.jsonl#L20) · [最贴集限](../runs/r43_day1_5bot/hy4_hy_3_bare/20261120-073403-369568.events.jsonl#L240)
- **r44 Day 1**：5 路正常入场、27 条采木最大 **45.013s**，≥60s 与 90s 空转均 0。33.343s 整集越界源于楼梯，BUG-047 仍开。等待 15 路。 [采木峰值](../runs/r44_day1_5bot/hy4_hy_2_bare/20261120-074027-502156.events.jsonl#L39)

### BUG-066 | P1（Day 1 场景跑偏）| Slice D2（场景）+ Integrator（runner）| 状态: FIXED（`5e871e6` 已启用，r43 四路无提前派下界，待原 15 路与合法过门）

- **影响**：`r41_day1_15bot` 的 HY4_6、HY4_7 以 `bare/overworld` 通过入口。两路 EP01 都获得 `enter_the_nether`，随后 **5 集**被派下界堡垒或遗迹，**0/5** 验证，消耗 **2006.324 Bot·s**（按各集 UTC 起止计，约占两路计划时长 **83.6%**）。两路均未验证石器或铁器，进下界各得 30 分。 [HY4_6 终版](../runs/r41_day1_15bot/hy4_hy_6_bare/wild-endurance-r41_day1_15bot_hy_6_bare-20261120-060107.json) · [HY4_7 终版](../runs/r41_day1_15bot/hy4_hy_7_bare/wild-endurance-r41_day1_15bot_hy_7_bare-20261120-060109.json)
- **实据**：两路首个 actor 工具之前已出现相邻的出场坐标回执（约 `[972,56,350.5]`），随后 watchdog 心跳确认 `the_nether`。HY4_6 曾短暂回到主世界，后续心跳又回到下界。日志未记录触发跨维度的具体传送门或指令。 [HY4_6 EP01](../runs/r41_day1_15bot/hy4_hy_6_bare/20261120-060123-062890.events.jsonl#L12) · [HY4_7 EP01](../runs/r41_day1_15bot/hy4_hy_7_bare/20261120-060119-514673.events.jsonl#L15) · [HY4_6 心跳](../runs/r41_day1_15bot/hy4_hy_6_bare/stdout.log#L38) · [HY4_7 心跳](../runs/r41_day1_15bot/hy4_hy_7_bare/stdout.log#L47)
- **源码与最小修复**：watchdog 和集入口只把下界场景离开下界视为维度偏离。主世界场景向下界发展原本用于合法传送门进度。为 Day 1 裸装基准配置无传送门干扰的开局，且在预准备后、派题前校验维度。识别通过明确传送门目标进入下界的正常晋级。 [看门狗](../scripts/l3_wild_endurance_runner.py#L1180) · [集入口](../scripts/l3_wild_endurance_runner.py#L1902)
- **验收标准**：同一 15 路 Day 1 配置下，明确传送门晋级前无裸装 Bot 因维度偏离获派下界目标。复测显式传送门目标仍能完成合法跨维度链。
- **r43 五槽启动观察**：三路 HY4 和 MoveTest 通过主世界入口。GuiTest 在 `(6400,-6400)` 与 `(6400,-6560)` 两处候选未通过传送门安全检查，最终 `not_ready`、0 集。拒绝回执未区分真的扫到门与无法确认扫描结果，也没有记录其他被过滤候选的原因。15 路配置、HY4_6/7 和显式传送门均待实机复验。 [GuiTest 启动](../runs/r43_day1_5bot/nju_guitest_01_bare/stdout.log#L1) · [GuiTest 终版](../runs/r43_day1_5bot/nju_guitest_01_bare/wild-endurance-r43_day1_5bot_guitest_01_bare-20261120-071614.json#L17) · [MoveTest 选到替代点](../runs/r43_day1_5bot/nju_movetest_01_bare/stdout.log#L1) · [扫描判定](../scripts/l3_wild_endurance_runner.py#L160)
- **r44 备用落点实测**：GuiTest 再次拒绝 r43 的两个原候选，通过 `c1ba4c3` 新增的独立西侧备用区，在 `(-23597.5,71,-6400.5)` 验证主世界并进入课程。5/5 均通过入口。原 15 路、HY4_6/7 和合法过门仍待验收。 [槽位日志](../runs/r44_day1_5bot/nju_guitest_01_bare/stdout.log#L1) · [区域生成](../scripts/l3_wild_endurance_runner.py#L172)

### BUG-067 | P1（`mc_skill_smelt` 93.6s 超时穿透 + 地表长距 `mc_move_to:move_timeout` 22x）| Slice C（#4 Codex #2，`skills/library.py` + `harness/mc_tools.py`）| 状态: FIXED（r43 熔炼与航点限时已复验，实测探索半径未达 180m，续验 BUG-071）

- **一句话**：① `mc_skill_smelt` 默认 `wait_timeout_s=95.0` 未受 `BUG-047` 单次 `<=55s` 与 `episode_deadline_mono` 钳制，单次熔炼 7 粗铜吃满 **93.6s**；② 地表寻村/探索时模型直接传 60–140m 远坐标给 `mc_move_to`，撞满 25s 预算触发 **9/9 bots、22 次 `move_timeout`**，把 10 分钟白天的有效推进半径锁死在 94–155m。
- **实测证据**：
  - `r42_day1_5bot`：`nju_guitest_01_bare` `mc_skill_smelt(raw_copper, count=7)` 耗时 **93.6s**（[r42_day1 榜单](../runs/r42_day1_5bot/triage-r42_day1_5bot.md)）。
  - `r42_day2_village_9bot`：`mc_move_to:move_timeout` 居全波第 1（**9 bots / 22 次**），例如 `hy4_hy_10_iron` 连续 5 次长距 `mc_move_to` 全部超时（[r42_day2 榜单](../runs/r42_day2_village_9bot/triage-r42_day2_village_9bot.md)）。
- **源码定位**：
  - [library.py](../src/mc_agent/skills/library.py#L4382-L4388)：`smelt_items(..., wait_timeout_s: float = 95.0)` 未在 `mc_tools.py` 经 `_clamp_tool_budget` 钳制。
  - [mc_tools.py](../src/mc_agent/harness/mc_tools.py#L1209-L1225)：`mc_move_to` 只钳制了 `eff_move_timeout <= 25s`，未对水平目标距离 `> 36m` 的地表长距寻路做分段步长截断（25s 内根本走不完 80–140m 复杂地形）。
- **最小修复**：
  1. `mc_tools.py` 调用 `smelt_items` 前用 `_clamp_tool_budget(..., hard_cap_s=55.0)` 钳制 `wait_timeout_s`（且 `smelt_items` 到点前自动取出炉内已烧出成品并返回 `partial_smelt`），确保任何 `mc_skill_smelt <= 55s`；
  2. `mc_move_to` 在水平距离 `> 36m` 且未指定 `entity_id` 时，自动按方向截断为 `<= 32m` 中途航点并在成功/超时后返回当前坐标与 `remaining_dist`，避免 100m 直连必撞 25s 超时；
  3. 顺手收尾 `P2-41`（`library.py:5325` `unstick` 四向安全出口）。
- **验收标准**：补离线单测覆盖 `smelt_items` 受集预算钳制（`<=55s`）与 `mc_move_to` 超 36m 自动截断航点；实机 `mc_skill_smelt >= 60s` 为 0，`day2_village` 探索 `move_timeout` 显著下降且单集推进半径 `>= 180m`。
- **r43 终版对照**：Day 1 熔炼最长 **93.626→49.975s**，五次调用中 `>=60s` 为 0，HY4_2 EP04 部分取出 **4+1** 铁锭。独立按同集配对，Day 2 整波 `move_timeout` **22/109→5/130**，寻村子集 **20/81→2/65**，有 **38** 条部分航点回执。近村首集已记录的最大水平推进 **139.621m**，未达票据 180m 目标。 [部分熔炼](../runs/r43_day1_5bot/hy4_hy_2_bare/20261120-073158-377280.events.jsonl#L367) · [最大部分航点](../runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-071636-357863.events.jsonl#L300)

### BUG-068 | P1（`mine_dfs` 地下 `climb_progress` 8 连切片 + `unknown_fail` 6x）| Slice A（#3 Codex #1，`skills/mining_dfs.py`）| 状态: FIXED（r43 回程子门禁通过，报表中的未分类抢占见 BUG-073）

- **一句话**：地下回地表时 `mc_skill_mine_dfs(action="climb_out")` 每爬 1–5 格就提前退出报 `climb_progress`（单机连刷 **8 次**），且 `mine_dfs` 在 `r42` 两波中出现 **6 次 `unknown_fail`** 未暴露结构化原因。
- **实测证据**：
  - `r42_day2_village_9bot`：`nju_ds_1_iron` 连续 8 次 `climb_progress`（`y=-26→-6→-3→-2→3→4`），并夹杂 2 次 `needs_surface_climb`；`hy4_hy_7/9_iron`、`nju_ds_1_iron` 合计 4 次 `mc_skill_mine_dfs:unknown_fail`（[r42_day2 榜单](../runs/r42_day2_village_9bot/triage-r42_day2_village_9bot.md)）。
  - `r42_day1_5bot`：`nju_guitest_01_bare` 2 次 `mc_skill_mine_dfs:unknown_fail`（[r42_day1 榜单](../runs/r42_day1_5bot/triage-r42_day1_5bot.md)）。
- **最小修复**：
  1. 在 `mining_dfs.py` `climb_out` 循环内遇到小幅阻挡/单步 `staircase` 部分进展时，只要剩余时间预算 `>= 10s` 且有垂直进展（`dy >= 1`），继续在同一调用内推进而非立刻退出报 `climb_progress`；
  2. 审计 `mine_dfs` 所有 `ok=False` 返回分支，补齐结构化 `error.code` / `stop_reason`，消除 `unknown_fail`。
- **验收标准**：离线单测覆盖“多步连续爬升合并为单次调用”和“异常分支无 `unknown_fail`”；实机单集 `climb_progress <= 2`、`unknown_fail = 0`。
- **r43 原始回执**：Day 2 两条 `climb_progress` 分属两集，之后均有地表完成，总回程比 **2/2**。原榜单的 11 条 `unknown_fail` 均有 Guardian 抢占标记，终版轨迹另两个 Bot 有同类回执；按 BUG-073 核对标签。 [NJU_3 回程成功](../runs/r43_day2_village_near_9bot/nju_nju_3_day2_village_near/20261120-073021-002663.events.jsonl#L89)

### BUG-069 | P1（地表涉水 `y=62` 与坡地撤退 `unsafe_landing` 致 4 台铁装 Bot 各 2 死）| Slice B（#2 子 AGY，`harness/guardian.py` + `skills/combat_env.py`）| 状态: VERIFIED（`r43_day2` 死亡 8→2，`water_escape_failed` 3→0）

- **一句话**：r42 四路铁装各 2 死（5 箭伤、2 爆炸、1 近战）。五次箭伤前均有撤退失败。另有 3 次 `water_escape_failed`（`y=62`），均发生在无显式死亡的寻村集。保留浮水和不安全撤退两个验收项。
- **实测证据**：`r42_day2_village_9bot` 中 `hy4_hy_4/6/8/9_iron` 各 2 死；`hy4_hy_6/8_iron` 报 `mc_skill_unstick:water_escape_failed`（`y=62.2..62.8`），`hy4_hy_5/6/9_iron` 多次报 `unsafe_landing`（[r42_day2 榜单](../runs/r42_day2_village_9bot/triage-r42_day2_village_9bot.md)）。
- **最小修复**：
  1. 核验刚合入的 `cf89eb0` 5 连提交是否已覆盖 `y=62` 地表开阔河流脱困；
  2. 当撤退方向全部被 `unsafe_landing` 拦截且受到远程/近战持续伤害时，优先触发 `raise_shield` / 反击或短距横向避障，严禁原地反复报 `unsafe_landing` 挨打致死。
- **验收标准**：离线单测覆盖“地表 `y=62` 涉水遇骷髅”与“撤退全被 `unsafe_landing` 拦截时切举盾/反击”；实机地表铁装波次死亡率明显下降、`water_escape_failed = 0`。
- **r43 同九槽、不同场景**：整波显式死 **8→2**，浮水失败 **3→0**，11 次浮水动作最大 **3.784s**。两条致命伤均为 `mob_attack`，HY4_6 在地表、HY4_7 在地下。HY4_7 EP01 先短时浮水，后转上岸成功。 [上岸](../runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L171) · [HY4_6 死亡](../runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-072048-411226.events.jsonl#L241) · [HY4_7 死亡](../runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-072929-890490.events.jsonl#L140)

### BUG-070 | P1（`r42_day1_5bot` & `r42_day2_village_9bot` 闭环深度取证）| Slice D1（#5 DeepSeek #1，只读 `runs/r42_*`）| 状态: FIXED（`5e0a750` + `1e4469a` 已合入）

- **核查清单**：
  1. `r42_day2_village_9bot`：`hy4_hy_7_iron` 命中 `dirt_path` 后为何未继续找到村庄主体（坐标/时间/工具链）？
  2. `r42_day2_village_9bot`：`hy4_hy_4/6/8/9_iron` 共 8 次死亡的精确伤害源与 `guard_mixed` 时间线；
  3. `r42_day1_5bot`：`nju_movetest_01_bare` 3 次死亡原因，以及 NJU 2 台下矿仅采到 1 块 `raw_iron`、却采了 7–20 块 `raw_copper` 的矿层/耗时账本。
- **交付**：把精确行号、时间线和必要的新工单/修订写回本看板与 `docs/D1-r11-triage.md`。
- **D1 已补取证**（审计报告 §4cl）：HY4_7 EP02 在土路后已到村庄建筑、从箱中取出物品并完成床交互。三次实体查询均缺 `villager`，无村民回执，终版验证字段仍为 false。四路铁装的 8 死为 5 箭伤、2 爆炸、1 近战。9 条混合事件中 1 条是重生后的连接恢复，3 次浮水失败均在无显式死亡的其他集。MoveTest 三死为两次箭伤与一次近战。两台 NJU 的 `raw_iron=1`、`raw_copper=7/20` 都有同期库存回执，逐次采矿、熔炼和死亡账见报告。 [土路](../runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L24) · [取箱](../runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L170) · [实体过滤](../runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L286) · [连接恢复](../runs/r42_day2_village_9bot/hy4_hy_4_iron/20261120-064217-563301.events.jsonl#L131) · [MoveTest 库存](../runs/r42_day1_5bot/nju_movetest_01_bare/20261120-064425-812878.events.jsonl#L98) · [GUITest 库存](../runs/r42_day1_5bot/nju_guitest_01_bare/20261120-063800-887294.events.jsonl#L88)
- **SCENE-002 复验补充**：命中村庄方块后直接查 `villager` 并核对仍可见的床、箱和交易对象。HY4_5 EP03 跨过黄昏阈值后仍开始移动与扫描，记录在 §4cl，场景需覆盖派发时为白天、执行中跨黄昏的路径。 [黄昏](../runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L244) · [后续移动](../runs/r42_day2_village_9bot/hy4_hy_5_iron/20261120-063759-468845.events.jsonl#L266) · [只在派发时的黄昏门禁](../src/mc_agent/harness/curriculum.py#L1487)
- **发布与复验**：`5e871e6` 已合入集成树。r42 的五个裸装槽没有重跑 HY4_6/7，且启动早于该补丁，0 次偏离是这五槽的观察。复验核对带保护的实际 runner 与受影响槽。 [r42 五槽终版](../runs/r42_day1_5bot/triage-r42_day1_5bot.md)

### BUG-071 | **P1（地表移动与搜寻效率极低：10 分钟仅推进 94–155m，缺少零 LLM 往返高速斥候技能）** | Slice C（#4 Codex #2，`skills/library.py` + `harness/mc_tools.py`）| 状态: 部分修复（r67 实机确认窄角修复，280m 半径待验）

- **一句话**：目前地表探索/寻村完全依赖 LLM 手动循环调用 `mc_move_to` + `mc_find_blocks` + `mc_get_entities`，每推进 ~30m 就要付 3 次 LLM 往返（15–30s），导致 10 分钟白天有效推进半径仅 **94–155m**（均速 **~0.25 格/秒**，比正常玩家疾跑慢 20 倍），远低于原版村庄 300–500m 的平均间距。
- **实测证据**：`r42_day2_village_9bot` 9 台 Bot 跑满整个白天，最大位移仅 `33–155m`，全波 22 次 `move_timeout`；且 `library.py` 当前无任何地表高速推进/巡航复合技能。
- **最小修复（新增 `mc_skill_scout_surface` 复合技能并暴露为工具）**：
  1. **零 LLM 往返多段连续推进**：在 `library.py` 新增 `scout_surface(direction="auto", target_types=None, max_duration_s=45.0, step_dist=28.0)`，受 `mc_tools.py:_clamp_tool_budget(..., hard_cap_s=50.0)` 与 `episode_deadline_mono` 严格钳制；单次调用内自动沿选定/未探索方向连续推进 **3–5 段 × 28–32m**（目标单次调用推进 **100–150m**，1 集即可覆盖 **350–500m**）。
  2. **每段落地零开销自动扫描 + 命中即早停**：每完成一段自动执行 `find_blocks(radius=64)` 搜村庄特征（`bell`, `hay_block`, `dirt_path`, `composter`, `lectern`）+ `get_entities(radius=64)` 搜 `villager`/`iron_golem`（同时把途经 `lava`/`obsidian` 记入结果），**一旦命中目标立刻早停并返回精确坐标、距离与群系**。
  3. **自动避水/避围栏/遇险早停**：每步出发前检查 `VoidGeofence`；若某段 `move_timeout` / `path_not_found` / 落入水格，自动向 ±45° 偏航绕行而非原地死磕或下河游泳；检测到近距敌怪或受击立即早停交还控制权。
  4. **单集推进效率可量化**：返回值包含 `distance_traveled`、`segments_completed`、`final_pos`、`detected_pois`、`detected_entities`、`stop_reason`。
- **验收标准**：补离线单测覆盖“多段零 LLM 连续推进 + 中途命中 `bell`/`villager` 立即早停 + 遇围栏/超时偏航绕开 + 严守 `<=50s` 预算”；实机纯随机空降（非 `day2_village_near`）1 集（~4–5 分钟）有效探索半径从 94–155m 提升至 **`>= 280m`**。
- **r44 读数口径**：runner 在落点建 `radius=220, margin=8` 围栏，斥候每个航点受 **212m** 安全半径约束。若按落点计算探索半径，当前固定场景无法验出 280m；若按集开始位置计算，应同时记录集起点距锚点、最大水平位移和围栏触发。单次或多次路径累计 `distance_traveled` 另计。复验 280m 需要保持出生区域隔离的相容测试场景。 [围栏参数](../scripts/l3_wild_endurance_runner.py#L2056) · [安全半径](../src/mc_agent/harness/geofence.py#L35) · [斥候航点检查](../src/mc_agent/skills/library.py#L1464)
- **r44 终版对账**：9 路各一集寻村，43 次斥候配对中 41 条结构化技能回执，最大 **42.151s**。90 段、30 次有位移、路径累计 **3059.56m**。同九路首轮寻村直接移动超时 **10/42→0/12**，整波 **22/109→1/75**；两类直接扫描 **32/24→7/5**。HY4_10 EP02 **606.15m** 是四次斥候的累计曲线路程，距已核对的集起点最远 **175.33m**。NJU_3 EP02 可核对的起点最大位移 **206.89m**，锚点最远斥候回执 **208.82m**。原记录的 `6/9 ≥280m` 口径为累计路程，280m 位移门槛未验证。9 集寻村均无客观验证。 [最长斥候](../runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074208-367761.events.jsonl#L16) · [HY4_10 第四段回执](../runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074208-367761.events.jsonl#L90) · [NJU_3 最大位移](../runs/r44_day2_wild_scout_9bot/nju_nju_3_iron/20261120-074200-755033.events.jsonl#L219)

- **r66 探索记忆版本的绕行回归**：`e2f4e0c` 下，离线草地场景只有正东第一次移动返回 `path_not_found`，后续方向可达。空白记忆得到 **6 次移动请求、5 段、140.01m**，预置两条探索记录时只发 **1 次请求、0m**。`scout_surface` 按记忆分数重排，90° 候选提到 ±45° 前，普通无路分支遇宽角直接退出。最小修复是先排允许的 ±45°，出现围栏或液体时可扩宽，组内保留记忆优先级。验收要求同地形两种记忆都实际尝试合法偏航，普通无路仍不放开宽角。真实出现频次等 r66 的斥候回执。 [记忆排序](../src/mc_agent/skills/library.py#L1695) · [提前退出](../src/mc_agent/skills/library.py#L1716)

- **候选修复 `4391434`** 已合入 `d4f2e30`，先排窄角可用候选再按记忆排序。D1 空记忆和有历史夹具 **2 项通过**，集成树窄角、围栏、水岸绕行等 **4 项通过**。r67 首批 actor 指纹 `bee6986cfb92c26f` 匹配合入版本。 [修复排序](../src/mc_agent/skills/library.py#L1702) · [集成夹具](../tests/test_skills_and_providers.py#L3114)

- **r67 实机复验**：HY4_2 EP02／03 的 **6/6** 条普通无路回执都列出原方向和两条 ±45°，各 0m。HY4_1 的两集寻村累计路程 1070.32m，最远斥候终点距落点 209.54m。实机确认窄角分支，整票仍待可容许 280m 的同口径场景。 [HY4_2 EP02](../runs/r67_expl_mem_3bot/hy4_hy_2_iron/20261121-065531-768950.events.jsonl#L18) · [HY4_2 EP03](../runs/r67_expl_mem_3bot/hy4_hy_2_iron/20261121-065750-378055.events.jsonl#L19) · [HY4_1 最远终点](../runs/r67_expl_mem_3bot/hy4_hy_1_iron/20261121-065732-821435.events.jsonl#L196)

### BUG-072 | P1（`mc_skill_flee` 79.7s 残余慢调用穿透 `BUG-047 <=60s`）| Slice B（#2 子 AGY）+ Slice C（#4 Codex #2，`mc_tools.py`）| 状态: FIXED（r68 井道回撤 20.952s，门限 35s）

- **r68 探索覆盖井道**：actor 撤退 **11** 次最大 **26.944s**；HY4_2 EP05 一次 `mode=retreat_shaft` 耗 **20.952s**，报告确封 6 格、站位上升 1 格，随后边界护航浮水 **14.719s**，回执 `in_water=false,on_ground=true`。两项本波均过 35s 门槛。 [撤退](../runs/r68_expl_mem_iron_3bot/hy4_hy_2_iron/20261121-073645-791746.events.jsonl#L493) · [随后浮水](../runs/r68_expl_mem_iron_3bot/hy4_hy_2_iron/20261121-073645-791746.events.jsonl#L495)

- **一句话**：`r43_day2_village_near_9bot` 全场唯一一条 `>=60s` 慢调用为 `nju_ds_1_day2_village_near` 的 `mc_skill_flee`（**79.7s**），说明 `flee` 未受 `episode_deadline_mono` 与 `<=35s` 硬预算钳制。
- **最小修复**：给 `mc_skill_flee` 接入 `_clamp_tool_budget(..., max_s=35.0)` 并在内部循环/单次 `move_to` 严格尊重剩余预算，确保任何撤退动作 `<=35s`。
- **D1 定位**：DS_1 EP03 L224→225 执行 **79.655s**，回执 `mode=retreat_shaft,hops=3,sealed=9`，从 Y−17 退到 Y−3。检查 actor 入口到矿道回撤、逐路点和封堵的共同截止。以该分支同场验收，不能只测地表奔逃。 [配对回执](../runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-072028-965473.events.jsonl#L224) · [actor 入口](../src/mc_agent/harness/mc_tools.py#L2485) · [矿道分支](../src/mc_agent/skills/library.py#L7694) · [三次回撤和封堵](../src/mc_agent/skills/mining_dfs.py#L3987)
- **r44 旧版本基线**：DS_1 EP03 的 `retreat_shaft` **93.012s**，仍是 3 路点、封 9 格（Y15→39）。HY4_10 EP03 同分支在只余 **8.222s** 时启动。r44 两波加载 `d1d572c`，下一波验收同时核对硬上限与本集剩余。 [DS_1 93s](../runs/r44_day2_wild_scout_9bot/nju_ds_1_iron/20261120-074936-839471.events.jsonl#L168) · [HY4_10 集尾撤退](../runs/r44_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-074538-967229.events.jsonl#L290)
- **r45 实机回归**：指纹 `4415809c8c8cdb72` 对应 `eb4a736`，包含 `fa4cd15` 与 `cfde702`。Day 1 HY4_3 EP01（+12.923s）、Day 2 HY4_7 EP01（+23.241s）因 `TypeError: '<' not supported between instances of 'float' and 'Timeout'` 收尾，首报时两路均无 Bot 终版。矿道会话给 status 传逐相位 `httpx.Timeout`，再进入共享 `_GuardSession.call`，在标量 `min(timeout_s, remaining)` 抛错。最小修复兼容 float 和 `httpx.Timeout`，保留请求各相位已设的紧限，继续共享集截止。验收新增真实类型的嵌套夹具，并要求同实机分支至少完整返回一次、无 `runtime_error`。 [Day 1 集尾](../runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L24) · [Day 2 集尾](../runs/r45_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-083557-171764.events.jsonl#L89) · [嵌套回溯](../runs/r45_day2_wild_scout_9bot/hy4_hy_7_iron/stdout.log#L82) · [Timeout 构建](../src/mc_agent/skills/mining_dfs.py#L376) · [类型冲突](../src/mc_agent/harness/guardian.py#L162)
- **r45 结算**：两波 Fleet 均已退出。Day 1 HY4_1 EP04、HY4_2 EP05、HY4_3 EP01，Day 2 HY4_4 EP03、HY4_6 EP03、HY4_7 EP01，共 **6/14** 个槽依次撞同一撤退异常，均无终版。全都是 actor 已启动后退出。后合入的相位修正 `3d00b90` 未被 r45 加载。 [HY4_1](../runs/r45_day1_5bot/hy4_hy_1_bare/20261120-084446-500156.events.jsonl#L299) · [HY4_2](../runs/r45_day1_5bot/hy4_hy_2_bare/20261120-084904-311884.events.jsonl#L191) · [HY4_4](../runs/r45_day2_wild_scout_9bot/hy4_hy_4_iron/20261120-083925-644268.events.jsonl#L57) · [HY4_6](../runs/r45_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-084026-929245.events.jsonl#L226)
- **r46 实测**：确认运行 `b0882ad`。发出 7 次 actor 撤退并有正常工具终态，Day 1/2 最大 **1.815／3.169s**，0 相位类型异常、0 缺终版；`retreat_shaft` 仍 0 暴露。 [铁装撤退](../runs/r46_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-091722-147672.events.jsonl#L117)

### BUG-073 | P1（`mc_skill_mine_dfs:unknown_fail` 11x / 3 bots 未结构化）| Slice A（#3 Codex #1，`skills/mining_dfs.py`）| 状态: FIXED（r46 榜单无真实探矿 unknown_fail，D1 回放区分 15 条结构化抢占）

- **一句话**：`r43_day2_village_near_9bot` 中 `mc_skill_mine_dfs:unknown_fail` 仍出现 **11 次 / 3 bots**（`hy4_hy_6/7`、`nju_nju_3`），虽 `climb_progress` 已从 8→2，但仍有未带结构化 `error.code` 的失败返回路径。
- **最小修复**：排查 `r43_day2_village_near_9bot` 中这 11 条 `mine_dfs` 返回体，补齐所有 `ok=False` 分支的 `error.code` 与可操作恢复建议，使 `unknown_fail = 0`。
- **D1 确认是抢占分类缺口**：原榜单这三路的 **11/11** 均为 `preempted_by_reflex=true`，Day 2 终版按同集真实调用复核共 **13 条／5 路**，其余两条在 DS_1、HY4_9。它们来自 Guardian 生成的结构化抢占回执，分类器忽略该字段，落入 `unknown_fail`。建议把抢占单独计入 `reflex_preempted`（或给 Guardian 回执补对应 code），保留 `mine_dfs` 真失败的独立门槛。 [三路样本](../runs/r43_day2_village_near_9bot/hy4_hy_6_day2_village_near/20261120-072048-411226.events.jsonl#L174) · [DS_1](../runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-072028-965473.events.jsonl#L209) · [HY4_9](../runs/r43_day2_village_near_9bot/hy4_hy_9_day2_village_near/20261120-071926-878155.events.jsonl#L143) · [回执生成](../src/mc_agent/harness/guardian.py#L2060) · [榜单分类](../scripts/v2_fleet.py#L422)
- **r44 对照**：D1 对照器 `cd99106` 已读结构化字段，Day 2 实际 **16** 条采矿抢占、未分类真实采矿结束回执 **0**。Fleet 的 `d8bb2fa` 已在 r44 后并入，待新进程实机。 [D1 分类](../../s-triage/scripts/d1_compare.py#L93)

### BUG-074 | **P1（`mc_skill_scout_surface` 40% 调用被 `in_water`/`liquid_ahead`/`void_geofence_blocked` 早停 + 字符串数字参数崩溃）** | Slice C（#4 Codex #2，`skills/library.py` + `harness/mc_tools.py`）| 状态: FIXED（r45 围栏早停归零，零位移因 BUG-078 反升，待组合复验）

- **一句话**：`r44` 全波 43 次 `mc_skill_scout_surface` 中，**17 次（40%）**因 `in_water`（9x）、`void_geofence_blocked`（5x）、`liquid_ahead`（3x）早停（大量返回 `0.0m`），且 `direction="auto"` 在围栏边缘仍反复选出撞墙方向；另有模型传字符串 `"45"`/`"30"` 触发类型报错。
- **实测证据**：
  - `nju_ds_1_iron`、`nju_nju_3_iron` 连续调 `direction="auto"` 共 4 次撞 `void_geofence_blocked`（`dist=0.0m`/`28.7m`）；
  - `hy4_hy_6_iron` 连续 `north`/`south`/`west` 三次全返回 `0.0m liquid_ahead`；`hy4_hy_5_iron` 传 `max_duration_s="45"` 直接失败；5 台 Bot 共触发 9 次 `in_water` 早停。
- **最小修复**：
  1. `scout_surface(direction="auto")` 选向时**必须预检 `VoidGeofence` 剩余安全距离**，排除距边界 `< 40m` 的方向；行进中撞 `void_geofence_blocked` 时自动向场内偏转 `±90°/135°` 继续推进，不直接返 `0.0m`；
  2. 遇 `liquid_ahead` 或浅水 `in_water` 时，先尝试 `±90°` 干地绕行（或 1 步短距上岸）再继续，仅全向被水围死才早停；
  3. `mc_tools.py:mc_skill_scout_surface` 对 `max_duration_s`/`step_dist` 兼容字符串数字强制转换 `float(...)`；
  4. 返回体附带当前 `biome`（从 `status()` 读取，不要恒为 `null`），遇到海洋/深林/丛林提示模型立即换向。
- **验收标准**：离线单测覆盖“贴近围栏 `auto` 自动向内折返”、“单侧水域自动 90° 绕行”、“字符串数字参数兼容”；实机 `scout_surface` `0m` 空转率大幅下降、`void_geofence_blocked` 归零。
- **r45 同六个有终版槽**：围栏／涉水／前方液体早停由 **8/26→3/37**，围栏 **5→0**。HY4_9 接近围栏时改向后完成 5 段。零位移反而 **7/26→16/37**，16 条全是 BUG-078 的普通箱触发。本波无模型传字符串数字，所有 47 条可确认技能回执群系字段为空。 [近围栏改向](../runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L254)
- **r46**：27 条含 11 条落水、0 围栏、0 前方液体。匹配两波均有终版且寻村的三路，落水 **2/13→8/16**。零位移主要由 BUG-078 改善，仍需复验绕水；字符串数字无实机暴露、27 条群系均为空。 [落水样本](../runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-090127-093178.events.jsonl#L73)
- **r47→r52 同八槽**：有停止原因的完整回执中，零位移 **9→2**、围栏与前方液体均 **0→0**，落水则 **9/60→28/74**。排除起点迁移 15km 以上的 HY4_7、NJU_3 后仍为 **9/50→16/53**。斥候内成功上岸 **3→7** 次，独立 Guardian 出水 **3→13** 次。HY4_7 一次调用先上岸后仍卡在水中，HY4_8 两次上岸后完成 146.34m。零位移和围栏门槛通过，持续绕水仍有缺口。 [先上岸后停水](../runs/r52_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-213720-045295.events.jsonl#L256) · [两次上岸](../runs/r52_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-213822-434393.events.jsonl#L84) · [斥候上岸实现](../src/mc_agent/skills/library.py#L1542)

### BUG-075 | **P1（`mc_find_blocks` 多类型查询被高频 `dirt_path`/`farmland` 挤爆 `limit`，淹没床/箱子/干草/钟）** | Slice C（#4 Codex #2，`harness/mc_tools.py` + `skills/library.py`）| 状态: FIXED（r46 有村庄床与箱回执，稀有混扫和取物转化仍待验）

- **一句话**：Bot 进村后调 `mc_find_blocks(types=["bell","dirt_path","hay_block","composter","lectern","bed","chest"], limit=20)`，返回 `total=833` 但前 20 条**100% 全是 `dirt_path`**，把近在咫尺的 `hay_block`/`bed`/`chest`/`bell` 彻底挤掉，导致 `DS_1` 站在村中心还误判“没找到干草块和床”。
- **实测证据**：`r43_day2_village_near_9bot/nju_ds_1_day2_village_near` 调 `mc_find_blocks(limit=20, radius=64, types=[...])` 返回 `total: 833`，前 20 条全为 `dirt_path`；随后模型误判“未找到 hay_block”，而下一集扫描发现 `hay_block` 距脚边仅 2.1m。
- **最小修复**：
  1. 在 `scout_surface` 内部扫村时，**优先扫稀有地标**（`bell`, `hay_block`, `composter`, `lectern`, `fletching_table`, `chest`, `*_bed`），仅作为补充再扫 `dirt_path`，并将命中村庄坐标直接写入 `WorldMemory`；
  2. 在 `mc_tools.py:mc_find_blocks` 当 `len(types) >= 2` 且单类方块霸占返回列表导致其他稀有目标被截断时，对结果做按类型保底或提示高计数方块（或在 `scout_surface` 中分两批小查询），确保 `bell`/`hay_block`/`bed`/`chest` 绝不被 `dirt_path` 淹没。
- **验收标准**：离线单测覆盖“200 个 `dirt_path` + 1 个 `hay_block` + 1 个 `yellow_bed` 时，进村扫描必返回 `hay_block` 与 `bed`”；实机进村后绑床/薅干草/开箱转化率显著提升。
- **r46**：HY4_6 EP02 真正进村，扫出白床与 `(-7796,78,2862)` 的箱，床交互返回成功。随后两次去箱只传 XZ，实际到 Y35.9/Y39.9，接着在 Y40 的三个开箱调用均失败。未见拥挤混扫标志或采干草、取物、交易回执。 [白床和正确箱高](../runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L100) · [第一处错误楼层](../runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L165) · [失败回执](../runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L295)
- **r47 Day 2 已完成集复核**：`hy4_hy_7_iron` EP02 再扫到同一 `(-7796,78,2862)` 的箱，于白床旁 Y71 起步，随后只传 XZ 和 `range=3`。调用 `ok=true`，落在 `(-7793.5,38,2863.5)`，离箱 40 格垂直距离。`unstick` 原地失败，18 级楼梯回到 Y56，本集无开箱回执。寻村收割提示应要求对已知箱/床用完整坐标接近并核对垂直差，验收用该村箱位置做同锚点对照，确认实际取物。 [箱坐标](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L119) · [床旁站位](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L157) · [XZ 参数](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L199) · [错误楼层](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L200) · [末次爬升](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L261) · [寻村收割提示](../src/mc_agent/harness/curriculum.py#L1318) · [移动工具入参](../src/mc_agent/harness/mc_tools.py#L1184)
- **r47 第二个同箱复现与床位正例**：HY4_6 EP02 同样甩到 Y37.9、离箱 40.1 格，三片楼梯才回到 Y65。HY4_6 和 HY4_7 均与 `(-7778,72,2879)` 白床交互成功，HY4_7 EP04 实际死亡后重生在 `(-7778.5,72,2878.5)`，距床 0.71 格。Day 2 终版 16 集寻村无取箱、干草、交易回执。 [HY4_6 错层](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-095905-635192.events.jsonl#L157) · [HY4_6 最后爬升](../runs/r47_day2_full_day_scout_9bot/hy4_hy_6_iron/20261120-095905-635192.events.jsonl#L239) · [HY4_7 床交互](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100027-077186.events.jsonl#L178) · [HY4_7 实际重生](../runs/r47_day2_full_day_scout_9bot/hy4_hy_7_iron/20261120-100828-101734.events.jsonl#L296)
- **r52**：16 集寻村，0 集验证，未记录绑床、干草、交易或取物。DS_1 EP03 对独立箱 `(-2896,71,-10094)` 传入完整 XYZ，仍返回 `no_window`。上一集试走同坐标时受伤中断。原 Y78 村箱未暴露，稀有混扫和同箱转化验收仍待覆盖。 [完整坐标开箱](../runs/r52_day2_wild_scout_9bot/nju_ds_1_iron/20261120-213833-041153.events.jsonl#L379) · [上一集走向该箱](../runs/r52_day2_wild_scout_9bot/nju_ds_1_iron/20261120-213332-943524.events.jsonl#L621)

### BUG-076 | P1（`r43`/`r44` 裸装 Day 1 `hy4_hy_1_bare` 连续两波 7–8 死异常回路）| Slice D1（#5 DeepSeek #1 取证）+ Slice B（#2 子 AGY 修复）| 状态: FIXED（r46 实测两次换安全退路，HY4_1 仍有 3 次箭伤，掩体与低血上岸待验）

- **一句话**：`r43_day1_5bot`（8 死，`guard=37`）与 `r44_day1_5bot`（7 死，`guard=41`）其余 Bot 几乎全 0 死，唯独 `hy4_hy_1_bare` 连续两波陷入高频死亡循环（伴随 `mc_skill_flee:unsafe_landing` / `mc_skill_unstick:unsafe_landing`）。
- **分工**：
  - `#5 D1`：审计 `runs/r44_day1_5bot/hy4_hy_1_bare` 7 次死亡的精确伤害源、坐标、`unsafe_landing` 时间线写入本看板；
  - `#2 子 AGY`：针对裸装开局在地形陡坡/夜怪围堵下 `flee:unsafe_landing` 连续触发致死的缺口补齐兜底动作。
- **D1 实测**：r44 七条显式死亡为 **3 箭伤、4 近战**。同一命中回执的 HP=0 伤害用于归因。五次在本集该次死亡前见过 `unsafe_landing`，末 20s 内有字面拒绝的仅两次。七次均回到 `(-10988.5,69.1,-11017.5)`。r43 八死均箭伤，全部回同一原锚点。r44 EP01、EP06 首死距 r43 两处箭伤点分别约 **11.0／3.3 格**，反复经过锚点附近河岸。日间样本已有掠夺者与近战。

  | r44 EP / 单集相对秒 | 致命伤与坐标 | 死前退路证据 |
  |---|---|---|
  | EP01 +194.436 | [箭伤 `(-10933,62,-10981.9)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L268) | [L236 见掠夺者，未着甲无副手](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L236)，[L245/L255 两次 `unsafe_landing` 且位移 0](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L255) |
  | EP04 +157.478 | [近战 `(-11052.3,70,-11087.6)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L171) | [L78 actor 撤退拒绝](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L78) 已早 126.7s。后在 [L166–170 挖矿被抢占](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074629-758857.events.jsonl#L166)，Guard 回执 `not_ready`、HP0 |
  | EP05 +47.611 | [近战 `(-11019.8,79.2,-11066)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L67) | [L10 `unsafe_landing`，近僵尸](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L10)，[L52 已移动 12.6 格](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L52)，仍被追上 |
  | EP05 +134.917 | [箭伤 `(-10968.5,75,-10975.4)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L223) | [L114 actor 撤退拒绝](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L114)，[L199/209 后两次撤退成功](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075217-648091.events.jsonl#L209)，终点仍中箭 |
  | EP06 +28.617 | [箭伤 `(-10955.9,64.2,-10979.5)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L48) | [L12 近掠夺者、无安全退路](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L12)，[L44 水中退避失败](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L44) |
  | EP06 +73.004 | [近战 `(-10964.7,71,-11006.9)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L104) | [L75 近僵尸、落脚拒绝](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L75)，[L85/L98 无屋顶](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075432-956708.events.jsonl#L98)，两次分别只撤 1.75／2.02 格 |
  | EP07 +42.825 | [近战 `(-10932.3,62.4,-11016.7)`](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L55) | [L9 近僵尸，HP3.86](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L9)，[L19–20 撤 13.1 格后入水、HP0.86](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L20)，[L30／L32 浮水及上岸失败](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-075653-135717.events.jsonl#L32) |

- **源码与验收补充**：核对 `_defend_or_flee` 在裸装无法格挡时的实际退路，`flee` 失败时保留安全落脚拒绝，增加可验证的掩体或干岸落点。撤退的 `ok=true` 只说明位移，验收再查脱离射线或取得地形遮挡。实机用原 HY4_1 区域及替代点对照，按显式死、伤害源和回出生点后的再遭遇分别计数。EP04 另覆盖长挖矿被近敌接管时，死亡前能否及时停工具。 [Guardian 防守入口](../src/mc_agent/harness/guardian.py#L391) · [撤退落脚检查](../src/mc_agent/skills/combat_env.py#L1458)
- **r45 取证结论**：HY4_1 只产生 4 个 actor 轨迹，第 4 集崩溃前有 1 条近战死亡，距 r44 落点约 39m 且高 12 格。本波 Day 1 无 `guardian.flee_exit_blocked`、`guardian.unstick_exit_blocked`、`unsafe_exit_retreat`，也未观测到 GUITest HP≤6 涉水。保留原地形、低血量涉水与真落脚拒绝的复验。 [近战回执](../runs/r45_day1_5bot/hy4_hy_1_bare/20261120-084446-500156.events.jsonl#L244) · [随后异常](../runs/r45_day1_5bot/hy4_hy_1_bare/20261120-084446-500156.events.jsonl#L299)
- **r46 实机子项**：HY4_1 与 r44 落点平距 **4.24m**，7 集有 3 次箭伤。两条原退路拒绝后的替代退路，均实际移动约 2.2m。EP02 从替代落点再过 **5.215s** 即被箭伤致死，死点距 r43 同河岸旧点 **4.58m**。MoveTest 湿地低血有一次尝试直接上岸却以重生收尾，未单独记录触发谓词。 [首条换路](../runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L67) · [后来致死](../runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090239-082942.events.jsonl#L69) · [低血上岸](../runs/r46_day1_5bot/nju_movetest_01_bare/20261120-090715-754576.events.jsonl#L266)
- **下一波发布**：`4757c8a` 已合入，只在退路被拦、无盾、近身单个近战威胁时走一次近击。现有四个参数化夹具均过，实机需实际退路拒绝和近敌反击回执。 [离线夹具](../tests/test_generation_guard.py#L1714)
- **r53 Day 1 终版**：新版本 `d41a8d0` 下，HY4_1 EP01 两次箭死回同一重生点。第一死距 r44 EP01 死点约 9m，缺安全脱困落点与实墙。重生后第一次通用出水 5.46s 后又入水，第二次出水后确认落脚，仍因未摆脱射手死在干地。HY4_1 EP03 边界向岸六次仅一次成功。HY4_2 EP04 通用浮水失败后走向岸兜底，出水未确认落脚，随后箭死。同三槽 r47→r53 死亡 **4→4**，HY4_1 **2→2**；近地形历史 r44 HY4_1 有 7 死，保留原高频循环改善与向岸失败两项实测。 [初次箭死](../runs/r53_day1_bare_3bot/hy4_hy_1_bare/20261120-215403-191849.events.jsonl#L116) · [干地落脚](../runs/r53_day1_bare_3bot/hy4_hy_1_bare/20261120-215403-191849.events.jsonl#L166) · [再次箭死](../runs/r53_day1_bare_3bot/hy4_hy_1_bare/20261120-215403-191849.events.jsonl#L192) · [向岸未落脚](../runs/r53_day1_bare_3bot/hy4_hy_2_bare/20261120-220642-668999.events.jsonl#L267) · [确认脚下支撑](../src/mc_agent/skills/combat_env.py#L2073)

### BUG-077 | P1（启动预准备回放把历史死亡计入最近 60 秒，误触发避难）| Integrator（`harness/guardian.py` + `bot.py`）| 状态: OPEN（实机与 D1 离线复现）

- **影响**：课程首集可在 actor 作出首个决策前报告 8–9 次“近期死亡”，提前执行紧急掩体或撤退。若掩体建成，当前入口还会等待由虚假时间戳计算的冷却窗口。此问题影响实机控制，Fleet 的 BUG-063 对账仍单独计算显式死亡。
- **实据**：r43 HY4_1 EP01 +1.510s 报 `recent_deaths=8`，掩体 `not_grounded`，接着实际撤退 5.5 格。L13 的后端 `last_death.t` 为 **14:36:43.709 UTC**，距告警 **2416.537s**。本集第一条显式死亡在 +33.981s。r44 同槽 EP01 +1.474s 又报 9，并实际撤 6.2 格。离线用真实 `GenerationGuard.prime()` 回放八个带不同事件 ID、约 40 分钟前的死亡，现版错误地计为 **8**。 [r43 告警](../runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L8) · [后端时间戳](../runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L13) · [首条本集死亡](../runs/r43_day1_5bot/hy4_hy_1_bare/20261120-071624-552133.events.jsonl#L84) · [r44 告警](../runs/r44_day1_5bot/hy4_hy_1_bare/20261120-074025-497605.events.jsonl#L5)
- **源码与最小修复**：`prime` 推进历史游标时复用读取函数。后者在关闭战斗观察时仍将死亡 ID 送给计数器，未携带原始事件时间。计数器用当前单调时间补值，actor 到 2 条即触发避难。保留游标和去重，在登记到 60 秒窗前按事件时间定龄。启动历史事件缺可校验时间时不得用观测到达时刻当作新死亡。 [启动预准备](../src/mc_agent/harness/curriculum.py#L2194) · [初始化](../src/mc_agent/harness/guardian.py#L321) · [读取时登记](../src/mc_agent/harness/guardian.py#L465) · [历史时间被覆盖](../src/mc_agent/bot.py#L52) · [避难门槛](../src/mc_agent/harness/agent.py#L1288)
- **验收标准**：挂载旧轨迹启动时，历史游标推进到最新且 `recent_deaths=0`。再发入窗的两条不同 ID 的真实新死亡，重复回放同一个 ID 后仍只计两条，保护照常触发。实机用之前跑过的同槽，启动前 60 秒无真实死亡时不得出现历史误避难。 [D1 离线夹具](../../s-triage/tests/test_d1_repro.py)
- **r45 同口径复现**：Day 1 HY4_3 EP01 L3 的后端最近死亡本地换算为 16:12:24.634 UTC，L8 在 16:36:17.746 UTC 误报近期 2 死，间隔 **1433.112s**。掩体失败后实际撤了 **4.6 格**，仍发生在首个 actor 决策前。 [旧时间](../runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L3) · [避难告警](../runs/r45_day1_5bot/hy4_hy_3_bare/20261120-083549-772628.events.jsonl#L8)
- **r45 启动范围**：五个 Day 1 槽中，HY4_1、HY4_3、GuiTest、MoveTest 在首个模型前各有 **17／2／3／4** 条所谓近期死亡，仅 HY4_2 未打印此类启动告警。HY4_1 的避难在 actor +61.392s 返回，已凿并封顶三格掩体，`held_s=0`，首个模型于 +61.467s 才启动。旧时间直接证据在 HY4_3，该槽以外不据计数反推后端实际历史。 [HY4_1 避难与首请求](../runs/r45_day1_5bot/hy4_hy_1_bare/20261120-083545-758409.events.jsonl#L5)
- **r46 观测口径**：3/5 个 Day 1 槽首个模型前出现告警（HY4_1 12，HY4_2 7，MoveTest 2）。HY4_1、HY4_2 各自最新后端死亡在告警前 **33.243／43.834s**，其余计入记录缺原始时间。历史误算的确定实据仍用 r43、r45 和离线夹具。 [HY4_1 起始后端时间](../runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L3) · [实际告警](../runs/r46_day1_5bot/hy4_hy_1_bare/20261120-090007-333544.events.jsonl#L8)

### BUG-078 | P1（地表寻村斥候把普通地下箱当作命中，反复零位移早停）| Slice C（`skills/library.py`）| 状态: FIXED（r46 24 次默认斥候 0 地下箱误停，同一地下箱已现场复验）

- **影响**：默认寻村在半径 64 的方块扫描中把孤立 `chest` 当作村庄线索。常驻旧箱会令多个方向的重复调用在起点就返回 `target_found`，同时跳过独立的土路扫描，实际寻村无法推进。保留 BUG-075 的箱子分组回传覆盖。
- **实据**：r45 的 HY4_9 EP02 6 条默认斥候均以 Y15 的同一组箱结束。首条在 Y64 看到 Y15 的箱，另外五条移动 0m。HY4_6 EP02 4 条均为 0m，从 Y80–81 看见 Y33 同一个箱。进行中的 5 路至少 29 条 `target_found` 只有普通箱可作为触发，无村民、床、钟等佐证。该数待终版重算。 [HY4_9 首次](../runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L23) · [HY4_9 重复零位移](../runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L46) · [HY4_6 原地](../runs/r45_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-083808-891436.events.jsonl#L18)
- **源码与最小修复**：`76a5d5a` 将 `VILLAGE_LANDMARK_BLOCKS` 直接用作默认肯定命中集合，集合含 `chest`；`found |= name in targets` 不要求同高程或村庄佐证，置位后还短路独立土路查询。默认寻村应把普通箱作为伴随观察，要求地表可及的村庄证据后才结束航段。显式找箱调用保留独立语义。 [默认目标](../src/mc_agent/skills/library.py#L1407) · [跳过土路查询](../src/mc_agent/skills/library.py#L1454) · [命中条件](../src/mc_agent/skills/library.py#L1501)
- **验收标准**：离线复现地表观察者与正下方 47–49 格的孤立箱，只返回伴随观察，默认寻村继续并仍可扫到地表土路。真实村庄地表标志仍提前停。实机相同地图核对默认寻村的地下箱独占 `target_found=0`，重复零位移消失，再核对实际地表位移和后续补给回执。
- **同位置对照**：HY4_9 EP02 L224→225 从 `(-5006.6,63,-7671.5)` 默认向东，仍命中 Y15 箱、0m。L253→254 显式只找村民与铁傀儡，箱仍作为观察回传，同位置完成 5 段 **134.84m**，最终 `segment_limit`。 [默认](../runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L225) · [仅实体目标](../runs/r45_day2_wild_scout_9bot/hy4_hy_9_iron/20261120-083711-098504.events.jsonl#L254)
- **r45 收官核查**：六个有终版的寻村集另两条仅原始轨迹有完整首趟，共 47 条技能回执。**29** 次 `target_found` 全是孤立普通箱，覆盖 **5** 路，其中 **24** 次 0m。HY4_10 追向 Y46 的箱坐标被围栏钳制，后续未开箱。新语义修正未被 r45 加载。 [追箱航点](../runs/r45_day2_wild_scout_9bot/hy4_hy_10_iron/20261120-083723-524840.events.jsonl#L177)
- **r46 终版验收**：r45 的确认默认调用（两路仅原始轨迹）为 **29/33** 孤立箱触发，r46 **0/24**。六个实际派发寻村，默认遇箱且继续实移 **5** 次。HY4_7 看到与 r45 HY4_6 完全相同的 Y33 箱，仍推进 **72.66m**；真实堆肥桶和讲台可立即停。 [相同地下箱继续走](../runs/r46_day2_wild_scout_9bot/hy4_hy_7_iron/20261120-090127-093178.events.jsonl#L73) · [真实地标命中](../runs/r46_day2_wild_scout_9bot/hy4_hy_6_iron/20261120-090131-903705.events.jsonl#L20)
- **r52 持续通过**：同八槽与 r47 相比，只有箱子的斥候回执 **23→16**，严格 `target_found` **0→0**，零位移 **9→2**。普通箱未触发提前停止，真实地标分支沿用 r46 正例。 [r52 有箱仍推进](../runs/r52_day2_wild_scout_9bot/hy4_hy_8_iron/20261120-213822-434393.events.jsonl#L84)

### BUG-079 | **P1（Day 1 `harvest_logs` 29/29 报失败均 11s/木 + `mine_blocks` 混镐级整拒 + `scout_surface:in_water` 11x）** | Slice C（#4 Codex #2，`skills/library.py`）| 状态: OPEN（`8ec11f9` 已合入，r47 采木门槛未通过）

- **一句话**：`r46_day1_5bot` 中 `mc_skill_harvest_logs` **29 次调用 100% 报 `ok=False`**（耗 827s 占 Day 1 总耗时 22.1%，单根原木平均 **11s**，44s 仅砍 4 根）；`mc_skill_mine_blocks` 混入 `iron_ore` 时木镐被整次拒绝；`r46_day2` 中 `scout_surface` 剩余 15 次失败里 **11 次是 `in_water`**。
- **实测证据（`r46_day1_5bot` / `r46_day2_wild_scout_9bot`）**：
  - `harvest_logs`：`time_budget` 15 次、`unreachable_targets` 6 次、`no_drops_collected` 4 次；多条 `(44.1s, dug=4, gained={'oak_log': 4})`、`44.0s, dug=5`——每砍 1 块原木都要重新寻路换树，而非站在同一棵树干下原地竖砍 `y..y+3`。
  - `mine_blocks`：木镐调用 `["stone", "coal_ore", "iron_ore"]` 直接 0s 返回 `tool_tier_insufficient` 整批拒绝，未跳过 `iron_ore` 去挖 `stone`/`coal_ore`。
  - `scout_surface`：`r46_day2` 30 次调用中 11 次因 `in_water` 提前中断。
- **最小修复**：
  1. **同树干原地竖砍（提速 4–5x）**：`_dig_target_blocks` 在砍原木时，到达某树干 `(x, y, z)` 后，优先连续原地砍完同一垂直柱 `(x, y..y+3, z)`（无需重新 `move_to`），再统一拾取掉落物；达到 `time_budget` 时若已入包 `>= 4` 块原木，返回 `ok=True, partial_progress=True`（附明确数量）避免模型误判失败反复重砍；
  2. **`mine_blocks` 混合镐级容错**：当 `block_types` 同时包含可挖方块（如 `stone`/`coal_ore`）和当前镐级挖不动的方块（如 `iron_ore`）时，仅剔除不可挖项（在返回体附 `skipped_unmineable`）并继续挖剩余合法目标；仅全部不可挖才返回 `tool_tier_insufficient`；
  3. **`scout_surface:in_water` 绕岸兜底**：遇到 `in_water` 时先尝试 1 次短距回干岸 + `±90°` 偏航再决定是否终止，减少水边早停。
- **验收标准**：离线单测覆盖“同树干原地连砍 4 根原木不重寻路”、“木镐混传 `stone+iron_ore` 自动跳过 `iron_ore` 挖 `stone`”、“`scout_surface` 遇水先绕干岸”；实机 Day 1 `harvest_logs` 失败率从 100% 降至 `< 20%`，单次 15–20s 稳拿 6+ 根原木。
- **D1 r47 终版复验**：r46→r47 已配对采木 **29→25**，排除反射后失败 **27/27→12/21（57.1%）**，仍高于 **20%**。技能耗时中位 **44.039→20.583s**，3 次净收至少 6 根，最快 **24.308s**，20s 内 **0/21**。按失败回执计 `time_budget` **15→4**、`unreachable_targets` **6→2**，另有一次 44.046s 收 6 根的超时部分成功。两条 r47 的 `tool_tier_insufficient` 均报告角色无镐，木镐混传暴露仍待新样本。Day 2 斥候有效回执 `in_water` **11/27→9/61**，新增 3 次 `shore_recoveries`，HY4_8 仍有 6 次涉水早停。 [最快六根](../runs/r47_day1_5bot/hy4_hy_1_bare/20261120-095716-917129.events.jsonl#L62) · [超时部分成功](../runs/r47_day1_5bot/hy4_hy_1_bare/20261120-100243-715803.events.jsonl#L71) · [无镐拒绝](../runs/r47_day1_5bot/hy4_hy_2_bare/20261120-101249-037804.events.jsonl#L116) · [上岸续跑](../runs/r47_day2_full_day_scout_9bot/nju_ds_1_iron/20261120-100224-945668.events.jsonl#L164)
- **r49 拾取核验补丁实测**：实际为 `852b6c9`（包含 `f2f84d8`）。裸装 8 路开跑，采木 31 配对、2 次反射，技能失败 **18/29（62.1%）**、中位 **30.831s**。5 次证实净收 ≥6 根，20 秒内 0 次，两项仍未达标。r47 裸装与本波槽位不同，记录门槛变化。

### BUG-080 | **P1（下界要塞 `mc_skill_tunnel:unsafe_or_unknown_floor` 29x + 下界高速斥候外推）** | Slice C（#4 Codex #2，`skills/library.py` + `harness/mc_tools.py`）| 状态: OPEN（r51 要塞斥候已接入，导航和烈焰棒剧本入口待验）

- **r82 门户实际过门后再次集中拒绝**。HY4_8 EP05 验证入下界，EP06／07 均未验证要塞，累计 **17** 次 `unsafe_nether_route`（6／11），终版留在下界。拒绝落在斥候的安全地形探测阶段，仍缺新可走方向的实机证据。 [过门终版](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/wild-endurance-r82_midgame_portal_30m_hy_8_midgame-20261121-203233.json#L251) · [EP07 首条](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/20261121-205557-121952.events.jsonl#L18) · [EP07 末条](../runs/r82_midgame_portal_30m/hy4_hy_8_midgame/20261121-205557-121952.events.jsonl#L674) · [安全探测](../src/mc_agent/skills/library.py#L1564)

- **r67 真实入下界后的新锚点**：HY4_8 EP06、HY4_9 EP05／06 合计 **19** 次 `unsafe_nether_route`（7／9／3）。三条堡垒集均未验证。首次各在门边 Y56，单次 0m、已有多方向拒绝回执；HY4_9 EP06 在稍后探索中死于岩浆，回主世界后找到已亮传送门并再次进入。 [HY4_8 首次拒绝](../runs/r67_midgame_portal_3bot/hy4_hy_8_midgame/20261121-073408-443583.events.jsonl#L20) · [HY4_9 首次拒绝](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-072051-436801.events.jsonl#L20) · [回程后已亮门](../runs/r67_midgame_portal_3bot/hy4_hy_9_midgame/20261121-072808-164375.events.jsonl#L659)

- **一句话**：`r48_nether_fortress_9bot` 6/9 找到要塞，但导航阶段出现 **29 次 `mc_skill_tunnel:unsafe_or_unknown_floor`**（5 bots）、**33 次 `movement_not_confirmed`**（7 bots）、**28 次 `path_not_found`**（4 bots），且 `mc_skill_scout_surface` 尚未支持 `the_nether` 维度（导致下界仍靠手动短步寻路）。
- **实测证据**：[r48_nether_fortress 榜单](../runs/r48_nether_fortress_9bot/triage-r48_nether_fortress_9bot.md)（9/9 完成，0 崩溃，0 慢调用，但 90 次导航失败）。
- **最小修复**：
  1. `mc_skill_tunnel` 在下界桥面/要塞走廊遇 `unsafe_or_unknown_floor` 时，若包内有 `cobblestone`/`netherrack`/`blackstone` 且脚下 1 格为空气（非岩浆），自动垫 1 格安全方块再继续，或返回明确的 `bridge_gap_ahead` 建议改向，不再盲目重复触发 29 次；
  2. 将 `mc_skill_scout_surface` 外推支持 `the_nether`（或放宽维度检查）：下界沿安全实心下界岩/地狱砖分段推进（步长 16–20m），自动避开 `lava`/`magma_block`/悬崖，每段优先扫 `nether_bricks`/`spawner`/`nether_wart`/`blaze`/`wither_skeleton`，绝不被 `netherrack` 淹没。
- **验收标准**：离线单测覆盖“下界 `scout_surface` 避岩浆/岩浆块并命中 `nether_bricks`/`blaze` 早停”和“`tunnel` 空隙提示/垫脚”；实机 `nether_fortress` 导航失败大幅下降、`obtain_blaze_rod` 破零。
- **D1 原始基线**：配对同一集工具起止，r48 下界 `tunnel:unsafe_or_unknown_floor` **30 次/6 路**，其中 **13** 条空气、**17** 条为已识别固体；`move_to:movement_not_confirmed` 33、`move_to:path_not_found` 32。另有挖掘和交互各一次寻路失败，整波 34。r48 指纹 `dcf003d816873b36` 和 r49 指纹 `0de7d140b4d3343e` 都早于 `643f2f1`。下界斥候与烈焰棒仍待新版本实机。 [空气间隙](../runs/r48_nether_fortress_9bot/nju_nju_3_nether_fortress/20261120-103419-975489.events.jsonl#L391) · [固体样本](../runs/r48_nether_fortress_9bot/hy4_hy_10_nether_fortress/20261120-103658-952025.events.jsonl#L103) · [榜单差异补漏](../runs/r48_nether_fortress_9bot/hy4_hy_5_nether_fortress/20261120-102651-372216.events.jsonl#L960)
- **r50 接线未完成**：两个下界正式剧本 `find_fortress` 和 `obtain_blaze_rod` 的 compact 工具注册表都没有 `mc_skill_scout_surface`。离线直接构造剧本工具表，得到 16／17 项且无斥候；同一注册器的 `follow_ender_eye` 有斥候。最小修复是给两个下界剧本加该工具并在任务中明确用它搜要塞和烈焰人，保留已合入的下界地形检查。r50 有新版空气空隙分类回执，斥候仍需入口修复后现场验收。 [剧本工具](../src/mc_agent/harness/curriculum.py#L767) · [compact 注册](../src/mc_agent/harness/mc_tools.py#L3473) · [空气间隙](../runs/r50_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-203803-299692.events.jsonl#L504) · [离线工具表夹具](../../s-triage/tests/test_d1_repro.py#L203)
- **r50 终版**：同九槽、20→30 分钟，位置已重新布置。空气缺口 **9** 次、不支持实心地板 **45** 次，通用错误 **5** 次均是重生锚。隧道地板失败 **30/30→59/60**，直接移动无路及未确认合计 **65/130→148/201**；两波均无下界斥候。烈焰棒派发 **0→9**，验证 **0→1**。HY4_10 EP02 在烈焰人受击后见烈焰棒实体，接近坐标后触发成就。 [当前实心地板](../runs/r50_nether_fortress_9bot/hy4_hy_10_nether_fortress/20261120-204226-511973.events.jsonl#L211) · [烈焰棒实体](../runs/r50_nether_fortress_9bot/hy4_hy_10_nether_fortress/20261120-203725-588843.events.jsonl#L599) · [终版成就](../runs/r50_nether_fortress_9bot/hy4_hy_10_nether_fortress/wild-endurance-r50_nether_fortress_9bot_hy_10_nether_fortress-20261120-202728.json#L150)
- **`347c444` 接入一条路径，r51 已上机**：新版 compact 里 `find_fortress` 已有斥候，`obtain_blaze_rod` 仍缺。D1 三例夹具验证该变化；r51 下界指纹 `afc99354919f3eac`，后续以首次派要塞目标时的真实工具调用验收。 [新要塞剧本](../src/mc_agent/harness/curriculum.py#L767)
- **r51 首次真实斥候**：HY4_5 EP02/03 已调用新工具，都从 `(-2271.1,64,3935.4)` 原地命中地狱砖和烈焰人，最近砖 Y54，回执 0 段、0m。EP02 以 `answered` 结束并马上重派，说明新工具已进正式运行入口，附近检测与抵达要塞仍需两个回执。 [EP02 斥候](../runs/r51_nether_fortress_9bot/hy4_hy_5_nether_fortress/20261120-210418-059840.events.jsonl#L21) · [集尾](../runs/r51_nether_fortress_9bot/hy4_hy_5_nether_fortress/20261120-210418-059840.events.jsonl#L51)
- **r51 终版**：8/9 开跑，6 次要塞派发均先用斥候。17 次配对全部命中实物，均 0 段/0m，最终要塞任务 **0/6**；同 8 槽 r50 为 **2/7**。后续 24 次移动中，20 次试图接近当前扫描地标，3 成功、8 未确认、6 无路、3 中断。直接移动无路加未确认 **134/183→108/170**，隧道地板阻断 **51/52→60/62**；分支仍缺斥候推进暴露。烈焰棒验证 **1/9→2/10**，由当前战斗剧本取得。 [近 2.8m 的地标](../runs/r51_nether_fortress_9bot/hy4_hy_10_nether_fortress/20261120-211237-269729.events.jsonl#L21) · [抵近寻路失败](../runs/r51_nether_fortress_9bot/hy4_hy_10_nether_fortress/20261120-211237-269729.events.jsonl#L45) · [烈焰棒验证](../runs/r51_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-212806-481545.events.jsonl#L198)

- **r53 同八槽**：要塞任务 **0/6→1/10**，烈焰棒 **2/10→1/8**。三十条下界斥候全部原地命中（0 段、0m），无推进暴露。DS_1 EP04 第一次接近失败、施工时坠落死亡，重生再原地侦测，最后直接移动到地狱砖边才触发要塞验证。移动无路与未确认合计 **108/170→149/200**；隧道地板拒绝 **60/62→63/72**，r53 包括 49 条不支持地板、11 条空隙、3 条通用拒绝。烈焰棒亦由战斗、实物检测、接近后验证。 [零位移斥候](../runs/r53_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-220209-917406.events.jsonl#L310) · [抵达要塞](../runs/r53_nether_fortress_9bot/nju_ds_1_nether_fortress/20261120-220209-917406.events.jsonl#L346) · [烈焰棒实物](../runs/r53_nether_fortress_9bot/hy4_hy_8_nether_fortress/20261120-221958-613457.events.jsonl#L219)
- **r54 同四锚点**：要塞目标 **0/4→1/5**，烈焰棒 **1/3→0/6**。斥候 **12→17** 条全为 0m，r54 16 条命中、1 条危险拒绝。唯一要塞目标成交来自 HY4_8 EP03，反复原地检测后，用普通移动靠近再阶梯下探，末段走到 Y57.9 的地狱砖旁才验证。 [原地斥候](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-232802-376779.events.jsonl#L624) · [阶梯推进](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-232802-376779.events.jsonl#L666) · [移动与成交](../runs/r54_nether_fortress_6bot/hy4_hy_8_nether_fortress/20261120-232802-376779.events.jsonl#L744)
- **r55 起跑优先级仍被覆盖**：`30fda6a` 将要塞与烈焰棒课程设为 tier 2，五路已加载对应源码，首集仍全是远古残骸。目标选择在实际下界把符合前置的正式下界课程改为 tier 1，再用负分值排序；通用远古残骸也在普通下界候选进入 tier 1，且属于无条件目标，60 分排在 30 分之前。应在最终下界排序层保留目标优先级，验收同一装载、三个目标均可选时首集优先要塞。 [首集原件](../runs/r55_nether_fortress_5bot/nju_nju_3_nether_fortress/20261120-234410-247343.events.jsonl#L1) · [正式课程覆盖](../src/mc_agent/harness/curriculum.py#L1586) · [通用候选](../src/mc_agent/harness/curriculum.py#L1613) · [排序元组](../src/mc_agent/harness/curriculum.py#L1650)

- **r55 同四锚点终版**：直接移动无路加未确认 **84/108→61/95**，隧道地板类拒绝 **38/43→40/45**。11 条斥候全原地命中。要塞 **1/5→0/3**、烈焰棒 **0/6→0/3**。HY4_5 EP03 从 Y64 看到 Y54 地狱砖却无法直接靠近，集末仍在同平台。唯一验证是 HY4_8 古残骸 +60，实际取物步骤缺成对成功挖掘回执。 [隔层检测](../runs/r55_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261120-235341-392278.events.jsonl#L18) · [末段仍在同平台](../runs/r55_nether_fortress_5bot/hy4_hy_5_nether_fortress/20261120-235341-392278.events.jsonl#L727) · [古残骸验证](../runs/r55_nether_fortress_5bot/hy4_hy_8_nether_fortress/wild-endurance-r55_nether_fortress_5bot_hy_8_nether_fortress-20261120-234417.json#L124)
- **r56 同五锚点首集排序通过**：`86edbe5` 在下界最终排序为要塞和烈焰棒保留 tier 0。实际指纹 `1a4ff6e724dbbb4b`，要塞首集 **0/5→5/5**。已结集的 HY4_7／9 首两集仍仅 `answered`，下一集回到残骸；同距检测与到达、棒转化继续验收。 [实际派单](../runs/r56_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-001752-415575.events.jsonl#L1) · [原地检测](../runs/r56_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-001752-415575.events.jsonl#L18) · [最终排序](../src/mc_agent/harness/curriculum.py#L1586)
- **r56 同五槽终版**：要塞 **0/3→3/10**，烈焰棒 **0/5→1/7**，总验证 **1→4**。HY4_8 EP02 击杀烈焰人后验证烈焰棒。斥候 11→25 条依旧 0m，直接移动无路和未确认 **80/119→94/144**，隧道地板类拒绝 **54/59→21/26**。保留 OPEN 追踪斥候推进和楼层接近。 [代表要塞完成](../runs/r56_nether_fortress_5bot/nju_nju_3_nether_fortress/20261121-001742-397963.events.jsonl#L1006) · [烈焰人击杀](../runs/r56_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-002147-056166.events.jsonl#L104) · [烈焰棒验证](../runs/r56_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-002147-056166.events.jsonl#L111)

- **r57 同五槽终版**：要塞 **3/10→1/10**，烈焰棒 **1/7→1/4**。22 条斥候全为 0m，直接移动无路或未确认 **94/144→82/138**，隧道地板类拒绝 **21/26→20/22**。HY4_7 EP10 经两次阶梯到地狱砖附近才验证要塞，HY4_8 EP05 在楼梯下探和战斗后实际击杀并取到棒。 [要塞斥候](../runs/r57_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-011150-847653.events.jsonl#L22) · [抵达验证](../runs/r57_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-011150-847653.events.jsonl#L328) · [击杀与物品](../runs/r57_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-011112-993776.events.jsonl#L623)

- **r58 同五槽终版**：要塞 **1/10→1/12**、烈焰棒 **1/4→1/6**，两项全是 HY4_8。实走下探和直接击杀拿棒，斥候 28 次里 26 条结构化移动回执均 0m，另 2 条无可计位移。直接移动无路或未确认 **82/138→93/134**，隧道地板类拒绝 **20/22→22/25**。 [要塞下探](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-012837-791102.events.jsonl#L562) · [烈焰棒入包](../runs/r58_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-013745-338680.events.jsonl#L398)

- **r59 同五槽终版**：要塞 **1/12→1/11**、烈焰棒 **1/6→1/5**，仍是 HY4_8，33 次斥候全 0m。直接移动无路或未确认 **93/134→81/115**，隧道地板类拒绝 **22/25→24/28**。 [要塞集验证](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-015305-124376.events.jsonl#L91) · [烈焰棒集验证](../runs/r59_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-020201-890698.events.jsonl#L322)
- **r60 同五槽终版**：石器误派 1→0，要塞验证仍一集，烈焰棒 **1/5→0/6**。斥候 30 条没有位移。直接移动无路或未确认 **81/115→96/145**，隧道地板类 **24/28→35/37**。 [要塞验证](../runs/r60_nether_fortress_5bot/hy4_hy_8_nether_fortress/20261121-023733-295063.events.jsonl#L203) · [原地斥候](../runs/r60_nether_fortress_5bot/hy4_hy_7_nether_fortress/20261121-022434-902864.events.jsonl#L18)

- **r61 同五槽转化**：正式要塞 **1/11→2/10**，烈焰棒 **0/6→0/8**。24 条斥候仍无位移，移动无路或未确认 **96/145→88/133**。 [HY4_8／NJU_3 证据汇总](../../s-triage/docs/D1-r11-triage.md)

### BUG-081 | P1（末影之眼测向正确，模型把 Minecraft 方位当罗盘方位反向追踪）| Slice C + Slice D2（工具回执与场景任务）| 状态: FIXED（r56 首调 0/9 背向或垂直，同要塞验证 1/7）

- **影响**：`r49_stronghold_end_5bot` 两个实际开跑槽共 27 次成功掷眼，5 集均未进入要塞或末地。两路均有原始向量指向和下一次斥候命令反向的实据，浪费剩余寻路和探洞预算。
- **实据**：MoveTest EP01 `eye_vector=[-0.1,+0.8], bearing_deg=6` 指南，下一调 north，最后一次斥候落在 `(-5660.6,67.2,-6409.5)`。HY4_1 EP01 `[+0.6,-0.5], bearing_deg=228` 指东北，下一调 southwest 且按该方向完成主段。 [MoveTest 向量](../runs/r49_stronghold_end_5bot/nju_movetest_01_village_to_stronghold_end/20261120-104400-405972.events.jsonl#L1234) · [MoveTest 反向](../runs/r49_stronghold_end_5bot/nju_movetest_01_village_to_stronghold_end/20261120-104400-405972.events.jsonl#L1270) · [HY4_1 向量](../runs/r49_stronghold_end_5bot/hy4_hy_1_village_to_stronghold_end/20261120-104503-073972.events.jsonl#L196) · [HY4_1 反向](../runs/r49_stronghold_end_5bot/hy4_hy_1_village_to_stronghold_end/20261120-104503-073972.events.jsonl#L240)
- **源码与最小修复**：`mc_use_item` 将服务端的向量和角度原样透传，场景提示解释了正 X 向东和正 Z 向南，仍由 actor 换算。对有效且非零的真实末影之眼向量，按斥候八向确定性回传可直接传入的方向；到达位移后重新测向，保留围栏和水边安全检查。 [透传](../src/mc_agent/harness/mc_tools.py#L1874) · [八向枚举](../src/mc_agent/skills/library.py#L51) · [场景提示](../scripts/l3_wild_endurance_runner.py#L1783)
- **验收标准**：离线覆盖两条已见向量及八象限、零向量和缺失向量。实机同锚点从首个有效测向启动，核对首次斥候朝向与向量一致，遇障绕行后有下一次测向，最终确认 `follow_ender_eye`。进入末地按 SCENE-003 单独验证。

- **r50 已配对原件复现**：已加载 `cc133e1` 的 HY4_3 EP01 连续两次收到 `[-0.6,+0.5]`，应沿西南，下一调用传 northwest 并向西北完成五段、141.49m。HY4_1 EP01 另有合法投眼回执向量 `[0,0]`、角度 0，应提示重新取样。该波仍进行中。 [两次向量之一](../runs/r50_stronghold_end_3bot/hy4_hy_3_village_to_stronghold_end/20261120-202749-504291.events.jsonl#L83) · [错误斥候](../runs/r50_stronghold_end_3bot/hy4_hy_3_village_to_stronghold_end/20261120-202749-504291.events.jsonl#L106) · [五段回执](../runs/r50_stronghold_end_3bot/hy4_hy_3_village_to_stronghold_end/20261120-202749-504291.events.jsonl#L107) · [零向量](../runs/r50_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-202732-947451.events.jsonl#L18)
- **r50 终版**：三路均启动，共 15 集、0 要塞验证、0 末地。50 次成功投眼，1 次零向量。有向量与后续首调可配的 43 组里，25 组首调前向（17 组是最近八方）、17 组背向、1 组垂直。HY4_2 EP01 三次把东北向量传成西南，每次完成约 142–144m；最后库存三个槽为 3／0／6 眼。 [一次完整背离](../runs/r50_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261120-202748-251829.events.jsonl#L599) · [零向量](../runs/r50_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-202732-947451.events.jsonl#L18) · [终版](../runs/r50_stronghold_end_3bot/triage-r50_stronghold_end_3bot.md#L3)
- **r51 起跑复核**：`347c444` 改了通用 `follow_ender_eye` 提示，但场景覆盖仍用 `STRONGHOLD_FOCUS`。HY4_2、HY4_3 的 r51 首集仍写初始 14 颗，提示中没有通用剧本的折半回退，工具也未增加确定性航向。应在两条入口共用回执方向及续航策略后复验。 [r51 已派发提示](../runs/r51_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261120-210133-121646.events.jsonl#L1) · [覆盖入口](../scripts/l3_wild_endurance_runner.py#L1781)
- **D1 复验门禁**：按每次有效眼向量和下一次斥候首个请求配对，排除零向量、被新向量取代和无后续首调，单列自动或未给方向。r49 **13/21** 首调背向或垂直，r50 为 **18/43**。进行中波次保留临时标签。 [采样与向量分组](../../s-triage/scripts/d1_ticket_gates.py#L98)
- **r51 终版**：2/3 开跑、10 集、0 要塞或末地验证。38 次有效投眼形成 28 个首调用配对，**8** 背向且无垂直。HY4_2 EP02 依据 `[+0.8,-0.3]` 向东 145.83m，距真实要塞约 508→377m。EP03 把 `[+.5,-.6]` 传成西南并移动 141.42m，距要塞约 429→570m。 [前向推进](../runs/r51_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261120-211037-364574.events.jsonl#L339) · [背向推进](../runs/r51_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261120-211822-415551.events.jsonl#L697)
- **r54 修复已加载，首调用实测**：`8792469` 的源摘要与首批 actor `d16997103e8aeb82` 一致，包含 `84e77ff`。HY4_1 EP01 `[+.7,-.4]` 回传 `scout_direction=northeast`，首调采用并走 **85.57m** 后遇水，随后复投回传 `east`。HY4_2 EP01 `[+.3,-.8]` 回传 `north`，首调同向。等待终版首调门禁及要塞验证。 [东北回执](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L34) · [遇水前位移](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L75) · [避障后复投](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L103) · [北向首调](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/20261120-231037-069823.events.jsonl#L39) · [离线夹具](../tests/test_skills_and_providers.py#L2725)
- **r54 终版**：39 次成功投眼均给航向，34 条首调配对 **0 背向、0 垂直、32 与推荐完全相同**。固定村庄、要塞的 r50 同两槽背向／垂直 **15/29→0/34**。HY4_1／2 两路均实际调整出生坐标，均跑 1800s。34 条明确给方向并完成位移的配对回执有三条输出反半平面方向，全在 HY4_1；EP01 发向东后，工具向西南走五段 **141.42m**，距调用前最近已见锚点 ΔX=−100m、ΔZ=+100m。源码遇障允许大角度折向并用新方向跑余下段。建议在显式追眼方向遇宽绕时返回需重新测向的回执，并用同一个锚点分别核对请求、实际净位移及最终成就。全波 9 集要塞目标 0 验证。 [调用前已见位置](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L361) · [实际逆向](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L382) · [EP03 另例](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-232713-238376.events.jsonl#L810) · [宽绕选向](../src/mc_agent/skills/library.py#L1657) · [更新航段方向](../src/mc_agent/skills/library.py#L1589)
- **r55 启动仍被场景覆盖**：三槽指纹 `023cf817bbf166f4` 确认加载 `132e530`，实际首集仍由 `STRONGHOLD_FOCUS` 构造，提示拾回眼、每段下挖八步，未包含通用课程的 100m 规则。HY4_1 首集间隔约 **38.85m** 复投，中途捡回 1 眼；DS_1 有一次约 **46.45m** 复投。通用提示还要求 `scout_heading`，实际回执字段是 `scout_direction`。优先统一真实派单入口与回执字段，再复验实际水平位移及确认翻向后的下探。 [实际提示](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L1) · [拾眼](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L218) · [短距复投](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L235) · [场景覆盖](../scripts/l3_wild_endurance_runner.py#L1827) · [通用提示](../src/mc_agent/harness/curriculum.py#L804) · [字段实现](../src/mc_agent/harness/mc_tools.py#L1910)

- **r55 翻向后仍继续追眼**：HY4_1 EP01 从 `[-.5,-.6]` 翻至 `[+.6,+.6]`（点积 −.66），随后依次捡物、扫描实体和向东南走 141.78m，过程中没有下行楼梯；再次投眼又翻回西北。新通用课程的转向下挖步骤未进入这条派单。 [西北向量](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L672) · [首次翻向](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L730) · [翻向后航段](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L790) · [再次翻向](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-234238-245897.events.jsonl#L808)
- **候选三角定位离线复跑**：`e4cf01b` 增加侧移、双射线估算及钻地识别，`6c7a1ad` 把估算坐标写入后续要塞任务。D1 12 项离线夹具通过。用 r54 两路首两次真实向量及最近已见坐标回放，估点距各自要塞 locate 点约 **30.02m／25.50m**。只有两个历史样本，投眼瞬时 Bot 坐标未单独记录。现场验收核对估点误差、实际是否前往、近点是否下挖及跨集是否继续同一目标。 [HY4_1 第二眼](../runs/r54_stronghold_end_4bot/hy4_hy_1_village_to_stronghold_end/20261120-231022-523804.events.jsonl#L103) · [HY4_2 第二眼](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/20261120-231037-069823.events.jsonl#L83) · [交点估算](../src/mc_agent/harness/mc_tools.py#L1948) · [跨集挂入任务](../src/mc_agent/harness/curriculum.py#L2115) · [理想向量夹具](../tests/test_skills_and_providers.py#L4448)

- **r55 要塞停滞**：三路无终版，三条 EP03 原始轨迹均缺 `run.finished`，最后记录在北京时间 16:02:12–23，检查本机无该波 Fleet 与 runner。快照索引的 34 组首调均前向，状态仍 PENDING。原始尾部没有统一退出信号或异常，整波未计入终版汇总。 [DS_1 尾部](../runs/r55_stronghold_end_3bot/nju_ds_1_village_to_stronghold_end/20261120-235933-978889.events.jsonl#L316) · [HY4_1 尾部](../runs/r55_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261120-235835-063949.events.jsonl#L482) · [HY4_2 尾部](../runs/r55_stronghold_end_3bot/hy4_hy_2_village_to_stronghold_end/20261121-000115-705957.events.jsonl#L147)
- **r56 起跑实测**：`42d3d13de1076216` 确认 `6c7a1ad`。HY4_1 EP01 距第三个估点到 0.5m 后实际下挖至 Y51。HY4_2 EP01 首次估点距 r54 同槽 locate 约 18.87m，第二个约 121.08m，后续继续更新，第四个进入 EP02 提示。新回执已覆盖接近和跨集注入，仍需跟踪估点偏差、下探和成就。 [接近估点](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-000255-282666.events.jsonl#L519) · [下探](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-000255-282666.events.jsonl#L623) · [偏离估点](../runs/r56_stronghold_triangulate_3bot/hy4_hy_2_village_to_stronghold_end/20261121-000305-582807.events.jsonl#L480) · [跨集提示](../runs/r56_stronghold_triangulate_3bot/hy4_hy_2_village_to_stronghold_end/20261121-001248-637140.events.jsonl#L1)
- **r56 终版验收**：相同要塞及村庄两槽 30 分钟，首调 **0/34→0/9** 背向或垂直，投眼 **39→10**，要塞验证 **0/9→1/7**。HY4_1 绕障后重新测向，末次估点偏离 locate 13.45m，沿同一跨集坐标下探，EP03 实际验证要塞。HY4_2 则把手上最后的 71.47m 偏差估点跨三集复用，缺少估点稳定性筛选，仍 0 验证。首调与要塞验证通过；偏差控制和末地闭环继续记 SCENE-003。 [首次正确调用](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-000255-282666.events.jsonl#L84) · [第二集已复用](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-001318-600958.events.jsonl#L1) · [要塞验证](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002233-173459.events.jsonl#L340) · [HY4_2 第三集沿用](../runs/r56_stronghold_triangulate_3bot/hy4_hy_2_village_to_stronghold_end/20261121-001854-640770.events.jsonl#L1)

- **两眼后的 D1 门禁口径**：首个斥候命令若正好采用回执的 `suggested_second_throw_direction`，独立计为侧移，不纳入前进方向分母；已有 `triangulated_direction` 则与估点航向相比。旧单眼仍按原始向量。r56 用新口径为 **0/8** 背向或垂直、1 侧移、6 组估点航向，原 BUG-081 通过判定保持。仅侧移仍为 PENDING，实际位移与要塞验证继续单独核对。 [侧移建议](../runs/r56_stronghold_triangulate_3bot/hy4_hy_2_village_to_stronghold_end/20261121-000305-582807.events.jsonl#L244) · [采取侧移](../runs/r56_stronghold_triangulate_3bot/hy4_hy_2_village_to_stronghold_end/20261121-000305-582807.events.jsonl#L289) · [门禁实现](../../s-triage/scripts/d1_ticket_gates.py#L172)

- **r57 复验通过**：16 组前向首调均无背向或垂直，另有 4 组侧移、7 组按估点配对。HY4_1 再次验证要塞。MoveTest_01 四集 14 投、0 个估点，14 次斥候 0 位移，末位距出生 XZ 1m。`remeasure_after_detour` 避免走错向，但同点复投仍无法取得三角基线。 [首次原地折返停止](../runs/r57_stronghold_end_3bot/nju_movetest_01_village_to_stronghold_end/20261121-003457-597348.events.jsonl#L64) · [要塞验证](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-005837-864818.events.jsonl#L385)
- **r58 要塞方向继续通过，实走未成交**：两路 11 集、15 次有效投眼、9 次估点配对，前向 **0/11** 背向或垂直。对同一终版定位，HY4_2 EP01 从误差 18.79m 的估点变为 205.91m，后者跨集传递；EP04 实走进入约 12m 后死亡。r59 首批加载 c8dbfff，包含 cf247fc 的原地停滞换点提示，等待回执验证。 [较准估点](../runs/r58_stronghold_end_45m/hy4_hy_2_village_to_stronghold_end/20261121-010803-244308.events.jsonl#L271) · [估点偏移](../runs/r58_stronghold_end_45m/hy4_hy_2_village_to_stronghold_end/20261121-010803-244308.events.jsonl#L532) · [真实定位](../runs/r58_stronghold_end_45m/hy4_hy_2_village_to_stronghold_end/wild-endurance-r58_stronghold_end_45m_hy_2_village_to_stronghold_end-20261121-010750.json#L41) · [r59 加载版](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-015348-145054.events.jsonl#L1)
- **r59 换点提示实测**：HY4_1 EP02 的东北追眼斥候位移 0m，回执 needs_new_vantage=true 且要求移至可达干地再投。回执已实际传到 actor，是否取得第二个不同投点待验。 [换点回执](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020106-394604.events.jsonl#L60) · [零位移分支](../src/mc_agent/skills/library.py#L1715)
- **r59 终版**：前向可配对 **0/13** 背向或垂直，另有 1 条自动方向无法判定，整项 PENDING。HY4_1 EP02／03 合计三次实际收到换点提示。MoveTest 真正移动斥候 12/12，未给该提示创造零位移暴露。HY4_2 EP03 已验证要塞，末地仍 0。 [换点样本](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020828-570948.events.jsonl#L325) · [要塞验证](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-021541-139945.events.jsonl#L470)

- **r59 提示与定位基线**：HY4_1 三次换点提示后，下一次复投水平位移约 **0.36／1.50／3.48m**，两次仍无新估点，最后估点距定位约 213m。斥候以不足 1m 判定需要换点，三角定位另要求两投点相距至少 25m。复验应同时记录真实投点位移与能否得到更好的估点。 [提醒](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020106-394604.events.jsonl#L60) · [偏离估点](../runs/r59_stronghold_end_40m/hy4_hy_1_village_to_stronghold_end/20261121-020828-570948.events.jsonl#L751) · [提醒触发](../src/mc_agent/skills/library.py#L1712) · [定位基线](../src/mc_agent/harness/mc_tools.py#L1947)

### BUG-082 | P1（夜间装备课零操作反复验收）| Integrator（`harness/curriculum.py`）| 状态: FIXED（r65 完整波实制盾与铁靴，零操作验收 0）

- **影响**：主世界已有铁镐、仍缺盾或铁甲时，夜间装备任务可以每秒重复派发多次。真实装备进度停摆，课程记录产生大量零动作、零增分的已验证集，影响成交统计。
- **实据**：`r58_day12_sunrise_village_4bot`，HY4_3 EP04 零模型烧铁并制作铁镐，+10 分。EP05–482 连续 **478** 集均为 `bootstrap_iron_age`、`answered`、`objective_verified=true`、0 actor 请求、0 增分。首尾两个报告均写明夜间应做盾和铁甲，预准备目标仍是两把镐，0 步。统计截至该快照，正在继续。 [首次真实铁镐](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-013911-329808.json#L5) · [EP05 报告](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-013951-601177.json#L12) · [EP482 报告](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-014125-253096.json#L12) · [课程快照 EP05](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/curriculum-20261121-012431-161108.json#L100)
- **源码**：实机加载 `815fe6b7f2bca974=fb73751`。夜间候选只替换 `bootstrap_iron_age` 的任务文案和层级，继承铁器完成条件。零模型预准备在已有铁镐时直接结束，结算再以既有铁器判定进度，反复清掉同任务的失败状态。 [夜间候选](../src/mc_agent/harness/curriculum.py#L1565) · [零模型结束](../src/mc_agent/harness/curriculum.py#L2369) · [结算与失败状态](../src/mc_agent/harness/curriculum.py#L2586)
- **最小修复**：让夜间装备任务的完成条件与派发条件一致，按实际盾与铁甲达成确认，补齐已有镐时的预准备需求。对无装备变化或缺材料的短路结果应用退避，保留正常日出候选。
- **D1 离线复现**：以实机同指纹源码、主世界 Y18、已完成全岛侦察、空村庄记忆、包里仅一把铁镐调用选择器。18000 tick 夜间选中夜间装备任务，沿用的谓词立即为真；换成 1000 tick 白天则选中 `day2_village_search` 并明确先 `climb_out`。 [铁器完成条件](../src/mc_agent/harness/curriculum.py#L222) · [日出派单](../src/mc_agent/harness/curriculum.py#L1540)
- **候选补丁 `d751834`，已合入 `c8dbfff`**：D1 离线以同一铁镐夹具复跑，新任务键 `day1_night_iron_prep` 清空预准备、拒绝已有镐验收、尊重新任务冷却，白天仍选寻村。验收细节仍待核对：只新解锁铁甲时谓词即真；铁甲已完成后，补齐盾牌未被谓词识别。待主世界实机验证新任务没有空验收，以及盾甲齐备怎样记账。 [独立任务定义](../src/mc_agent/harness/curriculum.py#L1576) · [通用验收条件](../src/mc_agent/harness/curriculum.py#L222)
- **r58 终版**：HY4_3 EP05–1120 连续 **1116** 集零动作、零增分验收，占全波 1147 集的 **97.3%**、1122 条机器验收的 **99.5%**。约 222 秒后，日出触发 EP1121 寻村，证实退出条件为进入白天。r58 余 31 集、6 集验证、65 分。 [最后一次重复](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-014334-052280.json#L12) · [终版重复末条](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/wild-endurance-r58_day12_sunrise_village_4bot_hy_3_bare-20261121-012425.json#L34910) · [终版日出派单](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/wild-endurance-r58_day12_sunrise_village_4bot_hy_3_bare-20261121-012425.json#L34941)
- **r59 主世界已加载候选**：HY4_3、HY4_4、DS_2 首集指纹均为 df8f6650f3655bcd，待真实夜间缺盾甲派发。 [HY4_3 首集](../runs/r59_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-015919-928906.events.jsonl#L3)
- **r59 终版实测**：HY4_3 EP07 零模型真实制出铁镐后，EP08／09 各实际派发独立夜间任务，分别 7／1 个 actor 请求，均 0 分、验证为假。EP08 挖得煤 8、粗铁 1，短预算内未做出盾甲；EP09 只剩约 1.8s 即随波次到期。全波空验收 **1116→0**，新实际任务已经触发，盾甲完成与缺料退避仍需实机。 [生产铁镐](../runs/r59_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-022700-912938.json#L5) · [夜间实际采铁](../runs/r59_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-022741-696365.events.jsonl#L111) · [首条终版](../runs/r59_day12_sunrise_village_4bot/hy4_hy_3_bare/wild-endurance-r59_day12_sunrise_village_4bot_hy_3_bare-20261121-015913.json#L511)
- **验收标准**：离线以夜间主世界、已有铁镐、无盾甲复现，已有镐不能触发夜间装备验收。实机复用相同入场条件，连续零动作成功 **0**，实际盾甲入包或无料后进入退避，日出时仍可派发寻村。

- **r61c 运行中已见盾甲正例**：HY4_4 EP07 在独立夜间任务下，实际合成盾、置入副手并合成铁胸甲，获 5 分、机器验证为真。胸甲合成回执没有装备槽，下一集状态确认有 6 点护甲且盾仍在副手。待终版复核整波零动作次数与回归门槛。 [盾和副手](../runs/r61c_day12_full_45m/hy4_hy_4_bare/20261121-034525-801667.events.jsonl#L398) · [铁胸甲](../runs/r61c_day12_full_45m/hy4_hy_4_bare/20261121-034525-801667.events.jsonl#L414) · [下一集已穿戴](../runs/r61c_day12_full_45m/hy4_hy_4_bare/20261121-035051-949219.events.jsonl#L45)

- **r61c 后续停写**：三个有回执的夜间课均有 actor 操作，盾甲正例仍可逐条复核。缺四份终版，保留部分修复状态，整波门禁待完整复验。

- **r62 完整波复验**：`r62_day12_full_45m` **4/4**。HY4_3、DS_2 各有一集夜间装备课，分别请求 19、22 次，均未验证盾甲或触发该课失败退避，重复空验收 **0**。HY4_3 挖出 3 粗铁，DS_2 挖出 6 粗铁并合成铁镐，保留部分修复。此前 r61c 有单集盾甲正例，后续仍需完整波内的盾甲交付与无材料退避。 [HY4_3 终版](../runs/r62_day12_full_45m/hy4_hy_3_bare/wild-endurance-r62_day12_full_45m_hy_3_bare-20261121-041820.json) · [DS_2 铁镐](../runs/r62_day12_full_45m/nju_ds_2_bare/20261121-045213-080359.events.jsonl#L301) · [夜间选择器](../src/mc_agent/harness/curriculum.py#L1573)

- **r63 实机缺燃料退避**：HY4_3 EP05、EP06 均 22 actor 请求、0 分、0 验证。第二集熔炼回执为 `missing_fuel`，终版课项 `cooled_down=true`，该槽余下 5 集未重派夜间课。完整波空验收 0，r61c 的盾甲正例仍待在完整波复验。 [熔炼失败](../runs/r63_day12_full_45m/hy4_hy_3_bare/20261121-052200-713074.events.jsonl#L422) · [退避终版](../runs/r63_day12_full_45m/hy4_hy_3_bare/wild-endurance-r63_day12_full_45m_hy_3_bare-20261121-050448.json#L368) · [课程退避](../src/mc_agent/harness/curriculum.py#L2619)

- **r64 完整波造盾实证**：HY4_3 EP05 夜间装备课先造第二把铁镐，随后新造一面盾，铁靴缺 3 铁锭。下一个有装备快照的 EP07 确认盾在副手、护甲值 0。完整波已见盾生产与实际使用，盾甲同时达成仍待验。 [新盾](../runs/r64_day12_full_45m/hy4_hy_3_bare/20261121-060642-853855.events.jsonl#L412) · [铁靴缺料](../runs/r64_day12_full_45m/hy4_hy_3_bare/20261121-060642-853855.events.jsonl#L428) · [副手实据](../runs/r64_day12_full_45m/hy4_hy_3_bare/20261121-061557-682634.events.jsonl#L19)

- **r65 完整波门槛**：HY4_4 EP07 **16 个 actor 请求**，实制盾并装备副手、实制铁靴并装备脚部，终版验真 **+5**，下一集正常派发日出寻村。同四槽 r58→r65 零操作装备验收 **1116→0**；r65 三条夜间课均有实际请求，终包保留盾、铁靴。此前 r63 缺燃料退避已验。课程通用谓词仍按新护甲进度验收，盾牌副手以实际回执复核。 [新盾](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-070237-818958.events.jsonl#L310) · [铁靴](../runs/r65_day12_full_45m/hy4_hy_4_bare/20261121-070237-818958.events.jsonl#L357) · [终版](../runs/r65_day12_full_45m/hy4_hy_4_bare/wild-endurance-r65_day12_full_45m_hy_4_bare-20261121-063752.json#L409)

### BUG-083 | P0（近战反击退化时读取未初始化的 `has_weapon`，整路崩溃）| Slice B／Integrator（`skills/combat_env.py`）| 状态: FIXED（r63 持剑无盾零命中回退，3/3 收官）

- **影响**：`r60_stronghold_end_40m` MoveTest_01 EP04，东南斥候在蜘蛛受击后交给 Guardian。2026-11-21 北京时间 **18:58:30** 抛出 `UnboundLocalError(has_weapon)`。集尾有 `runtime_error`，异常继续越过课程 runner，整路不写 `wild-endurance` 终版。 [斥候开始](../runs/r60_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-025738-953603.events.jsonl#L91) · [集尾异常](../runs/r60_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-025738-953603.events.jsonl#L95) · [工具抢占堆栈](../runs/r60_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/stdout.log#L650) · [未初始化异常](../runs/r60_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/stdout.log#L702)
- **加载版根因**：`3189f9e1e38747f9=a85d1ab` 的 `combat_env.py:2802` 只在非近战反击的 `else` 里赋值 `has_weapon`。正常近战反击遇攻击零命中或超时可改成 `mode=cover_or_flee`（`:2776–2789`），无盾时共同尾部 `:2996` 立即读取这个变量。读取由 `32ec8bd` 引入，当前源码同样保留条件赋值。 [近战退化](../src/mc_agent/skills/combat_env.py#L2788) · [局部赋值](../src/mc_agent/skills/combat_env.py#L2814) · [共同尾部](../src/mc_agent/skills/combat_env.py#L3008)
- **最小修复与验收**：在分流前基于随身和手持物计算 `has_weapon`。离线覆盖无盾、有近战武器、单一蜘蛛且反击返回零命中或超时的共同尾部，保持有盾及无武器后续撤退。实机复验反击后正常返回，波末终版写齐。
- **D1 离线定位**：同一入口用 HP19、钻石剑、无盾、单只蜘蛛，分别注入零命中和 `ApiError(timeout)`。两条都在共享尾部读 `has_weapon` 时复现同一异常。现场缺被异常截断的 combat 完成回执，无法单独确认是哪一种退化。 [现场装备](../runs/r60_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-025738-953603.events.jsonl#L14) · [离线夹具](../../s-triage/tests/test_d1_repro.py#L207)

- **候选修复 `7b397d0`**：集成树已在汇合处重新按库存计算 `has_weapon`。D1 零命中和超时两例均转正常返回，已启用为回归用例。r61 两次要塞场景均在 actor 启动前超时，实机待验。 [补丁落点](../src/mc_agent/skills/combat_env.py#L3005) · [通过夹具](../../s-triage/tests/test_d1_repro.py#L207)

- **r61c 日出重跑恢复 actor**：首批日志指纹 5b1818e95693bd6f 对应 7b397d0。暂未见无盾、已有武器、近战攻击失败后的回退重现。 [真实加载](../runs/r61c_day12_full_45m/hy4_hy_3_bare/20261121-032127-531071.events.jsonl#L3)

- **r62 要塞两路已加载**：MoveTest／HY4_2 指纹 60d8bfad46993a2a 对应 708787c，含 7b397d0。HY4_1 场景选址未就绪，MoveTest 真正回退入口待实测。 [MoveTest 轨迹](../runs/r62_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-032742-916130.events.jsonl#L1)

- **r62 结束前的两种近例**：两个持盾槽曾反击零命中后正常返回，MoveTest 末次也在持盾时击杀蜘蛛；无盾退化条件仍缺。原始日志随后在 20:03 停写，无两路终版。 [持盾零命中后回退](../runs/r62_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-032742-916130.events.jsonl#L511) · [蜘蛛击杀](../runs/r62_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-040051-463667.events.jsonl#L182)

- **r63 实机验收**：`r63_stronghold_end_40m` MoveTest_01 EP05 的斥候被僵尸受击反射抢占。钻石剑反击走 `unreachable_flee`，回执显示主手钻石剑、副手空，正常返回 `cover_or_flee`，随后继续执行并完成 EP05。三路 **3/3** 写终版，runner abort 与 `runtime_error` 均为 0。服务端攻击超时兜底另有离线夹具通过。 [斥候开始](../runs/r63_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-044647-173446.events.jsonl#L786) · [无盾回退](../runs/r63_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-044647-173446.events.jsonl#L788) · [集尾](../runs/r63_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/20261121-044647-173446.events.jsonl#L1343) · [收官](../runs/r63_stronghold_end_40m/nju_movetest_01_village_to_stronghold_end/wild-endurance-r63_stronghold_end_40m_movetest_01_village_to_stronghold_end-20261121-042117.json#L97) · [共同尾部](../src/mc_agent/skills/combat_env.py#L3128)

### BUG-084 | P1（Day 2 寻村缺客观完成判据）| Integrator（`harness/curriculum.py`）| 状态: OPEN（r64 实据与离线复核）

- **影响与样本**：`r64_day12_full_45m` HY4_10 EP08 实际扫到村民、床、钟和堆肥桶。绑床回执 `ok=true, changed=false`，之后取箱被抢占。终版为 `limit_reached`、`objective_verified=false`，5 分来自击杀成就。当前汇总的寻村验证为 0，无法区分村庄已发现、后续动作未完成与从未找到。 [村庄回执](../runs/r64_day12_full_45m/hy4_hy_10_bare/20261121-062923-563834.events.jsonl#L55) · [绑床回执](../runs/r64_day12_full_45m/hy4_hy_10_bare/20261121-062923-563834.events.jsonl#L189) · [终版](../runs/r64_day12_full_45m/hy4_hy_10_bare/wild-endurance-r64_day12_full_45m_hy_10_bare-20261121-055131.json#L483)
- **源码与离线实证**：`day2_village_search` 的 `advancement_id=None`，通用客观判据只特判石器／铁器并读取其余目标的成就 ID。下一次选课用村庄记忆判断是否继续派单，集内完成和终版验证仍走固定为假的判据。直接回放目标对象，即使提供睡过床、交易和补给，也返回假。 [目标定义](../src/mc_agent/harness/curriculum.py#L1359) · [通用谓词](../src/mc_agent/harness/curriculum.py#L222) · [集内完成判据](../src/mc_agent/harness/curriculum.py#L2494) · [终版判据](../src/mc_agent/harness/curriculum.py#L2606)
- **最小修复与验收**：记录本集确认发现村庄和后续任务回执，区分 `village_found` 与任务完成。以本集可信的发现、床或补给实据核对既有课程完成条件，回执足以达成时落 `objective_verified=true`，并在运行中正确结束。回归覆盖空扫描、旧记忆、只发现、床状态不变的交互、真实完成五类。保留已有成就评分口径。

- **D1 终版交叉复核**：r62–r66 的九个完整波中，43 个 `day2_village_search` 终版均为零机器验证。直接实体回执至少确认 1 集见村民，3 集有 `mc_skill_bind_bed ok=true, changed=false`，没有寻村集的成功交易回执。三条床回执分别来自 r64 HY4_10、r66 LC2、r66 HY4_7，需保持「发现」「操作回执」与「整课验证」三项可追溯。 [HY4_10](../runs/r64_day12_full_45m/hy4_hy_10_bare/20261121-062923-563834.events.jsonl#L189) · [LC2](../runs/r66_lc_village_midgame_4bot/lc_lc_2_day2_village_near/20261121-063345-417248.events.jsonl#L148) · [HY4_7](../runs/r66_village_trade_2bot/hy4_hy_7_day2_village_near/20261121-063516-265761.events.jsonl#L162)

### BUG-085 | P1（Fleet 启动可读到含合并标记的工作树，整槽无法导入）| Integrator + Slice D2（`v2_fleet.py`／运行目录）| 状态: OPEN（r68 中期 1 槽启动崩溃）

- **影响与实据**：北京时间约 **23:41:24**，`r68_midgame_portal_3bot` 的 HY4_9 在 actor 创建前退出，无集和终版。traceback 从 runner 导入课程，经 `skills/__init__.py` 到 `skills/library.py:1623`，明确打印 `<<<<<<< .merge_file…`，抛出 `SyntaxError`。同波 NJU_3、HY4_8 更早完成导入，继续运行且指纹 `328d1430f7830c51` 对应 `b328836`。 [HY4_9 traceback](../runs/r68_midgame_portal_3bot/hy4_hy_9_midgame/stdout.log#L15) · [HY4_8 已加载](../runs/r68_midgame_portal_3bot/hy4_hy_8_midgame/20261121-074120-311973.events.jsonl#L1)
- **根因定位**：runner 由集成工作树的可变 `src` 导入共有模块。崩溃日志早于 `23:41:30`、`23:41:48` 的两项合入。可确认 import 读取到冲突标记，单凭日志无法归到某一条合入命令。现 `0a68fc8` 工作树的 45 个 Python 模块均可离线解析，已退出槽缺复跑终版。 [runner 延迟导入](../scripts/l3_wild_endurance_runner.py#L2139) · [包公共导入](../src/mc_agent/skills/__init__.py#L7)
- **最小修复与验收**：Fleet 从同一已校验提交的固定源码目录启动整波，避免槽间启动的间隔读取正在改动的树。夹具覆盖合并标记快照被拒且零子进程，正常快照三槽可导入并记录一致指纹。下一次同组三槽需均有终版，启动 `SyntaxError` 为 0。
- **r69 同波确有两套源码**：`r69_midgame_real_drop_3bot` 的 DS_1、HY4_1 加载 `1cb0d85b7407d8e6=0a68fc8`，约晚 10s 启动的 HY4_2 加载 `07b007e5d536b6ab=f22bfc9`。期间 23:51:56 完成合入。三槽均有 actor，后续合计按场景报告，补丁复验按各槽指纹取证。 [DS_1 指纹](../runs/r69_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-075150-863009.events.jsonl#L1) · [HY4_2 指纹](../runs/r69_midgame_real_drop_3bot/hy4_hy_2_midgame/20261121-075210-667417.events.jsonl#L1)
- **r68 已收官**：两条存活槽均 1800.1s、写出终版。HY4_9 为唯一启动崩溃，整波 **2/3**。 [HY4_8 终版](../runs/r68_midgame_portal_3bot/hy4_hy_8_midgame/wild-endurance-r68_midgame_portal_3bot_hy_8_midgame-20261121-074113.json) · [NJU_3 终版](../runs/r68_midgame_portal_3bot/nju_nju_3_midgame/wild-endurance-r68_midgame_portal_3bot_nju_3_midgame-20261121-074103.json)

### BUG-086 | P0（取消生成时重建 HTTP 客户端失败，整槽崩溃）| Integrator（`harness/model.py`）| 状态: OPEN（r69 HY4_9 无终版）

- **影响与实据**：北京时间 **00:35:33**，`r69_midgame_portal_3bot` HY4_9 EP05 已发 26 次 actor 请求。控制层先抛 `RequestCancelled`，重建 HTTP 客户端时再抛 `ssl.SSLError: [X509] PEM lib`。该异常越过课程 supervisor，HY4_9 无终版，整波 **2/3**。 [取消与清理堆栈](../runs/r69_midgame_portal_3bot/hy4_hy_9_midgame/stdout.log#L593) · [SSL 异常](../runs/r69_midgame_portal_3bot/hy4_hy_9_midgame/stdout.log#L669) · [EP05 终态](../runs/r69_midgame_portal_3bot/hy4_hy_9_midgame/20261121-083201-056436.events.jsonl#L414)
- **源码与最小修复**：`_process_request` 在 `finally` 中直接调用 `on_cancel`。`_abort_and_rotate_client` 建立替换连接池时重读证书链，初始化异常会覆盖原取消异常，新客户端尚未赋值。取消清理应保留 `RequestCancelled`，记录轮换失败并保持可用连接池状态，由后续有界恢复处理。现有证据确认 SSL 初始化错误，文件内容或外部更新来源待查。 [取消清理](../src/mc_agent/harness/model.py#L838) · [客户端轮换](../src/mc_agent/harness/model.py#L863)
- **验收**：离线注入同款 `ssl.SSLError`，原取消语义可见、调用方继续执行，新客户端成功时仍关闭旧池并保持冷却。下一轮加载修复版实际发生生成取消，零同类进程退出并写终版。
- **D1 离线复现**：替换客户端构造时注入 `ssl.SSLError`，真实线程取消路径当前稳定传播 SSL 异常，用例为预期失败。内存对照仅阻止这一清理异常外溢，调用方收到 `RequestCancelled`，下一次离线请求可返回。 [D1 夹具](../../s-triage/tests/test_d1_repro.py#L110)

### BUG-087 | P1（课间就绪轮询遇到传输重置，整槽丢终版）| Integrator（`harness/curriculum.py`／`bot.py`）| 状态: OPEN（r79 中期 DS_1 已确认）

- **影响与实据**：`r79_midgame_real_drop_3bot` DS_1 最后一集在 UTC 23:00:25 以 `disconnected` 结算，随后课间恢复走到 `wait_until_ready`。stdout 堆栈显示 `status()` 的 `httpx.ReadError: [Errno 104] Connection reset by peer` 越过课程 supervisor，退出后无 `wild-endurance` 终版。其他同窗停写槽另按各自证据登记。关联 BUG-020 的共时传输故障。 [最后一集](../runs/r79_midgame_real_drop_3bot/nju_ds_1_midgame/20261121-145253-984910.events.jsonl#L664) · [课间堆栈](../runs/r79_midgame_real_drop_3bot/nju_ds_1_midgame/stdout.log#L373) · [终端异常](../runs/r79_midgame_real_drop_3bot/nju_ds_1_midgame/stdout.log#L423)
- **源码与最小修复**：`wait_until_ready` 能处理 API 级状态，但 `httpx.ReadError` 属于 `httpx.TransportError`。课间捕获清单已处理 API／系统错误，缺该传输类型。将传输故障纳入现有有界恢复，受波次余量和重试上限约束；耗尽时返回可写盘结算，保留原始错误类型与失败阶段。 [课间重连](../src/mc_agent/harness/curriculum.py#L1964) · [现有恢复捕获](../src/mc_agent/harness/curriculum.py#L2155) · [状态轮询](../src/mc_agent/bot.py#L317) · [终版写入](../scripts/l3_wild_endurance_runner.py#L2433)
- **验收**：离线在重连后的状态轮询注入单次 `ReadError`，能够进入下一集或在集槽截止前返回有效结算。持续 `ReadError` 时有界结束、保留诊断并写终版；确认认证错误仍走原有结算。新波遇到同类短暂故障后保留 runner 及终版。
- **D1 离线夹具**：真实 `CurriculumSupervisor`、`BotSession` 和模拟传输执行 `status→connect→status`，单次与持续重置约束两例均复现未捕获 `ReadError`。只在内存中加入 `httpx.TransportError` 到现有捕获清单，两例分别恢复开课与有界结算。 [离线夹具](../../s-triage/tests/test_d1_repro.py#L43)

### BUG-088 | P1（磁石课超额重复投炉，单集额外约 250s）| Slice D2／Integrator（`harness/curriculum.py`）| 状态: OPEN（r81 HY4_1 EP09）

- **影响与实据**。`r81_midgame_real_drop_3bot` HY4_1 EP09 八次对同炉请求熔炼 12 圆石，共 **399.605s**。前三次已从回执确认 **4＋5＋5=14** 石头，足够脚本要求的 12。后五次仍调用同工具同参数，配对约 **249.8s**。随后造出 12 石砖、磁石与指南针，合成 12 石砖后还有 **29** 石头。该集 579.095s 以 `answered` 结束，目标未验证。 [启动任务](../runs/r81_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-193819-486282.events.jsonl#L2) · [第三次取货](../runs/r81_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-193819-486282.events.jsonl#L152) · [第四次再次投炉](../runs/r81_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-193819-486282.events.jsonl#L170) · [合成与库存](../runs/r81_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-193819-486282.events.jsonl#L272) · [终态](../runs/r81_midgame_real_drop_3bot/hy4_hy_1_midgame/20261121-193819-486282.events.jsonl#L547)
- **源码与最小修复**。课程文案给固定数量，`partial_smelt` 返回这次取货并建议查炉。通用防循环把取货视为材料进展，符合技能语义。给磁石任务补库存门槛，累计确认已有 12 石头后立即进入石砖；不足时先取出原炉余货，再按实际缺口添料。保留 BUG-067 的单次时限和部分取货。 [任务配方](../src/mc_agent/harness/curriculum.py#L511) · [部分取货](../src/mc_agent/skills/library.py#L5072) · [进展判定](../src/mc_agent/harness/agent.py#L798)
- **验收**。离线回放 `4→5→5` 的三条部分取货，检查第四次决策进入合成。另测库存仍不足时可先收原炉产物并按缺口补料。实机同类磁石课累计已有 12 石头后新增投料 **0**，并记录磁石绑定的验证终态。

### BUG-089 | P1（死后世界加载等待用错截止变量，课程 supervisor 中断结算）| Integrator（`harness/curriculum.py`）| 状态: FIXED（`317a6c7`，r79 GLM_1 八次后续课并收官）

- **影响与实据**。官方 r78 GLM_1／5Ease 在加载 `d893801` 后分别落 `runtime_error/[not_ready]`，其后旧进程退出。两槽的课程快照分别停在前一集，官方终版均缺。现有轨迹可对到触发输入，两个外层缺直接异常栈。同期 r78 MoveTest 末条为工具启动，同因待证。 [GLM_1 最后一集](../runs/official_r78_40m_glm1/GLM_1/20261121-205109-768232.events.jsonl#L10) · [5Ease 最后一集](../runs/official_r78_40m_5ease/5Ease_Bot/20261121-204912-886489.events.jsonl#L121) · [MoveTest 末条](../runs/official_r78_40m_movetest/MoveTest_01/20261121-204619-523145.events.jsonl#L167)
- **根因与最小修复**。`8df2f2a` 在 `CurriculumSupervisor.run` 的世界加载后等待分支引用了未定义的 `deadline`，位置在写课程快照之前。`317a6c7` 改用本槽已计算的 `deadline_mono`。D1 用原方法离线复现 **2/2 `NameError`**，当前版 **2/2 通过**，分别验证等待 2.5s 后续课以及剩 1s 时有界结算，临时加载不增加普通失败罚次。 [等待分支](../src/mc_agent/harness/curriculum.py#L2746) · [槽截止定义](../src/mc_agent/harness/curriculum.py#L2057) · [离线夹具](../../s-triage/tests/test_d1_repro.py#L111)
- **实机验收**。r78→r79 官方三槽终版 **0/3→3/3**。新版 GLM_1 保持原 PID，前八次 `not_ready` 后第九集有 14 请求，完整槽 **2100.1s／18 集**，外层重连 0。原条件的续课及终版门均通过。单集报废继续按 BUG-020 核验。 [r79 同 PID 后续](../runs/official_r79_35m_glm1/GLM_1/20261121-205329-538873.events.jsonl#L3) · [r79 官方终版](../runs/official_r79_35m_glm1/GLM_1/official-official_r79_35m_glm1-GLM_1-20261121-205140.json#L7) · [官方终版写入](../scripts/run_official_bot.py#L121)

## P2 Backlog（不抢 P0/P1；KB/脚本类由 #6 随时消化，核心代码类由 #2/#3/#4 在各自 P0/P1 清零后顺延处理）

> 来源：Slice D1 `/home/xzq/projects/mc/.worktrees/s-triage/docs/D1-r11-triage.md`（`r11_22bot` 22 路轨迹）。以下均为策略/提示/非阻塞小优化，不丢弃。

- **P2-41** `unstick` 只尝试东向出口。r40 的 37 次执行中 **30** 次被 `guardian.unstick_exit_blocked` 拦截，涉及 **7 路、16 集**，中位 **1.466s**，最长 **11.777s**。出口循环设置首个东向并无条件退出，Guardian 随后只验证该朝向。最小修复是在同一预算内按方向寻找经过现有落脚判定的出口。全部不合格时保留拒绝。验收覆盖东侧悬空但另一方向安全、四向均不安全两个夹具。 [HY4_4 EP03 拒绝](../runs/r40_hy4_combat/hy4_hy_4_cave_combat/20261120-052020-665582.events.jsonl#L48) · [候选朝向](../src/mc_agent/skills/library.py#L5325) · [安全判定](../src/mc_agent/harness/guardian.py#L1563)
- **P2-42** Day 1 黄昏提示在零模型预准备后丢失。选择器给铁器目标添加夜间封井提示，预准备后的成功与失败分支都从静态 playbook 重新生成任务。离线按同一调用顺序回放，两条路径均丢失提示。r41 的 25 次铁器派发均走预准备（7 成功、18 失败），actor 任务里均无该提示，实际选中黄昏分支的次数未记录。最小修复是保留带条件的提示，再按新库存更新材料提示。验收覆盖预准备成功、失败各一例，最终 actor 任务保留黄昏提示。 [选择器](../src/mc_agent/harness/curriculum.py#L1414) · [成功重渲染](../src/mc_agent/harness/curriculum.py#L2215) · [失败重渲染](../src/mc_agent/harness/curriculum.py#L2230) · [r41 失败样本](../runs/r41_day1_15bot/hy4_hy_10_bare/20261120-061436-382140.events.jsonl#L2)

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
  r42 的 `day2_village_search` 同样无成就验证器。HY4_7 EP02 已实际取箱，绑床工具也返回成功交互，两种报表进展字段仍为 false。重生点是否持久未验证。验收 Day 2 据点时逐项引用工具回执。 [取箱](../runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L170) · [床交互](../runs/r42_day2_village_9bot/hy4_hy_7_iron/20261120-063311-834631.events.jsonl#L229) · [Day 2 配置](../src/mc_agent/harness/curriculum.py#L1326)
- **P2-13**（原 BUG-004 降级）`dig_staircase` 地形边界停止：`movement_not_confirmed 9 / missing_step_support 7 / fluid_or_bedrock 8 / cave_or_drop_ahead 4`；66 次调用中位 17.9s、仅 5 次 >60s、max 92.3s。属正常地形判定，已有可执行 hint；只需优化"换向/垫脚"提示与失败方向记忆，不占 P1。
- **P2-14**（持续发生，建议 D2 顺手改）下界 prompt 歧义：任务文本"钻石镐在下界Y8-22挖ancient_debris"被模型读成"在下界挖钻石"。`bad_target_for_dimension` 每波稳定触发 **4~7 次**（r13 5 / r15 4 / r18 7 / r19 7 / r21 6 / r22 6 / r23 4），每次浪费一个模型轮次。建议改为"用钻石镐挖 ancient_debris（下界 Y8-22）；钻石仅存在于主世界"。
- **P2-15** 长尾工具耗时（r12）：`mine_blocks` 单次 **300.6s**（obsidian×4，`temporarily_avoided:1/timeout`，0 产出）、`smelt` 102s（12→9）、`harvest_logs` 87~91s（部分产出）。`mine_blocks` 内部 `mine_deadline` 仅 45~90s（`library.py:2524`），但 `dig`（:2568）未传 `timeout_s`，单次服务器超时即可吃掉全预算。建议同 `_StairSession` 加硬 deadline。
- **P2-16** `mc_move_to` 在动作租约未释放时阻塞 ~101s 才返回 `busy: running move_to`（3 bots：`glm_glm_5_midgame`、`hy4_hy_7_village`、`hy4_hy_10_enchant_anvil`）。建议 `busy` 前置探测（<2s）或直接返回 `action_busy`。
- **P2-27** `mc_skill_brew` 参数契约（r16 早期 8 次调用）：`bottles:[{"item":"water_bottle","slot":0}]` → `bad_args`（未说明期望 schema，模型重复传）；`fuel_item=blaze_powder` → `missing_item`（未回报剩余数量）。建议 bottles 容忍 `item/slot/count` 三种写法，并在 `missing_item` 中返回库存计数。
- **P2-31** 无界半径搜索（r20_long `glm_5_midgame` 找床）：`mc_get_entities`/`mc_find_blocks` 半径 64→128→200→300→400→600→800→1000 连发 10+ 次，全部 `ok` 但无结果；随后 `craft_gear(white_bed)` 缺羊毛。建议 KB/工具层把探索半径限制在 128，超过时改为"沿一个方向跑图 + 途中扫描"，并提示先找羊（`mc_get_entities(types=['animal'], radius=64)` 已够）。
- **P2-30** `pickaxe_low` 发生在地下且无木头时（r20_long `ds_1_bare`：y=17 断镐，背包无原木），需要 ~5 次 `climb_out`（每片只上升 3~5 格）才能回地表补镐；期间 `craft_gear` 只能报 `missing_ingredients`。建议 `pickaxe_low` 直接给出"用剩余耐久挖 3 石→就地补镐"或"一次 climb_out 到目标 y"的量化建议。
- **P2-29（正向确认）** `staircase` 的 `no_support` 兜底已生效：`glm_5` 的 `vertical=up, steps=20` 先 `missing_step_support`，随后自动 `pillar_fallback` 成功——BUG-024 修复在长任务中同样工作。
- **P2-29** `mc_skill_tunnel:unsafe_or_unknown_floor` 13 次（r20_short）：多为洞穴/悬空边缘的正常拒绝，但同一站位反复尝试。建议 tunnel 失败后记忆该方向 30s 并提示换向。
- **P2-28（升级关注）** `mc_skill_smelt:missing_fuel`：r20_long `ds_1_bare` 报"背包没有煤炭、木炭、木板或原木燃料"，直接卡住 `bootstrap_iron_age`。建议 `missing_fuel` 的 message 明确给出 `mc_skill_harvest_logs(target_logs=2)` 下一步，并在有原木时自动转换木板作燃料（`_select_safe_fuel` 未覆盖"零燃料但有原木"的组合）。
- **P2-26** `mine_dfs` 的 `stop_reason` 被拼进整段诊断串，如 `backtrack_blocked@-11011,34,-11125(feet=air,head=air,from=-11011,33,-11126)`（r15 至少 5 次）。建议固定枚举 `stop_reason`，坐标/脚部方块放 `blocker` 字段，否则聚合统计与模型分支判断都不可靠。
- **P2-25**（课程观察，**不改顺序/权重**）`shiny_gear` 在同一 bot 连续 2 次 `timeout`（`glm_3` 28+22 calls、`hy4_4` 40+36 calls），`strike_limit=2` 仅在 `progress_made=False` 时累计；一旦拾到 1 颗钻石/换镐就会清零 strike，导致 20 分钟内反复选中。建议 D2 评估"仅按 gained 成就清零 strike"是否更合理。
- **P2-24（已修复）** `mc_window_view` 对村民交易：`glm_glm_5_midgame` 10 次调用 **6 stalled + 3 no_window**（66.7s）。`9ecb239` 已改为"NoAI 村民 + 4m 前置移动"；r20_short 实测 `window_view ok 2 / out_of_reach 1 / no_window 0`，`mc_skill_trade ok 2` → 通过。
- **P2-21** `mc_dig:timeout` 4→15 次 / 7 bots（r15）：`dig` 单次超时后模型重试同格。建议 `dig` 返回 `timeout` 时自动标记该格 15s 避开并给替代格。
- **P2-22** `mc_place:no_support` 6 次 / 5 bots（r15）：与 BUG-024 同源，原始 `mc_place` 也应给"先 dig 侧袋/换参考面"的可执行提示。
- **P2-23** `staircase:time_budget` 仍 36 次 / 8 bots（r15）：45s 硬帽的 churn 仍在，随 P2-20 一起评估。
- **P2-19** `harvest_logs` 新增 `time_budget` 早退（r14：18 次/6 bots，max 145.8→75.0s）：提示建议降 `count` 或先靠近目标 2 格，避免连续早退。
- **P2-20**（升级，r20_long 定量）`staircase` 45s 硬帽 vs 模型请求 `steps` 中位 **40**：`ds_1_bare` 6 次 `time_budget` 每次只完成 7~9 步，等于把一个 40 步请求拆成 5~6 次调用。建议 ① 工具入口 `steps=min(steps, 10)` 并把可完成步数回传；② 或预算 `min(90, 8 + steps*5)` 让长请求真正完成。
- **P2-18** `mc_skill_tunnel:missing_pickaxe` 15 次 / 3+bots（r13 新增）：镐子耗尽/未装备时直接失败。建议 tunnel 先自动换镐/合成石镐，再返回 `missing_pickaxe`。
- **P2-17（已修复）** 模型参数串位：`mc_move_to {"x":-7828,"y":-2814,"z":-2744}`（y 填成了 z）。越界保护已忽略该 y，但浪费一次 90.9s `path_not_found`。建议在参数校验层对 `|y|>320` 直接 `bad_args` 并提示。 

---

## 2026-11-20 过夜长短轨逐条复核

覆盖北京时间 01:22–09:27 的 **6 个波次、40 条 Bot 轨迹、1,581 集、667,056 条事件记录**，实际累计 124.8 Bot 小时。以最终 `curriculum-*.json` 为集目录，逐集匹配报告和原始 JSONL 并核对完成回执、失败、抢占、退避与最后一次增分。1,528 集有 `run.finished`，其余 53 集有课程收尾记录（40 次预准备短路、13 次 `paused_for_world_job`，都为零模型请求）。有运行打点的集使用同一 Python 源码指纹 `c395de55e2b26f7d`，与审查时工作树源码一致。 [全量逐集证据索引](overnight-20261120-episode-index.md)

表中分数含场景预授，`最后增分` 取真实 `score_delta > 0` 的最后一集。目标的 `0/N` 用 `objective_verified`，该集仍可能靠其他成就增分。死亡按明确 `[死亡 …]` 回执计。找到候选资源与突破瓶颈分别判断，正常的波次收尾缩短预算另记在索引。

六波合计，发光告示牌 **0/116**、骑炽足兽 **0/57**、种植 **1/78**、睡觉 **2/79**。这些重复派发与单次动作超时分别跟踪在 BUG-050/054/060 和 BUG-047/056。 [逐集验证状态与失败行](overnight-20261120-episode-index.md)

### 主轨第一波 `overnight_12bot`（12 路 / 582 集 / 4 小时）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `ds_ds_1_bare` | 35 / 55 / EP35 | EP06 伐木 179s 超预算。EP19 丢铁镐触发门控，EP25–33 在地下反复遇到工作台与木料缺口，`need_crafting_table` 14 次。重复补石/铁 16+8 集。EP35 有旁路增分，`shiny_gear` 目标仍超时。 | BUG-032/038/043/047/052 |
| `ds_ds_2_cave_combat` | 58 / 120 / EP21 | 先拿杀怪和钻石，EP04/07/08 因 `lost_iron_pickaxe` 中止。EP22–58 无增分。`shoot_arrow`、`plant_seed`、`heart_transplanter` 各 0/6，返地表阶梯 63 次 `time_budget`，EP44 的 `move_to:busy` 约 102s。 | BUG-037/043/047/050/051/054 |
| `ds_ds_3_enchant_anvil` | 41 / 150 / EP12 | EP01 先侦察，遭遇水域死亡后没有利用开局经验与附魔台。`enchant_item` 0/7、`lava_bucket` 0/10，EP13–41 没有增分。EP02/28 都有约 102s 的 `move_to:busy`，整路有 23 条死亡回执。 | BUG-026/047/050/053 |
| `ds_ds_4_nether_fortress` | 43 / 210 / EP13 | `ancient_debris` 0/12、`ride_strider` 0/12、哭泣黑曜石 0/8。EP11/12 是 MC API `ReadTimeout`。明确死亡 41 条，含岩浆 20、岩浆块 10。末集仍在退避、迫降与短程寻路失败之间循环。 | BUG-026/044/047/050/057/058 |
| `glm_glm_1_nether_fortress` | 34 / 205 / EP03 | 找到堡垒后 31 集无增分。`ancient_debris` 0/11、`ride_strider` 0/7。死亡后反复进入补镐支线，`tunnel:missing_pickaxe` 31 次、合成满包 24 次。模型 429 硬失败 3 集，另有 1 集 MC API 超时。 | BUG-042/044/045/049/050/052 |
| `glm_glm_2_nether_bastion` | 30 / 270 / EP25 | 残骸目标有 1/11 成功。堡垒 0/10、炽足兽 0/6；狭窄地形下阶梯 `pillar_fallback` 46 次、移动未确认 75 次。EP09 到堡垒的单次移动等了约 152s 后仍无路径。 | BUG-037/045/047/050/055/058 |
| `glm_glm_3_village_trade` | 61 / 155 / EP58 | EP01/02 交易通过，之后并行卡在地下补装备、找地表资源。`plant_seed` 0/8，高空交易 0/4；EP55 确认地表村民在场，但无已验证的垂直搬运和高空交易链。EP08/60/61 各遇一次约 102–103s 的移动锁等待。 | BUG-047/050/051/052/054 |
| `glm_glm_4_brewing_alchemy` | 56 / 195 / EP13 | 开局酿造成功，EP08 后继续在地下接到睡觉、繁殖、种植各 6 次且均未验证。末段合成/熔炼分别有 8/10 次 `inventory_full`。EP14–56 不涨分，堡垒线未能接上。 | BUG-029/051/052/053/054/059 |
| `glm_glm_5_midgame` | 72 / 185 / EP56 | 早段做出磁石与附魔，后续 `lava_bucket` 0/14、发光牌 0/8、移植树脂核心 0/7。找不到资源后目标反复重派；最后两集种植也未验证。EP72 同时遇 429 退避和桶交互垂直距离拒绝。 | BUG-042/045/050/054/060 |
| `nju_guitest_01_nether` | 75 / 180 / EP06 | 69 集未涨分。EP22 起多工具共 154 次围栏拒绝，转阶梯仍有 55 次垫高回退。附魔 0/9，炽足兽、堡垒、哭泣黑曜石各 0/4。EP66/67 钓鱼走错入口，有竿和静水时仍未见工作回执。 | BUG-048/050/055/059/061 |
| `nju_movetest_01_bare` | 21 / 25 / EP09 | EP10 开钻石任务，在积水路径和爬升之间耗掉完整长集。EP11–20 **连续 10 集**各被 `boundary_escape_water` 约 499s 占住，最后一集仅余很短收尾预算。补石器 13 集仅 1 次验证。 | BUG-032/038/047/056 |
| `nju_nju_3_village_trade` | 56 / 150 / EP45 | 交易拿分后，EP11 有钻石但缺可用工作台/木料；合成 `need_crafting_table` 49 次，阶梯到时限 75 次。高空交易 EP54/55 无有效搬运链，末集附魔仍在阶梯与反射抢占之间打转。 | BUG-037/050/051/052/055 |

### 主轨第二波 `overnight_12bot_part2`（12 路 / 596 集 / 14,100 秒）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `ds_ds_1_bare` | 40 / 105 / EP21 | 四集下界合金锄 0/4，接目标时仍缺跨维度、镐或模板前置。钓鱼 0/8，既有岸边错入口，也有入水后缺工作台。EP24 解卡单次 **404.4s**；EP22–40 无增分。 | BUG-032/047/050/056/061 |
| `ds_ds_2_cave_combat` | 64 / 150 / EP13 | 钻石线推进后，EP14–64 无增分；短目标射箭/种植均 0/8，睡觉 0/6。阶梯 25 次退化为垫高，`move_to` 有 43 次位移未确认；返铁器验证成功主要恢复装备，没有净新增分。 | BUG-037/050/051/054/055/059 |
| `ds_ds_3_enchant_anvil` | 45 / 135 / EP18 | 先侦察，附魔仍 0/7；睡觉 0/8、钓鱼 0/6，EP15/25/39/40 反复以右键水格代抛竿。EP02/24/36 均卡 `busy` 约 102–103s。EP14/16 的磁石暂停是世界任务等待，EP18 恢复完成。 | BUG-047/050/053/054/061 |
| `ds_ds_4_nether_fortress` | 57 / 210 / EP21 | 目标骑炽足兽 0/13、残骸 0/12、哭泣黑曜石 0/10、凋灵头 0/7。53 条明确死亡（岩浆 20、岩浆块 10）；EP09 连死后 `not_connected`，另 3 集 MC API 超时。EP22–57 未再涨分。 | BUG-026/044/050/057、P2-34 |
| `glm_glm_1_nether_fortress` | 80 / 240 / EP04 | EP05 受击死亡，EP06 起下界反复补主世界石/铁，其中 **0/52 + 0/23**。EP18 等事件有 `the_nether` 实证；伐木无目标 89 次、材料缺失 95 次。16 集 429，撞 80 集限早停约 28 分钟。 | BUG-042/045/046/049 |
| `glm_glm_2_nether_bastion` | 33 / 270 / EP25 | 残骸有 1/14 成功，堡垒 0/11，炽足兽 0/4。阶梯垫高 35 次，岩浆/掉落/岩浆块伤害拖慢移动。EP11/33 移动约 102s 后才报 `busy`，9 集 429。 | BUG-026/045/047/050/055/057/058 |
| `glm_glm_3_village_trade` | 32 / 90 / EP24 | 开局交易有效。`shiny_gear` 0/7，37 次 `climb_progress` 反复烧在返程；EP21 磁石等世界任务后 EP24 达成。EP29 暂停来自 `preempt_opportunity`，独立于失败。末段钻石任务又因移动和抢占耗尽预算。 | BUG-026/045/050/052/059 |
| `glm_glm_4_brewing_alchemy` | 45 / 145 / EP40 | EP01/02 瓶子装水假成功，EP03 是 429；没拿到开局酿造。EP20 起围栏拒绝累计 129 次。发光牌 0/11，另 9 集 429；EP40 磁石增分后再次困在台阶和合成。 | BUG-045/048/050/051/062 |
| `glm_glm_5_midgame` | 80 / 180 / EP09 | EP10–80 无净增分；岩浆桶 0/14、树脂核心 0/14、种植 0/12、悦灵 0/7。15 集 429，EP19/27 的 `move_to:busy` 各约 103s，80 集提前 3745s 收尾。 | BUG-042/045/046/047/050/054 |
| `nju_guitest_01_nether` | 41 / 200 / EP40 | EP02 进下界，残骸 0/2、附魔 0/11、发光牌 0/7。阶梯垫高 43 次，最长一段 29 次。EP38 等磁石的世界任务后 EP40 成功；EP41 回矿 692s 仍未附魔。 | BUG-044/050/053/055/059 |
| `nju_movetest_01_bare` | 30 / 70 / EP26 | 补石/铁器花掉 13 集，三次伐木达到约 90s。EP26 桶目标成功，EP27/28 黑曜石目标困于阶梯、窒息或返矿；EP30 装备目标再次超时。 | BUG-026/032/037/038/047/052 |
| `nju_nju_3_village_trade` | 49 / 150 / EP21 | 早段交易、钻石和磁石有效，EP22–49 死循环恢复原料和工具。补石器 1/17、补铁器 0/8，缺工作台和找不到木头各 55 次。EP24 熔炼单次约 101s，后续爬阶梯积累 83 次 `time_budget`。 | BUG-032/037/047/052/055/059 |

### hy4 短轨 `hy4_audit_r25`（4 路 / 17 集 / 配置 20 分钟）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `hy4_hy_2_iron` | 3 / 20 / 无 | EP01 侦察无增分，EP02/03 钻石装备任务均超时。挖掘虽有 16 个进展切片，中间遇到 `shaft_exhausted` / `no_progress`，波次内没完成材料链。 | BUG-030/037/051 |
| `hy4_hy_5_brewing_alchemy` | 8 / 75 / EP06 | 开局酿造已拿分，EP06 补铁时挖掘/熔炼耗光时间但有旁路增分；EP07 预准备短路，EP08 钻石线因受击停挖后超时。 | BUG-026/037/043 |
| `hy4_hy_6_nether_bastion` | 3 / 210 / EP03 | 残骸两集未验证，期间遇到错误矿种与井重置。全程 6 次 `path_not_found`，EP03 寻堡垒时遇到岩浆块伤害和无安全撤退。4 条死亡集中在 2 集。 | BUG-026/030/050/057/058 |
| `hy4_hy_9_deep_water` | 3 / 30 / EP02 | 侦察后两集钻石任务均卡在切片与回程。EP02 单次挖掘约 85s，有击杀增分；EP03 收尾无伤亡但仍未合成装备。 | BUG-030/037/038/059 |

### hy4 审计 `hy4_audit_r26`（4 路 / 92 集 / 配置 2 小时）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `hy4_hy_2_iron` | 18 / 100 / EP04 | EP02 装备目标成功，EP05–18 停分。发光牌和弩各 0/4。EP15 已有移动失败链，随后 MC API `ReadTimeout`；整路 13 条死亡，另有 10 次移动 `stalled`。 | BUG-026/044/047/050/060 |
| `hy4_hy_5_brewing_alchemy` | 19 / 105 / EP18 | 酿造后恢复石/铁器 13 集，中间合成满包 7 次、爬升进展切片 21 次。EP18 有钻石成就旁路增分，EP19 未做出目标装备。 | BUG-032/038/052/059 |
| `hy4_hy_6_nether_bastion` | 20 / 210 / EP10 | 残骸 0/7、堡垒 0/6、炽足兽 0/4。EP03 有连续岩浆逃生失败，整轨 20 条死亡分布在 11 集。EP06 是 502。EP11–20 无增分，寻路无路径 60 次。 | BUG-026/050/055/057/058 |
| `hy4_hy_9_deep_water` | 35 / 120 / EP09 | 钻石装备在 EP09 验证，之后岩浆桶、发光牌、床和多种稀有目标重派，无净增分。挖掘有 5 个同位置无掉落切片，另 2 条溺水回执。 | BUG-023/030/050/054/056/060 |

### hy4 审计 `hy4_audit_r27`（4 路 / 127 集 / 配置 3 小时）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `hy4_hy_2_iron` | 27 / 100 / EP16 | **EP02–12 共 126 次围栏拒绝**，覆盖阶梯、寻路、隧道、挖矿等，钻石任务 0/4。EP16 才再涨分，之后短地表任务和稀有结构搜索仍无进展。 | BUG-047/048/050/051/054 |
| `hy4_hy_5_brewing_alchemy` | 31 / 165 / EP14 | 开局酿造成交，补装备与回程反复。EP04/06 移动锁约 102–103s；`craft_gear:inventory_full` 17 次。EP14 后因岩浆桶距离、找不到发光物资和满包附魔支线停分。 | BUG-029/047/050/052/059 |
| `hy4_hy_6_nether_bastion` | 50 / 195 / EP08 | EP09–50 反复派主世界石/铁器共 **42 集全零**；在下界遭遇缺原木、镐等级和挖不出掉落，找路失败 72 次。另有 1 集输出截断报废。 | BUG-026/049/055/058、P2-33 |
| `hy4_hy_9_deep_water` | 19 / 60 / EP05 | EP05 磁石成功，EP07 装备门控中止，EP08 起多次补石/铁。EP11–19 **九集**被约 560.5s 的边界浮水占住；整路 13 集超时、无明确死亡回执，瓶颈是动作时限与脱困。 | BUG-032/043/047/056 |

### hy4 审计 `hy4_audit_r28`（4 路 / 167 集 / 配置 2.5 小时）

| Bot | 集数 / 终分 / 最后增分 | 逐条复核的停滞链 | 工单 |
|---|---|---|---|
| `hy4_hy_2_iron` | 50 / 135 / EP26 | 前段磁石、矿线有进展，之后多次搜不到目标结构。种植 0/6、树脂核心 0/4，EP50 仍在找催发体；无 429，但 50 集配额提前约 6 分钟用尽。 | BUG-046/050/051/054/060 |
| `hy4_hy_5_brewing_alchemy` | 50 / 165 / EP13 | EP01/02 装水并酿造成功，EP14–50 无增分。治愈村民 0/4、末影之眼 0/3（EP25/50 缺料，EP26 报缺用眼入口）、钓鱼 EP43/44 再走错接口。无 429 但提前约 18 分钟触集数限。 | BUG-046/050/053/061 |
| `hy4_hy_6_nether_bastion` | 24 / 210 / EP05 | EP06–24 无增分，残骸与堡垒均 0/8。阶梯垫高 66 次、寻路无路径 80 次；明确死亡 17 条含岩浆 8 条，EP12 两条岩浆死亡，EP23/24 仍失败。 | BUG-026/050/055/057/058 |
| `hy4_hy_9_deep_water` | 43 / 135 / EP25 | 磁石 EP25 再得分；从 EP26 岩浆桶找路失败开始停分。种植 0/5、树脂核心和催发体各 0/4，另有 5 次围栏拒绝与 3 条溺水回执。 | BUG-023/048/050/054/056 |

## 场景需求队列（Scene Requests — 由 #1~#5 提单，#6 DeepSeek #2 按单实现）

### SCENE-001 | 状态: FIXED（`7d5a571`，r16 已实测交易成功）| 提单人: #5 D1 | stage: village_trade / brewing_alchemy / cave_combat

- **r67 `village_trade` 实测**：HY4_7 和 MoveTest_01 EP01 各真实交付 24 张纸取得 1 颗绿宝石，商人同一交易 `uses` 0→1，均验证 `minecraft:adventure/trade`、各 +15。LC1/LC2 四条初始交易课均硬 429，未调工具。 [HY4_7 交易回执](../runs/r67_village_trade_4bot/hy4_hy_7_village_trade/20261121-070643-268665.events.jsonl#L57) · [MoveTest 交易回执](../runs/r67_village_trade_4bot/nju_movetest_01_village_trade/20261121-070633-745290.events.jsonl#L107)
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

### SCENE-002 | 状态: READY_FOR_RUN（r43 落点和床重生已验证，补给交易链待复验）| 提单人: #1 主 AGY | stage名: `day2_village_near`（Slice D2 `#6 DeepSeek #2`）
- **地形/结构要素**：用 `locate structure #minecraft:village` 定位真实村庄，但把 Bot 出生点设在村庄外围 **120–180m** 的主世界安全地表（避免直接降落在村中心，专门考验中距发现+进村）。
- **初始背包与成就**：同 `iron` 阶段（铁镐/铁斧/铁盾/火把/圆石/面包/木头），时间设为 **Day 2 早晨（`time set 1000`）**，确保有完整白天验证寻村与占屋。
- **死亡拉回规则**：沿用主世界安全地表出生点，禁止误跨维度（复用 `5e871e6` 护栏）。
- **核心验证目标**：端到端验证 `day2_village_search` 完整闭环（发现 `bell`/`dirt_path`/`hay_block` → 占领村民屋 `bind_bed` 绑重生点 → 薅干草块/箱子 → 交易或入夜切地下矿井），避免随机空降 9 台全在 300m+ 荒野测不到进村逻辑。
- **r43 收官**：9/9 验证落点，距对应村庄中心 **135.85–164.78m**。9 路强制寻村首集全用满 14 请求，4 路找到结构线索，HY4_7 首集绑定后在 EP04 死亡实测重生在床旁 **1.58m**。全波无取箱、干草入包或交易。首集 25 次实体查询中只有 1 次含村民，随后 Day 1 侦察才实际看到村民。续验村民过滤与查到目标后的补给／交易衔接。 [范围下端](../runs/r43_day2_village_near_9bot/hy4_hy_4_day2_village_near/wild-endurance-r43_day2_village_near_9bot_hy_4_day2_village_near-20261120-071619.json#L37) · [绑床](../runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-071642-835969.events.jsonl#L266) · [重生](../runs/r43_day2_village_near_9bot/hy4_hy_7_day2_village_near/20261120-072929-890490.events.jsonl#L140) · [后来看到四位村民](../runs/r43_day2_village_near_9bot/nju_ds_1_day2_village_near/20261120-071902-718275.events.jsonl#L225)

- **r57 随机裸装接续**：与 r56 同四路，铁器验证 **2/12→3/9**，Day 2 寻村派发 **0→1**。HY4_4 EP05 用已有铁锭自动合成铁镐，EP06 三次实走合计 364.50m，期间一次近战死亡，三次扫描没有村庄 POI 或村民。r58 日出波加载 `9e13605` 的地下派单和先回地表提示，等待该条件实机。 [零模型合成](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/20261121-011558-233184.json#L40) · [实际派单](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/20261121-011611-871769.events.jsonl#L1) · [第三次斥候](../runs/r57_day1_day2_fast_4bot/hy4_hy_4_bare/20261121-011611-871769.events.jsonl#L110) · [日出选单条件](../src/mc_agent/harness/curriculum.py#L1541)

- **r58 日出终版**：HY4_3 在首条可确认站位 Y52、DS_2 在 Y101 接续寻村，各两集，均未见村庄线索。HY4_3 能完成部分斥候路段；DS_2 两集七条斥候回执均为 0m。四次任务均没有地下先回地表的提示，Y<48 实机派单仍待验证。 [HY4_3 位移](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-014334-214769.events.jsonl#L21) · [日出状态](../runs/r58_day12_sunrise_village_4bot/hy4_hy_3_bare/20261121-014334-214769.events.jsonl#L67) · [DS_2 首条原地斥候](../runs/r58_day12_sunrise_village_4bot/nju_ds_2_bare/20261121-014507-509185.events.jsonl#L54)

- **r59 日出终版**：HY4_10 EP07 真制铁镐，随后两次白天寻村。EP08 实走 145.13m 得土路和蓝床，EP09 再扫到蓝床。最近实走仍距床约 42m，0 绑床或交易，寻村验证 0/2。其它三路无寻村派发。 [土路与床](../runs/r59_day12_sunrise_village_4bot/hy4_hy_10_bare/20261121-022210-195645.events.jsonl#L34) · [实物扫描](../runs/r59_day12_sunrise_village_4bot/hy4_hy_10_bare/20261121-022422-378685.events.jsonl#L37) · [最近移动](../runs/r59_day12_sunrise_village_4bot/hy4_hy_10_bare/20261121-022422-378685.events.jsonl#L56)

- **r60 裸装日出真实低位派单**：DS_2 EP07／09 分别在 Y16／26 收到新版“先回地表”任务。两集均实际调用回程，最终未进入地表斥候。第一集支持不足且中途转夜，次集到 Y35 随波结束。地下派单已实机复验，实地出井寻村仍待完整链。 [Y16 派单](../runs/r60_day12_full_45m/nju_ds_2_bare/20261121-030135-415499.events.jsonl#L1) · [Y26 派单](../runs/r60_day12_full_45m/nju_ds_2_bare/20261121-031515-000911.events.jsonl#L1)

- **r66 补给交易复验**：`r66_village_trade_2bot` 2/2 收官，两槽落点距各自村庄 138–146m。HY4_7 EP01 实扫床与村落结构，绑床返回 `ok=true, changed=false`，两条箱子调用均未取物，一条明确要求 `put` 或 `take`。整波没有交易工具调用，故补给交易链仍待验。 [斥候命中](../runs/r66_village_trade_2bot/hy4_hy_7_day2_village_near/20261121-063516-265761.events.jsonl#L42) · [绑床](../runs/r66_village_trade_2bot/hy4_hy_7_day2_village_near/20261121-063516-265761.events.jsonl#L162) · [取箱参数](../runs/r66_village_trade_2bot/hy4_hy_7_day2_village_near/20261121-063516-265761.events.jsonl#L270)

### SCENE-003 | 状态: READY_FOR_RUN（`c9e550f` 已合入，`--stage village_to_stronghold_end` 就绪）| 提单人: #1 主 AGY（用户规划阶段三）| stage名: `village_to_stronghold_end`（Slice D2 `#6 DeepSeek #2`）
- **地形/结构要素**：定位主世界真实村庄安全地表作为起点（距离最近主世界要塞 `#minecraft:stronghold` 在合理可追范围，记录要塞坐标用于汇总但不泄露给模型）。
- **初始背包与成就**：钻镐/钻剑/铁或钻甲/盾牌/食物/圆石/火把 + **14 颗 `ender_eye`**（或等量 `ender_pearl` + `blaze_powder`），已解锁铁/钻基础成就，早晨开局。
- **死亡拉回规则**：绑定村庄出生点，禁止非传送门意外跨维度（进入 `the_end` 视为合法终局晋级！）。
- **核心验证目标**：端到端验证“从村庄据点出发 → 掷末影之眼测向 → 复用高速斥候追眼定位要塞 → 下挖进传送门房 → 填眼激活进入末地（`follow_ender_eye` + `enter_the_end`）”。
- **r49 终版**：5/5 有终版文件，仅 HY4_1 与 MoveTest 完成运行，各跑满 1200s；HY4_2/3 预准备远程命令 30s 超时、GuiTest 找不到满足 120–600m 的安全村庄。实际 5 集皆派 `follow_ender_eye`，0 集验证；测向反转追踪见 BUG-081。应把安全落点预检通过数和实际端到端通过数分别记录。 [终版](../runs/r49_stronghold_end_5bot/triage-r49_stronghold_end_5bot.md#L3) · [场景准备超时](../runs/r49_stronghold_end_5bot/hy4_hy_2_village_to_stronghold_end/wild-endurance-r49_stronghold_end_5bot_hy_2_village_to_stronghold_end-20261120-104353.json#L13) · [村庄约束未匹配](../runs/r49_stronghold_end_5bot/nju_guitest_01_village_to_stronghold_end/wild-endurance-r49_stronghold_end_5bot_guitest_01_village_to_stronghold_end-20261120-104349.json#L13)
- **r51 落点复盘**：HY4_1 两次都在主世界，水平偏差 1.10／3.94m，高差 0.4／0.6m，都符合 6m／2m 容差。剩下的失败判定在干燥立足条件，日志缺 `on_ground`、`in_water`、`in_lava`。最后一次重置后未再次验证。实机启动复验可记录三个标志，以及最后一次重置后的采样结果。 [失败与重置](../runs/r51_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/stdout.log#L1) · [校验条件](../scripts/l3_wild_endurance_runner.py#L1592)
- **r54 收官**：磁盘 3 个终版槽，HY4_1／2 开跑，HY4_3 未匹配距要塞 120–600m 的安全村庄。两路 9 集要塞任务 0 验证、末地进入 0/2，终包末影之眼均为 0。HY4_2 EP01 搜到石砖后走到附近，往下分别受水和岩浆阻断，在 Y53 仍能扫到石砖而无门框。端到端需继续验证安全下探、门框填眼及维度接入。 [村庄预检](../runs/r54_stronghold_end_4bot/hy4_hy_3_village_to_stronghold_end/wild-endurance-r54_stronghold_end_4bot_hy_3_village_to_stronghold_end-20261120-231028.json#L13) · [地表发现](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/20261120-231037-069823.events.jsonl#L578) · [下探遇水](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/20261120-231037-069823.events.jsonl#L651) · [下探遇岩浆](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/20261120-231037-069823.events.jsonl#L751) · [活跃槽终版](../runs/r54_stronghold_end_4bot/hy4_hy_2_village_to_stronghold_end/wild-endurance-r54_stronghold_end_4bot_hy_2_village_to_stronghold_end-20261120-231020.json#L46)
- **r56 进入要塞，末地仍待**：HY4_1 EP03 验证要塞 +60，EP04 开场即扫到 **12 个 Y−15 的空眼门框**，最近 57.5m。两次直接寻路未位移，后续挖隧道将门框距离缩到 16.7m，向上施工两次被战斗抢占，集限内 0 次填眼。EP04 开场和终版都只有 **11 眼**，差 1 眼；两路终态均主世界，末地 0/2。HY4_2 缓存估点的误差一度由 12.04m 增至 71.47m。继续分别核对估点筛选、门框的可达路径和实际填眼。 [要塞验证](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002233-173459.events.jsonl#L340) · [首次空门框](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002551-640618.events.jsonl#L22) · [11 眼库存](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002551-640618.events.jsonl#L46) · [集尾门框](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/20261121-002551-640618.events.jsonl#L692) · [终版未入末地](../runs/r56_stronghold_triangulate_3bot/hy4_hy_1_village_to_stronghold_end/wild-endurance-r56_stronghold_triangulate_3bot_hy_1_village_to_stronghold_end-20261121-000244.json#L46)
- **r57 开跑时的末地候选**：`cb3d956` 的通用课程增加依据门框海拔开路和逐一填眼，实际首批指纹 `083edb69731c5bd6` 已加载。场景仍从 `STRONGHOLD_FOCUS` 重建两条任务，actor 使用选中任务的文本。HY4_1 首集仍为八步追眼旧场景稿，初始实际 14 眼。要塞验证后需核对真正派单内容、门框可达性、眼数量及跨维度。 [版本和首集文本](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-003500-968952.events.jsonl#L1) · [初始眼数](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-003500-968952.events.jsonl#L20) · [通用末地任务](../src/mc_agent/harness/curriculum.py#L814) · [实际场景任务](../scripts/l3_wild_endurance_runner.py#L1800)


- **r57 末地派单覆盖已证实**：HY4_1 EP04 真派 `enter_the_end`，启动文本仍是场景稿，缺通用新版受阻挖通指引。门框 `eye=false` **12**，背包 **7 眼**，最近从 **35.2→20.8m**。六条无路，0 隧道／楼梯／填眼，180.7s 超时；三路末地均未验证。修复场景入口后，用同一要塞核对派单文本、持续挖通、填满所需眼和末地状态。 [实际末地稿](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L1) · [7 眼](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L22) · [最近框](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L301) · [超时](../runs/r57_stronghold_end_3bot/hy4_hy_1_village_to_stronghold_end/20261121-010200-251477.events.jsonl#L388)
- **r58 要塞终版**：同两个实际槽、同村庄和要塞定位，30→45 分钟，真实出生点各有变化。要塞验证 **1→0**、末地派发 **1→0**。HY4_1 EP03 近处扫描见要塞石砖，HY4_2 EP04 扫到 Y54 地标但下挖前死亡。11 集均为追眼，无找到门框、填眼或跨维度回执。 [HY4_1 地标](../runs/r58_stronghold_end_45m/hy4_hy_1_village_to_stronghold_end/20261121-012627-132142.events.jsonl#L372) · [HY4_2 地标](../runs/r58_stronghold_end_45m/hy4_hy_2_village_to_stronghold_end/20261121-013541-415229.events.jsonl#L196) · [HY4_2 随后死亡](../runs/r58_stronghold_end_45m/hy4_hy_2_village_to_stronghold_end/20261121-013541-415229.events.jsonl#L333)
- **r59 两次实际末地派单**：HY4_2 EP03 扫到 12 框、下到 Y−17 后验证要塞。EP04 实际最近约 14.65m、EP05 约 18.91m（水平与高度合计）；两集各尝试 5 次隧道，仍无近距可填眼位。12 框始终 1 枚眼，背包 8 枚，缺 3 枚。两集均使用旧场景稿，无填眼工具请求及末地维度回执。 [要塞发现](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-021541-139945.events.jsonl#L418) · [EP04 最近](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-022007-633658.events.jsonl#L28) · [EP05 八眼](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-022731-782008.events.jsonl#L669) · [隧道因未知地板停](../runs/r59_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-022731-782008.events.jsonl#L802)
- **r60 要塞带一崩收官**：HY4_2 EP04 验证要塞 +60，EP05 真实末地派单，背包 10 眼、两次半径 64 门框扫描皆 0。约 323s 因发现取岩浆桶机会被暂停，末地未验证。HY4_1 未就绪、MoveTest 崩溃，后者见 BUG-083。 [要塞验证](../runs/r60_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-030645-991143.events.jsonl#L93) · [末地首扫](../runs/r60_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-030757-260278.events.jsonl#L28) · [暂停原因](../runs/r60_stronghold_end_40m/hy4_hy_2_village_to_stronghold_end/20261121-030757-260278.json#L7863)

> 任何会话需要特定实机复现/验收环境时，按以下 5 行模板追加需求单；`#6` 实现为 `l3_wild_endurance_runner.py --stage <name>` 并接入 `v2_fleet.py` 后标为 `READY_FOR_RUN`，由 `#1 主 AGY` 合入上机。
>
> ```markdown
> ### SCENE-XXX | 状态: OPEN | 提单人: #X | stage名: <name>
> - **地形/结构要素**：(如 locate fortress / fill 3x3 岩浆池 / 混合有业+无业村民)
> - **初始背包与成就**：(...)
> - **死亡拉回规则**：(如设 spawnpoint 或下界死亡回传补装)
> - **核心验证目标**：(验证哪个 Bug 修复或哪个成就链路)
> ```



---

## 2026-11-22 正式赛（`survivaltest2` / `5Ease_Bot`）现场实测登记

### BUG-090 | P1 | Slice C（熔炉技能）+ Integrator（集间炉务）| 状态: OPEN（2026-11-22 正式赛现场发现）

- **比赛实据**。`5Ease_Bot` EP09 对 `[-12268,31,-6065]` 请求熔炼 6 生铁，**50.017s** 后 `partial_smelt` 记录本次取出 **4 锭**。第 5 锭来自开局备料。模型再投 2 生铁报缺料，合铁镐耗 3 锭、背包剩 2，随即 `goal_satisfied`；EP10 转挖钻。本次投料对应的另外 2 份产出没有后续回收回执，炉内生铁、成品和燃料各余多少未取证。 [开局备料](../../../runs/20261121-235251-766106.json#L5245) · [本次部分出炉](../../../runs/20261121-235251-766106.events.jsonl#L382) · [再投失败](../../../runs/20261121-235251-766106.events.jsonl#L403) · [合铁镐及剩余](../../../runs/20261121-235251-766106.events.jsonl#L421) · [下集开挖](../../../runs/20261121-235709-070941.events.jsonl#L31)
- **源码与最小修复**。同步技能在切片尾取一次可见成品，部分回执靠提示续取。复用已有 `WorldJobRegistry` 登记本次已投数量、炉坐标及待核验余量，目标提前结算时也续接炉务。在当前预算内可取则取，离区前或下集现场安全时优先收已完成品，再决定继续烧或取回残留原料、可退燃料。取货用窗口操作的实际转移回执或库存增量确认，避免重复投料或重复入账。 [同步取货](../src/mc_agent/skills/library.py#L5267) · [集间炉务](../src/mc_agent/harness/curriculum.py#L2140)
- **验收**。以投 6、先确认取 4、立即切集为夹具，下集从同炉再取 2，新增投料为 0；重复回收不会重复入账。背包满、受袭抢占、尚在燃烧分别保留待办和可复查回执。沿用 BUG-067 的单次时限；炉务结清后交 BUG-093 回收临时炉。

### BUG-091 | P1 | 挖矿引擎（`mc_skill_mine_dfs`）| 状态: OPEN（2026-11-22 正式赛现场发现）
- **影响**：`5Ease_Bot` 拿到 `iron_pickaxe` 切入 `shiny_gear`（`target='diamond', target_y=-54`）后，连续 3 个 60s 时间片（第 38~40 分钟）均返回 `collar_node_id=null, node_count=0`，在 `Y=20~24` 徘徊 3 分钟未能开井下潜，且石镐爆后切木镐慢挖。
- **现象与根因**：新目标与上一阶段留下的 `exhausted_collars` 距离过近，引擎反复试图选新井口却未真正下切阶梯；同时 `mine_dfs` 内石镐耗尽后未优先就地重铸石镐。
- **最小修复方向**：
  1. 切换 `target`（如 `iron -> diamond`）或 `abs(y - target_y) > 20` 时，若旧 `exhausted_collars` 在不同深度层，允许就地直接建新井口下潜（或自动水平偏移 12~16 格强制落井），避免连续多片 `collar_node_id=null`；
  2. 随身原木+圆石充足且 `pickaxe_tier < 2` 时，在 `mine_dfs` 下潜前自动重铸石镐再挖。

### BUG-092 | P2 | 挖矿策略与收益权衡（`mine_dfs` / 课程策略）| 状态: OPEN（2026-11-22 正式赛现场发现）
- **影响**：正式赛实机观察到两极分化：一方面对 10m 外隔墙/带水的 `iron_ore` 容易反复拉扯耗掉十几分钟；另一方面在铁镐刚出、铁锭尚未凑齐盾牌/铁甲/铁桶时，下潜途经矿道壁上**贴脸暴露的 `iron_ore`/`deepslate_iron_ore`** 却直接路过未顺手采集。
- **现象与根因**：缺少基于“边际时间成本 × 当前物资缺口”的顺采权衡规则——没有区分“贴脸零寻路成本（`dist <= 3m`、无相邻流体、1~2 秒可顺手敲掉）”与“需要绕路/穿岩/排水的远距矿”。
- **最小修复方向**：
  1. 在 `mine_dfs` 下潜/掘进途中，当背包 `iron_ingot + raw_iron` 低于基建阈值（如 `< 12`，覆盖铁镐+盾+铁甲+水桶）且矿道 3 格内出现无流体遮挡的暴露铁矿时，允许 0-LLM 顺手顺采（单次最多消耗 3~5s）；
  2. 对 `dist > 4m` 或相邻流体的次级矿坚决不绕路，保证直冲 `Y=-54` 钻石层的总时效。

### BUG-093 | P0 | 窄矿道临时工作台／熔炉挡路及误触发 `needs_surface_climb` | Slice C（临时站点）+ Slice A（矿道回返）| 状态: OPEN

- **比赛实据**。`5Ease_Bot` EP10 到 Y−54、`node_count=14`，随后因返回当前节点失败报 `needs_surface_climb`，**306.3s** 后爬到 Y−9。第三人称现场看到工作台挡住 1×2 矿道，JSONL 确认回撤失败与爬升，阻挡坐标待取证。熔炉有独立回执，官方 r77 GLM_1 EP16 在 `[6176,27,-110]` 烧铁，EP17 东向掘进在同一格返回 `blocker.block=furnace`，位移为 0。该炉归属及阻挡时各槽状态待核。 [正式赛回撤](../../../runs/20261121-235709-070941.events.jsonl#L589) · [爬到 Y−9](../../../runs/20261121-235709-070941.events.jsonl#L808) · [r77 同炉烧铁](../runs/official_r77_40m_glm1/GLM_1/20261121-203044-183312.events.jsonl#L16) · [r77 熔炉挡路](../runs/official_r77_40m_glm1/GLM_1/20261121-203159-666541.events.jsonl#L84)
- **Slice C 最小修复**。工作台、熔炉优先放在可操作的侧格，预留通行空间；记录本次临时放置的坐标与维度。每次用完回收本次的工作台并确认入包。临时熔炉等 BUG-090 炉务结清、读回输入／输出／燃料槽为空且已熄火后再挖回；沿用或在用的站点留在原处。 [工作台摆放](../src/mc_agent/skills/library.py#L2939) · [熔炉摆放](../src/mc_agent/skills/library.py#L5093)
- **Slice A 最小修复与验收**。在目标深度回撤失败时，先定位阻挡、尝试安全同层重连或就地重锚，再决定是否上爬。用 Y−54 的 1×2 矿道覆盖新放工作台、活跃炉、已空临时炉及沿用站点。合成后通道双向可走且工作台回包，已空临时炉回包，活跃炉材料与沿用站点完整，同层节点回返失败有具体原因和回退动作。 [同层重连](../src/mc_agent/skills/mining_dfs.py#L2242) · [回撤分支](../src/mc_agent/skills/mining_dfs.py#L5918)

### BUG-094 | P0 | 挖矿后未收尽可达目标掉落物 | Slice A（采矿确认）+ Slice C（共享拾取）| 状态: OPEN

- **比赛实据**。`5Ease_Bot` EP11 第 61 分钟停在 `[-12202.5,-25,-6104.7]` 时确认入包 **3 钻石**、`goal_met=false`，随后继续下潜。现场观察提到两颗仍在脚边。JSONL 的 `dug_count=4` 含所有方块，下一片再次报告 3 钻石且累计确认次数没增长，掉落总数待实体回执核实。EP14 挖掉一格深层铁矿后目标增量为 0，附近扫描找到高约 **4.4 格**的 `raw_iron×1`，按建议向南爬时被相邻水格拦住。官方 r79 GLM_1 EP15 的同一堆粗铁 **3 块**在三次扫描间持续存在，五次靠近工具的位置回执无变化。水流带动掉落物的逐实体轨迹待验。 [EP11 三钻入包](../../../runs/20261122-001808-345130.events.jsonl#L91) · [EP14 高处掉落](../../../runs/20261122-002711-521374.events.jsonl#L514) · [EP14 水阻](../../../runs/20261122-002711-521374.events.jsonl#L536) · [r79 持续未取掉落](../runs/official_r79_35m_glm1/GLM_1/20261121-210952-579757.events.jsonl#L404)
- **源码与最小修复**。现有 DFS 收到任意 1 件匹配物品就确认；补扫应按目标产物及实体当前位置核对余物。在预算内优先收当前任务需要、可安全到达的掉落，以实际库存变化或拾取回执确认。实体隔墙、从高处坠落、随水上浮时先刷新实体坐标并查落脚与短程路线，路径不通或收益不足就记原因并继续任务。对暂未拾取的可疑目标留短期记录，避免重复绕路。矿格数只用于定位，掉落数量以实体或实际包增量为准。 [DFS 确认](../src/mc_agent/skills/mining_dfs.py#L3044) · [附近拾取](../src/mc_agent/skills/library.py#L2958)
- **验收**。同层近距掉两堆钻石，先入一堆后仍追踪另一堆；墙后无通路、高处落下、水流改变高度各有离线夹具。每次给出目标实体、最后坐标及已拾／不可达／跳过原因。限时内验证可安全捡到的高价值目标，低价值杂物不会引发绕路。正式赛同类深挖核对目标矿入包与课程验证回执。

### BUG-095 | P1 | 小脑遇袭 `ReadTimeout` 后模型未优先补逃跑（`guardian.py` / `agent.py`）| 状态: OPEN（2026-11-22 正式赛现场发现）
- **影响**：第 64 分钟在 `[-12208.4, -53.4, -6133.5]` 遭遇贴脸僵尸（`dist=1.6m`），小脑抢占接管因后端 `ReadTimeout` 中止（`reflex_mode=network_timeout`, `HP=11`），模型随后未补调 `mc_skill_flee` 反而调 `mine_dfs(reset)` 导致阵亡爆装。
- **最小修复方向**：当小脑因 `network_timeout` 中止且 `nearest_hostile.dist <= 4m` 时，向模型返回强指令要求立即优先 `mc_skill_flee` / 封堵退路，严禁继续执行 `mine_dfs` 或原地 `reset`。

### BUG-096 | P2 | `--curriculum --interactive` 未将 stdin `/now` 注入单集上下文（`curriculum.py` / `agent.py`）| 状态: OPEN（2026-11-22 正式赛现场发现）
- **影响**：正式赛开启 `--curriculum --interactive` 后，向 stdin 发送的 `/now ...` 仅回显在终端日志，未真正进入当前集模型上下文，导致人工轻介入无法即时纠偏。
- **最小修复方向**：在 `run_curriculum` 中透传交互式队列 / stdin 监听线程给每集 `run_agent`，使 `/now` 可在课程模式下实时生效。

### BUG-097 | P1 | `mc_skill_container` 7/7 全部 `bad_args`（盲目查箱 + 动词别名拒绝）| 状态: OPEN（2026-11-22 正式赛 15 集全量审计）
- **影响**：EP05（3 次）、EP08（2 次）、EP15（2 次）累计调用 `mc_skill_container` **7 次，7/7 全部 `bad_args` 失败**，白白浪费开集轮次。
- **日志证据**：`runs/curriculum-20261121-231923-527585.json`（EP05 L32/L63/L101、EP08 L60/L119、EP15 L33/L53）——反复传 `action="get"`、`items=[]` 或漏传坐标。
- **根因**：① `bootstrap_iron_age` 剧本无条件写“若记忆箱已有备用铁镐/铁锭优先调 `mc_skill_container`”，即便 `WorldMemory.containers` 为空也诱导模型盲调；② `mc_skill_container` 严苛要求 `action in ("put", "take")`，拒绝常见同义词 `get`/`withdraw`/`store`/`deposit`。
- **最小修复方向**：① 仅在 `WorldMemory.containers` 存在含对应物资的箱子时才注入取箱提示（并直接附带箱子坐标）；② 在 `mc_skill_container` 中做别名归一（`get`/`withdraw`→`take`，`store`/`deposit`→`put`）。

### BUG-098 | P1 | 地下矿道遇水引发 `water_surface` 小脑连环抢占（11 次）| 状态: OPEN（2026-11-22 正式赛 15 集全量审计）
- **影响**：EP08（5 次）、EP09（5 次）、EP10（1 次）累计触发 **11 次 `water_surface` 抢占**，导致 `mine_dfs` / `unstick` / `mine_blocks` 连续被打断，耗掉数分钟。
- **根因**：`water_surface` 仅在原地执行上浮，未把 Bot 带回干燥巷道或封堵水格；下一轮工具调用立刻重新踏入水流再次触发抢占。
- **最小修复方向**：地下（`y < 55`）60s 内连续触发 `>= 2` 次 `water_surface` 时，小脑自动尝试向相邻干格后撤或用圆石封堵水格，并在抢占返回中明确给出“避开当前水格坐标，向干燥方向平推 8+ 格”的提示。

### BUG-099 | P2 | 采铁阶段 `staircase(down)` 滑穿 `Y=16` 进入深板岩 | 状态: OPEN（2026-11-22 正式赛 15 集全量审计）
- **影响**：EP14（`bootstrap_iron_age`）无铁镐状态下连续调 `mc_skill_staircase(vertical='down')` 一路滑到 `Y=-2` 深板岩层，整集 600s 超时 0 收获。
- **最小修复方向**：在 `bootstrap_iron_age` 或无铁镐下潜到 `y <= 16` 时，返回提示改用水平 `mc_skill_mine_dfs(target='iron', target_y=16)`，禁止继续 `staircase(down)` 滑入深板岩。

### BUG-100 | P0 | `mc_skill_mine_blocks` 88% 失败率（2/17）与 `bootstrap_iron_age` 耗时 33 分钟 | 状态: OPEN（2026-11-22 正式赛全量时间账审计）
- **影响**：EP04~EP09 仅仅为了凑齐 6 块铁，累计消耗了 **33 分钟**（占 80 分钟赛程的 41%！），直接挤没了中后期村庄/下界/附魔的拿分窗口。
- **数据证据**：`mc_skill_mine_blocks` 全场调用 17 次仅成功 **2 次**（失败 15 次，失败率 88%）；`mc_skill_staircase` 6 次触发 `ore_detected`，但探测到的矿大多隔着 8~12m 实心岩壁，导致模型紧接着调 `mine_blocks` 必报 `out_of_reach / no_targets_available`；同时 EP05 跑了整整 10 分钟 `mine_dfs(iron, Y=16)` 却在 `(-12261, 13, -6065)` 小范围反复回撤（`dry_calls=4`）0 块铁入包。
- **最小修复方向**：① `staircase` 的 `ore_detected` 仅在矿石**紧贴通道 3~4 格内且无隔墙/流体**时才触发中断，隔墙远矿不打断下潜也不诱导 `mine_blocks`；② `mine_dfs` 采铁在 `Y=16` 改为长直巷道快速平推（减少短支道高频折返与 `movement_not_confirmed`）。

### BUG-101 | P1 | 集间切换与 `pre_ensure` 隐形耗时 8.9 分钟（占赛程 11%）| 状态: OPEN（2026-11-22 正式赛全量时间账审计）
- **影响**：全场 81.0 分钟中，工具执行 58.2m + LLM 等待 13.8m = 72.0m，剩余 **8.9 分钟（537s）**全部消耗在 15 次集间切换、`pre_ensure` 和重连/状态轮询上。
- **最小修复方向**：审计 `curriculum.py` 集间 `pre_ensure` 耗时（尤其 EP02/EP06 `actor_requests=0` 的纯 `pre_ensure` 集），合并冗余状态查询并减少不必要的跨集重切。
