# `[L3-Sched-1/2/3]` 隔离工作区实机验证报告

> 日期：2026-11-18  
> 工作区：`/home/xzq/projects/mc/.worktrees/l3-sched`（分支 `feat/l3-scheduler`，基线 `63df850`）  
> Bot：`MapTest_01`（只读复用 `/home/xzq/projects/mc/.state-maptest/tokens.json`，未重新登录）  
> 运行环境：Leaves `1.21.11` + `mc-agent-api v1.3.14` + Qwen 比赛接口  
> Python：`PYTHONPATH=<worktree>/src /home/xzq/projects/mc/.venv/bin/python`；已确认 `mc_agent.__file__` 指向 worktree。

---

## 1. 总览

| 阶段 | 结果 | 关键产物 |
|---|---|---|
| 离线回归 | **199 passed, 1 skipped** | `tests/` |
| Phase A（0-LLM ensure_items） | **3/3 PASS** | `runs/l3-scheduler-live-20261118-180750.json` |
| Phase B（4 Episode 课程） | **PASS，60 分** | `runs/l3-scheduler-live-20261118-185905.json` |
| 野外 35–36 分钟课程 | **完成，20 分** | `runs/curriculum-20261118-191042-706198.json` |

Phase A–C 均只读写 worktree；主目录 `src/`、共享 `.venv` 的 editable `.pth` 未改动。

---

## 2. Phase A：0-LLM `ensure_items` 多级物理链

### 结果

- **A1 PASS**：`mc_window_view(block="chest")` 无 `x/y/z` 自动定位并同步：
  ```text
  iron_ingot:9, redstone:1, coal:4, emerald:12
  ```
- **A2 PASS**：`ensure_items({"lodestone":1,"compass":1,"iron_boots":1})`
  - 耗时 **255.0s**
  - 完整链路：
    ```text
    砍树 -> 木板/木棍 -> 工作台 -> 木镐
    -> 12 圆石 -> 合成并放置 furnace
    -> 从记忆箱取 coal 2
    -> 12 圆石烧成 12 stone
    -> stone_bricks x12 -> stone_brick_slab x18 -> chiseled_stone_bricks x8
    -> lodestone x1
    -> 从记忆箱取 iron_ingot x4 + redstone x1 -> compass
    -> 从记忆箱取 iron_ingot x4 -> iron_boots，装备到 feet
    ```
  - 真实解锁：
    `story/mine_stone`、`story/smelt_iron`、`story/obtain_armor`
- **A3 PASS**：`crossbow` 立即返回：
  ```text
  stop_reason=requires_world_hunt
  missing_hunt_items={"string": 2}
  ```
  0.87s 内干净交还世界狩猎。

### 实机修复

1. **配方 top-up pass**（`src/mc_agent/skills/ensure.py`）
   - 原问题：`wooden_pickaxe` 的 `stick`/`crafting_table` 先消耗了预留给镐的木板，最终只剩 2 木板，`craft_gear("wooden_pickaxe")` 失败。
   - 修复：在 recipe 分支完成 table 保证后，重新逐项检查所有 ingredients，缺料则递归补齐，再执行最终合成。
   - 验证：单独 `ensure_items({"wooden_pickaxe":1})` 从失败变为 PASS。

2. **自动补 furnace**
   - 原问题：空测试台上没有 furnace，`smelt_items` 试图合成 furnace 时缺少可用工作台/木板而失败，导致 stone/lodestone 链中断。
   - 修复：smelt 分支在无背包 furnace 且 8m 内无炉时，递归 `_satisfy_item("furnace",1)`；实测成功合成并放置。

3. **熔炼等待上限**
   - 原问题：12 个石头需要约 120s，但 `smelt_items` 调用被 `95s` 上限截断，只烧出 3–4 个 stone，返回 `smelt_incomplete`。
   - 修复：`ensure_items` 等待窗口提升到 `min(180s, deadline-now)`；Phase A A2 预算调整为 360s。
   - 实测：`collected stone x12`。

4. **记忆箱通配提取**
   - 原问题：`coal_or_charcoal`/`any_log`/`any_planks` 这类通配材料不会从 `WorldMemory.containers` 取货，测试台明明有 coal 却继续挖煤。
   - 修复：`_try_withdraw_from_memory_chest` 展开具体候选（coal/charcoal、各类 log、各类 planks）后逐个 `container_withdraw`。

---

## 3. Phase B：4 Episode 连续课程督导

### 结果：**PASS，60 分**

| Episode | 目标 | 轮数 | 收获 |
|---:|---|---:|---|
| 1 | `adventure/trade` | 7 | `adventure/trade` + `story/smelt_iron` |
| 2 | `adventure/use_lodestone` | 22 | `story/mine_stone` |
| 3 | `adventure/use_lodestone` | 6 | `adventure/use_lodestone` |
| 4 | `story/lava_bucket` | 4 | `story/lava_bucket` |

最终计分：

```json
{
  "adventure/trade": 15,
  "adventure/use_lodestone": 15,
  "story/lava_bucket": 15,
  "story/iron_tools": 5,
  "story/mine_stone": 5,
  "story/smelt_iron": 5
}
```

### 关键实机链路

- Episode 1：
  - `select_next_target` 注入 `[仓储提示]` 与村民提权；
  - Qwen 从记忆箱取 `iron_ingot`/`emerald`，与图书管理员完成 1 emerald -> 1 lantern；
  - 真实解锁 15 分 `adventure/trade`。
- Episode 3：
  - pre-ensure 已把 `lodestone` + `compass` 备入包；
  - Qwen 放置 `lodestone @ [-3800,150,3800]`，手持 compass `mc_interact`；
  - 真实解锁 `adventure/use_lodestone`。
- Episode 4：
  - 记忆库已记录 `lava`；
  - Qwen 持 bucket 对岩浆 `mc_interact`，真实取得 `lava_bucket` 并解锁 15 分。

### 实机修复

1. **pre-ensure 时间片**
   - 原问题：`pre_slice_s = min(ep_timeout*0.35, 90s)`，12 石烧制链需要 120s+，只烧出 3 个 stone 就 `time_budget_slice`，导致 use_lodestone 连续失败。
   - 修复：`min(ep_timeout*0.6, 300s)`；`duration_s` 提升到 1800s 以允许 4 集。
   - 效果：Episode 3 的 0-LLM 备料完成，Qwen 只需 6 轮完成放置/交互。

2. **Phase B 测试台稳定性**
   - 增加不可见 barrier 围栏 + `VoidGeofence(center=(-3800,3800), radius=50, margin=2)`，防止模型误传 `z=-3798` 等错误坐标时走出天空岛。
   - 开始 Phase B 前先 `mc_window_view(block="chest")` 同步箱内库存，否则 `select_next_target` 看不到 emerald/iron，无法注入仓储提示。
   - 预置 3 个 emerald，确保 trade episode 有支付物；记忆箱仍保留 12 个 emerald 供记忆/取货测试。

3. **容器参数容错**
   - 模型多次把数量写成 `items: 9` 而不是 `count: 9`，导致只取 1 个。
   - `_window_item_entries` 现在兼容 `count / items / qty`，避免这种参数名误差放大成多轮取货。

---

## 4. 野外 35–36 分钟原生课程

### 运行

```bash
spreadplayers -3400 -3400 8 24 false MapTest_01
clear MapTest_01
advancement revoke MapTest_01 everything
```

然后以 `--curriculum --curriculum-hours 0.6` 在纯天然主世界运行。

### 结果

- 实际运行：**2181.6s（36.36 分钟）**
- Episode：4
- 最终原始分：**20**
- 计分项：
  ```text
  story/mine_stone 5
  story/upgrade_tools 5
  story/smelt_iron 5
  story/iron_tools 5
  ```

| Episode | 目标 | 状态 | 轮数 | 分数增量 |
|---:|---|---|---:|---:|
| 1 | `bootstrap_stone_age` | answered | **0（0-LLM）** | +10 |
| 2 | `bootstrap_iron_age` | timeout | 18 | 0 |
| 3 | `bootstrap_iron_age` | runtime_error（外部 Qwen 429） | 9 | +5 |
| 4 | `bootstrap_iron_age` | timeout | 18 | +5 |

### 野外记忆

```json
{
  "spawn_points": {"overworld": [-3388, 68, -3417]},
  "last_death": {"dim": "overworld", "pos": [-3396, 16, -3489]},
  "containers": 0,
  "pois": 0,
  "biomes": 0,
  "habitats": {},
  "visited_chunks": 12
}
```

说明：本局课程目标是石器/铁器，主要活动在地下矿井，因此 POI/容器/群系为空是预期；`WorldMemory` 仍记录了出生点、12 个区块足迹和一次真实死亡点。

### 野外发现

1. **Episode 1 的 0-LLM 前置有效**：`actor_requests=0`，直接完成石镐与石器基建，节省一整集模型轮数。
2. **Episode 3 的 `runtime_error` 来自外部并发限流**：
   ```text
   Qwen API HTTP 429: Harness group concurrency limit: 2 simultaneous requests
   ```
   不是本地工具/调度崩溃；Supervisor 继续了后续 Episode，并仍拿到 `story/smelt_iron`。
3. **野外 36 分钟只推进到铁器基建**：4 集预算被 iron age 的采矿/熔炼/超时消耗，未进入 `island_recon_sweep` / `shiny_gear`。要覆盖后续阶段，需要更长课程时长或更激进的铁器阶段收敛。
4. **地下采矿不会生成地表 POI**：这与 WorldMemory 设计一致（`*_ore` 明确排除），但意味着纯挖矿课程不会产生丰富地图记忆。

---

## 5. 变更文件

全部位于 worktree：

- `src/mc_agent/skills/ensure.py`
  - recipe 材料 top-up pass
  - furnace 自动补齐
  - 熔炼等待上限 180s
  - 通配材料从记忆箱取货
- `src/mc_agent/harness/curriculum.py`
  - pre-ensure 时间片 `min(ep_timeout*0.6, 300s)`
- `src/mc_agent/skills/library.py`
  - `_window_item_entries` 兼容 `count/items/qty`
- `scripts/l3_scheduler_live_test.py`
  - A2 预算 360s、stderr 步骤日志、barrier 围栏、Phase B geofence、箱子预同步、emerald 预置、Phase B duration 1800s

离线回归：**199 passed, 1 skipped**。

---

## 6. 结论与后续

- `L3-Sched-1` 的持久 `McToolset`/跨集 WorldMemory 已在 Phase B 与野外课程中实际运行；
- `L3-Sched-2` 的 0-LLM `ensure_items` 已打通 12 石烧炼、furnace 自建、记忆箱取煤/铁/通配材料，A2 全链 255s；
- `L3-Sched-3` 的仓储提示、村民提权、use_lodestone/lava_bucket 动态目标在 Phase B 真实拿分；
- 野外 36 分钟课程达到 20 分，下一轮建议：
  1. 将 iron-age 单集预算/收敛策略再压缩，避免 3 集重复目标；
  2. 对外部 Qwen 429 增加更长退避或跨集任务让渡；
  3. 若目标是丰富地图记忆，野外课程应在铁器后切到 `island_recon_sweep`，而不是继续深挖。
