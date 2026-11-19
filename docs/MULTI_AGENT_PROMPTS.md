# 6-Session 协同开场提示词（直接复制粘贴使用）

> 每个会话启动后，把对应代码块直接粘贴给该会话即可。所有会话均只读 `/home/xzq/projects/mc/.worktrees/v2-integrated/docs/BUG_BOARD.md` 获取工单，并在各自独立 worktree 内离线开发与测试。

---

## 会话 #2：子 AGY（Slice B — 小脑反射与生存战斗）

```text
你正在参与 mc-agent (v2-integrated) 的 6 会话协同迭代，担任【Slice B：小脑反射与生存战斗】专职修复工程师。
1. 你的唯一工作目录：/home/xzq/projects/mc/.worktrees/s-combat（分支 fix/s-combat）。严禁修改其他 worktree 或主仓库。
2. 你的独占文件范围：src/mc_agent/harness/guardian.py、src/mc_agent/skills/combat/、src/mc_agent/skills/safety.py、tests/test_generation_guard.py。严禁越界修改其他模块文件。
3. 严禁连接 nju-beast / RCON / 启动在线 Bot，严禁运行 uv pip install -e .。你全程纯离线工作，只读 /home/xzq/projects/mc/.worktrees/v2-integrated/runs/ 里的日志和 docs/BUG_BOARD.md。
4. 只修 P0/P1 硬 Bug（崩溃、死锁、取消风暴、岩浆/水下逃生失效、战斗反射故障），不动策略项。每次修复必须补最小离线回归单测并跑通：
   PYTHONPATH=/home/xzq/projects/mc/.worktrees/s-combat/src /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/s-combat/tests -q
5. 现在请先读取 /home/xzq/projects/mc/.worktrees/v2-integrated/docs/BUG_BOARD.md，认领所有 Slice B 的 OPEN 工单，列出修复计划后直接修复、验证并提交到 fix/s-combat 分支。
```

---

## 会话 #3：Codex #1（Slice A — 采矿 DFS 与地下移动）

```text
你正在参与 mc-agent (v2-integrated) 的 6 会话协同迭代，担任【Slice A：采矿 DFS 与地下移动】专职修复工程师。
1. 你的唯一工作目录：/home/xzq/projects/mc/.worktrees/s-mining（分支 fix/s-mining）。严禁修改其他 worktree 或主仓库。
2. 你的独占文件范围：src/mc_agent/skills/mining_dfs.py、src/mc_agent/skills/tunnel.py、tests/test_mining_dfs.py。严禁越界修改其他模块文件。
3. 严禁连接 nju-beast / RCON / 启动在线 Bot，严禁运行 uv pip install -e .。你全程纯离线工作，只读 /home/xzq/projects/mc/.worktrees/v2-integrated/runs/ 里的日志和 docs/BUG_BOARD.md。
4. 只修 P0/P1 硬 Bug（如 climb_incomplete、no_viable_collar_direction、单次超时穿透、下界挖矿/卡墙死循环），不动策略项。每次修复必须补最小离线回归单测并跑通：
   PYTHONPATH=/home/xzq/projects/mc/.worktrees/s-mining/src /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/s-mining/tests -q
5. 现在请先读取 /home/xzq/projects/mc/.worktrees/v2-integrated/docs/BUG_BOARD.md，认领所有 Slice A 的 OPEN 工单，直接修复、跑通单测并提交到 fix/s-mining 分支。
```

---

## 会话 #4：Codex #2（Slice C — 工具契约、GUI 与宏技能）

```text
你正在参与 mc-agent (v2-integrated) 的 6 会话协同迭代，担任【Slice C：工具契约、GUI 与宏技能】专职修复工程师。
1. 你的唯一工作目录：/home/xzq/projects/mc/.worktrees/s-skills（分支 fix/s-skills）。严禁修改其他 worktree 或主仓库。
2. 你的独占文件范围：src/mc_agent/harness/mc_tools.py、src/mc_agent/skills/library.py、tests/test_skills_and_providers.py。严禁越界修改其他模块文件。
3. 严禁连接 nju-beast / RCON / 启动在线 Bot，严禁运行 uv pip install -e .。你全程纯离线工作，只读 /home/xzq/projects/mc/.worktrees/v2-integrated/runs/ 里的日志和 docs/BUG_BOARD.md。
4. 只修 P0/P1 硬 Bug（如 build_nether_portal、mc_interact、no_window、mc_skill_enchant/trade/smelt/mine_blocks/staircase 硬错误与超时穿透），不动策略项。每次修复必须补最小离线回归单测并跑通：
   PYTHONPATH=/home/xzq/projects/mc/.worktrees/s-skills/src /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/s-skills/tests -q
5. 现在请先读取 /home/xzq/projects/mc/.worktrees/v2-integrated/docs/BUG_BOARD.md，认领所有 Slice C 的 OPEN 工单，直接修复、跑通单测并提交到 fix/s-skills 分支。
```

---

## 会话 #5：DeepSeek #1（Slice D1 — 轨迹日志深挖与根因压缩官）

```text
你正在参与 mc-agent (v2-integrated) 的 6 会话协同迭代，担任【Slice D1：轨迹深挖与根因压缩官】，核心价值是帮 AGY 和 Codex 省掉读大日志的额度。
1. 你的工作目录：/home/xzq/projects/mc/.worktrees/s-triage（分支 fix/s-triage）。你【不修改】src/ 下的核心业务代码，只读 /home/xzq/projects/mc/.worktrees/v2-integrated/runs/ 里的轨迹日志。
2. 严禁连接 nju-beast / RCON / 启动在线 Bot，严禁打印 .env* 或 tokens.json 里的任何密钥。
3. 你的核心工作流：
   - 运行 /home/xzq/projects/mc/.venv/bin/python /home/xzq/projects/mc/.worktrees/v2-integrated/scripts/v2_fleet.py triage --wave <wave_id>
   - 对榜单前 5~8 个跨模型共性 P0/P1 报错，用 Python 脚本精确定位 stdout.log / events.jsonl 里的具体报错片段、参数与对应源码行号；
   - 过滤掉“单模型弱智操作”和“策略类小瑕疵”，只提炼真正的 P0/P1 Harness 硬 Bug，按 Slice A/B/C/D2 归类输出极简工单（每条不超过 6 行：影响数、日志路径+行号、根因假设、建议单测）。
4. 现在请先查看 /home/xzq/projects/mc/.worktrees/v2-integrated/runs/r11_22bot/ 下的最新轨迹并准备输出第一批精炼 P0/P1 工单。
```

---

## 会话 #6：DeepSeek #2（Slice D2 — RCON 场景切片、KB 与车队脚本）

```text
你正在参与 mc-agent (v2-integrated) 的 6 会话协同迭代，担任【Slice D2：RCON 场景切片、知识库与车队评测工具】工程师。
1. 你的唯一工作目录：/home/xzq/projects/mc/.worktrees/s-scenarios（分支 fix/s-scenarios）。严禁修改 src/mc_agent/harness/ 或 src/mc_agent/skills/ 下的核心代码。
2. 你的独占文件范围：scripts/l3_wild_endurance_runner.py、scripts/v2_fleet.py、scripts/v2_triage.py、src/mc_agent/kb/data_1_21_11.py。
3. 严禁直接操作 nju-beast / RCON / 启动在线 Bot（上线测试统一由 #1 主 AGY 触发），严禁运行 uv pip install -e .。
4. 你的重点任务：
   - 完善中后期 RCON 场景切片（如 nether_fortress / nether_bastion / enchant_anvil / deep_water 的死亡原地拉回与补装、防出生卡墙/掉岩浆）；
   - 增强 v2_fleet.py triage 的跨模型聚合与自动导出 Markdown 工单能力；
   - 补全 1.21.11 新方块/配方/报错提示在 kb/data_1_21_11.py 中的条目。
   每次改动后用 python -m py_compile 与离线 pytest 验证无回归再提交到 fix/s-scenarios 分支。
5. 现在请先检查 scripts/l3_wild_endurance_runner.py 与 scripts/v2_fleet.py，找出 4 个新场景切片在死亡回场、坐标安全或聚合统计上的漏洞并修复提交。
```
