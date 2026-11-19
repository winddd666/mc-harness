# 模式 B「找到村庄」实机验证（2026-11-18）

> Bot：`MapTest_01`（`.state-maptest`）  
> 提示词：`docs/prompts/surface-explore-system.txt` + `docs/prompts/find-village-task.txt`  
> 运行：120 轮（约 30 分钟），状态 `answered`  
> 报告：[`runs/20261118-161423-475679.json`](../runs/20261118-161423-475679.json)  
> 日志：[`runs/village-hunt-20261118-v5-near.log`](../runs/village-hunt-20261118-v5-near.log)

---

## 1. 结果

为了排除“世界太大、随机走碰不到”的干扰，这次把 Bot 放在最近的针叶林村庄外约 23m 处再启动。结果：

- 首轮扫描到 `torch`、`bell`；
- `mc_get_entities` 看到 3 个 `villager`；
- 模型判断“发现村庄”，移动到村庄中心；
- 成功打开箱子并记录内容；
- 成功绑定村庄的蓝色床作为重生点；
- 后续模型离开村庄进入水域，最后停在 `(-51, 62, 360)`，但**村庄记忆已完整写入 WorldMemory**。

## 2. 最终 WorldMemory（村庄相关）

### 锚点

```json
{
  "spawn_points": {"overworld": [-109, 64, -420]},
  "bed_spawn": {"dim": "overworld", "pos": [-825, 72, 714]},
  "home_bases": {},
  "portals": {},
  "last_death": {"dim": "overworld", "pos": [1995, 150, 1995]}
}
```

### 容器

```json
{
  "pos": [-825, 76, 713],
  "block": "chest",
  "items": {
    "large_fern": 1,
    "emerald": 3,
    "spruce_log": 1,
    "potato": 2,
    "bread": 6,
    "bundle": 1
  }
}
```

### 村庄 POI

```text
bell      [-807, 75, 707]
white_bed [-825, 72, 714]
blue_bed  [-825, 72, 714]（头部）
purple_bed[-826, 76, 713]（尾部）
chest     [-825, 76, 713]
```

### 栖息地

```json
{"overworld:villager": 1}
```

本次磁盘文件已包含 `habitats`，验证了上一轮修复后的持久化链路。

### 总体记忆规模

| 项目 | 数量 |
|---|---:|
| 区块足迹 | **199** |
| POI | **49** |
| 容器 | **1** |
| 栖息地 | **1**（villager） |
| 群系 | 0（API `status.biome` 为空） |

POI 分类：

```text
water 32, pumpkin 6, chest 4, lava 2,
purple_bed 2, bell 1, blue_bed 1, white_bed 1
```

## 3. 关键过程

```text
mc_find_blocks(types=["bell","lectern","composter","hay_block","farmland","torch"])
-> bell @ [-807,75,707], torch @ ...

mc_get_entities(types=["villager","iron_golem","cat"])
-> villager x3

mc_window_view(block="chest", x=-825, y=76, z=713)
-> 记录 emerald:3, bread:6, potato:2, ...

mc_skill_bind_bed(x=-825, y=72, z=714)
-> bed_spawn 写入成功
```

## 4. 发现的问题

1. **最终自然语言总结会“忘记”已经找到村庄**  
   模型最后回答写“尚未找到目标村庄”，但 WorldMemory 里 bell/bed/chest/villager 都已记录。  
   → 结论：**以 WorldMemory/工具结果为准，不要只看模型收尾文字**。

2. **找到村庄后容易继续走远进入水域**  
   绑定床、开箱后，模型继续按“扩大探索”走，进入海边浅水并反复 `unstick/flee`，最终离村庄 1km 以上。  
   → 下一版提示词可以加：找到村庄并完成记录后，优先在村庄 200m 内继续扫描/记录，不要立刻远征。

3. **`bed` 类型已做别名归一化**  
   `mc_find_blocks` 现在会把 `bed` 展开为 16 种床 ID，避免 `unknown block bed`。

## 5. 结论

- 模式 B 的核心目标已达成：**找到村庄 + 记录村民/钟/床/箱子**。
- WorldMemory 的村庄层、容器层、床锚点、栖息地持久化均实际验证。
- 需要在提示词里补一条“找到村庄后不要立刻远离”，否则容易把已找到的村庄丢在身后。
