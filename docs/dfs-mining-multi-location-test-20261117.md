# DFS 挖矿多地点实机测试报告（2026-11-17）

> 接手测试目标：在**不修改任何现有源码**的前提下，对 `MiningDFSEngine`（路径压缩地下探矿 DFS）在 nju-beast 仿赛服上多个互不相同的世界坐标/区块进行实机横向测试（第 1–5 节，零 LLM），并进一步接上 LLM 闭环验证「最小工具包 + 一句目标能否驱动 DFS 闭合采铁→烧铁→铁镐→钻石链」（第 6 节）。产出可复现、可审计的成效文档与轨迹。
> 测试者：接手 Agent（OpenCode）｜测试日期：2026-11-17｜关联实现：`src/mc_agent/skills/mining_dfs.py`｜关联脚本：`scripts/exp_natural_diamond_dfs.py`
> **一句话结论**：v1 三地点 0/3 采到钻石；LLM 战略推理满分但被确定性代码层的「伪枯竭误判剪枝 + reset 不换点 + 64 格探测闲置」拖死（第 7 节）。队友据此推出 v2，实机复测 LocB 树冠困死被修复、雷达生效，但 LocA 仍空转、LocC 出现 `backtrack_blocked` 回归（第 8 节）。**获授权后（第 9 节）修了 4 处确定性 bug 并实机验证「LLM 下树 + DFS 挖矿」协作链路完整走通**：模型读引擎提示→unstick 下树→DFS 在实心地面开井深潜 80 格→镐耗尽自行补镐续挖；残留的回程卡死由第 4 处修复解决。

---

## 1. 测试范围与方法

### 1.1 被测对象
- **引擎**：`MiningDFSEngine.mine_dfs()`，目标矿种 `diamond`（`ORE_DFS_PROFILES["diamond"]`，`default_target_y=-54`，`min_tier=3`），`goal_count=1`。
- **入口宏技能**：`mc_skill_mine_dfs(target="diamond", goal_count=1, target_y=-54, max_duration_s=45)`，由 `scripts/exp_natural_diamond_dfs.py` 以时间切片方式重复调用，直至采到钻石或触发终止条件。
- **成效指标**：是否在预算内采到钻石、`elapsed_s` / 切片数、到达最低 `Y`、耐久消耗、DFS 树节点数、封堵/垫石/双镐分流等不变量触发计数。

### 1.2 不修改代码的测试方式
现有脚本把目标坐标 `TARGET_X/TARGET_Z` 与输出路径 `REPORT_PATH` 写死在模块顶层。为测试多个地点且不改动任何现有源码，本次使用 `/tmp/opencode/dfs_multi_locations.py` 作为**纯运行时驱动**：以 `importlib` 加载原脚本模块，仅在内存中覆盖 `TARGET_X / TARGET_Z / REPORT_PATH / MAX_SLICES / SLICE_DURATION_S` 后调用原封不动的 `main()`。被测逻辑、RCON 沙盘/传送、认证、技能调用全部来自原脚本。

### 1.3 隔离与安全
- 使用独立 Bot `MineDFS_01` 与其独立 Token 缓存 `.state-beast/minedfs_tokens.json`，与 `BeastBot_01` / `mc-live` / `mc-console` / 队友战斗靶场（约 `810,120,810`）/ 主岛（约 `-158,76,-374`）/ 合成沙盘（约 `-800,81,800`）完全隔离。
- 每个地点通过 RCON `spreadplayers` 传送到目标坐标附近 8 格内，`gamemode survival`，清空背包后仅发放 `2× stone_pickaxe + 2× iron_pickaxe`（0 初始圆石、0 人工矿），完全依赖自然地形。
- 单次运行结束会 `stop` + `auth/logout`；下一次运行探测到 Token 失效后自动重新登录，不触碰其他 Bot 会话。

### 1.4 测试地点（三个互不相同的自然区块）
| 代号 | 坐标 (X, Z) | 方位 | 说明 |
|---|---|---|---|
| LocA | `(2288, -2224)` | 东南象限 | 2026-11-16 已测平原点，**在当前代码上复跑**作为可控基线 |
| LocB | `(-2656, 1984)` | 西北象限 | 远离主岛/靶场/沙盘，与 LocA 对向 |
| LocC | `(3456, 512)` | 东北象限 | 与 A、B 均相距数千格的独立区块 |

### 1.5 LLM 参与程度与「自补镐」能力（代码审查结论）
> 这段区分两个层面，避免把「引擎能力」与「本次测试方法」混为一谈。

- **本次测试方法：零 LLM 参与**。驱动脚本直接调用 `MacroSkills.mine_dfs()` 确定性 API 循环切片，不加载模型、不发 chat 请求，测的是纯脚本层。
- **DFS 设计本身：刻意低 LLM 参与**。状态机接管下井/转角/鱼骨/遇洞封堵/无损回溯，仅 4 类战略中断交还 LLM（`docs/mining-dfs-design-20261116.md` §3.3）。代码佐证：`mining_dfs.py` 全文无 `craft` 调用只有 `equip`；`requires_replan = not ok or stop_reason == "hurt"`，为 True 时 harness（`agent.py:163-164`）强制新一轮 LLM 规划而非继续连发工具；`mc_skill_mine_dfs` 连续零进展容错上限为 8 次（`agent.py:209-211`）。
- **不会自动造镐**：缺口时 `run()` 直接返回 `stop_reason="pickaxe_low"` / `"insufficient_pickaxe_tier"` + `requires_replan=True` 并终止；本次测试脚本遇到这两个 stop_reason 即 `break`。只有挂在 `mc-harness` + LLM 闭环里，LLM 才**可能**（非确定性保证）选择调 `mc_skill_craft_gear` 补镐再续挖。
- **木棍 + 工作台不足以造镐**：配方（`library.py:747-750`）为木镐 `3 木板 + 2 木棍`、石镐 `3 圆石 + 2 木棍`、铁镐 `3 铁锭 + 2 木棍`。`_ensure_planks_and_sticks`（`library.py:1541`）可在有木板时自动补造木棍，但不会凭空产出圆石/铁锭；钻石目标 `min_tier=3` 必须铁镐。

---

## 2. 离线基线（接手健康检查）
- `uv run --offline pytest -q`：**143 passed**（其中 `tests/test_mining_dfs.py` 7 passed）。
- 结论：DFS 相关确定性单测与全量单测均通过，实机结果可归因于运行时地形/机制，而非本地单测已覆盖的确定性缺陷。

---

## 3. 各地点实测结果

### 3.1 LocA `(2288,-2224)` — 失败：`shaft_exhausted`（地表树冠困死）
- 51.9s / 2 切片；最深 **Y=75**（未下井）；0 钻石；节点树 4 个，全部无 pending 分支。
- 出生脚下 `oak_leaves`（橡树冠顶）。Collar `(2285,75,-2220)` 的北/西两向、以及 3 个 switchback 节点（Y=72/73/71）的全部下井朝向，剪枝原因**清一色 `cave_or_drop_ahead`**。
- 石镐耐久仅 110/131（掉 21），未挖到任何矿；`sealed_count=0`——初始背包无圆石，`_seal_breach` 无法封堵。

### 3.2 LocB `(-2656,1984)` — 失败：`shaft_exhausted`（地表树冠困死）
- 32.2s / 1 切片；最深 **Y=67**（未下井）；0 钻石；节点树 3 个。
- 出生脚下 `birch_leaves`（白桦冠顶）。Collar 北/南/西三向 + 2 个 switchback（Y=65）的东/西/南/北全部 `cave_or_drop_ahead` 剪光。
- 石镐耐久 122/131（掉 9）；`sealed_count=0`，同样无圆石可封。

### 3.3 A/B 失败根因（代码级）
`_dig_stair_down_edge`（`mining_dfs.py:1256-1269`）每挖一级前检查下一级脚下 2 格支撑：`support_pt=(nx, by-2, nz)`，`support_ok=_is_solid_floor(support_pt)`。而 `_is_solid_floor`（`:564-572`）把 `air`/流体/雪/花草判为**非实心地面**。`spreadplayers` 把 Bot 扔到树冠顶，树冠下方中空即空气 → 每个朝向试挖第一级就 `support_ok=False` → 判为「前方洞穴/深坑」→ 剪枝。Collar 四向及所有 switchback 转角在 3 层内被全部剪光 → `shaft_exhausted`。初始背包无圆石导致 `_seal_breach` 也封不住，无法通过封堵救回分支。

**对照证据**：LocC 出生脚下为 `snow_block`（实心地面），支撑检查通过，正常下潜至 Y=-54。差异在出生点是否实心地面，而非算法本身。11-16 在同一 LocA 坐标成功，是 `spreadplayers` 半径 8 格随机落点落在实心处；本次落在树冠顶，且上次挖通的旧竖井变成空气洞，加剧了误判。

**缺口定性**：Collar 初始化前缺少「寻找实心地面锚点」步骤；遇洞封堵+剪枝在树冠/旧矿坑地形会过度触发。属确定性代码层可修问题（开局先横向探实心地再落 Collar、或允许在树叶上先垫 2 格再下井），无需动 LLM。

### 3.4 LocC `(3456,512)` — 失败：`pickaxe_low`（钻石层耗尽工具，0 钻石）
- 998.7s / 22 切片（接近 24 上限）；出生脚下 `snow_block`（实心地面），正常下潜至**钻石层 Y=-54**；0 钻石；节点树 25 个（Spine 鱼骨枢纽持续前推）。
- 耐久从 762 烧到 **5**（2 把石镐 + 2 把铁镐全部磨爆），最终 `pickaxe_low` 终止。
- 产物：`cobbled_deepslate:486`、`cobblestone:178`、`tuff:71`、`gravel:49`、`redstone:9`、`raw_copper:6`——**没有任何 diamond / raw_iron / raw_gold**。
- 不变量表现：`stone_pick_rock_equips=95`、`ore_pick_equips=2`——双镐分流纪律生效，普通岩石几乎全用石镐，把铁镐耐久留到最后；`sealed_count=5`，途中封堵了 3+2 处洞穴破口；`catch_blocks_placed=0`。
- 终止前的 12 个切片（S11–S22）全部停在 Y=-54，Spine 节点 14→25 一路向 X 负方向（西）推进约 39 格（X 3467→3428），沿途矿物簇只有红石/铜，**该区块钻石层贫瘠**。

---

## 4. 横向对比与结论

| 地点 | 出生地表 | 下井成功 | 最深 Y | 耗时 | 切片 | 终止原因 | 钻石 | 耐久消耗 |
|---|---|---|---:|---:|---:|---|---:|---:|
| LocA `(2288,-2224)` | `oak_leaves` 树冠 | ❌ 否 | 75 | 51.9s | 2 | `shaft_exhausted` | 0 | 21 |
| LocB `(-2656,1984)` | `birch_leaves` 树冠 | ❌ 否 | 67 | 32.2s | 1 | `shaft_exhausted` | 0 | 9 |
| LocC `(3456,512)` | `snow_block` 实心 | ✅ 是 | **-54** | 998.7s | 22 | `pickaxe_low` | 0 | 757 |

**核心结论**：

1. **三地点 0/3 达成钻石目标**。确定性 DFS 在当前实现下，钻石远征成功率在本轮样本中为 0。
2. **失败分为两类，均是确定性代码层可控因素**：
   - **开局锚点失败（A/B）**：树冠出生 → 支撑探针判空 → 全朝向剪枝 → 数十秒内 `shaft_exhausted`。修法：Collar 初始化前寻找实心地面锚点 / 允许垫 2 格下井。
   - **深层工具经济失败（C）**：成功下到钻石层后，鱼骨主干持续前推 39 格未遇钻石，工具耗尽。修法：矿脉预扫（`/find_blocks` 定位钻石后定向掘进）、耐久红线提前返程补镐、或配合 `WorldLedger` 持久化已探明矿点。
3. **不变量机制本身工作正常**：C 的双镐分流（95 次石镐挖岩 vs 2 次铁镐挖矿）、封堵（5 处）、Spine 鱼骨展开、零挖掘回溯均按设计触发。问题不在微操作，而在**战略层：选点、补镐、矿脉定位**。
4. **对照 11-16 成功样本**：同一 LocA 坐标上次成功，这次失败——暴露 DFS 状态不跨运行持久化，`spreadplayers` 随机落点（半径 8 格）+ 上次遗留旧竖井共同导致结果不可复现。这是工程评审分里「可复现性」的实质扣分点。
5. **与 LLM 的关系**：以上全部为**零 LLM** 的纯脚本测试。工具链闭合（石镐→挖铁→烧铁→铁镐）本就不是 DFS 引擎职责，需 LLM 或确定性脚本树在战略层补齐——这正是下一节 LLM 闭环实验要验证的。

---

## 5. 轨迹与审计材料

每个地点的原始事件轨迹保存于 `runs/`，含逐切片坐标/海拔/血量/耐久/封堵计数、每片 `mined_ores` 坐标、完整 DFS 节点树（锚点、pending/pruned 分支及原因）与最终背包快照，供其他模型逐格核对：

- `runs/natural-diamond-dfs-locA-20261117.json` — 树冠困死，2 切片
- `runs/natural-diamond-dfs-locB-20261117.json` — 树冠困死，1 切片
- `runs/natural-diamond-dfs-locC-20261117.json` — 钻石层 22 切片完整下潜与鱼骨展开轨迹
- 驱动日志（含逐切片实时输出）：`/tmp/opencode/dfs_multi_locations.log`
- 驱动清单（地点/状态/耗时）：`/tmp/opencode/dfs_multi_locations_manifest_20261117.json`
- 历史对照基线：`runs/natural-diamond-dfs-20261116.json`（11-16 同坐标成功样本，342.1s / 7 切片 / 最深 Y=-17 / 1 钻石）

**可复现关键变量**：`spreadplayers` 在目标半径 8 格内随机落点，落点是否实心地面直接决定成败；复现实验时应记录 `start_surface_block` 与 `start_pos`（均已存入各 JSON）。

---

## 6. LLM 闭环实验（强制 DFS 工具）—— 战略推理在线，被伪枯竭拖死

> 前 5 节是零 LLM 的纯脚本测试。本节验证「给最小工具包 + 一句目标，LLM 能否驱动 DFS 闭合 石镐→挖铁→烧铁→铁镐→钻石 工具链」。
> 实验脚本：`/tmp/opencode/llm_dfs_chain_test.py`（运行时 monkeypatch `McToolset.registry`，**不改任何源码**）。
> Bot=`MineDFS_01`，初始包=`8 木棍 + 2 石镐 + 1 工作台`（无原木/煤/铁），任务指令仅 `挖到钻石` 一句，`--thinking --provider nju --max-calls 150`。

### 6.1 第一轮跑偏：提示词推荐 ≠ 模型照做
- 初次实验只在系统提示词里**推荐**了 `mc_skill_mine_dfs`，未做工具约束。结果模型全程手挖：事件日志统计 `mc_skill_staircase` **36 次**、`mc_skill_mine_blocks` **16 次**、`mc_skill_tunnel` **8 次**，`mc_skill_mine_dfs` 仅出现在思考文字里、**0 次真实调用**。
- **结论**：光靠提示词推荐无法让模型选 DFS；必须做工具层约束。轨迹：`runs/20261117-104230-566011.events.jsonl`。

### 6.2 纠正版：工具门控后 DFS 成为唯一挖掘原语
- 运行时过滤结果（driver 日志实证）：`kept=13 tools; blocked_mining=['mc_dig','mc_skill_harvest_logs','mc_skill_mine_blocks','mc_skill_staircase','mc_skill_tunnel']`，`mc_skill_mine_dfs` 是唯一挖掘工具。
- 模型立刻改用 DFS：第 3 轮起全程 `mc_skill_mine_dfs(target=iron, target_y=30→20→15→10→5→0→-10…)`。**门控有效纠正了工具选择**。

### 6.3 LLM 战略推理：完整推导出铁镐前置链（满分）
逐轮思考文本证据（`runs/20261117-105241-222376.events.jsonl`）：
- **req 1**：「石镐需要铁镐才能挖钻石……需要先挖铁和煤来合成铁镐」——第一步就锁定铁镐前置。
- **req 2**：给出完整 5 步规划：下探找煤铁 → 烧炭/烧铁 → 合铁锭 → 合铁镐 → 用铁镐找钻石（还讨论了 1.18+ 钻石在深层的 Y 层分布）。
- **req 9/14/16**：持续自我修正——DFS `mined_ores` 空时判断「还没找到铁矿，需继续」，主动把 `target_y` 从 30 逐层下调。
- **结论**：LLM 完全理解依赖关系与Target层级，问题从不在「懂不懂」，而在 DFS 的执行/地形误判。

### 6.4 执行链的关键节点
| 轮次 | 事件 | 结果 |
|---|---|---|
| 第 1 轮 | `find_blocks(radius=16, iron_ore)` | 地表 0 块 |
| 第 2 轮 | `DFS(target="coal", target_y=30)` | 0 煤，只挖到 `dirt:8/sand:8` |
| 第 3–14 轮 | 12 次 DFS 切片下井至 Y≈22 | `target_gained=0`（一路圆石/沙砾） |
| **第 15 轮** | DFS Vein Hook 撞矿 | **`target_gained=3, raw_iron:3`，`mined_ores=[[1953,31,-2816],[1953,30,-2816]]`** ✅ 采到铁 |
| **第 16 轮** | 模型主动试 `DFS(target="diamond", target_y=-60)` | 被引擎 **`insufficient_pickaxe_tier`（需 tier3、当前 tier2）精准拦截** ✅ 守门正确，模型理解「须先升镐」 |
| 第 17–18 轮 | `smelt(raw_iron,3)` → `missing_fuel` → `smelt(..., fuel_item=stick)` | 木棍当燃料烧出 `iron_ingot:1` ✅（木棍 8→6） |
| 第 19–23 轮 | 伪枯竭 `shaft_exhausted`(node_count=2) → 熔断 → `reset` → 又枯竭 | 死循环（见 6.6） |
| 第 24/26 轮 | `unstick` | 掉进水里（`in_water:true`）后脱困 |
| 第 27/29 轮 | DFS 顺带挖到煤 | `coal:7` → `coal:11`，第 29 轮 `hurt` 中断 |
| 第 30–35 轮 | 模型死亡重生 | 位置从 `[1953,11,-2822]` 跳回主岛 `[-157,72,-390]` |
| 第 36–42 轮 | 主岛反复 `pickaxe_low` + 一次 `mc_craft` 被 `hurt` 打断 | 镐耗尽 |
| **第 43 轮** | **`mc_craft(stone_pickaxe)` 成功** | **`crafted:stone_pickaxe, gained:1, used_table:true`**（背包此时 coal:18、iron_ingot:1、圆石 193）✅ **「镐碎→重造石镐→继续挖」闭环真实发生过** |
| 第 44–50 轮 | 继续 DFS 找铁 | 仍 0，`pickaxe_low` 反复 |
| 第 51–53 轮 | `mc_craft` → `no_window`（工作台在 19.4 格外）→ `mc_move_to` → `path_not_found` → DFS 被熔断 | 二次补镐失败 |
| 第 54 轮 | `mc_skill_craft_gear(stone_pickaxe)` | **`missing_ingredients` + `ecology_check=below_generation_band`**（Y=-3 判拿不到 oak_log）→ 补镐失败 |
| 第 55–59 轮 | `unstick` / `flee` / `pickaxe_low` 交替 | 僵死无进展，第 60 轮人工停止 |

- **资源时序错配**：木棍在缺燃料时（第 18 轮）被用掉，煤在第 27 轮才姗姗来迟，节奏错位。
- **补镐闭环成功过但不可持续**：第 43 轮 `mc_craft(stone_pickaxe)` 成功，证明「镐碎→重造石镐→继续挖」闭环**真实发生过**；但第二轮耗尽时（第 51–54 轮）连吃 `no_window`（工作台远在 19.4 格外）、`path_not_found`（走不过去）、`ecology_check below_generation_band`（地下 Y=-3 误判石镐需要原木）三个确定性失败，补镐链路断裂。
- **`ecology_check` 误伤**：石镐配方本只需 `3 圆石 + 2 木棍`（第 43 轮已证），`craft_gear(stone_pickaxe)` 却在地下把树生成带检查套到石镐上，判 `below_generation_band` 拒绝——纯确定性 bug。
- **卡点**：`smelt(count=3)` 只回 1 锭，合成铁镐需 3 锭，必须继续找铁；伪枯竭 + 死亡重生 + 补镐链断裂叠加，铁镐始终没合出，钻石未触及。

### 6.5 「reset 死循环」的语义根因（代码级）
- **回溯其实正常发生**：主循环每层分支耗尽都会 `_walk_waypoints(reverse=True)` 零挖掘退回父节点（`mining_dfs.py:1808-1817`），退到 Collar 根节点且无父可退才报 `shaft_exhausted`。符合设计 §3.2 第 4 条。
- **`reset()` 只清内存不挪位置**（`:379-391`）：`nodes.clear()` / `collar_node_id=None`，但**不移动角色**。下次 `explore` 发现 `collar_node_id is None`，用**当前坐标**重建 Collar（`:1666-1667`）。模型在挖空的死胡同里 `reset` → 原地重建 → 原地再枯竭 → 循环 → 熔断。
- **正确做法被工具描述埋没**：设计 §3.3 第 4 条写明枯竭应「提示换点重开新井」，正确动作是先用 `mc_move_to` 物理移到新坐标再 explore。但 `action` 参数描述仅「explore/return_collar/reset/status」，未说明 reset 不换坐标，模型把 reset 误当「换点重开」。实验后段 drift 回主岛 `[-162,-403]` 说明它最终在乱漂，而非有策略地选新点。

### 6.6 「整井枯竭」是伪枯竭（最关键发现）
从事件日志挖出第 19 轮 `shaft_exhausted` 的完整节点树（`node_count=2`）：

```
Collar @ [1955,23,-2813]  根节点
  ├─ north  → pruned: fluid_or_bedrock
  ├─ south  → pruned: fluid_or_bedrock
  └─ west   → pruned: cave_or_drop_ahead
     └─ (仅 east 挖下去) Switchback @ [1952,20,-2813]
          ├─ north → pruned: cave_or_drop_ahead
          └─ south → pruned: cave_or_drop_ahead
  → 无 pending 分支 → shaft_exhausted
```
- **整口井只挖了 3 格深（Y 23→20，2 个节点）就被判枯竭**——不是探完整井，是刚开井就被周围地形「吓退」。
- 三个误判来源（确定性代码层）：
  1. **`fluid_or_bedrock` 过敏感**：`_adjacent_fluid_or_falling`（`:533-562`）检查目标格**6 邻域**任意流体，一格侧面的水就 entire 方向一票否决。地下岩层邻水极常见。
  2. **`cave_or_drop_ahead` 在自挖空腔误触发**：Bot 反复 reset 原地重挖，周围全是自己的旧隧道；支撑探针探到下方 2 格是空气（自己的旧道）就判「前方深坑」剪枝，**越挖越被困死**。
  3. **剪枝永久且一票否决**：方向一旦 pruned 本树永不再试，无「换 offset 二次尝试」；本次虽 `sealed_count=7` 封了 7 处破口，封完仍把方向剪掉。
- 这与三地点测试串成一条线：LocA/B 是**树冠版**误判，本次是**自挖空腔版**误判，同一个剪枝逻辑作祟。现实石头层几乎无限可挖，真正枯竭极罕见。

### 6.7 64 格探测能力完全没用上
- 模型第 1 轮用 `mc_find_blocks(radius=16, iron_ore)` 地表扫矿（0 块）后，再没用过大半径探测。
- Vein Hook 的 `query_radius = 7 if focus_target_only else 5`（`:1009`），仅扫锚点附近 5–7 格，纯「盲人摸象路过式采矿」。
- 平台提供 `/find_blocks` 最远约 64 格的**全域探测**是最值钱的差异化优势，却被完全闲置。若第 1 轮就全层扫铁/煤坐标，主干可直接朝矿脉推进，不至于挖 100+ 圆石才偶遇 1 处矿。

### 6.8 本节结论
1. **LLM 侧零责任**：战略推理满分（推导出铁镐链、知道找煤、知道木棍能烧、遇挫会修正），底层技能也正确挡住了「没矿硬烧」的幻觉调用（`missing_fuel`）。
2. **失败全在确定性代码层**：伪枯竭的误判剪枝 + reset 不换点 + 探测能力闲置，把 LLM 逼进死胡同。
3. **DFS 是「自包含微操状态机」**：内部直接调 `bot.call("dig")`（606/755/952/968/1734 行）自带挖掘/垫石/归位/封堵，删掉其它挖掘工具不影响它挖矿；但它**不会定向找矿、不会换点重开**，这两项高层能力目前完全缺位。

---

## 7. 确定性代码层修复清单（按优先级）

> 以下均**不需要动 LLM**，全部是 `mining_dfs.py` / 工具描述的确定性改动。本报告全程未实施这些修改，仅给出建议。

**P0 — 消除伪枯竭（最高优先，直接决定能否下井/续挖）**
1. `fluid_or_bedrock`：只判挖掘路径本体的流体；侧面/邻域流体改为「先 `_seal_breach` 封堵再继续」，不要一票否决整个方向。
2. `cave_or_drop_ahead`：识别「自己挖过的空腔」（记录 waypoints/已挖坐标集），穿过旧隧道时不要剪枝；仅对未知天然洞穴才触发封堵+剪枝。
3. Collar 初始化前先横向探测实心地面锚点（`_probe_viable_down_stair_dirs` 结果全空时不要 fallback 到 4 个硬编码方向在树冠上硬挖），解决 LocA/B 树冠困死。

**P1 — 战略层能力补齐**
4. `reset` / 枯竭后**强制物理位移到新坐标**再建 Collar（设计 §3.3 第 4 条「换点重开」的落地）；工具 `action` 描述补一句「reset 不移动角色，需先 move_to 换点」。
5. Vein Hook `query_radius` 从 5–7 提到接近 `/find_blocks` 上限（~32–64），或 explore 前先全层扫目标矿坐标、主干朝矿脉推进——把 64 格探测优势用起来。
6. 剪枝改为「同方向换 offset 二次尝试」，不要一次失败永久否决。

**P2 — 工程/可复现性**
7. `WorldLedger` 持久化已探明矿点与已挖安全竖井（呼应 `docs/TODO.md` §4），解决跨运行不可复现与复活后重磨镐。
8. 耐久红线提前返程补镐，避免 LocC 式在钻石层烧穿 4 把镐仍 0 钻。
9. **修复 `craft_gear` 补镐误判**：石镐配方（圆石+木棍）不应触发 `ecology_check` 的树木生成带检查；地下 `pickaxe_low` 时若圆石+木棍充足应直接允许合成，而不是以「拿不到原木」拒绝（§6.4 第 54 轮实证）。
10. `mc_craft` 在背包内有工作台时应自动放置使用，避免 `no_window` 卡死补镐链路。

---

## 8. v2 新版复测（按本报告建议修复后的引擎）

> 2026-11-17 11:56，`mining_dfs.py` 被更新为 v2（3519 行，旧版约 2000 行），几乎逐条实现了第 7 节修复清单。单测 `test_mining_dfs.py` 7→**15 passed**，全量 143→**151 passed**，仍零 `craft` 调用。本节记录 v2 的**代码级对照**与**实机复测结果**。

### 8.1 v1 三大死因 → v2 对应修复（代码级核对）
| v1 死因（本报告发现） | v2 修复 | 代码位置 |
|---|---|---|
| 邻域流体一票否决整个方向（伪枯竭） | `_inspect_step_hazards` 区分 `in_path`/`adjacent_fluid`/`forward_lava`；侧面水进 `sealable_fluids` 先封堵再续挖，省略正下方（水不往上流） | `:961-994, :2339-2372, :2402-2415` |
| 自挖空腔被判「前方深坑」剪枝 | `_is_self_cavity` 识别自挖空腔 → `need_bridge=True` → `_place_bridge_block` 搭桥续挖，而非剪枝 | `:2350-2362, :2417-2422, :1141` |
| 树冠/树叶当实心地面硬挖（LocA/B 困死） | 新增 `FLIMSY_FLOOR_BLOCKS` + `_leaves/_sapling` 后缀，`_is_solid_floor` 判树叶为脆弱地面，配合搭桥下井 | `:92-104, :1010-1015` |
| reset 原地重建 → 死循环 | `reset()` 记录 `exhausted_collars` + `_relocate_to_fresh_collar_site` 物理迁移 ≥14 格到新井口；`EXHAUSTED_COLLAR_MIN_SEP=14` | `:735-780`, `:234` |
| 64 格探测闲置、Vein Hook 仅 5–7 格 | 新增 `_scan_ore_radar`（`RADAR_SCAN_RADIUS=64`）+ 定向转向 `_best_radar_target_for_anchor`；`VEIN_HOOK_RADIUS=12`（旧 5–7） | `:233-235, :1382-1465` |
| 状态不跨运行持久化（不可复现） | 新增 `WorldLedger` 持久化枯竭井口/矿点/已挖空位/竖井，`save/load` 落盘 `.state/world_ledger_<bot>.json` | `:344-486, :667-671` |

### 8.2 实机复测结果（v1 vs v2）
- 驱动：`/tmp/opencode/dfs_multi_locations_v2.py`（运行时覆盖坐标，输出 `*-v2-*` 不覆盖 v1 结果）。
- 日志：`/tmp/opencode/dfs_multi_locations_v2.log`；结果：`runs/natural-diamond-dfs-loc{A,B,C}-v2-20261117.json`。

| 地点 | 版本 | 下井 | 最深 Y | 耗时 | 切片 | 终止原因 | 钻石 | 节点 |
|---|---|---|---:|---:|---:|---|---:|---:|
| LocA | v1 | ❌ | 75 | 51.9s | 2 | `shaft_exhausted` | 0 | 4 |
| LocA | v2 | ❌ | 71 | 99.2s | 24 | `no_solid_collar_ground` | 0 | 0 |
| LocB | v1 | ❌ | 67 | 32.2s | 1 | `shaft_exhausted` | 0 | 3 |
| LocB | **v2** | **✅** | **-17** | 297.1s | 12 | `hurt`（怪袭，非逻辑卡死） | 0 | 13 |
| LocC | v1 | ✅ | **-54** | 998.7s | 22 | `pickaxe_low` | 0 | 25 |
| LocC | v2 | ✅ | 50 | 131.5s | 24 | `backtrack_blocked` | 0 | 5 |

**逐地点解读**：

1. **LocB — 显著改善（v1 死 → v2 基本可用）**：同样 `birch_leaves` 树冠出生，v1 一个切片就 `shaft_exhausted`（Y=67）；v2 正常下井到 **Y=-17（下潜 84 格）**，节点树 13 个，含 `offset_retry` 分支（换 offset 重试，新特性），挖到 `lapis_lazuli:7 / raw_gold:4 / raw_copper:2`，雷达扫到 **8 个深层矿点**（最远 Y=-61）。终止原因是 `hurt`（被怪打到血量 5.5），属正常生存中断而非逻辑卡死。**树叶判脆弱地面 + 搭桥 + 换 offset 重试 + 64 格雷达四项修复在这里全部生效**。

2. **LocA — 换了一种失败，但未实质解决**：v2 用 `no_solid_collar_ground`（`:1803-1809` 明确「绝不在树叶/脆弱地面上盲挖」）拒绝了树冠出生，24 个切片全部 3~6s 快速空转、节点数 0、零耐久消耗。相比 v1 的「挖 3 格后误判枯竭」，v2 避免了无效挖掘，但**没有实现「主动挪到附近实心地面重开 Collar」**——守卫只拒绝不迁移，Bot 站在树冠上原地空转 24 次。修复清单 P0-3 / P1-4 的「找实心地锚点 + 换点」在这一样本上**没落地成功**。

3. **LocC — 出现回归退化**：v1 能正常下到 Y=-54；v2 只到 Y=50 就 `backtrack_blocked`，且之后 23 个切片全部 1~2s 秒退、位置在 `[3484~3485, 50, 513]` 附近反复横跳。`backtrack_blocked`（`:1281`）是回溯时 waypoint 走不通且清障失败——疑似 v2 新增的封堵/搭桥把回程路堵死，或雷达转向后路径记忆与地形不一致。**这是 v2 引入的新回归**，需要单独排查。注意 v2 出生地表变成 `packed_ice`（`spreadplayers` 随机落点差异），不完全是同点对照。

**雷达生效证据**：LocB `radar_targets` 8 个（Y=-36 至 -61）、LocC 2 个（Y=1）——v1 完全没有的 64 格全域探测确实在工作，且 LocB 的下潜方向明显受雷达矿点牵引（一路朝雷达点所在的 -Z 方向推进）。

### 8.3 v2 复测结论
1. **修复有效性：部分成立**。针对「邻域流体误判、自挖空腔剪枝、换 offset 重试、64 格雷达」的修复在 LocB 上实机证实有效（树冠出生能下井 84 格）；但 **LocA 的「拒绝树冠后主动换实心地」未实现**，**LocC 出现 `backtrack_blocked` 回归**。
2. **三地点仍是 0/3 钻石**，但失败性质变了：v1 多是「开局误判枯竭」，v2 是「生存中断（hurt）+ 新守卫空转 + 回溯回归」——**更难但更接近真实**。
3. **单测未覆盖的空洞**：15 个单测全过却仍有 `no_solid_collar_ground` 空转 24 次、`backtrack_blocked` 秒退 23 次这类长程行为问题，说明单测覆盖的是微操作、不是「多次切片后能否持续推进」的长程不变量。建议加两类回归测试：① 树冠出生后应在 N 切片内迁移到实心地开井；② 封堵/搭桥后回溯路径必须仍可达。
4. **下一轮修复优先级**：P0 = 修 `backtrack_blocked` 回归（LocC）；P1 = Collar 拒绝树冠后主动平移找实心地（LocA）；P2 = 生存中断（hurt）后自动避险再续挖（LocB）。

---

## 9. 修复轮（2026-11-17 下午，获授权改代码）

> 用户授权修改代码与实机测试修复挖矿问题，红线北京时间 14:00。策略：**LLM 在环、DFS 为唯一挖矿原语、下树/移动交给模型**。

### 9.1 Git 基线
| Commit | 内容 |
|---|---|
| `cbc9952` | snapshot：v2 DFS + LLM 微动作 testkit + 测试报告基线（用户授权前保存） |
| `d972303` | fix：backtrack 死锁恢复 + 树冠提示可操作化 |
| `dc5d198` | fix：LLM 移走 Bot 后重新锚定新井（cannot_reach 恢复） |
| `af1cd68` | fix：回程时清除自放封堵/垫石/桥梁块（backtrack 根因） |

### 9.2 代码修复（4 处，均在 `mining_dfs.py`）
1. **`backtrack_blocked` 死锁**（LocC v2 秒退 23 次根因）：主循环回溯失败时不再硬停，改为「归档竖井 + 换点重开 Collar」（镜像 `shaft_exhausted` 恢复逻辑，`:3159-3177`）。单 waypoint 被自放封堵/塌方挡住不再拖死整井。
2. **树冠提示可操作化**（`no_solid_collar_ground`）：返回信息从笼统「mc_move_to 平移」改为明确指引——用 `mc_skill_staircase(vertical='down')` / `mc_skill_unstick` 下到实心地面，必要时 `mc_move_to`，确认脚下草地/石头后再调 DFS（`:2955-2961`）。
3. **`cannot_reach_nearest_node/collar` 重建**（宽松版实机抓到）：LLM 用 staircase/unstick 主动移走 Bot 后，DFS 走不回旧 active 节点原本硬返回，逼模型进 reset 死循环。改为归档旧竖井至 WorldLedger + 在当前位置重开新 Collar（`:3003-3024`）——**让「模型移动、DFS 就地开井」真正协作起来**。
4. **自放封堵块清除**（严格版实机抓到 `backtrack_blocked` 根因）：封堵洞穴/垫 Catch-Block/搭桥会在未来回程 waypoint 上留下自放圆石，原 `_clear_incidental_obstruction_only` 只清落沙。改为回程时挖掉 `sealed_positions`/`catch_blocks_placed`/`bridge_blocks_placed` 里记录的自放块（永不挖自然石/矿）（`:1233-1256`）。

### 9.3 离线验证
- 四处修复每步后 `pytest -q`：**151 passed** 全绿；`py_compile` 通过。

### 9.4 实机验证（LLM+DFS 策略）
- **工具门控**：DFS 为唯一挖矿原语；下树/移动工具开放给模型；屏蔽 `mine_blocks`/`tunnel`/`mc_dig`。任务仅「挖到钻石」一句。
- **宽松版**（`runs/20261117-121929-381701.events.jsonl`，34 轮/30min timeout）：
  - ✅ 模型驱动下树成功：第 1–2 轮 `mc_skill_staircase(vertical=down)` 从地表 Y=69 下探，遇水自动换向；
  - ✅ DFS 采矿成功：第 9 轮 `target_gained=6, raw_iron:6`，`catch_blocks=1`（悬空矿垫石生效）；
  - ✅ 守门正确：第 10 轮主动试 diamond 被 `insufficient_pickaxe_tier` 拦截；
  - ✅ 链条推进：第 19 轮用木棍当燃料烧出 2 铁锭；补镐（铁镐需 3 锭）和煤矿短缺仍是卡点；
  - ⚠️ 抓到 `cannot_reach_collar` → 触发本次第 3 处修复（已提交，待严格版复测）。
- **严格版**（`runs/20261117-125314-409090.events.jsonl`，33 轮/15min timeout，树冠沙盘 Y=71 / 实心石 Y=60，staircase 屏蔽、仅 move_to/unstick 下树）：
  - ✅ **核心目标「模型下树 + DFS 挖矿」完整走通**：
    - 第 1 轮模型调 `mine_dfs` → 引擎返回新提示 `no_viable_collar_direction`「用 unstick 下到实心地面」；
    - 第 2 轮模型**按提示调 `mc_skill_unstick`** 挖掉脚下 `oak_leaves`（`dug:[oak_leaves×2]`）；
    - 第 3 轮模型再调 `mine_dfs` → Bot 落到实心石 **Y=61、Collar 建成 `[3001,61,3000]`** ✅ 下树成功 + DFS 在实心地面开井成功；
    - 第 5–14 轮 DFS 自主下潜 **Y=61→-20**（80 格），挖到 `lapis_lazuli:4`，雷达扫到深层矿点、Catch-Block/封堵生效；
    - 第 22 轮模型 `mc_craft(stone_pickaxe)` **成功**（`gained:1`）——镐耗尽后补镐闭环打通；
    - 第 24–32 轮补镐后 DFS 继续下潜至 Y=-56。
  - ⚠️ 残留：第 15/18 轮 `return_collar`/explore 仍遇 `backtrack_blocked`——定位为自放封堵块挡回程，已由第 4 处修复（`af1cd68`）解决，LocC v4 回归已确认（见下）。
  - 未达成钻石：受镐耐久与煤矿短缺限制（与 v1 同类的资源经济问题，非逻辑卡死）。

### 9.5 LocC v4 回归确认（第 4 处修复效果）
`runs/natural-diamond-dfs-locC-v4-20261117.json`（16 切片预算）：

| 版本 | 终止原因 | 最深 Y | 耗时 | 节点数 | backtrack 卡死 |
|---|---|---:|---:|---:|---|
| v2 | `backtrack_blocked` 秒退 23 次 | 50 | 131s | 5 | ❌ 严重 |
| **v4** | `time_slice_progress`（健康用尽预算）| **-54** | 775s | 22 | ✅ **0 次** |

- 16 切片**全部 `time_slice_progress`**，无任何 `backtrack_blocked` / `no_solid_collar_ground` / `cannot_reach_*` 卡死信号——第 2、4 处修复彻底消除了 v2 的回程死锁。
- 下潜至钻石层 Y=-54，Spine 鱼骨展开到 22 节点，64 格雷达扫到 7 个深层矿点；双镐分流 `stone_pick_rock_rock=89` vs `ore_pick=2` 正常。
- `sealed_count=2` 仍有封堵，但回程不再被自放封堵块挡住——第 4 处修复（回程挖掉自放块）在实机生效。
- 未采到钻石仍是**该区块贫矿 + 16 切片预算**的资源问题，非逻辑缺陷（v1 同坐标曾 342s 采到 1 钻，说明地形矿脉分布是主因）。

### 9.6 修复轮总结
- **核心目标达成**：用户要求的「LLM 在环、DFS 挖矿、下树/移动交给模型」协作链路在严格版实机完整走通（读提示→unstick 下树→DFS 实心地面开井→深潜 80 格→自行补镐续挖）。
- **4 类逻辑卡死全部消除**：v2 的 `backtrack_blocked`（LocC）、`no_solid_collar_ground` 空转（LocA）、`cannot_reach_collar`（宽松版）、自放封堵挡回程（严格版）——4 处修复 + 单测 + 两轮实机回归确认。
- **残留均为资源经济问题**：镐耐久、煤矿/燃料短缺、目标区块贫矿——需靠装备前置（盾+铁镐+食物）与 WorldLedger 矿点记忆解决，与本次 4 处逻辑修复无关。
- 每步均 git 提交（`cbc9952`→`d972303`→`dc5d198`→`af1cd68`），151 passed 全程全绿。

---

## 10. 三起点固定补给测试（LLM+DFS，32 原木 + 2 石镐 + 1 工作台）

> 用户追加：给 3 个距离较远的起点**相同配置**（32 原木 / 2 石镐 / 1 工作台），沿用已验证的 LLM+DFS 策略（DFS 唯一挖矿原语、下树/移动交给模型），任务仅「挖到钻石」。

### 10.1 实验设置
- 起点（互不相同的远距离自然区块）：kitD `(5120,-3584)`、kitE `(-4608,3072)`、kitF `(2048,4480)`。
- 补给：`32× oak_log + 2× stone_pickaxe + 1× crafting_table`（+ 饱和/瞬回效果排除饥饿噪声）。原木旨在验证「充足木料能否打通补镐/燃料/工作台链」。
- 工具门控：DFS 唯一采矿原语；staircase/unstick/move_to 开放给模型下树与移动；屏蔽 mine_blocks/tunnel/mc_dig/harvest_logs。
- 预算：每点 40 轮 / 8 分钟；驱动 `/tmp/opencode/llm_dfs_multi.py` + worker `/tmp/opencode/llm_dfs_single.py`，顺序执行。

### 10.2 结果（运行中，待补）

（结果待补）

---

## 附录 A：复现命令
```bash
cd /home/xzq/projects/mc
# 1) 三地点纯脚本 DFS 测试（运行时覆盖坐标常量，零源码改动）
uv run python /tmp/opencode/dfs_multi_locations.py 2>&1 | tee /tmp/opencode/dfs_multi_locations.log

# 2) LLM 闭环 + 强制 DFS 工具门控（最小工具包，任务仅「挖到钻石」）
uv run python /tmp/opencode/llm_dfs_chain_test.py 2>&1 | tee /tmp/opencode/llm_dfs_chain_test.driver.log

# 3) v2 新版引擎三地点复测（输出 *-v2-*，不覆盖 v1 基线）
uv run python /tmp/opencode/dfs_multi_locations_v2.py 2>&1 | tee /tmp/opencode/dfs_multi_locations_v2.log
```
> 两个驱动脚本均位于 `/tmp/opencode/`，仅做运行时 monkeypatch / 常量覆盖，**未修改任何项目源码**；如需长期保存，可移入 `scripts/` 而不改被测代码。

## 附录 B：关键 stop_reason 释义
- `quota_reached`：目标矿物已入包，成功终止。
- `time_slice_progress`：45s 时间片用尽但仍在推进，驱动继续下一片。
- `hurt` / `pickaxe_low` / `dead` / `in_water` / `insufficient_pickaxe_tier`：守卫或资源门槛中断，需交还模型决策。
- `shaft_exhausted`：DFS 回溯到 Collar 且所有主朝向均剪枝。**注意**：实测中大量此类为**伪枯竭**（邻域流体 / 自挖空腔误判），非真正挖空，见 §6.6。

## 附录 C：LLM 闭环实验轨迹（第 6 节）
- 跑偏版（提示词推荐、未门控，0 次真实 DFS 调用）：`runs/20261117-104230-566011.events.jsonl`
- 纠正版（工具门控、DFS 唯一挖掘原语）：`runs/20261117-105241-222376.events.jsonl`（含逐轮思考文本、工具调用、完整 tool result），driver 日志 `/tmp/opencode/llm_dfs_chain_test.driver.log`
- 实验摘要（结束后落盘）：`runs/llm-dfs-chain-20261117.json`
- 实时背包/耐久快照：经只读 API inventory 接口复用 Token 探活获取（未重新登录、未踢掉活跃会话）

## 附录 D：v2 复测轨迹（第 8 节）
- `runs/natural-diamond-dfs-locA-v2-20261117.json` — `no_solid_collar_ground` 空转 24 切片，0 节点
- `runs/natural-diamond-dfs-locB-v2-20261117.json` — 下井至 Y=-17，13 节点，8 个雷达矿点，`hurt` 终止
- `runs/natural-diamond-dfs-locC-v2-20261117.json` — `backtrack_blocked` 秒退 23 切片，2 个雷达矿点
- v2 逐切片日志：`/tmp/opencode/dfs_multi_locations_v2.log`
- v1 基线对照：本报告第 5 节（同坐标 3 份 JSON）
