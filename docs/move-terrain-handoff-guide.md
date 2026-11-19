# `[L2-Move-Primitives]` 局部地形跨越、软陷阱自救与面包屑原子模块开发及实机验收指南

> 分级定位：**`L2` 无策略原子算子层**（只负责“给定明确输入 -> 确定性完成局部地形跨越/脱困并返回结构化回执”，**严禁在内部掺杂全局任务切换、主观门禁或课程调度优先级**；远航调度接力由 `L3` 统一把关）  
> 目标：在不建造全局三维体素寻路器（山寨 Baritone）的前提下，补齐 `POST /move_to` 面对 `1.21.11` 全部 **10 类地形死局**时的局部跨越与脱困原语，并在 `nju-beast` 通过 M1–M8 八关障碍赛场实机验证。

---

## 1. 工作区隔离与实机安全红线（必读！）

1. **严禁污染主工作区与其他在跑分支**：
   - 请在 `/home/xzq/projects/mc/.worktrees/move-terrain` 创建独立本地克隆或 `git worktree`（分支名建议 `feat/move-terrain`）。
   - **严禁运行 `uv pip install -e .`**（会改写主仓共享 `.venv` 的 `.pth` 指向！）。所有单测与实机脚本一律通过显式环境变量启动：
     ```bash
     PYTHONPATH=/home/xzq/projects/mc/.worktrees/move-terrain/src \
       /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/move-terrain/tests -v
     ```
2. **专用测试 Bot 与单 Token 互斥红线**：
   - **严禁触碰** `BeastBot_01`（直播/控制台常驻）、`MapTest_01`（L3 课程测试）、`GuiTest_01`、`FightBot_01` 及 `MineDFS_*`！
   - 请使用独立角色名（如 `MoveTest_01`，缓存至独立状态目录 `.state-move-test/tokens.json`，权限 `0600`），严禁将任何密码或 Token 打印到终端、日志或报告中。
3. **高空靶场坐标隔离**：
   - `FightBot_01` 靶场位于 `(790..830, 117..125, 790..830)`，`GuiTest_01` 位于 `(3080, 150, 3096)`；
   - 本模块 RCON 实机障碍赛场请统一建在 **`(5200..5320, 145..175, 5200..5320)`**，测试结束后 `forceload remove` 并恢复原状。

---

## 2. 核心设计边界：什么能做，什么严禁做

- **严禁手搓全局三维体素图搜索**：`mc-agent-api` 的 `POST /get_block` 一次 HTTP 请求仅查 1 格（`10~50ms`），禁止扫描几百格构建本地三维 A\* 网格。
- **混合接力架构（Hybrid Relay）**：
  - **90% 常规地形**：继续调用服务端内存级 `POST /move_to`（及现有 [`navigate_segmented`](file:///home/xzq/projects/mc/src/mc_agent/skills/library.py#L1020) `24m` 分段跳点 + `±35°` 偏角绕障）；
  - **10% 物理断点与死局**：仅在 `move_to` 返回 `path_not_found / no_path / timeout / stalled` 或状态监测命中陷阱时，按确定性判据触发下方 **5 个局部算子**，跨过断点后立即交回 `move_to`。
- **逃跑与赶路严格区分**：
  - 赶路（Navigation）允许潜行搭桥、切山挖阶梯、造船渡水；
  - 挨打紧急逃跑（Escape）**严禁潜行搭桥或悬空跳搭**——优先走平缓扇面或沿「来路面包屑」倒退，若四面绝壁死角则就地挖掩体/封口（`dig_cover_pocket` / `dig_safety_bunker`）。

---

## 3. 必须实现的五大子能力（覆盖全部 10 类边缘地形）

建议新增独立模块 `src/mc_agent/skills/movement.py`（面向对象封装如 `TerrainNavigator`），并在 `MacroSkills.navigate_segmented`、`unstick` 与 `escape_water` 中干净接线：

### 3.1 子能力一：来路面包屑轨迹栈（`BreadcrumbTrail` —— 专治三维多层溶洞与死胡同）
- **痛点**：天然溶洞套溶洞、悬崖双层聚落、下界堡垒楼上楼下等复杂三维重叠迷宫，局部射线算不出绕行楼梯，乱挖易坠入暗河或深渊。
- **机制**：
  1. 维护按维度隔离的环形路点栈（每维度最多保留 `48` 个点）：每当 Bot 处于 `on_ground=True`、非水中、脚下为实心非陷阱方块，且距栈顶路点三维距离 `>= 6.0m`（且 `<= 20.0m`）时，自动压入 `[x, y, z]`；若发生死亡/传送（瞬移 `> 48m`）则开启新段。
  2. 提供 `backtrack_breadcrumbs(steps=3, max_dist=24.0)`：当进入多层洞窟死胡同连续寻路失败，或遇险需要撤回已知安全区时，依次沿最近踩过的 100% 物理连通的栈顶路点倒序 `move_to` 回退；仅当连最近路点也因塌方/坠崖无法回退（如从高处掉进封闭深谷）且目标在地表时，才回退调用 `mc_skill_staircase(vertical="up")` 斜凿出坑。

### 3.2 子能力二：软控制陷阱破除与建筑门禁交互（`clear_traps_and_doors`）
- **痛点**：
  1. **伪实心软陷阱**：细雪 `powder_snow`（`in_water=false`，未穿皮靴下陷禁跳冻死）、甜浆果丛 `sweet_berry_bush`（减速 80% + 每 `0.5s` 扎血打断默认 `stop_on=["hurt"]`）、蜘蛛网 `cobweb`、密竹林 `bamboo`、岩浆块 `magma_block`、冻洋水下冰盖 `ice` / `packed_ice`。
  2. **村庄门与 `1.5` 格高围栏**：关闭的木门 `*_door` / 栅栏门 `*_fence_gate` 会把 `move_to` 挡在屋外；栅栏 `*_fence` 与石墙 `*_wall` 碰撞箱高 `1.5` 格，加 `jump=True` 也跳不过。
- **机制**：
  - 当移动受阻（`stalled` / 位移 `< 0.3m`）或 L0 检测到扣血/下陷时，探测玩家碰撞箱覆盖格及朝向正前方 `1` 格的脚部/头部方块：
    - 若命中 `cobweb`、`sweet_berry_bush`、`bamboo`、树叶 `*_leaves`：强制传 **`stop_on=["death"]`**（防止浆果刺伤打断自身挖掘！）直接 `/dig` 清除阻挡格；
    - 若命中 `powder_snow`：若包内有 `bucket` 优先舀起成 `powder_snow_bucket`，否则传 `stop_on=["death"]` `/dig` 破坏脚下与前方细雪块走出（并标记无 `leather_boots` 时绕避大片细雪区）；
    - 若 `submerged=True` 且头顶为 `ice`：立即向上 `/dig` 破冰换气；
    - 若正前方为关闭的木门 `*_door`（非铁门）或 `*_fence_gate`：调用 `/interact(pos=[x, y, z])` 开门后继续行进；
    - 若正前方为 `*_fence` / `*_wall` / ` dirt_path` 微高差：优先沿侧向搜寻 `<= 6m` 内的栅栏门交互通过，无门且非保护区时垫 1 块废石翻越或绕行。

### 3.3 子能力三：锁潜行直线搭桥宏（`sneak_bridge` —— 跨裂谷与下界岩浆断桥）
- **痛点**：主世界峡谷裂隙、虚空岛内裂口、下界跨岩浆海连接堡垒断桥，`move_to` 全部报 `path_not_found`；在沙漠/河岸若踩在悬空 `sand / gravel` 上放第一块砖会触发连锁重力坍塌坠亡。
- **机制**：
  1. **前置安全门禁**：
     - 检查背包可用非保护实心垫脚块（圆石/深板岩/下界岩/泥土等，严禁使用工作台/箱子/矿块/重力方块 `sand`/`gravel`）数量 `>= needed_steps + 2`；
     - **防坍塌硬校验**：检查起桥站立点脚下 `[bx, by-1, bz]`——若为 `sand / red_sand / gravel / suspicious_*` 且再下方为空，先向后退回稳固实心基岩/石土块或先垫实底层，严禁在悬空沙砾上直接起桥；
     - 校验目标点不越过 `VoidGeofence`，且周围无未遮挡远程骷髅/恶魂正在射击（防潜行慢走当靶子）。
  2. **搭桥执行循环（最多单次 `max_steps=20`）**：
     - 调用无锁 `POST /posture {"sneak": True}` **持久锁死潜行**（物理保证绝不滑出方块边缘）；
     - 沿正交主轴方向 `(dx, dz)` 逐格推进：探测 `[cx+dx, cy-1, cz+dz]` 为空气/水/岩浆且头顶两格无阻挡时，调用 `POST /place {"item": filler, "pos": [cx+dx, cy-1, cz+dz]}` 依附身下桥砖侧面贴放，确认放置成功后短步移动至新桥砖中心 `(cx+dx+0.5, cz+dz+0.5)`；
     - 探测到前方脚下已是天然连续实心陆地时，走上对岸，在 `finally` 中**务必调用 `POST /posture {}` 解除潜行**。

### 3.4 子能力四：无伤上下陡崖与横向脱流（`climb_or_descend_cliff`）
- **痛点**：面前是 `3~12` 格陡崖或高台下不去（盲跳摔死、露天 `1×1` 跳搭易被射落且下不来），或山壁瀑布 `flowing_water` 逆流冲刷导致原地超时。
- **机制**：
  - **上陡崖（目标高于当前 `3 ~ 14` 格）**：
    1. 若面前为可挖掘实心山体（非保护区、无流体）：优先复用现成 `mc_skill_staircase(direction=..., vertical="up")` 切山挖阶梯安全上行；
    2. 若处于保护区或贴外壁无厚山体：采用 **「`2×1` 交替台阶垫升（Step-Climb）」**（往面前放 1 块 -> 走上 1 格 -> 再往前上方放 1 块 -> 走上 1 格），不依赖苛刻的 `210ms` 起跳悬空放置时间窗；仅在被困 `1×1` 窄坑且头顶净空时才使用垂直 `pillar` 跳垫。
  - **下高崖（目标低于当前 `>= 4` 格，严禁直接跳崖或尝试帧级落地水 MLG！）**：
    1. 优先向山体内侧调用 `mc_skill_staircase(vertical="down")` 挖阶梯下行；
    2. 若主世界下方无岩浆且随身有 `water_bucket`，可选站在崖边先对边缘放水形成水柱，等水流到底后顺水滑下再收水。
  - **瀑布/急流脱出**：当 `in_water=True` 且非静水湖面、朝目标前进连续 2 秒水平净进展 `< 0.5m` 时，判定遭遇 `flowing_water` 逆流推力，立即改向水流垂直侧向（`±90°`）短步走出水幕后再继续寻路。

### 3.5 子能力五：大水域自动造船渡海与贴岸回收（`cross_water_with_boat`）
- **痛点**：出生在海岛/海面或遭遇 `> 24m` 宽河/海洋（如实机 `s07 / s08`），`escape_water` 扫不到近岸，徒手游泳极慢且易溺水；此外离岸 `> 1` 格调 `/dismount` 会使 Bot 站在船顶导致寻路器假死。
- **机制**：
  1. 当 `in_water=True`（开启 `posture(jump=True)` 浮面防沉）或目标方向连续 `> 24m` 均为水方块且无绕行陆路时：
     - 若背包无 `*_boat` 且木板/原木充裕（且附近有陆地/浅滩可放工作台，或提前备有船），使用或合成木船；
     - 对水面调用 `use_item` / `place` 放船 -> 获取船 `entity_id` -> `/interact(entity_id=boat_id)` 上船；
  2. **微航点折线驾驶**：因载具态 `/move_to` 仅走直线不绕障，将水路按每段 `10~14m` 切分为无礁石水面航点依次 `/move_to`；
  3. **安全登陆铁律**：航行至距离实心河岸 **`<= 1.2m`** 处停船 -> 调用 `/dismount` -> 若登陆后仍站在船碰撞箱上立即补一次朝岸上的 `control(forward=True, jump=True, ms=350)` 跳上实心岸基 -> 顺手用剑/斧 `/attack` 拆船回收进包（若河岸高出水面 2 格则先在水边垫 1 块或挖开岸沿 1 格台阶再上岸）。

---

## 4. `navigate_segmented` 断点接力状态机集成要求

将上述算子以**有限次重试、严禁死循环**的方式挂入 `MacroSkills.navigate_segmented`：

1. 正常分段 `move_to` 成功时，沿途更新 `BreadcrumbTrail`；
2. 当某段 `move_to` 失败（`path_not_found / stalled / timeout`）且 `±35°` 常规偏角绕行也失败时，对面前 `1~8` 格做一次轻量诊断（最多触发 **2 次** 跨越恢复，仍不通立即报明确原因交回上层，绝不无限造桥/乱挖）：
   - 身陷软陷阱 / 面前关门或树叶薄墙 -> 调 `clear_traps_and_doors`；
   - 面前为 `3~12` 格窄裂谷/断桥且对岸有实心地面 -> 调 `sneak_bridge`；
   - 高差 `>= 4` 格陡崖 -> 调 `climb_or_descend_cliff`；
   - 连续 `> 24m` 水面 -> 调 `cross_water_with_boat`；
   - 身处地下封闭多层溶洞且连续受阻 -> 调 `backtrack_breadcrumbs` 回退。
3. **战略禁区绕避提示**：在探索选点时，若未穿 `leather_boots` 扫到大片 `powder_snow`，或在下界扫到密集玄武岩柱+单格岩浆（玄武岩三角洲），直接标记该方向降权绕避。

---

## 5. 验证与交付要求（离线单测 + `nju-beast` 8 关实机障碍赛）

### 5.1 离线单元测试（`tests/test_movement_terrain.py`）
使用 Fake Bot 覆盖全部边界条件（零网络依赖）：
- `BreadcrumbTrail` 采样间距、维度隔离、瞬移断段与倒序回退；
- 甜浆果丛/细雪/蛛网挖掘时强制携带 `stop_on=["death"]`；木门自动 `/interact`，`1.5` 格栅栏不盲跳；
- `sneak_bridge` 悬空 `sand/gravel` 起桥拦截、潜行锁开启与异常退出时的 `finally` 潜行释放；
- 船只贴岸 `<=1.2m` 下船与船顶跳岸恢复。

### 5.2 `nju-beast` 高空隔离 8 关实机障碍赛（`scripts/move_obstacle_live_test.py`）
使用 RCON 在高空隔离区 `(5200..5320, 150, 5200..5320)` 自动搭建并依次实测以下 **8 个真实物理关卡**（全部禁止瞬移作弊，必须靠 `MoveTest_01` 真实动作抵达终点信标格并记录耗时与扣血）：

| 关卡编号 | 实机搭建场景 | 通过标准 |
|---|---|---|
| **M1 软陷阱走廊** | 铺满 `sweet_berry_bush` + `cobweb` + 密 `bamboo` 的 `10m` 通道 | 0 死亡、不被刺伤无限打断，`<= 25s` 抵达终点 |
| **M2 细雪脱困** | 掉入 `3×3×2` `powder_snow` 坑（无皮靴） | 自动桶舀或破雪脱出，不冻死 |
| **M3 村屋门与栅栏** | 关着 `oak_door` 的木屋 + 带 `oak_fence_gate` 的 `1.5` 格围栏院子 | 自动开门/开栅栏门进入屋内终点，不在围栏前盲跳超时 |
| **M4 裂谷潜行搭桥 & 防沙塌** | `8` 格宽无底深渊（起桥岸边故意铺 2 格悬空 `sand`） | 识破并避开/加固悬空沙子，锁潜行搭桥 `0` 坠落过渊，到岸后 `sneaking == False` |
| **M5 陡崖上下通行** | `7` 格垂直石崖上山 + 另一侧 `8` 格断崖无伤下山 | 0 摔落伤害完成登顶与下山 |
| **M6 森林树叶迷障** | `3` 层厚 `oak_leaves` 封死的林间小道 | 自动破叶通过，不报 `stalled` 死锁 |
| **M7 40m 宽海域渡船** | `40×20` 深水池（对岸为高出水面 `1~2` 格的石岸，给 1 艘 `oak_boat` 或木板） | 自动放船/乘船折线渡水、贴岸安全下船上岸且不卡船顶 |
| **M8 双层溶洞死胡同回退** | 先走过一段 `L` 形通道进入封闭双层盲肠洞触发不可达目标，随后执行面包屑回退 | 沿 `BreadcrumbTrail` 准确倒退回入口开阔平台 |

### 5.3 交付产物清单
完成后请在你的隔离工作树提交，并产出：
1. 源码 `src/mc_agent/skills/movement.py` 及 `library.py` 最小接线；
2. 离线单测通过记录（确保全套 `pytest` 零回归）；
3. 实机报告 `runs/move-obstacle-live-<timestamp>.json` 与复盘文档 `docs/move-terrain-live-report-20261118.md`（写清每关耗时、扣血、暴露出的服务端怪癖与实际限制）。
