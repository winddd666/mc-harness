# WorldMemory 实机回测（2026-11-18）

> 环境：nju-beast · Leaves `1.21.11` · `mc-agent-api v1.3.14`  
> 测试 Bot：`MapTest_01`（独立 `.state-maptest` Token，不触碰 `BeastBot_01`）  
> 结果：**M1–M6 17/17 通过**（M0 离线基线 4/4）  
> 报告：[`runs/world-memory-live-20261118-122519.json`](../runs/world-memory-live-20261118-122519.json)  
> 复跑脚本：[`scripts/world_memory_live_test.py`](../scripts/world_memory_live_test.py)

---

## 1. 测试计划与执行结果

| 组 | 覆盖内容 | 结果 |
|---|---|---|
| M0 | `pytest tests/test_world_memory.py`：五层记忆、HUD 预算、110m 回捞、冷却、持久化 | 4/4 |
| M1 | 初始锚点、区块足迹、群系 | 3/3 |
| M2 | POI 记录、>64m 记忆回捞、陈旧 POI 惰性销账、分段远航 | 4/4 |
| M3 | Home Base 自动提升、容器快照、缺料提示、床锚点 | 4/4 |
| M4 | 栖息地回查、HUD 预算 | 2/2 |
| M5 | `0600` 持久化与全量重载 | 1/1 |
| M6 | 传送门锚点、维度隔离、死亡锚点 | 3/3 |

---

## 2. 关键实测数据

### M1 锚点与足迹

- 初始 `spawn_points["overworld"] = [1995, 150, 1995]`
- 沿测试走廊移动 4 段，记录 **5 个区块足迹**：
  - `(124,124) (126,124) (128,124) (130,124) (131,124)`
- **群系层限制**：当前 `Leaves + mc-agent-api` 的 `/status.biome` 返回空字符串（`None`），因此实机无法写入 `biomes`；群系/多群系去重继续由离线单测覆盖。报告中该项标记为 `supported=false`，不视为记忆层故障。

### M2 POI 回捞

- 24m 与 110m 各放置一个 `gold_block`，分别通过 `mc_find_blocks` 记录为 POI。
- 移除 24m 目标后，本地扫描为空；记忆库惰性 `get_block` 校验把陈旧 POI 弹出。
- `mc_skill_mine_blocks(["gold_block"], count=1)`：
  - `recalled_from_memory = {"pos": [2105, 150, 1994], "dist": 107.9, "bearing": "东(+X)"}`
  - 通过 24m 分段远航抵达并真实挖到 **1 个 gold_block**。
- 从 110m 处 `mc_move_to` 返回起点成功。

### M3 基地与容器

- 工作台 + 熔炉 + 箱子自动提升 Home Base：
  - `home_bases["overworld"] = [1996, 150, 1992]`
- 箱子快照：`iron_ingot:8`、`bread:5`
- 背包无铁时 `mc_skill_craft_gear("iron_pickaxe")` 正确返回：
  - `memory_hint` 指向箱内 `iron_ingot×8`
- 床锚点写入：`bed_spawn.pos = [1999, 150, 1992]`

### M4 栖息地与 HUD

- 村民/羊被记录为 habitat；远离后 `mc_get_entities(types=["villager"])` 返回 `memory_habitats`。
- HUD：
  - 总长 **295** 字（`<650`）
  - `记忆库:` 单行 **77** 字（`<=84`）
  - 示例：
    ```text
    记忆库: 家@[1996, 150, 1992](107m,箱:iron_ingot:8,bread:5), bed(104m), chest(105m)
    ```

### M5 持久化

- 强制保存后重新 `WorldMemory(path)` 加载：
  - `spawn_points`、POI、容器、`visited_chunks=5` 全部恢复；
  - 文件权限 `0o600`。
  - `biomes` 为空与 M1 的 API `status.biome` 限制一致。

### M6 维度与死亡

- `portals["overworld"]` 成功记录下界传送门锚点。
- 切到下界后：
  - `spawn_points["the_nether"] = [0, 100, 0]`
  - `visited_chunks` 出现 `("the_nether", 0, 0)`
  - 主世界锚点与区块足迹保持独立。
- 受控 `kill` 一次后：
  - 状态回报 `last_death.pos = [1995, 150, 1995]`
  - `WorldMemory.last_death` 正确写入并持久化。

---

## 3. 结论

- 五层记忆的核心链路在实机环境可用：锚点、容器、POI 回捞/销账、分段远航、栖息地、HUD、持久化、维度隔离、死亡点。
- 实机唯一未能验证的是**群系图鉴**，原因是上游 `/status.biome` 为空；离线单测已覆盖多群系与 48m 去重逻辑。
- 未发现需要修改 `WorldMemory` 核心代码的缺陷；本次新增的是可复跑的实机回归脚本与归档。

---

## 4. 复跑

```bash
# 离线基线
uv run --offline pytest -q tests/test_world_memory.py

# 实机 M1-M6
uv run --offline python scripts/world_memory_live_test.py
```

脚本期望 `MapTest_01` 的 Token 已存在于 `.state-maptest/tokens.json`：

```bash
MC_STATE_DIR=.state-maptest uv run mc-bot --env-file .env.beast login MapTest_01
```

> `MapTest_01` 与 `BeastBot_01` 使用不同 Bot 名和独立 Token 文件，不会触发单 Token 互斥。
