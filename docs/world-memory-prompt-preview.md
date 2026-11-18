# 模型看到的记忆与提示词（WorldMemory / 自由巡游）

> 说明：90 分钟长跑本身用的是**无 LLM 的地表巡游脚本**，所以那一段没有模型提示词。  
> 下面是：① 当前 `mc-harness` 实际发给模型的 system/task/HUD 结构；② 第一次 LLM 尝试实际使用的提示词；③ 为下一次“模型驱动的地表巡游”准备的推荐提示词；④ 长跑最终留下的 WorldMemory 形状。

---

## 1. 提示词如何组装

`src/mc_agent/harness/agent.py`：

```python
initial_user_content = f"任务目标：{task}"
messages = [
    {"role": "system", "content": MC_COMPACT_SYSTEM_PROMPT},
    {"role": "user", "content": initial_user_content},
]
```

每轮请求前：

```python
ephemeral_hud = build_world_hud(toolset, task=task, ...)
messages = context.build(messages, ephemeral_hud=ephemeral_hud)
```

`_append_ephemeral_hud` 把 HUD 贴到最后一条消息末尾；它**不会进入历史**，只影响当次请求。

也就是说模型看到的不是完整 `world_memory.json`，而是：

1. `MC_COMPACT_SYSTEM_PROMPT`；
2. `任务目标：...`；
3. 动作历史/工具结果；
4. 末尾 `[实时HUD]`，其中包含一行 `记忆库:`；
5. 工具返回里的 `memory_blocks` / `memory_habitats` / `memory_hint`。

---

## 2. 当前 compact system prompt（实际文本）

```text
你是 MC 1.21.11 竞赛生存智能体。每轮末尾附最新 `[实时HUD]`（格坐标/HP/饱食/耐久/暴露方块/缺口），勿空转查状态。
铁律：
1. 调工具前仅输出<=20字当前真实意图（禁复读旧口号/手写JSON；思考模式仅在think内<=120字核对HUD与缺口），直接发 `tool_calls`。
2. 优先宏技能：`mc_skill_harvest_logs`、`mc_skill_craft_gear`、`mc_skill_mine_dfs`（地下下井找矿/双镐切换/遇洞转角/零挖返井口）、`mc_skill_mine_blocks`、`mc_skill_smelt`、`mc_skill_collect_drops`、`mc_skill_staircase`、`mc_skill_tunnel`、`mc_skill_unstick`（树冠/卡死/水上脱困）、`mc_skill_bunker`、`mc_skill_flee`；禁止胡乱挖泥土/装饰石或盲调`mc_craft`寻路旧台。
3. 敌情与保坑：HUD标`隔墙怪`(岩壁阻隔)且未掉血时安全，严禁调`mc_skill_flee`弃坑回地表，继续就地采矿/熔炼并避开该方向掘进！仅当`近敌(无遮挡!)`或刚受击：单只僵尸/蜘蛛且有剑/斧且HP>=12调`mc_attack(entity_id)`反杀；苦力怕/骷髅/群怪/无剑斧调`mc_skill_flee`(探矿穴内自动封堵保坑，露天冲刺逃离)。
4. 硬规：仅真实入包才算取得材料；3x3 配方直接用`mc_skill_craft_gear`就地建台合装；镐级(stone/coal=木,iron/copper=石,diamond=铁,obsidian=钻)；`mc_move_to` 禁传 y；`in_water` 禁强挖先 `mc_skill_unstick`；勿重试失败坐标；虚空岛禁越界。
5. 目标确认达成后停用工具并简述结果。
```

注意：这个 system prompt 是**生存/采矿优先**的，所以第一次 LLM 尝试在 `mc_move_to` 一步后直接 `mc_skill_mine_dfs(iron)`，随后又转钻石；这与“地表远行/记忆采集”目标冲突。长跑因此改用无 LLM 的地表巡游脚本。

---

## 3. 第一次 LLM 尝试实际使用的 task prompt

```text
在接下来90分钟自由探索当前世界。优先沿地表向远处走，尽量覆盖不同方向与新区块，不要长时间原地挖矿或下潜。沿途观察并记录：地形与生物群系、裸露矿脉、湖泊/岩浆、村庄或其他结构、箱子/容器、动物与村民。每走一段用HUD确认位置并利用记忆库，遇到怪物先保命。目标是在安全前提下走出尽可能大的探索范围，并留下尽可能丰富的WorldMemory。
```

实际首轮动作：

```text
mc_move_to({"x":-100,"z":-392,"range":50})
-> ok, pos [-115.5,70,-391.5]

mc_skill_mine_dfs({"action":"explore","goal_count":20,"target":"iron","target_y":-30})
-> stop_reason: time_slice_progress, pos_after [-123.6,62,-391.5]

mc_skill_mine_dfs({"action":"explore","goal_count":15,"target":"diamond","target_y":-50})
```

结论：task prompt 不足以压过 system prompt 的“优先挖矿”铁律。

---

## 4. 下一次“模型驱动地表巡游”提示词（已落地为文件）

`mc-harness` 已新增 `--system-prompt-file`，可直接加载下面的专用 system prompt，避免继续使用默认的采矿优先 system prompt。

### 4.1 地表广域探索

- system：`docs/prompts/surface-explore-system.txt`
- task：`docs/prompts/surface-explore-task.txt`
- 用法：`docs/prompts/README.md`

task 明确要求：

```text
在接下来 90 分钟沿地表自由巡游。优先向东/南/西/北不同方向走远，单步 48–72m，连续走 4–6 步再停下观察；目标至少走出 1500m、覆盖 60 个新区块。...不要为挖矿下潜...
```

### 4.2 找村庄专项

- system：`docs/prompts/surface-explore-system.txt`
- task：`docs/prompts/find-village-task.txt`
- 用法：`docs/prompts/README.md`

task 明确要求：

```text
在接下来 90 分钟沿地表寻找至少 1 个村庄，并记录村庄记忆。... mc_find_blocks(types=["bell","lectern","composter","hay_block","farmland","torch"], radius=64) ... mc_get_entities(types=["villager","iron_golem","cat"]) ...
```

### 4.3 旧版建议文本（保留作参考）

```text
你是 MC 1.21.11 地表探索与地图记忆采集智能体。本轮唯一目标是安全扩大 WorldMemory 覆盖，不追求挖矿、装备或计分进度。

铁律：
1. 禁止主动挖矿/下潜/开隧道：不要调用 mc_dig、mc_skill_mine_dfs、mc_skill_mine_blocks、mc_skill_tunnel、mc_skill_harvest_logs。只有连续两次 move_to 位移为 0 且 y<55 时，才允许 mc_skill_staircase(vertical="up", steps=8) 返回地表。
2. 地面移动优先：每步用 mc_move_to(x, z)（禁传 y），单步 48–72m；方向优先 WorldMemory.unexplored_directions 的未探索方向，遇到 path_not_found/stalled 立即换向，不反复撞同一坐标。
3. 每 2–4 步调用 mc_find_blocks(radius=24) 和 mc_get_entities(radius=32)，让记忆记录水、岩浆、南瓜、村庄、箱子、动物与村民；看到村庄/箱子/床要靠近并用 window/view 记录容器。
4. 保命优先：HP<12、食物<14、近敌<=7 格或进入水下/岩浆时，先用 mc_skill_stabilize / mc_skill_flee 处理，再继续。
5. 到安全且资源合适的位置，可放下床/箱子并绑定重生点，把 bed_spawn/home_bases/containers 也写入记忆。
6. 每 10 步查看 HUD 的 `记忆库:` 行，确认最近地标；不要在同一区域反复扫描。
7. 目标不是“挖到东西”，而是“走过更多区块、留下更丰富的可召回记忆”。
```


---

## 5. 模型实际看到的 HUD 示例（来自长跑 step 210）

```text
[实时HUD（刚采样，勿再空转查status/inventory）]
位置=[2725,38,-805](overworld/?,夜晚) | HP=20 Food=20 | 状态:地形=探矿穴(3面岩壁,开:west);近敌=spider#925255@12.4m
背包: cobblestone:286, torch:64, coal:32, oak_planks:32, crafting_table:1, furnace:1, iron_sword(满250), raw_iron:1, shield(满336), cooked_beef:64, granite:37, andesite:36, bread:32, pointed_dripstone:17, +9种 | 副手=shield 护甲=iron_helmet,iron_leggings,iron_boots 空槽=9
16m暴露: 暂无明显资源(可阶梯下探或换向寻路)
缺口提示: 木板与圆石均已充足，可先工作台+木镐或直接调mc_skill_craft_gear合木镐后合石镐
记忆库: lava(435m), pumpkin(559m), water(18m)
```

其中只有最后一行 `记忆库:` 是 WorldMemory 的直接投影，严格 ≤84 字；完整 40 条 POI 不会全部塞给模型。

---

## 6. 工具结果里的记忆透穿示例

- `mc_find_blocks` 本地无目标时：

```json
{
  "blocks": [],
  "memory_blocks": [
    {"name": "gold_block", "pos": [2105, 150, 1994], "dist": 107.9, "bearing": "东(+X)"}
  ],
  "memory_hint": "当前 32m 内未扫到目标，但记忆库已探明最近 gold_block 位于 ..."
}
```

- `mc_get_entities` 本地无村民时：

```json
{
  "entities": [],
  "memory_habitats": [
    {"name": "villager", "pos": [4, 64, 2], "dist": 96.0}
  ]
}
```

- `craft_gear` 缺料时：`memory_hint` 指向箱内库存，例如 `iron_ingot×8`。

---

## 7. 最终 WorldMemory 形状

> 具体数值见 `runs/world-memory-roam-*-summary.md` 与 `.state-maptest/world_memory_MapTest_01.json`。

```json
{
  "version": 1,
  "anchors": {
    "spawn_points": {"overworld": [-109, 64, -420]},
    "bed_spawn": null,
    "home_bases": {},
    "portals": {},
    "last_death": {"dim": "overworld", "pos": [1995, 150, 1995]}
  },
  "containers": [],
  "pois": [
    {"name": "water", "pos": [1717, 62, -694], "dim": "overworld"},
    {"name": "pumpkin", "pos": [2166, 66, -887], "dim": "overworld"},
    {"name": "lava", "pos": [2239, 62, -917], "dim": "overworld"}
  ],
  "biomes": [],
  "habitats": [
    {"dim": "overworld", "mob": "cow", "clusters": [{"pos": [..]}]},
    {"dim": "overworld", "mob": "sheep", "clusters": [{"pos": [..]}]}
  ],
  "visited_chunks": [["overworld", 45, -44], ["overworld", 46, -44]]
}
```

注意：实际 JSON 的 `pois` 为 40 条、`visited_chunks` 为 84 条；这里只展示结构。`habitats` 在本次 90 分钟跑结束后才补上持久化，下一次运行会正常写入。
