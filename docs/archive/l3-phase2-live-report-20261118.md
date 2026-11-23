# L3/L4 二期核心底座实机验收报告（2026-11-18）

## 1. 交付范围与严格文件边界

工作分支：`/home/xzq/projects/mc/.worktrees/l3-sched` (`feat/l3-scheduler`)

本次 L3/L4 核心实现仅修改自有文件，与并行分支（`library.py`、`world_memory.py`、`mc_tools.py`、`mining_dfs.py`、`run_combat_tests.py`）保持 **0 行重叠**：
- `src/mc_agent/skills/combat_env.py`、`src/mc_agent/skills/__init__.py`：`CombatEnv` v1（4 只读几何探针 `has_los` / `probe_ray` / `scan_safe_yaws` / `arena_snapshot` + 3 委托动作）+ 可热插拔 `CombatControllerSlot` + 内置 0-LLM `SubcorticalInstinctController`。
- `src/mc_agent/harness/model.py`、`src/mc_agent/harness/guardian.py`、`src/mc_agent/harness/agent.py`：全生命周期脊髓/小脑本能抢占（`RpmLimiter.acquire` 0.15s 切片、并发 429 8 次退避、`0.25s` 流式轮询、苦力怕/近战/无遮挡远程受伤前触发、`ReflexPreempted` 宏工具中途抢占不重放、工具后/限流前 `stabilize_after_tools` 自稳、空点空气格 `interacted_with_air` 与自身格放置提示）。
- `src/mc_agent/skills/ensure.py`、`src/mc_agent/harness/curriculum.py`：D1–D9 缺陷全闭环修复、`allow_underground_dfs=False` 禁盲挖、脏空箱 `missing_item` 自愈、72m 箱距上限、`post_ensure_task` 重渲染与 `eff_extra_tools` 透传、双向 `episode_gate`（`COMPLETE` / `150s shiny_gear` 顺采窗口 / `ABORT` / `Y>=48` 单层 `PAUSE` + 干净快照恢复）、`>=25s` 双炉并行 `WorldJobRegistry` + 挂起父任务战略物资预留（`_compute_reserved_materials`）+ 浅坑防丢单回收。

## 2. 离线与 `nju-beast` 实机验收结果

- **全量离线单测**：`240 passed, 1 skipped, 0 failed`（含 `test_combat_env.py`、`test_spinal_reflex.py`、`test_l3_phase2.py`）。
- **Phase A（`MapTest_01` 0-LLM 实机验收）**：**5 / 5 PASS**（`runs/l3-phase2-live-20261118-083400.json`）
  - `A1`：3D 体素视线与安全撤退射线实机精确区分开阔生物与 2 格石墙后生物；
  - `A2`：`harvest_logs` 砍树中途遭真实僵尸突袭，0-LLM 抢占中断砍树并切铁剑反杀（满血 20.0）；
  - `A3`：苦力怕 2.3m 接近未扣血即触发 `pre_hurt_creeper` 举盾后撤至 8.2m（满血 20.0）；
  - `A4`：脏空箱 `missing_item` 1.77s 自愈清空记忆快照，零盲挖返回 `requires_world_hunt`；
  - `A5`：双炉 6+6 并行非阻塞烧制 12 石头，等待时间减半，柔性回收合出磁石与指南针。
- **Phase B（`MapTest_01` 单并发 Qwen 课程全闭环验收）**：**2 / 2 PASS**（`runs/l3-phase2-live-20261118-095924.json`）
  - `B1`：覆盖历史 `story/iron_tools` 假完成陷阱，2 轮请求合出 `iron_pickaxe` 后由 `episode_gate` 当场 0 额外请求收工；
  - `B2`：Ep1 4 轮完成村民交易(+15) -> Ep2 `use_lodestone` 0 轮双炉点火挂起并预留 5 铁锭+1 红石 -> Ep3 挂起期内 2 轮顺手舀岩浆(+15) -> Ep4 80m 围栏内微整备 -> Ep5 自动回收 12 石头、0-LLM 合齐磁石与指南针、重渲染 `post_ensure_task` 后 **仅 2 轮 Qwen 请求** 放置绑定解锁 `use_lodestone`(+15)，总分 **60 分**。
