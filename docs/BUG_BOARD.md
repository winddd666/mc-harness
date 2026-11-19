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

### BUG-001 | P0 | Slice D2（#6 DeepSeek #2）| 状态: OPEN
- **跨模型影响**：`3 bots` 启动崩溃（`glm_glm_1_nether_fortress`, `glm_glm_2_nether_bastion`, `ds_ds_4_nether_fortress`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/glm_glm_1_nether_fortress/stdout.log` (`NameError: name 're' is not defined` at `l3_wild_endurance_runner.py:167`)
- **现象与根因**：`select_nether_structure_site` 使用了 `re.search` 但未导入 `re`；同时 `nether_fortress`/`nether_bastion` 缺下界死亡自动回场补装逻辑，且 `v2_fleet.py triage` 解析 `wild-endurance-*.json` 分数/错误字段未对齐 `v2_triage.py`。
- **验收标准**：补 `import re`、补下界死亡回场、`v2_fleet.py triage --wave r11_22bot` 能准确打印 22 路分数与 TopErr 榜单，通过 `py_compile` 与离线 `pytest`。

### BUG-002 | P0 | Slice C（#4 Codex #2）| 状态: OPEN
- **跨模型影响**：`1 bot / 15 次连续失败`（比赛模型 `nju_guitest_01_nether` EP02：`mc_skill_build_portal:unknown_fail×13` + `repeated_identical_failure_blocked×2`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/nju_guitest_01_nether/stdout.log`
- **现象与根因**：`build_nether_portal` 在特定地形/障碍下连续 13 次失败且未返回清晰可恢复错误码。
- **验收标准**：定位精确失败点（放置/点火/步入/地形清障）并修复 `library.py:build_nether_portal`，补离线回归单测并跑通全量 `pytest`。

### BUG-003 | P1 | Slice A（#3 Codex #1）| 状态: OPEN
- **跨模型影响**：`3+ bots`（`hy4_hy_9_deep_water`, `nju_movetest_01_bare`, `hy4_hy_8_nether`：`mc_skill_mine_dfs:climb_incomplete×5`，且单次耗时 `174s`/`132s`/`127s` 穿透 `max_duration_s`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_9_deep_water/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/nju_movetest_01_bare/stdout.log`
- **现象与根因**：`mine_dfs` 内部循环（尤其 `climb_out` 与深层寻路/挖矿子步骤）未严格遵守 `deadline` 导致单次调用超 2~3 分钟，且地下脱节后强迫反复 `climb_out`。
- **验收标准**：硬收紧 `mining_dfs.py` 墙钟 `deadline` 检查并修复 `climb_incomplete` 死循环，补离线回归单测并跑通全量 `pytest`。

### BUG-004 | P1 | Slice C（#4 Codex #2）| 状态: OPEN
- **跨模型影响**：`6 bots / 20+ 次`（`ds_ds_2_iron`, `ds_ds_3_village`, `hy4_hy_2_bare`, `hy4_hy_3_iron`, `nju_nju_3_village`, `glm_glm_3_village`）
- **日志证据**：`mc_skill_staircase:missing_step_support×6`、`movement_not_confirmed×6`、`time_budget×3`（`92s/81s`）、`mc_move_to:busy×5`
- **现象与根因**：`dig_staircase` 遇空洞/落差垫脚支撑或单步卡住时耗时超 80~90s；`mc_move_to` 在前序动作未完全清理时触发 `busy`。
- **验收标准**：修复 `library.py:dig_staircase` 与 `mc_tools.py:mc_move_to` 边界故障，补离线单测并跑通全量 `pytest`。

### BUG-005 | P1 | Slice B（#2 子 AGY）| 状态: OPEN
- **跨模型影响**：`4 bots` 高死亡/高频抢占（`hy4_hy_2_bare` 5 死、`hy4_hy_10_enchant_anvil` 4 死、`hy4_hy_8_nether` 3 死、`glm_glm_3_village` `guardian=19/5preempt`）
- **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_2_bare/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/hy4_hy_10_enchant_anvil/stdout.log`、`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/glm_glm_3_village/stdout.log`
- **现象与根因**：排查上述 4 条轨迹中的连续死亡原因与 `guardian.py` 高频反射未脱险问题。
- **验收标准**：修复对应 `guardian.py` / `combat/` / `safety.py` 生存反射硬漏洞，补离线单测并跑通全量 `pytest`。

---

## P2 Backlog（不抢 P0/P1；KB/脚本类由 #6 随时消化，核心代码类由 #2/#3/#4 在各自 P0/P1 清零后顺延处理）

- *(等待 #5 DeepSeek #1 从轨迹中归档)*

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


