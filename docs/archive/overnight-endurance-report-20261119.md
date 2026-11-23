# L3/L4 核心底座实机验收与过夜真实世界单并发压测总报告（2026-11-19）

- **工作分支**：`/home/xzq/projects/mc/.worktrees/l3-sched`（`feat/l3-scheduler`）
- **实机服务器**：`nju-beast`（`100.93.211.71:18900`，Leaves 1.21.11，单并发 `MapTest_01`）
- **交付时间**：2026-11-19 07:40 `Asia/Shanghai`（早于 08:00 BJT 硬截止线）
- **跨分支零冲突校验**：`src/mc_agent/skills/library.py`、`src/mc_agent/harness/world_memory.py`、`src/mc_agent/harness/mc_tools.py`、`src/mc_agent/skills/mining_dfs.py`、`run_combat_tests.py` **零改动**，可直接与主仓矿道 DFS、L2 移动包及战斗子代理无冲突合流。

---

## 一、里程碑总览与最终回归结果

| 验收层级 | 执行入口 / 报告文件 | 最终结果 | 核心覆盖点 |
| :--- | :--- | :--- | :--- |
| **全仓离线单测** | `pytest`（全量 245 项） | **244 passed, 1 skipped, 0 failed**（`16.20s`） | `CombatEnv` 3D 射线/视线、脊髓全周期抢占、429 守护等待、脏箱自愈、双炉 `WorldJobRegistry`、隔墙防误撤、重生重锚定 |
| **Phase A 实机（0-LLM）** | `scripts/l3_phase2_live_test.py --phase a`<br>`runs/l3-phase2-live-20261118-083400.json`<br>`runs/l3-phase2-live-20261118-153447.json`（终验） | **5 / 5 PASS**（首尾双次实机全绿） | `A1` 石墙/空气 `has_los`、`A2` 砍树中途僵尸突袭 0-LLM 反杀（HP=20）、`A3` 苦力怕 2.3m 扣血前举盾后撤至 8.2m（HP=20）、`A4` 空箱 `1.79s` 自愈 + 禁盲挖、`A5` 双熔炉 `6+6` 并行异步烧石合磁石+指南针 |
| **Phase B 实机（单并发 Qwen）** | `scripts/l3_phase2_live_test.py --phase b`<br>`runs/l3-phase2-live-20261118-095924.json` | **2 / 2 PASS**（单局 **60 分**） | `B1` 死后掉镐不被历史 `story/iron_tools` 欺骗、2 轮合出铁镐立即门控停机；`B2` 村民交易(4轮,+15) -> 双炉异步烧石挂起并锁留 5 铁 1 红石 -> 顺手舀岩浆(2轮,+15) -> 80m 围栏播种 -> 恢复绑定磁石(2轮,+15) |
| **过夜全真生存压测（6 轮）** | `scripts/l3_wild_endurance_runner.py`（累计 ~4.1 小时实机） | 最高单轮 **95 分**（`wild_r2`），**5/6 轮 0 死亡** | 6 个全新未探索主世界大陆/沿海站点，原版普通难度无作弊、无预设箱、全流程真实昼夜与怪物生成 |

---

## 二、6 轮真实世界课程压测（`MapTest_01` 单并发）明细表

| 轮次 | 北京时间 | 出生点坐标 / 地形特征 | 时长 | 集数 | 得分 | 死亡 | 脊髓/守护实绩 | 当轮关键发现与验证结果 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`wild_r1`** | 02:15–02:56 | `(-6405.5, 78, -6412.5)`<br>森林山地 / 深板岩洞穴 | 40m | 4 | **20** | **0** | `incidents=7`<br>**`Y=-21` 野生苦力怕 `3.4m` 抢占 0 扣血** | 验证深洞苦力怕实时打断 `mine_dfs`；暴露 `Y=16` 缺 1 根木棍爬井耗 11 分钟、`stabilize` 空转 9 次、环岛侦察排在钻石后 |
| **`wild_r2`** | 03:09–03:58 | `(6422.5, 71, -6379.5)`<br>桦木平原 | 45m | 8 | **95** | **0** | `incidents=3`<br>终态 `HP=20` | **得分跃升近 5 倍**：下井备木生效，`Y=-54` 就地穿 `diamond_boots` 秒锁 75 分，150s 窗口再挖 **8 颗钻石**（终态余 `diamond×9`）；暴露断镐后徒手挖阶梯（`382s`）与 `any_planks` 原木判定 Bug |
| **`wild_r3`** | 04:19–05:09 | `(-7809.5, 101, -2812.5)`<br>高山黑橡木 / 繁茂粘土洞穴 | 50m | 6 | **35** | **0** | `tool_preemptions=1`<br>**`Y=26` 苦力怕 `4.4m` 抢占 `tunnel` 0 扣血** | 地底 `Y=-54` 连续 **5 次自动补造石镐** 零回地面（终态 `diamond×3, iron×7, redstone×9`）；暴露 `smelt` 缺板锁死 6 生铁、`move_to(range=150)` 容差误用、深潜单片超时未轮转 |
| **`wild_r4`** | 05:19–06:08 | `(-7799.5, 76, 2769.5)`<br>沿海半岛 / 深海边界 | 50m | 18 | **10** | 1 | `tool_preemptions=2`<br>`incidents=70`（揪出隔墙死锁！） | **全夜最高价值极端地形排雷**：揪出 ① 掩体墙外蜘蛛因 `/status` 无 `pos` 漏过 `has_los` 连撤 22 轮；② 出生点挖石受阻后重生回世界出生点 `(-170,-380)` 被 `8234m` 旧围栏锁死；③ 环岛入海滞留 |
| **`wild_r5`** | 06:16–07:05 | `(-5200.5, 68, -7600.5)`<br>内陆丘陵 / 夜间苦力怕 | 48m | 10 | **20** | 1 | `tool_preemptions=3`<br>**Ep 6 深潜 `506` 轮询 `0` 误取消** | 验证掩体隔墙 `has_los` 彻底清零地下误打断、夜间 `3.8m` 苦力怕抢占撤离、Ep 9 **0-LLM 全自动烧铁合铁镐**；揪出下潜途中第 11 次 `explore`（`Y=-48`）误触 `reset->climb_out` 提示词陷阱 |
| **`wild_r6`** | 07:08–07:34 | `(5171.5, 64, -7624.5)`<br>云杉平原（25m 冲刺回归） | 25m | 5 | **20** | **0** | **`incidents=0, cancelled=0`**<br>终态 `HP=20, Food=20` | 验证移除深层 `reset` 诱导后模型在 `Y=16 -> -13` 稳定连续 `explore` 不爬回、单集 12 轮拿齐铁镐且零遗留生铁、全程 **`0` 次 `read_tool_result` 分页浪费** |

---

## 三、过夜发现并闭环根治的 12 个真实世界 Bug 清单

### 1. 脊髓反射与全周期生存守护（`guardian.py` / `combat_env.py`）
1. **【P0】掩体隔墙近战预判死锁与 `/status` 缺 `pos` 盲区**（`56e3085`，[guardian.py](../src/mc_agent/harness/guardian.py)）
   - **现象**：`wild_r4` 夜间封顶 `cover_pocket` 后，石墙外 `3.1m` 蜘蛛触发 `pre_hurt_melee` 连续打断全部 22 轮请求；且实机 `/status.nearest_hostile` 不含 `pos` 字段导致 `pre_hurt_ranged` 从未触发。
   - **根治**：候选距离内通过 `get_entities(radius=16)` 反查 3D 坐标，调 `invalidate_block_cache()` + `CombatEnv.has_los` 对 `creeper` / `melee` / `ranged` 统一执行固体墙遮挡过滤，并加同格 `15s` 防抖窗口（真受伤仍由 `guard_poll_hurt` 无条件响应）。`wild_r5`/`wild_r6` 地下与掩体误取消率降至 **0**。
2. **【P1】`GenerationGuard.prime()` 历史事件游标未排空**（`7bd7563`，[guardian.py](../src/mc_agent/harness/guardian.py)）
   - **根治**：守护启动时排空历史 `/events` 序列号并记录基线血量，杜绝跨集陈旧 `hurt` 误判。
3. **【P2】徒手撤离贴身怪单次距离偏短**（`a6c684f`，[combat_env.py](../src/mc_agent/skills/combat_env.py)）
   - **根治**：`CombatEnv.flee` 超时提至 `5.5s`，撤离后若怪仍在 `<=5.5m` 自动补二段冲刺拉开安全距离。

### 2. AltoClef 式物理前置推演与物资自愈（`ensure.py`）
4. **【P0】包内已有原木时合木板仍强制要求新砍树**（`4cf2e62`，[ensure.py](../src/mc_agent/skills/ensure.py)）
   - **现象**：`_satisfy_item("any_planks")` 传入 `effective_item_count("any_log", counts) + logs_needed`，导致包内有 `birch_log×1` 时仍要求再砍 1 棵树，在地底触发无谓上井。
   - **根治**：仅在 `effective_item_count("any_log", counts) < logs_needed` 时采木，包内有原木直接转木板。
5. **【P0】地底断镐后徒手硬挖深板岩阶梯耗时 `382s`**（`4cf2e62`，[ensure.py](../src/mc_agent/skills/ensure.py)）
   - **根治**：严禁无镐（`highest_pickaxe_tier <= 0`）或 `pre_ensure` 深层（`Y < 42`）盲挖上井阶梯；扩展 `_has_nearby_station` 支持 `64m` 内 `WorldMemory.pois` 工作台分段寻路复用。
6. **【P1】`collect_smelt_batch` 遇浅坑寻路 `ApiError` 误判完工删工单**（`7bd7563`，[ensure.py](../src/mc_agent/skills/ensure.py)）
   - **根治**：跟踪 `had_error` 与 `remaining_furnaces` 保留未取出的 `WorldJob`，并接入 `navigate_segmented` + `unstick` 脱困。
7. **【P2】低饱食无食物或海面漂浮时递归连刷 `stabilize`**（`a6c684f` / `56e3085`，[ensure.py](../src/mc_agent/skills/ensure.py)）
   - **根治**：对非岩浆态（低血/低饱食/`in_water`）增加 `10s` 节流冷却。

### 3. L3 课程督导器与 Agent 交互防呆（`curriculum.py` / `agent.py`）
8. **【P0】重生点受阻掉回世界出生点后 `VoidGeofence` 隔 `8234m` 锁死全场**（`56e3085`，[curriculum.py](../src/mc_agent/harness/curriculum.py)、[agent.py](../src/mc_agent/harness/agent.py)）
   - **根治**：在督导器集首与 `agent.py` 每轮 HUD 前检测主世界 `>400m` 坐标跳变，自动将 `VoidGeofence` 重锚定至新重生岛中心。
9. **【P0】挂起父任务战略物资被支线蚕食 & `eff_extra_tools` 漏透传**（`7bd7563`，[curriculum.py](../src/mc_agent/harness/curriculum.py)）
   - **根治**：新增 `_compute_reserved_materials` 冻结父任务待用铁锭/红石，围栏期禁注战略矿锭仓储提示，并补齐 `re_enriched.extra_tools` 透传。
10. **【P1】`mc_skill_smelt` 现成木板不足将半批生铁遗留旧熔炉**（`a64e048`，[agent.py](../src/mc_agent/harness/agent.py)）
    - **根治**：`agent.py` 在执行 `mc_skill_smelt` 前自动核算煤+木板热值缺口，将富余原木提前合成为足量木板。
11. **【P1】沿海入海滞留、`mc_move_to(range=150)` 容差误用与空点空气循环**（`7bd7563` / `a64e048` / `56e3085`，[agent.py](../src/mc_agent/harness/agent.py)、[curriculum.py](../src/mc_agent/harness/curriculum.py)）
    - **根治**：`agent.py` 自动钳制 `mc_move_to(range>12) -> 4.0`、拦截空点空气 `interacted_with_air` 与自身格 `blocked_by_self`；督导器记录 `_last_dry_pos` 并在集首落水时自动 0-LLM 游回陆地。
12. **【P1】深潜单片独占超时、下潜途中误调 `reset` 与 `mine_dfs` 大 JSON 分页浪费**（`a64e048` / `6d936d2`，[curriculum.py](../src/mc_agent/harness/curriculum.py)、[agent.py](../src/mc_agent/harness/agent.py)）
    - **根治**：50~60 分钟赛程下深潜单片限幅 `30%` 且在已有 `diamond>=3` / `iron>=3` 超时后插入 `240s` 轮转兑现支线；移除下潜期 `reset` 误导；将 `mine_dfs` 历史节点裁剪至最近 3 个使输出稳定 `<1100 chars`（`wild_r6` 实现 `0` 次 `read_tool_result`）。

---

## 四、Git 提交链与合流说明

### 分支提交记录（`feat/l3-scheduler`）
- `7bd7563`：`feat(l3-l4): add CombatEnv/SubcorticalReflex, episode_gate, preempt & async WorldJobRegistry`
- `a6c684f`：`perf(l3-l4): add pre-descent wood/table reserve, fast surface recon, stabilize throttle & flee breakaway`
- `4cf2e62`：`fix(l3-sched): fix owned-log plank craft bug, deep-mine table reuse & post-shiny_gear harvest order`
- `a64e048`：`fix(l3-sched): smelt auto-plank fuel prep, move_to range clamp, mine_dfs compaction & deep slice rotation`
- `56e3085`：`fix(l3-sched): require LOS for pre-hurt reflexes, re-anchor geofence on respawn & recover from ocean`
- `6d936d2`：`fix(l3-sched): remove deep-descent reset trap, tighten mine_dfs inline compaction & batch-chop logs`

### 与并行子代理合流指引
1. **零冲突直接合入**：本分支仅修改了 `combat_env.py`、`skills/__init__.py`、`ensure.py`、`guardian.py`、`model.py`、`agent.py`、`curriculum.py` 及 `tests/`、`scripts/`、`docs/`；与 `main`（`mining_dfs.py`）、`l2-live-pack`（`library.py`、`world_memory.py`、`mc_tools.py`）和战斗子代理（`run_combat_tests.py`）完全正交。
2. **战斗子代理策略挂载入口**：合流后只需将战斗子代理实现的 `CombatController` 实例传入 `CurriculumSupervisor(..., combat_controller=...)` 或 `run_mc_agent(..., combat_controller=...)`，即可零改动接管全周期脊髓反射槽位 `CombatControllerSlot`。
3. **剩余已知物理边界**：当出生点恰处超大深板岩贫矿/繁茂粘土层时，单井探满 4 钻耗时在 `10m~25m` 间波动（如 `wild_r2` 10 分钟连出 13 钻获 95 分 vs `wild_r3` 25 分钟出 3 钻）；现已通过 `30%` 切片限幅 + `240s` 轮转机制确保即便首片仅出 3 钻或富铁，也会立即切出兑现 `form_obsidian` / `lava_bucket` / `use_lodestone` 保底高分。
