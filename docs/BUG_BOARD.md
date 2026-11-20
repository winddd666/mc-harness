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
- **D1 r20_short 收官（17/18）**：仍有 **2 路**超限：`glm_3_village_trade` cobble **242**、`hy4_8_nether` netherrack **182**。
  → `53835cf` 只在 `mine_dfs` 返回后清；这两路的剩余石料来自 `mine_blocks`/`tunnel`/地形收集，没有触发点。
  → 剩余修复：把同一清理挂到 `mine_blocks`/`tunnel`/`harvest_logs` 返回路径，或在 episode 收尾统一清一次；并断言返回的 `dropped` 非空时才计为生效。
  → **已合入（23:47 `e60a8d5`）**：`989d757` 现为 HEAD 祖先，`grep _cleanup_inventory_after` = **4 处**；r22 进程加载于 23:48，**包含该修复**，可用 `owned_end` 验收。
- **D1 r20_long 实测（修复前）**：`hy4_hy_1_bare` cobbled_deepslate **493**、`ds_1_bare` cobble 228 —— 长任务堆料更严重，正是本补丁的目标场景。
- **D1 r19 实测（当时未达标）**：18 路中仍有 **3 路**超限：`nju_guitest 234 cobble`、`ds_2 261 cobble`、`glm_2 138 netherrack`（阈值 192/128）。
  → 说明 cap 判断已可达，但**清理仍只在 craft/harvest 时触发**；最后一次挖矿后没有调用点，残留无法清。
  → 剩余修复：`mine_dfs`/`mine_blocks` 结束（或 episode 收尾）加一次 `_ensure_free_slots(min_free=5)` 软清理；或把 `pre_over_cap` 检查挂到 `mine_dfs` 返回路径。
- **D1 代码复核（通过）**：`4161d8f` 已在 `_ensure_free_slots` 的提前 return **之前**计算 `pre_over_cap`（cobble/deepslate >192、netherrack >128、dirt >64），并改为 `if empty_now >= min_free and not pre_over_cap: return`——与建议一致。r19_short 起用 `owned_end` 与 `dropped` 事件验收。

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

### r20_long / r21_short 终验摘要（D1，23:57）

- **r20_long 4/4**（0 Traceback）：`glm_5` 180（12 集）、`hy4_1` 40、`ds_1` 15、`nju_movetest` **0（auth_error，BUG-036）**；门禁仅 `BUG-029 over-cap 2` 失败（`hy4_1` cobbled_deepslate **493**、`ds_1` cobble 228）。
- **r21_short 17/18**：仅 `startup crash 1`（BUG-039 `glm_1`）；BUG-035 实机 0 复发；`over-cap 0`；deaths 9、冷却 111.7s、mine 0.90/6.6%。
- 分数：r20_short 1680 → r21_short 1690（17 vs 17）；若 `glm_1` 不丢（上波 160 分），本波约 **1850**。

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

### BUG-045 | P1 | Integrator（#1，`v2_fleet.py` / 调度层）| 状态: OPEN（过夜主轨实测）
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

### BUG-046 | P1 | Integrator（#1，`scripts/l3_wild_endurance_runner.py` / 波次配置）| 状态: OPEN（过夜主轨实测）
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

### BUG-047 | P1 | Integrator（#1，`harness/mc_tools.py` + `bot.py`）| 状态: OPEN（overnight_12bot_part2 实测）
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

### BUG-048 | P1 | Integrator（#1，`harness/geofence.py` + 工具层）| 状态: OPEN（过夜两波实测）
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
- **影响**：开局现成场景资源未被优先收割，且预授成就与物理维度脱节，锁死主世界满装 Bot 的下界高分主线。
- **轨迹实据**：
  - `ds_ds_3_enchant_anvil` 出生在附魔台 + 书架 + 30 级经验平台，EP01 却先被派 `island_recon_sweep` 跑出平台掉水被溺尸击杀，30 级经验瞬间清零，后续 `enchant_item` 0/7；
  - `glm_glm_4_brewing_alchemy` / `hy4_hy_5_brewing_alchemy` 开局预授 `story/enter_the_nether`，但 Bot 站在主世界；1 小时穿满钻甲到 195 分后，规划器误以为去过下界，后 3 小时再也不派建门进下界（`195→195→195→195`）。
- **最小修复**：
  1. `enchant_anvil` 切片同 `brewing_alchemy`/`village_trade` 一样加开局专属目标（优先附魔，不先跑 `island_recon_sweep`）；
  2. 规划器判断“能否走下界高分线”看 `cur_dim == "the_nether"` 或已知传送门，而非仅看 `enter_the_nether` 成就位；主世界满钻装后若尚未在下界，主动派建门进下界。
- **验收标准**：`enchant_anvil` 开局 2 集内完成首次附魔；主世界满钻装 4h 长测能主动进入下界继续推进至 ≥250 分。

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

### BUG-056 | P0 | Integrator（#1，`harness/guardian.py` + `skills/library.py`）| 状态: OPEN（过夜 6 波全量审计）
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

### BUG-057 | P1 | Integrator（#1，`harness/guardian.py`）| 状态: OPEN（过夜 6 波全量审计）
- **一句话**：站在**岩浆块（hot_floor）**上持续掉血时，guardian 把 `hot_floor` 归入 `NON_COMBAT_HURT_SOURCES` **压制了 `fresh_hurt`**，只有 HP ≤ 6 才升级，且升级后的 `emergency_bunker` / `flee` 在下界岩浆区**必然失败**（`unsafe_open_side` / `no_safe_retreat`）。
- **实测（6 波）**：
  - `hot_floor` 伤害事件 **2,129 次**，涉及 **58 个 episode**
  - **因 hot_floor 死亡 34 次**：`ds_4_nether_fortress` **25**、`glm_2_nether_bastion` **7**、`hy4_6_nether_bastion` 2
  - 典型片段（`glm_2` EP1）：`[受伤 health=19 by hot_floor]×10` → HP 20→13.3，同一 tick 内 `emergency_bunker=unsafe_open_side`、`flee=no_safe_retreat`，然后继续掉血
- **源码定位**：`guardian.py:52-72 NON_COMBAT_HURT_SOURCES` 含 `hot_floor`；`guardian.py:744-755` 用它算出 `non_combat_only_hurt` → `fresh_hurt` 不加；`guardian.py:790` 只有 `critical_non_combat = non_combat_only_hurt and hp_cur <= 6.0` 才触发动作。**全程没有"离开该方块 1~2 格"这个最直接的反射**。
- **分支复核**：近处有敌人时，`hot_floor` 仍可能成为 `fresh_hurt`。这会走通用对敌反射；两种分支都缺少针对脚下岩浆块的脱离动作。不能把近敌样本也归因于 HP ≤6 的门槛。 [Guardian 受伤分类](../src/mc_agent/harness/guardian.py)
- **最小修复**：新增 `hot_floor` 专属反射：检测到 `hurt.source == "hot_floor"` → `mc_move_to` 到相邻**非岩浆块**（1~3 格）或 `posture jump` 跳出；给该方块坐标加 60s `block_avoid`；不必等 HP ≤6。
- **验收标准**：单集 `hot_floor` 伤害 ≤10 次；`hot_floor` 死亡 ≤3 次/波。

### BUG-058 | P1 | Integrator（#1，`harness/mc_tools.py` + 模型提示）| 状态: OPEN（过夜 6 波全量审计）
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

### BUG-059 | P1 | Integrator（#1，`skills/mining_dfs.py`）| 状态: OPEN（过夜 6 波全量审计）
- **一句话**：`mine_dfs(action="climb_out")` **回不到地表**：单集内连续 `climb_progress` 最多 16 次，深挖后必然在回程超时。
- **实测（6 波）**：
  - `climb_progress` **400 次** + `climb_blocked` 40 次，而 `surface_reached` 仅 **102 次**（成功率 ~20%）
  - **58 个 episode 出现 ≥3 次连续 climb_progress**，累计 **13,113s = 3.6 小时**
  - 最长：`glm_4` 一段 **10 连击 839s**、`nju_guitest` **16 连击 424s**、`nju_movetest` 8 连击 431s
- **为什么爬不出来（量化）**：331 个 `climb_progress` 样本 → **每次中位 +5 格 / 29.3s ≈ 7 秒/格**。`nju_guitest` EP37 从 **y=-48** 开始，16 次调用 458s 只到 **y=1**，仍未到地表（collar-16≈48）→ 需要 ~96 格 × 7s ≈ **672s > 600s 集预算**。**数学上不可能完成**。
- **源码定位**：`mining_dfs.py:3022 _climb_out(deadline, max_attempts=40)`，每次 `staircase(up,1)` 都要 `get_block + dig + place + 位置确认` 3~4 次 HTTP（≈7s/格）。
- **最小修复**：① `explore` 下潜深度按 episode 剩余预算**上限钳制**（`max_depth = min(target_y, start_y - remaining_s/7*0.6)`）；② `climb_out` 用"垂直垫高"（每格 1 次 place，≈1~2s/格）替代阶梯；③ 集预算剩余 <35% 时**强制回程**（在 `mine_dfs` 内部触发，不等模型）。
- **验收标准**：`climb_progress`/`surface_reached` ≤ 1:1；`≥3 连击` 的 episode ≤5/波；深挖集不再因回程超时。

### BUG-060 | P2 | Integrator（#1，`harness/mc_tools.py`）| 状态: OPEN（过夜 6 波全量审计）
- **一句话**：实体类目标（发光鱿鱼/动物）会陷入"实体 id 失效 → 重新扫描 → 再失效"的循环。
- **实测**：`no_such_entity` **43 次**（`nju_guitest` 12、`glm_5` 10、`glm_3` 8、`glm_4` 5）；`mc_get_entities` 共 3,493 次，其中 **118 次 radius ≥ 4096**（最大 65,536）。典型（`glm_5` EP39 `make_a_sign_glow`）：radius 256→512→…→65,536 连发 9 次，然后 `move_to(entity_id)` 两次 `no_such_entity`，最后摔死，集满 22 请求 0 分。
- **最小修复**：① `mc_get_entities` 对 `radius>512` 直接拒绝并提示"实体不在渲染距离内，请换目标"；② `move_to(entity_id)` 失败时返回"该实体已消失，请改用坐标或换目标"，并对该类型加 60s 冷却。
- **验收标准**：`no_such_entity` ≤5/波；`radius≥4096` 的 get_entities 为 0。

### BUG-061 | P1 | Integrator（课程工具授权）+ Slice C（交互回执）| 状态: OPEN（过夜 6 波复核）

- `fishy_business` 共 **0/18 集验证通过**，分布在 4 条轨迹。18 集中 `mc_use_item` 调用为 **0**。`part2/ds_ds_1_bare` EP14 已合成鱼竿，对 `level=0` 水格调用 `mc_interact` 返回 `ok=true, changed=false`，空目标调用还返回 `bad_args`。EP37 在开阔水边复现，四次交互均无抛竿/钓获回执。 [EP14](../runs/overnight_12bot_part2/ds_ds_1_bare/20261119-150646-372249.events.jsonl#L99) · [EP37](../runs/overnight_12bot_part2/ds_ds_1_bare/20261119-171154-936339.events.jsonl#L131)
- 同一代码指纹离线复现：`_infer_extra_tools_for_advancement("minecraft:husbandry/fishy_business", "持fishing_rod对开阔水域使用，钓上1条鱼。")` 只返回 `mc_interact`，`COMPACT_CORE_TOOLS` 没有 `mc_use_item`。实际抛竿、等待和收竿由后端的 `use_item(fishing_rod)` 执行。 [课程分配](../src/mc_agent/harness/curriculum.py#L792) · [工具集](../src/mc_agent/harness/mc_tools.py#L98) · [后端钓鱼分支](../experiments/20261115-until-1900/backend_patched/src/session.js#L2348)
- `follow_ender_eye` 也触发同一授权缺口。对实际 KB 文案调用推断器返回 `()`；`r28/hy4_5` EP26 明确报缺投掷入口，EP25/50 又有独立的合成材料缺口。持眼时能否观测飞行方向仍待验证。 [EP26](../runs/hy4_audit_r28/hy4_hy_5_brewing_alchemy/20261119-163706-486842.events.jsonl) · [KB 文案](../src/mc_agent/kb/data_1_21_11.py#L900)
- 验收时为适用目标公开对应工具并给到准确工具名。对鱼竿走错入口给可执行的改用提示。用持竿、岸边、静水的回归检查真实抛竿回执与目标验证，再用持眼夹具检查投掷和可观测结果。钓鱼轨迹中的缺工作台、积水矿井问题继续关联 BUG-032/052/056。

### BUG-062 | P1 | Slice C（交互）+ Slice D2（酿造水源）| 状态: OPEN（过夜主轨第二波）

- `part2/glm_glm_4_brewing_alchemy` EP01/02 对 `level=1` 或 `level=8` 的水位 **共 13 次**使用玻璃瓶。回执均为 `ok=true, changed=false`，没有 `used/gained`，酿造台瓶槽仍空，随后 `missing_item`。EP03 是独立的 429。第一波同 Bot 的对照回执明确 `used.glass_bottle=1`、`gained.potion=1`，再把 `potion` 放进瓶槽后成功。 [第二波 EP01](../runs/overnight_12bot_part2/glm_glm_4_brewing_alchemy/20261119-133151-986552.events.jsonl#L30) · [第二波 EP02](../runs/overnight_12bot_part2/glm_glm_4_brewing_alchemy/20261119-133259-566434.events.jsonl#L32) · [第一波成功对照](../runs/overnight_12bot/glm_glm_4_brewing_alchemy/20261119-092305-340302.events.jsonl#L35)
- BUG-008 的库存变化判定只覆盖桶类。玻璃瓶仍会把无效交互报成功，目标没得到换水格提示。回执应确认玻璃瓶消耗与水药瓶入包，或明确提示改找可达水源。场景侧核查出生点给出的水格是否可交互。P2-27 继续跟踪 `mc_skill_brew` 的参数说明。 [交互判定](../src/mc_agent/harness/mc_tools.py#L1353)
- 验收覆盖无变化且无物品增减、方块不变但确实装水两路。真实酿造开局可在前两集得到水药瓶并将其投进酿造台。

### BUG-063 | P2 | Slice D1 + Integrator（评测口径）| 状态: OPEN（D1 离线通过，待 Integrator 合入）

- Fleet 的 `Deaths` 同时为 `guardian.incident/death_or_connection_changed` 和含 `死亡` 的 `mc.notable_events` 各加一。连接变化可以进入前者，真实死亡也可能两处都出现。同一消息里的多条死亡只会在后者加一。`r26/hy4_hy_6_nether_bastion` 榜单为 **51**，JSONL 有 **20 条明确死亡回执**，分布在 11 集。 [计数代码](../scripts/v2_fleet.py#L569) · [收官榜单](../runs/hy4_audit_r26/triage-hy4_audit_r26.md)
- 验收时区分死亡、重生和连接变化。有事件 ID 时按 Bot 与 ID 去重，缺 ID 时展示回执口径。离线夹具覆盖单次死亡同时出现在两个事件、仅掉线、同一消息两次死亡。
- D1 用只读回放复现 `hy4_audit_r26/hy4_hy_6_nether_bastion` 的 **51 = 31 条混合 Guardian 事件 + 20 条含死亡的消息**。新 Fleet 输出为 **20 次明确死亡（全部无源 ID）**、**22 次重生或出生回执**、**0 条显式连接变化**、**31 条混合事件**。旧日志的连接变化总数未知。离线夹具通过双发、掉线、同批两死、`×N` 和跨 Bot 去重。 [EP02 混合事件](../runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1017) · [EP02 死亡与重生回执](../runs/hy4_audit_r26/hy4_hy_6_nether_bastion/20261119-094622-266718.events.jsonl#L1020) · [D1 报告](../../s-triage/docs/D1-r11-triage.md#4ca-bug-063-fleet-死亡口径与-r26-对账)

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


