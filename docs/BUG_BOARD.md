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

## 当前待处理 P0/P1 工单（等待 `r11_22bot` 首飞数据填充）

### [待填充 — `r11_22bot` 完赛后由 #1 主 AGY / #5 DeepSeek #1 自动更新]

> 工单模板：
> ```markdown
> ### BUG-XXX | P0/P1 | Slice A/B/C/D | 状态: OPEN
> - **跨模型影响**：`N bots / M 次`（涉及模型：GLM / DS / HY4 / NJU）
> - **日志证据**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/<wave>/<bot>/stdout.log` (Lxxx)
> - **现象与根因**：...
> - **验收标准**：...
> ```

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


