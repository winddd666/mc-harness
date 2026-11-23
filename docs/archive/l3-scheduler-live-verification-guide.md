# `[L3-Sched-1/2/3]` 隔离工作区实机运行与验证派单指南

> **目标读者**：接手执行实机验证与边界补测的编码/测试 Agent  
> **创建日期**：2026-11-18  
> **隔离工作区路径**：`/home/xzq/projects/mc/.worktrees/l3-sched`（独立 Git 仓库克隆，分支 `feat/l3-scheduler`）

---

## 1. 严禁越界的隔离与安全红线（必读）

1. **严格在隔离工作区内读写代码与产物**：
   - 所有待测代码、测试脚本、运行状态与轨迹日志**只能读写在 `/home/xzq/projects/mc/.worktrees/l3-sched/` 下**（状态目录 `.worktrees/l3-sched/.state-l3-live/`，轨迹目录 `.worktrees/l3-sched/runs/`）。
   - **严禁修改主工作区 `/home/xzq/projects/mc/src/`**，也**严禁在隔离目录执行 `uv pip install -e .` 篡改共享 `.venv` 的 editable `.pth` 指向**！主目录可能有长期压测进程正在运行。
   - 运行 Python 或 `pytest` 时，统一使用显式前缀：
     ```bash
     PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src /home/xzq/projects/mc/.venv/bin/python ...
     ```
     并在启动时确认 `mc_agent.__file__` 指向 `/home/xzq/projects/mc/.worktrees/l3-sched/src/mc_agent/__init__.py`。

2. **账号与单 Token 互斥红线**：
   - **严禁触碰 `BeastBot_01` 或正在运行挖矿压测的 `MineDFS_01..04`**！
   - 实机测试请只读复用已签发且空闲的独立测试账号：
     - 首选：`MapTest_01`（只读读取 `/home/xzq/projects/mc/.state-maptest/tokens.json`）
     - 备选：`GuiTest_01`（只读读取 `/home/xzq/projects/mc/.state-gui-test/tokens.json`）
   - **禁止对已有缓存 Token 的角色再次执行 `mc-bot login`**（服务端单 Token 互斥会踢掉旧 Token）；直接加载 `tokens.json` 探活 `status()`，若返回 `not_connected` 则调 `bot.connect()` 即可。
   - 开跑前先用 `pgrep -af "mc_agent|MapTest_01|GuiTest_01"` 确认所选 Bot 当前无其他本地进程占用，且测试选点远离其他在线 Bot（建议 `(-3800, ~, 3800)` 或 `(-3600, ~, -3600)`）。

---

## 2. 本次待验证的核心模块清单

| 模块编号 | 核心文件（均位于 `.worktrees/l3-sched/`） | 待验证核心行为 |
| :--- | :--- | :--- |
| **`L3-Sched-1`** | [`src/mc_agent/harness/curriculum.py`](../src/mc_agent/harness/curriculum.py)<br>[`src/mc_agent/harness/agent.py`](../src/mc_agent/harness/agent.py)<br>[`src/mc_agent/harness/mc_tools.py`](../src/mc_agent/harness/mc_tools.py) | 1. `CurriculumSupervisor` 全场共享唯一持久 `McToolset`，跨 Episode 保留五层 `WorldMemory` 与 `MiningDFS` 鱼骨树；<br>2. 按 `micro/short/medium/deep/expedition` 5 档动态分配 `8~45` 轮与 `180~1800s` 预算；<br>3. 深潜钻石/黑曜石/探索新区块取得材料增量（`material_progress=True`）时免记 Strike，不误拉黑。 |
| **`L3-Sched-2`** | [`src/mc_agent/skills/ensure.py`](../src/mc_agent/skills/ensure.py)<br>[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py) | 1. 0-LLM 物理硬前置递归器 `MacroSkills.ensure_items`（仅含原版配方、熔炼对、最低采集镐级 `tier 1..4`，**0 软门禁**）；<br>2. 优先盘点 `背包 + 副手 + 护甲` -> 72m 内 `WorldMemory.containers` 箱子自动 `container_withdraw` 提货 -> 28m 内已有 `furnace`/`crafting_table` 自动走回复用；<br>3. 步间 `check_and_stabilize` 守护，近敌掉血返回 `threat_interrupted`，看脸掉落物（如 `string`/`leather`/`blaze_rod`）立即返回 `requires_world_hunt` 不死循环。 |
| **`L3-Sched-3`** | [`src/mc_agent/harness/curriculum.py`](../src/mc_agent/harness/curriculum.py) | 1. `select_next_target` 联合盘点背包+穿戴+仓箱库存并自动注入 `[仓储提示]` + 解锁 `mc_skill_container`；<br>2. `WorldMemory` 探明 `villager`/`lava`/`obsidian`/`bed`/动物后动态提权 `adventure/trade`、`lava_bucket`、`sleep_in_bed` 等支线；<br>3. `shiny_gear` 同层套餐合并（首 4 钻立合 `diamond_boots` 锁 75 分，余力顺采 5 钻备钻镐+附魔台）；<br>4. `enable_pre_ensure=True` 时可由 0-LLM 前置直接完成简单目标（`actor_requests=0`）或将已备料回执注入本集任务头部。 |

---

## 3. 现成离线与实机测试脚本

### 3.1 离线全量回归（先跑一次确认环境干净）
```bash
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src \
  /home/xzq/projects/mc/.venv/bin/pytest \
  /home/xzq/projects/mc/.worktrees/l3-sched/tests -q
```
- 当前基线：**`199 passed, 1 skipped`**（专项单测见 [`tests/test_l3_scheduler.py`](../tests/test_l3_scheduler.py)）。

### 3.2 结构化实机测试脚本：[`scripts/l3_scheduler_live_test.py`](../scripts/l3_scheduler_live_test.py)
脚本已在隔离工作区就绪，通过 SSH RCON 在 `nju-beast` 天空隔离台 `(-3800, 150, 3800)` 自动搭建箱子、原木、石头露头、受控岩浆池与图书管理员村民，分两阶段验收：

```bash
# 仅跑 Phase A（0-LLM ensure_items 多级物理链 + 无坐标箱子记忆同步 + 看脸材料边界）
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/l3-sched/scripts/l3_scheduler_live_test.py --phase a

# 仅跑 Phase B（真 Qwen 模型驱动多 Episode 持久课程督导器连续拿分）
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/l3-sched/scripts/l3_scheduler_live_test.py --phase b --episodes 4

# 全流程连跑（Phase A + Phase B）
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src \
  /home/xzq/projects/mc/.venv/bin/python \
  /home/xzq/projects/mc/.worktrees/l3-sched/scripts/l3_scheduler_live_test.py --phase all --episodes 4
```

### 3.3 原生野外自由生存课程实机跑测（真地形端到端检验）
完成可控测试台验收后，把 `MapTest_01` 用 RCON `spreadplayers -3400 -3400 8 24 false MapTest_01` + `clear` + `advancement revoke MapTest_01 everything` 放到纯天然主世界野外，直接跑原生 `--curriculum` 30~40 分钟：

```bash
cd /home/xzq/projects/mc/.worktrees/l3-sched && \
MC_STATE_DIR=/home/xzq/projects/mc/.state-maptest \
MC_WORLD_MEMORY_PATH=/home/xzq/projects/mc/.worktrees/l3-sched/.state-l3-live/world_memory_wild.json \
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l3-sched/src \
  /home/xzq/projects/mc/.venv/bin/python -m mc_agent.harness.agent \
  --env-file /home/xzq/projects/mc/.env.beast \
  --bot MapTest_01 \
  --curriculum \
  --curriculum-hours 0.6 \
  --survival-guard \
  --hud-mode ephemeral \
  --context-mode sliding \
  --tool-profile compact
```

---

## 4. 前序冒烟已跑出的实机现象与重点排查项

前序短冒烟已在 `nju-beast` 实机跑过一轮，结果如下（供接手 Agent 直接重点跟进）：

1. **已实机验证通过项**：
   - `A1_window_view_no_xyz_syncs_chest` **PASS**：不传 `x,y,z` 仅调 `mc_window_view(block="chest")`，实机成功定位 `(-3798, 150, 3798)` 并将箱内 `iron_ingot:9, redstone:1, coal:4, emerald:12` 同步入 `WorldMemory`。
   - `A2` 半程（`compass` + `iron_boots` 0-LLM 全链）**PASS**：裸手自动从箱子取铁锭/红石 -> 砍树合木镐 -> 摆工作台合出 `compass:1` 与 `iron_boots:1` 并自动穿到 `feet` 槽，真实解锁服务器 `minecraft:story/obtain_armor`。
   - `A3_mob_drop_clean_handback` **PASS**（耗时 `0.05s`）：请求 `crossbow` 立即返回 `stop_reason="requires_world_hunt", missing_hunt_items={"string": 2}`。
   - `Phase B Episode 1`（真 Qwen + `WorldMemory` 村民提权交易）**PASS**：`select_next_target` 自动识别 `WorldMemory` 中的村民与箱内绿宝石，将 `minecraft:adventure/trade`（15分）提权至第 1 集并注入 `[仓储提示]` + `[记忆库提权]`；Qwen 仅用 **7 轮请求**自动去箱子取绿宝石（同步更新记忆剩余 `emerald:11`）并调 `mc_skill_trade` 换出 `lantern` 真实解锁 15 分交易成就！

2. **接手 Agent 需重点深测与打磨的实机细节**：
   - **排查点 1（`A2` 中 `lodestone` 12 块石头采集/烧炼衔接收尾）**：前序冒烟中 `ensure_items({"lodestone": 1, "compass": 1, "iron_boots": 1})` 在 34.9s 内完成了 `compass` 和 `iron_boots`，但 `lodestone` 停在了 `remaining_deficit: {"lodestone": 1}`。请打印 `ens["steps"]` 逐步核查：是砍完树后距离石头露头 `>28m` 没扫到 `stone`、还是烧出 `12 stone` 后合成 `stone_bricks -> stone_brick_slab -> chiseled_stone_bricks` 某一步配方名称/批量在 Mineflayer `POST /craft` 中有差异（例如 Mineflayer `craft` 的 `count` 是产物个数还是合成次数 `recipe_calls`！注意核查 `stone_bricks` 1 次产出 4 个时传 `count=12` 还是 `count=3`！）。
   - **排查点 2（Phase B 后续 Episode 连续得分验证）**：完整跑完 `Phase B` 4 个 Episode，确认 `use_lodestone`（摆磁石 + 手持 `compass` 右键交互拿 15 分）与 `lava_bucket`（0-LLM 备好 `bucket` + 按记忆坐标去岩浆池 `mc_interact` 拿 15 分）均真实触发成就，且 `WorldMemory` POI 在跨集切换后零丢失。
   - **排查点 3（纯天然野外 35 分钟课程实测）**：在天然地形跑通 `bootstrap_stone_age -> bootstrap_iron_age -> island_recon_sweep / shiny_gear`，记录每集动态预算、0-LLM 节省轮数、跨集矿井树复用与最终 `RULES.md` 总得分，生成实测复盘报告写入 `.worktrees/l3-sched/docs/l3-scheduler-live-report-20261118.md`。
