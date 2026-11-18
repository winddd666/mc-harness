# WorldMemory 90 分钟自由巡游与留存分析（2026-11-18）

> Bot：`MapTest_01`（独立 `.state-maptest`）  
> 实际运行：首段约 8 分钟（19 步）+ 续跑 80 分钟（208 步），合计约 88 分钟  
> 报告：[`runs/world-memory-roam-20261118-125531.json`](../runs/world-memory-roam-20261118-125531.json)  
> 摘要：[`runs/world-memory-roam-20261118-125531-summary.md`](../runs/world-memory-roam-20261118-125531-summary.md)  
> 轨迹：[`runs/world-memory-roam-20261118-125531.jsonl`](../runs/world-memory-roam-20261118-125531.jsonl)  
> 脚本：[`scripts/world_memory_roam.py`](../scripts/world_memory_roam.py) / [`scripts/world_memory_roam_report.py`](../scripts/world_memory_roam_report.py)

---

## 1. 轨迹规模

| 指标 | 数值 |
|---|---:|
| 续跑步数 | 208 |
| 起点 | `[661.5, 62.5, -586.6]` |
| 终点 | `[2734.7, 38, -804.5]` |
| 直线位移 | **2084.8 m** |
| 轨迹累计路程 | **4124.2 m** |
| 包围盒 | X `719.5 ~ 2733.7`，Z `-957.5 ~ -518.5` |
| 包围盒尺寸 | 约 **2014m × 439m** |
| 独立区块足迹 | **84** |

巡游主要向东推进，前 ~2km 在地表/浅层，后段进入 `Y≈38` 的洞穴并在最后约 30 分钟反复卡住（`mc_move_to` 多次 0 位移）。

---

## 2. 实际留存的记忆

### 核心锚点

```json
{
  "spawn_points": {"overworld": [-109, 64, -420]},
  "bed_spawn": null,
  "home_bases": {},
  "portals": {},
  "last_death": {"dim": "overworld", "pos": [1995, 150, 1995]}
}
```

- `spawn_points` 是最早观察到的落脚点。
- `last_death` 是从 Bot 历史状态带入的旧测试死亡点，不是本次巡游死亡；说明它是“历史锚点”，HUD 里可能继续显示旧死亡位置。
- 本次没有放床、没有建 Home Base、没有进入传送门/下界，因此这些层为空。

### 区块足迹

- **84 个独立区块**，覆盖 X `719 ~ 2733`、Z `-957 ~ -518` 的带状区域。
- 区块足迹只记录 `mc_status`/HUD 实际采样到的位置，不等同于路径经过的每一个区块；因此 4.1km 轨迹对应 84 个采样区块。

### POI（非矿脉稀缺地标）

- 共 **40 条**：
  - `water`: 32
  - `pumpkin`: 6
  - `lava`: 2
- POI 集中在 X `1657 ~ 2740`、Z `-949 ~ -694`。
- 没有记录到村庄、箱子、要塞、废弃传送门等结构；沿途没有打开容器，因此 `containers=0`。

### 栖息地

本次运行内存中保留了 **11 个栖息点**：

```json
{
  "cow": 2,
  "sheep": 1,
  "pig": 3,
  "chicken": 2,
  "wolf": 3
}
```

注意：`habitats` 原本只存在内存，`save/load` 未写入磁盘，且 TTL 为 15 分钟；本次运行结束后已修复持久化（见第 4 节）。

### 群系

- `biomes=[]`。
- 原因是当前 Leaves/API 的 `/status.biome` 返回空字符串，WorldMemory 无法获得群系名；该层继续由离线单测覆盖。

### HUD 记忆目录示例

- 第 5 步：`记忆库: water(66m)`
- 第 10 步：`记忆库: water(56m)`
- 第 210 步：`记忆库: lava(435m), pumpkin(559m), water(18m)`

说明 HUD 会按距离/类型压缩展示最近的可召回地标，而不是把 40 条 POI 全量塞入上下文。

---

## 3. 主要发现

1. **广域记忆链路有效**：4.1km 轨迹、84 区块、40 个 POI、11 个栖息点都能在运行中积累，HUD 能稳定回显最近地标。
2. **后 30 分钟被洞穴卡死**：在 `Y≈38` 的三面岩壁洞穴中，`mc_move_to` 目标在外侧但寻路失败，`unstick` 只能小幅挪动。
3. **洞穴脱困第一版验证**：10 分钟续跑从 `Y=38` 抬到 `Y=46`，随后被头顶水层 `water@[2736,48,-796]` 挡住；`mc_skill_staircase` 连续返回 `fluid_or_bedrock`，且单次调用耗时较长，导致 10 分钟预算实际跑了约 22 分钟。已改为：
   - 优先 `mc_move_to` 回最近 `Y>=58` 的地表航点；
   - 否则最多尝试 2 个方向的 `staircase up`；
   - 失败后进入 5 分钟冷却，禁止在同一步反复 staircase。
4. **没有结构级记忆**：本次路线是平原/水域带，未遇到村庄/箱子，`containers` 和结构 POI 为 0。下一步可让巡游优先沿 `unexplored_directions` 走，并对 `village/chest/portal` 做定向扫描。
5. **`habitats` 持久化缺口已修复**：`save()` 现在写入 `habitats`，`load()` 会恢复最近 6 个 cluster（`seen_at` 重置为加载时刻，继续受 15 分钟 TTL 约束）。10 分钟续跑期间没有遇到新动物，因此当前磁盘 `habitats=[]`；长跑报告里的 11 个栖息点是运行期内存值。
6. **旧死亡锚点会跨会话保留**：`last_death` 来自 Bot 状态，清空 WorldMemory 后仍会被首次 `observe_status` 写入；这是历史锚点语义，不是本次轨迹的死亡点。

---

## 4. 代码修复

[`src/mc_agent/harness/world_memory.py`](../src/mc_agent/harness/world_memory.py)：

- `save()` 新增 `habitats` 字段：`dim`、`mob`、最近 6 个 cluster 的 `pos`。
- `load()` 恢复 `habitats`，并将 `seen_at` 重置为当前 `monotonic`，避免跨进程时间戳失效。
- 单元测试 [`tests/test_world_memory.py`](../tests/test_world_memory.py) 增加重载后 `villager/sheep` 栖息地仍存在的断言。
- **轨迹深审补充修复（`195 passed`）**：
  1. **`navigate_segmented` 单跳超时与防重跑**（[`library.py`](../src/mc_agent/skills/library.py)、[`mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）：`143507` 续跑第 3->4 步暴露在封闭海底洞窟（第 65–74 步沉入海平面裂谷至 `Y=38`，头顶 `water@[2736,48,-796]`）中单次 `mc_move_to` 因重复直连 + 无超时 3 向跳点阻塞达 **1003.2s**；现已传入 `skip_direct=True` 并为每个 24m 跳点强制设定 `timeout=12000ms`。
  2. **跨会话陈旧 `last_death` 过滤**（[`world_memory.py`](../src/mc_agent/harness/world_memory.py)）：全新 `WorldMemory` 首帧存活轮询仅将历史遗留 `status.last_death` 记为会话基线签名，不再污染新会话记忆；会话内真实新死亡正常记录。
  3. **自由地表探索防下井偏置**（[`hud.py`](../src/mc_agent/harness/hud.py)、[`agent.py`](../src/mc_agent/harness/agent.py)）：针对 `124221` 探索轨迹第 2 轮无视“不要长时间原地挖矿”手滑调用 `mc_skill_mine_dfs` 的问题，剥离否定句与 `"裸露矿脉"` 观察词精准识别地表探索模式，消除 HUD 下井提示并在禁挖矿任务中直接剔除 `mc_skill_mine_dfs`；同时修正 [`world_memory_roam.py`](../scripts/world_memory_roam.py) 外层 `"ok"` 解析导致的 `move_ok=0/208` 统计偏差。
- 全量离线测试：**195 passed**。

---

## 5. 后续建议

- 给地表巡游脚本加入洞穴/水域脱困：连续 0 位移时优先 `staircase up` 回地表，而不是无限 `unstick`。
- 巡游目标中增加“结构优先级”：每 N 步定向扫描 `chest/barrel/village` 相关方块或实体。
- 长途巡游前在安全点放床/箱子并绑定 Home，能把 `bed_spawn/home_bases/containers` 三层也纳入长跑记忆。
- 群系图鉴需要等上游 `/status.biome` 恢复；或在 harness 侧用其他可用信号补充。
