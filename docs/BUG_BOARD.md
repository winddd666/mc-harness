# `v2-integrated` 6-Session 共享 P0/P1 Bug 看板 (`BUG_BOARD.md`)

> **唯一主线**：`/home/xzq/projects/mc/.worktrees/v2-integrated`（分支 `feat/v2-integrated`）
> **协作铁律**：
> 1. **只修 P0/P1 硬 Bug**（崩溃、死锁、取消风暴、超时穿透、工具契约/方块/GUI/跨维度硬故障），**不动课程先后顺序与分数权重等策略项**。
> 2. **RCON / 线上 Bot 单一入口**：只有 **#1 主 AGY** 操作 `nju-beast` 与 `v2_fleet.py launch`；其余 5 个会话全程纯离线工作（只读 `runs/` + 跑离线 `pytest`）。
> 3. **严禁跨文件修改**：每个会话只在自己的 Worktree 里改自己 Slice 内的文件，禁止运行 `uv pip install -e .` 污染公共 `.venv`。
> 4. **离线测试命令**：
>    `PYTHONPATH=<你的worktree>/src /home/xzq/projects/mc/.venv/bin/pytest <你的worktree>/tests -q`

---

## 角色与 Worktree 映射

| 会话 | Slice | Worktree 路径 | 分支 | 独占文件范围 |
|---|---|---|---|---|
| **#1 主 AGY** | Integrator | `/home/xzq/projects/mc/.worktrees/v2-integrated` | `feat/v2-integrated` | 总控、合入、RCON、`v2_fleet.py launch`、`harness/agent.py`、`harness/model.py` |
| **#2 子 AGY** | Slice B | `/home/xzq/projects/mc/.worktrees/s-combat` | `fix/s-combat` | `harness/guardian/`, `skills/combat_env/`, `skills/combat/`, `skills/safety.py`, `tests/guardian/` |
| **#3 Codex #1** | Slice A | `/home/xzq/projects/mc/.worktrees/s-mining` | `fix/s-mining` | `skills/mining_dfs.py`, `skills/mining/`, `skills/tunnel.py`, `tests/mining/` |
| **#4 Codex #2** | Slice C | `/home/xzq/projects/mc/.worktrees/s-skills` | `fix/s-skills` | `harness/mc_tools/`, `skills/library.py`, `skills/macro/`, `tests/skills/` |
| **#5 DeepSeek #1** | Slice D1 | `/home/xzq/projects/mc/.worktrees/s-triage` | `fix/s-triage` | 只读 `runs/`，输出根因/行号/最小失败单测至本看板 |
| **#6 DeepSeek #2** | Slice D2 | `/home/xzq/projects/mc/.worktrees/s-scenarios` | `fix/s-scenarios` | `scripts/l3_wild_endurance_runner.py`, `scripts/v2_fleet.py`, `kb/data_1_21_11.py` |

---

---

## 当前主线基线与验收摘要（2026-11-23 01:02 更新）

- **服务端 API**：`5ease` 已升级至 `mc-agent-api v1.3.21` (`a9d5ceb`) — 支持 `find_blocks(radius=128)`（实测 5.8ms 命中 89.5m 目标）、`move_to(range=0.0)`（实测 0.000m 对齐）与 `allow_partial=True`。
- **并发限流**：`BUG-150` 跨进程 2 槽 `flock` 闸门已扩展覆盖 `hy4` 与 `nju`（按网关 `base_url` 共享组锁），防止多路触发 `Harness group concurrency limit: 2`。
- **10/10 RCON 微场景实机 PASS**（`scripts/verify_historical_bugs_live.py`）：`BUG-002`、`BUG-007`、`BUG-129`、`BUG-134`、`BUG-138`、`BUG-142`、`BUG-154`、`BUG-155`、`find_blocks(128)`、`allow_partial=True` 均在真实 `5ease` 服通过并已归档。
- **Wave 21 补跑 6/12 正式终档（正式槽共 1 死，另六槽缺终档）**：`glm_village_30m=135`、`glm_nether_fortress_30m=195`、`nju_midgame_30m=135`、`glm_bare_20m=25`、`hy4_bare_20m=15`、`nju_bare_20m=5`。三个 20m 均 0 死，NJU OB2 中期 EP04 明确箭伤 1 死。 [死亡原迹](../runs/l1_w21_v1321_20261123/nju_ob_2_midgame_30m/20261123-003325-689127.events.jsonl#L994)
- **D1 验收范围**。BUG-155 的底层坐标微场景覆盖显式 2.0 和 0.0。当前 `e7ff425` 用完整 actor 入口仍复现默认半径误判、精确零的部分推进被误标已到、占位 Y0 被外层重复计入。整局工具回执门仍待。 [微测入口](../scripts/verify_historical_bugs_live.py#L240) · [actor 外层判断](../src/mc_agent/harness/agent.py#L738)
- **D1 网关候选复验**。BUG-150 `88177ee` 定向 1/1，同网关 HY4 和 NJU 默认共享 2 槽。全局覆盖继续共享，单 provider 显式覆盖分组，不同网关独立。退避持锁范围未改。 [网关键](../src/mc_agent/harness/model.py#L583) · [共享槽夹具](../tests/test_v2_harness.py#L4033)
- **历史归档**：已将 **129 条**历史/已修/实机已验工单无损归档至 [`docs/archive/BUG_BOARD_ARCHIVE.md`](archive/BUG_BOARD_ARCHIVE.md)；本看板仅保留 **5 条活跃 P0 + 19 条活跃 P1**。

---

## 一、活跃 P0 工单（5 条）

### BUG-126 | P0 | 主世界门户课 17 集零验证，三次建门均缺料 | Integrator + Slice D2 / Slice C | 状态: OPEN

- **实证。** 六个 `midgame`／`village` 槽的 17 集合计 **10554.986s**，0 目标验证，无实证当集跨维。`mc_skill_build_portal` 实派 **3 次**。Long2 H1 EP04 两次黑曜石／打火石为 4／1、9／1，Long3 OB3 EP07 为 4／0，均缺料早返。4 次机会暂停含 Long1 OB3 EP03 和 EP05 各在 3.203s／2.400s 被交易抢占，另两次是睡觉。梯／隧道 30 次围栏拒绝全在 Long2 H1，同一围栏中心，目标距 213.5–250.1m 超过安全半径 212m，另有 8 次其他动作拒绝。 [差一块黑曜石](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L1288) · [Long3 缺双料](../runs/l2_long3_20261122/nju_ob_3_village/20261122-124520-307017.events.jsonl#L481) · [超距实据](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-121741-513412.events.jsonl#L574) · [交易抢占](../runs/l2_long1_20261122/nju_ob_3_village/20261122-104050-819036.json)
- **最小修复与验收。** D2 处理门户目标内可达补料，算准黑曜石和打火石缺口，改沿圈内航点，材料齐立即建门。压住该目标中不必要的交易抢占，继续保留紧急生存。离线覆盖 9→10、确有村民但门户不中断和真实危险。实机验建门写、入门后的当前维度和正式目标验证。 [配方门禁](../src/mc_agent/skills/library.py#L8855) · [机会抢占入口](../src/mc_agent/harness/curriculum.py#L2677)

---

### BUG-132 | P0 | 下界回场后可从锚点近侧重返危险路径 | Slice B（Guardian）+ Integrator（Runner 死亡回场）| 状态: OPEN

- **D1 死亡口径复核。** Long3 H3 Bastion 10 集共 **7 次死亡（岩浆 6、坠落 1）、10 次 respawn、16 条 mixed guardian incidents**。EP02 三次死亡在 142.879s／328.461s／470.323s，间距约 185.6s／141.9s。EP02 的 150.704s／476.203s 是 mixed guardian，159.304s／484.788s 只有 respawn。按事件时间算，全槽最短真死亡间隔 81.935s，原 6–9s 连死结论撤回。 [首死](../runs/l2_long3_20261122/hy_5e_3_nether_bastion/20261122-124907-198187.events.jsonl#L333) · [mixed](../runs/l2_long3_20261122/hy_5e_3_nether_bastion/20261122-124907-198187.events.jsonl#L347) · [仅 respawn](../runs/l2_long3_20261122/hy_5e_3_nether_bastion/20261122-124907-198187.events.jsonl#L350)
- **仍有近锚危险路线。** EP04 在锚点近侧入岩浆而死，EP06 在锚点上方挨火球后从高台坠死。核实锚点、解除贴邻岩浆及无支撑接敌路径，复验实际返场站位、用绝对时间配对真实死亡与返场，跟踪从安全站位再次进入危险点的路径。 [EP04 岩浆死](../runs/l2_long3_20261122/hy_5e_3_nether_bastion/20261122-130837-508657.events.jsonl#L1050) · [EP06 坠落死](../runs/l2_long3_20261122/hy_5e_3_nether_bastion/20261122-131810-618352.events.jsonl#L513)

---

### BUG-141 | P0 | `shiny_gear` 下潜 `mc_skill_mine_dfs` 86s 切片内静默死亡（`sr=not_ready`），Guardian `acts=[]` 未出手 | Slice B（Guardian）+ Slice A（采矿）| 状态: OPEN

- **B `06eab5b` 已进 `e7ff425`。** 预期运行指纹 `daf2500030f3c3c7`，晚于 W21。两处 DFS 南北偏航计算待 A，实测须取本次偏航与落脚。 [第一处控制](../src/mc_agent/skills/mining_dfs.py#L1792) · [第二处控制](../src/mc_agent/skills/mining_dfs.py#L1843)

- **D1 对到账面坠落与当前控制。** W16 GLM HY8 EP07 第二片配对工具耗 **86.0736s**，树节点 5 的北支路记录 `cave_or_drop_ahead`、本片 `catch_placed=0`。L30 明确末伤 `fall`、死点 Y−18，L35 守卫看到的是已重生 Y85.1、HP20、暂未就绪。该段无逐次控制体和落脚坐标，死亡前的具体控制仍待证。当前 `8bb199b` 的两处 DFS 前跳用 `atan2(-dx,dz)`，目标正南时生成 0 弧度，而控制协议 0 朝 −Z。此当前可证问题已交 A。B `06eab5b` 的第一航点前偏航保护，D1 定向 **4/4**，覆盖可证悬崖、岩浆、有支撑、查询不可用。待实际载入后按真实行进方向、第一步派发和安全落脚验收。 [第二片回执](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_8_bare_3600s/20261122-195429-860116.events.jsonl#L29) · [落地前末伤](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_8_bare_3600s/20261122-195429-860116.events.jsonl#L30) · [重生状态](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_8_bare_3600s/20261122-195429-860116.events.jsonl#L35) · [控制协议](../agent-api-spec-dist.md#L835) · [偏航计算](../src/mc_agent/skills/mining_dfs.py#L1792) · [护航候选夹具](../../s-combat/tests/test_generation_guard.py#L16393)

- **实证（W16 `glm_hy_5e_8_bare_3600s` EP07）**。该槽 20m 内 0 死出铁镐进入 `shiny_gear` 下潜挖钻，但在第 2 个 90s `mc_skill_mine_dfs` 切片（92s+86s）结束时返回 `sr=not_ready`，紧随 `GUARD reasons=['death_or_connection_changed'] acts=[]`，全程未触发保命抢占，随后 `aborted` 且 8/8 `recover_death_drop` 均未捡回铁镐，整局退回石器。 [W16 GLM H8](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_8_bare_3600s/stdout.log)
- **最小修复与验收**。核查该次死亡原因（坠落/岩浆/窒息/贴脸怪）及为何 `mine_dfs` 运行期间 Guardian 未抢占拦截；补离线复现单测。

---

### BUG-143 | P0 | W16 裸装返场固定在悬空 Y80，连续七次重生即摔至 6–7 HP | Slice D2（Runner）+ Integrator | 状态: OPEN

- **W17b HY4 首回场正例。** 新集成已含 `e623b56`。EP02 L505 确认近战死，重生到原锚 `[157031.5,66.1,156973.5]`。L510 独立守卫读回同 XZ、Y66、HP20、`ready=true` 且 `on_ground=true`，中间无 actor 工具及坠落挫伤。原悬空的 HY11 未在 W17b 裸装重跑，仍待同条件复验。 [死亡与回场](../runs/l1_w17b_20261122/hy_5e_4_bare/20261122-202651-772539.events.jsonl#L505) · [落地实读](../runs/l1_w17b_20261122/hy_5e_4_bare/20261122-202651-772539.events.jsonl#L510)

- **D2 `e623b56` 离线候选。** 安全选址验脚下与头脚净空，仿真原点 Y80.1 掉至 Y64 会拒验。已验证的裸装候选脚下单格固定支撑，入场和回场记录实读。候选增加一格支撑作为场景变更，应留在终档供比较；部署后独立核对七次回场无落地伤。

- **后续课结果。** 第七次死亡所在 EP06 时长 214.987s、22 actor 请求，19 次取消，课程铁器未验证、得分保持 10。首七次近战死的最短事件间隔约 35.21s。 [EP06 收尾](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/20261122-195005-102950.events.jsonl#L949)

- **影响与实据。** W16 P0 `HY_5E_11 bare` 自 03:46:25 至 03:50:55 UTC 共七次明确近战死亡，七次均返回 `[133972.5,80.1,147014.5]` 后在同批事件记录中因坠落掉至 6–7 HP。第一回之后的 guardian 读回相同 XZ、Y64、HP7。近战死亡和回场坠落分开记；末段有两笔死亡位置相同，但相隔约 42.7s 且分别有重生回执。 [首回事件](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/20261122-194409-626214.events.jsonl#L279) · [落地实读](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/20261122-194409-626214.events.jsonl#L284) · [重复地点](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/20261122-195005-102950.events.jsonl#L59) · [第七回](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/20261122-195005-102950.events.jsonl#L185)
- **根因边界。** Runner 回场日志重复设 Y80。`restock_scene` 对主世界沿用固定锚的坐标派发回场与重生点，`prepare_scene_ground` 在 bare 路径返回空列表，无地面复核或备用站位。原锚何时失去支撑待取证。 [回场日志](../runs/l1_w16_p0_verify_20261122/hy_5e_11_bare/stdout.log#L128) · [Runner 回场](../scripts/l3_wild_endurance_runner.py#L1630) · [裸装地面分支](../scripts/l3_wild_endurance_runner.py#L985)
- **最小修复与验收。** 在首次回场时复核当前真实支撑、头顶净空和安全落脚，原点失效时仅更新到围栏内实证可站点，实际位置验收连续七次返回零无动作坠落伤，并保留裸装发育条件。离线加入缺支撑、实体占位、流体及原点安全四种情况。

---

### BUG-152 | P0 | W19 裸装临死前多次生成请求无工具派发，撤退与掩体重复失败 | Slice B（Guardian）+ Integrator | 状态: OPEN

- **同蜘蛛候选 `ece4f4b`，D1 独立回归 8/8。** 两次相同威胁取消后，重复观测只在同命、同干燥实位、HP≥16、同一蜘蛛、无伤/额外敌、上次真实回执零撤时保留当前生成。真实伤、逼近和状态不完整单独复查。W21 早于候选，下波要实证保留生成后派发的首个写动作仍安全。 [定向夹具](../../s-combat/tests/test_generation_guard.py#L2819)

- **W21 HY2 20m EP04 尾段十次连续取消。** 从 Y18 炉位为补木棍上到 Y34，L472 最后一笔阶梯后，十个 `pre_hurt_melee` 各对应一次取消，同一蜘蛛、同一实位、HP20，无 actor 工具派发，结集 `limit_reached`、无死。下一集同点同敌再取消两次，仍无工具，另有两笔 429 退避。复验要保留真实伤害、位移和第二威胁的接管。 [首次接管](../runs/l1_w21_v1321_20261123/hy_5e_2_bare_20m/20261123-003947-838655.events.jsonl#L480) · [末次接管](../runs/l1_w21_v1321_20261123/hy_5e_2_bare_20m/20261123-003947-838655.events.jsonl#L593)

- **W20 OB3 EP04 区分封挡和后续近战。** L43 井口两次放置均未成立，后续撤退因实脚位拒绝，L63 同位置爆炸死，具体爆炸实体与放置错误码未记录。L231–236 是另一次近战首挖时死亡。B `6d1eb40` 仅在几何确认自碰撞、同命同高落点可站时让开 0.55m 再试封挡，仍占格或换命停止，待下一波实载。先退后掩体的 `0b04e7f` 已进入 `2c256f6`，亦晚于 W20。 [封口和首死](../runs/l1_w20_20261122/nju_ob_3_bare_20m/20261122-225817-196409.events.jsonl#L63) · [次死](../runs/l1_w20_20261122/nju_ob_3_bare_20m/20261122-225817-196409.events.jsonl#L236)

- **D1 05:56:38 UTC 原迹复算。** 49 次明确死亡。逐集从死亡窗口内最后一次 actor 工具派发后计数，并在距死亡回执不超过 5s 的第一笔混合守卫信号前截断；42 次有该信号，其余用死亡回执。44/49 有至少两次请求未派发工具，44/49 有至少一次已落盘的取消。以完成、非取消失败和工具派发断开取消序列，34/49 有至少两次连续已结束的取消，21/49 有至少三次。HY1 在守卫报死亡前已有两次真正取消，末请求随死亡一并取消；HY5 首次溺死前只有一次已结束取消，末请求随死亡一并取消。混合信号仅用作提前截断，死亡计数只取明确事件。 [HY1 两次取消和死时信号](../runs/l1_w19_20261122/glm_hy_5e_1_bare_20m/20261122-214320-396623.events.jsonl#L401) · [HY5 一次取消和死时信号](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-213526-667307.events.jsonl#L305)

- **实证（Wave 19 全部 10 只发生死亡的 `bare` 槽，共 49 次 `death_return`）**。逐例回溯 `death_return` 前 3 行，绝大多数死亡前均为连续 `[Qwen 请求 #N] -> [#N+1] -> [#N+2]` 零工具输出直接被小脑打断直至阵亡：`glm_hy_5e_1` L273（`shiny_gear` 死亡 `#25/#26/#27`）、`glm_hy_5e_5` L221（`#16/#17/#18`）、`glm_hy_5e_6` L113/L257、`glm_hy_5e_8` L161/L201/L272、`hy_5e_10` L151/L407、`hy_5e_2` L81、`hy_5e_3` L70/L110/L149、`hy_5e_9` L144/L207/L237、`nju_ob_2` L125/L201/L219、`nju_ob_3` L58。同期 `mc_skill_flee:unsafe_landing ×15`、`mc_skill_unstick:unsafe_landing ×10`。
- **根因**。Guardian 检测到近敌/受击后反复取消正在生成的 LLM 请求，但其零 LLM 自救动作（撤退/掩体）在复杂地形或矿道内因 `unsafe_landing` 无法真正脱离或击杀敌怪，导致 Bot 原地瘫痪、模型始终发不出任何工具调用，直至血量归零。
- **最小修复与验收**。1）同一持续威胁下对“取消 LLM 生成”设冷却/去重门控，避免 10 秒内连续取消 3 次把 Actor 彻底饿死；2）Guardian 撤退失败时必须有保底有效动作（就地 2 格封口/击退/低头垫脚），不能原地空转到死。

---

## 二、活跃 P1 工单（19 条）

### BUG-009 | P1 | Slice B（#2 子 AGY）| 状态: OPEN

- **W21 OB2 中期错层岸与 HY2 同蜘蛛取消。** OB2 EP04 岸目标和末位同 XZ，仍湿且低一格，无起跳前位置。B `f151e9b` 避免向正上方的岸格前跳，D1 两个定向通过。HY2 EP04 尾段同一蜘蛛、同站位、HP20，十次取消均无 actor 写，见 BUG-152。 [岸位回执](../runs/l1_w21_v1321_20261123/nju_ob_2_midgame_30m/20261123-003325-689127.events.jsonl#L491) · [岸位夹具](../../s-combat/tests/test_generation_guard.py#L21253) · [末次近战接管](../runs/l1_w21_v1321_20261123/hy_5e_2_bare_20m/20261123-003947-838655.events.jsonl#L593)

- **W20 HY8 EP20 干岸后的第二次受击。** L237 实到干岸，L238 为 6.17 HP，L246 进入 3.986s 的生成前冷却，L250 已到 3.17 HP。集成 `7f84599` 含 B `36a3b26`，同命干地、低血、无盾无武器且同一近敌经复查仍可达时，先尝试有界短撤再交还 actor。D1 定向 8/8，现场初到岸时敌坐标与路线尚待新原迹。 [干岸](../runs/l1_w20_20261122/glm_hy_5e_8_bare_60m/20261122-233302-996317.events.jsonl#L237) · [冷却内再伤](../runs/l1_w20_20261122/glm_hy_5e_8_bare_60m/20261122-233302-996317.events.jsonl#L250) · [定向夹具](../tests/test_generation_guard.py#L14391)

- **HY8 地下候选离线待实载。** B `d6083a9` 同命同侧且仍低于失败岸沿时保留短时排除，`d73684e` 对确认低氧且原地上浮停滞时复读近旁水柱、同命与高处空气，再给一次有界上浮。D1 独立复跑此处水柱与 HY7 第二段撤退共 13 项通过。旧实机缺完整空气格和落点，后续按确实露头和氧量回升验。 [水柱夹具](../../s-combat/tests/test_generation_guard.py#L5676)

- **W20 HY8 60m EP13 水下仍无法换气。** 铁 DFS 首次抢占氧气 17，卸交动作后仍湿。下一次抢占氧气只剩 4，落点接近 Y16.2。后续反复尝试岸跳与横移，L165、L172、L182 回执仍在水中，L196 明确溺死。W20 已载 `8ef0054`，原迹未记录该点的可用空气格或回落后逐次氧量，尚不能判断是无候选还是未走到候选。`respawned` 后离水只记重生。 [水下受控交还](../runs/l1_w20_20261122/glm_hy_5e_8_bare_60m/20261122-230615-543611.events.jsonl#L140) · [氧气 4 与重复退路](../runs/l1_w20_20261122/glm_hy_5e_8_bare_60m/20261122-230615-543611.events.jsonl#L162) · [死亡](../runs/l1_w20_20261122/glm_hy_5e_8_bare_60m/20261122-230615-543611.events.jsonl#L196)

- **同槽 W19 续见回尸落水与复装后同坑第三死。** GLM HY5 EP05 回尸 XYZ 超时，改 XZ 后报成功在 Y34.2，随后多次水险接管但原位溺亡。EP06 重制石镐；EP07 带两石镐、两台再尝试采铁，同坑附近先破两格但未入铁，末在 `219028.3,35.2,172184.3` 第三次溺亡。将尸点进水的预检、持续浮水的有效上岸证据、采矿旁格重入分开验。 [回尸入水](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-214326-008865.events.jsonl#L54) · [第二次溺死](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-214326-008865.events.jsonl#L144) · [第三次](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-214652-686749.events.jsonl#L160)

- **W19 GLM HY5 重入同水坑的旁格。** EP03 首条铁 DFS 持续沉头后接管并回报已出水，回执登记水格 `219030,32,172181`。复位要求地表，随后续挖在相邻 `219030,31,172180` 再次沉头。后三次脱困都因水险中止，岸边复验仍湿且未到目标岸，明确溺死。以水坑或可连通入口验邻格重入判定，同时保留达到干岸的成功对照。 [首次上岸](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-213526-667307.events.jsonl#L136) · [相邻再入水](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-213526-667307.events.jsonl#L175) · [上岸失败](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-213526-667307.events.jsonl#L304) · [溺死](../runs/l1_w19_20261122/glm_hy_5e_5_bare_20m/20261122-213526-667307.events.jsonl#L316)

- **W18 HY8 裸装同锚同敌仍可能有新动作。** EP03 两轮守卫在同站位、HP20，近敌仍是同一僵尸，L282→294 的独立快照中圆石从 7 到 5，中间没有 actor 写动作。只按血量、坐标和目标去抖会丢这类实际变化。候选 `c81bcba` 补封挡坐标、放置及尝试数，只在状态稳定且零放置尝试的真重复上保留生成，待整合和实载。 [第一次守卫](../runs/l1_w18_glm_20261122/glm_hy_5e_8_bare/20261122-204747-556952.events.jsonl#L282) · [再次独立包差](../runs/l1_w18_glm_20261122/glm_hy_5e_8_bare/20261122-204747-556952.events.jsonl#L294)
- **保留生成后已有首条旧动作进入挖掘派发的实机证据。** B、C、D1 核对 Long2 OB2 EP06 请求 2。Guardian 实撤 1.92m，回读 `[378981.1,81,318949.8]` 并保留生成。模型回复仍写 `[378981.3,82,318953.4]` 和撤退失败，随后 `harvest_logs` 在同一只骷髅箭伤下中断。本地写调用边界记录 `dig [378979,81,318953]`，无破格或入包回执。C、D1 各用 `b04a430` 的真实 actor 与替身传输对照，生成时同命挪 3m，保留生成会从新站位对旧坐标发 1 次 `/place`，要求重规划的对照写 0。验收需在写调用之前阻挡已失效的计划，保留当前状态的只读查询及已核实可执行的动作。 [实撤](../runs/l2_long2_20261122/nju_ob_2_bare/20261122-114900-281455.events.jsonl#L23) · [旧模型站位](../runs/l2_long2_20261122/nju_ob_2_bare/20261122-114900-281455.events.jsonl#L32) · [挖掘派发与抢占](../runs/l2_long2_20261122/nju_ob_2_bare/20261122-114900-281455.events.jsonl#L38) · [放置写入口](../src/mc_agent/harness/mc_tools.py#L1717)
- **实机同集可记录大于和小于等于 20 的原始氧气值。** Long2 H3 EP12 在水中露头且未着地时读到 400。Long3 H1 EP05 同集见干地 269、持续沉头 17、随后干地 20。三轮有明确沉头和氧气字段的 7 条状态记录均 ≤20，400 等读数来源仍待确认。B/C/D1 复核，当前候选在已知沉头时按时长兜底。`7b00f0a` 下 28 项定向通过，包含沉头读 400、氧气缺值、失氧和满氧露头。实机验收同时查头位、氧气趋势和近敌。 [Long2 头露出 400](../runs/l2_long2_20261122/hy_5e_3_enchant_anvil/20261122-122356-714709.events.jsonl#L62) · [Long3 干地 269](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1075) · [沉头 17](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1247) · [干地 20](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1258) · [沉头兜底夹具](../../s-combat/tests/test_generation_guard.py#L16452)
- **B／C／D1 新联审，把三条派发链分别验收。** 同批第一条独占工具已经返回 `guardian_preempted` 时，现有调度器会保留实收、放行只读观察并以 `previous_action_failed` 拦下后续独占。D1 用 Long3 H1 EP05 实收 4 砾石的回执重放通过。三轮 L2 的 106 份抢占回执均没有同批还有后续独占的实机样本。仍需处理保留生成后的**首条**旧动作及新生成的重复派发。Long2 H1 EP02 请求 11 在 Bot 悬空且掩体失败后保留生成，旧攻击进入工具才因目标不可见被拒。Long3 H2 EP03 请求 34 在撤出 7.1m 后保留生成并成功执行只读扫描，应继续放行。请 Integrator 在首条副作用派发前核当前生命、目标和落脚；B 提供守卫事实，C／Integrator 确认真正最终回执进入防重。零进展重复应拦，实收保留且下一轮重访同一危险作业点需另验。全局 8–10s 取消窗暂缓。 [同批调度](../src/mc_agent/harness/scheduler.py#L145) · [原始砾石回执](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1248) · [保留生成](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-113755-455034.events.jsonl#L364) · [旧攻击被拒](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-113755-455034.events.jsonl#L382) · [只读正例](../runs/l2_long3_20261122/hy_5e_2_nether_fortress/20261122-124126-040839.events.jsonl#L797)
- **晨报需补 Long3 取消回升。** 按 actor 请求，Long1→Long2→Long3 为 **205/1317→66/1264→247/1417**。Long2 单槽 3.0%–9.1%，Long3 扩到 0%–65.6%，OB3 0/142、OB2 105/160。Long3 冷却申报 **513.8s**。三轮有过 429 又取消的请求为 44／17／71，其中 27／12／49 个在最后一次 429 标称退避结束前至少 10ms 取消；表中保留交集，通过阶段识别区分请求发出前冷却。此前 OB2 EP11 三次满血预警均带抵抗 V，第二次回读悬空，第三次切换贴脸目标。满血条件不能单独作稳定证据。 [晨报比较](overnight-report-20261123.md#L29) · [第一段抵抗状态](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L53) · [悬空状态](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L64) · [换近敌](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L74)
- **B `7b00f0a` 在岸沿跳后短查真实着地。** 同命、已释放的持续沉头采矿，岸跳回执干态成功而暂悬空时，仅在距回执落点 ≤1.5m、血量稳定且近敌缺席时追加有界读取。复读确认干地着地才发 `shore_landing_confirmed_after_jump`，并按原机制登记湿点。D1 定向 **35 项通过**，另用 H1 EP05 的水位、岸位和未着地坐标独立重放 10 种受控情形通过。0.5s 模拟落地登记 `[402859,60,342980]`，持续悬空、返水、换命及悬空时有近敌的对照均未登记。候选尚未合入。下一波分别验该字段、实际着地，以及旧水点是否在新一轮的真实派发前被拒。 [岸跳回执](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1246) · [未着地状态](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1248) · [候选短查](../../s-combat/src/mc_agent/harness/guardian.py#L680) · [夹具](../../s-combat/tests/test_generation_guard.py#L16541)
- **B `6e1f27b` 将共享氧气口径接到 Guardian 与 L0。** D1 **38 项定向通过**。满氧 20、着地、无头位字段且无其他危险时，常规观察、工具边界与抢占后均不因氧气上浮。近期恢复失败加满氧不跳过重试间隔。真沉头、失氧、明确溺伤／近敌及兼容用例 7 泡和 120 旧 tick 仍可恢复。L0 脚踩浅水满氧保持地面姿态，真实悬空或沉头仍上浮。候选未合入 `b04a430`，Long3 缺头位字段且满氧的实机暴露仍待。 [共享算法](../../s-combat/src/mc_agent/skills/safety.py#L120) · [Guardian 判断](../../s-combat/src/mc_agent/harness/guardian.py#L2203) · [Guardian 入口回归](../../s-combat/tests/test_generation_guard.py#L607) · [L0 夹具](../../s-combat/tests/test_generation_guard.py#L671)
- **Long3 氧气样本补齐两笔实沉头。** 四个满氧 20、头未沉入的独立位置，另有三条 incident 重述；两笔工具抢占分别是 H1 EP04 氧 19／近溺尸和 EP05 氧 17／持续沉头 2.129s。EP05 `mine_blocks` 释放后实收砾石 4，靠岸动作包含 `shore_lip_jump`，其后独立状态较技能落点高 0.4m 且未着地，所以未登记湿航点。接着 request 38 三次 429、无新 actor 动作，40.647s 后的独立战斗回执才确认干地着地，首次着地时刻缺证。缺 `submerged` 的满氧回包仍无实机对照。 [溺尸前水况](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-125202-191391.events.jsonl#L935) · [持续沉头](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1247) · [出水与实收](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1248) · [确认着地](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-130153-153530.events.jsonl#L1258) · [湿点登记条件](../src/mc_agent/harness/guardian.py#L5724)
- **Long3 七槽终档。** 80 集、6 验证，Long2 同为 80 集、13 验证。actor 取消 66/1264→**247/1417**，Long3 分期 37／71／9／130；冷却申报 131.0→**513.8s**，至少 61.108s 未走完。此波保留原生高危快速接管，测试中的新水况／封边候选未实载。 [终档与比较](../../s-triage/docs/D1-r11-triage.md#4nb-long3-七槽终档榜分上升而实证目标减少) · [编排版本指纹](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-124014-202849.events.jsonl#L1)
- **B `ad26985` 归一内置战斗四处水况门。** 近战入口、循环、退出和直向岸边的额外上浮共用判断。确认浅水且头露出、氧气仍为正时不过早上浮；真沉头、零氧、未报头位且低氧仍可发起上浮。D1 水况／封边／战斗回归同批 **21 项通过**，Long3 未载。实机验满氧近敌正常动作、真沉头及时脱困，并看实际岸上落脚。 [共享判断](../../s-combat/src/mc_agent/skills/combat_env.py#L273) · [直朝岸边](../../s-combat/src/mc_agent/skills/combat_env.py#L3147) · [定向](../../s-combat/tests/test_generation_guard.py#L9698)
- **C／D1 将近战旧氧量纲从一处扩到三处。** 在脚踩浅水、着地、未沉头、近僵尸的同一离线夹具中，满氧 20 在入口直接上浮、0 攻击、退出强制跳；300 或缺值则攻击 1 次后击杀，退出姿态为空。切 200 仍能攻击、但退出跳，独立实证收尾的 <240 门。0 氧与真沉头对照仍会上浮。连同上条近战中的 <150，三处统一复验。 [入口 <180](../src/mc_agent/skills/combat_env.py#L4033) · [中段 <150](../src/mc_agent/skills/combat_env.py#L4262) · [收尾 <240](../src/mc_agent/skills/combat_env.py#L5043)
- **内置近战循环仍有旧氧气阈值。** `dc58897` 在近战追击中读取 `in_water && (submerged || oxygen < 150)`。D1 用现有近战夹具保留第一次攻击后踩水、但保持未沉头，满氧 20 只攻击一次便 `water_surface_abort`，150 或 300 则连续打五次。沉头对照仍会上浮。沿 API 0–20 契约修正这一独立入口，回归分开验证真实低氧、充分氧的浅水、近敌和不明氧气。Long3 H1 EP08 L86 朝岸出水未落地、近僵尸 2.6m，L88 换命，L90 确认末伤为生物近战。L86 未带氧气，现场这次走中哪个条件仍缺输入。 [旧阈值](../src/mc_agent/skills/combat_env.py#L4262) · [原夹具](../tests/test_generation_guard.py#L9479) · [API 契约](../agent-api-spec-dist.md#L205) · [H1 未落地](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-131504-018043.events.jsonl#L86) · [死亡](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-131504-018043.events.jsonl#L90)
- **RFC 联审新增 Long3 满血反例与请求分期。** OB2 EP11 三次 `pre_hurt_melee` 在 16.939／21.583／25.131s，HP 均为 20，后两次撤退均为 `no_safe_retreat`，Y66.6→63.6，后段出现贴身新威胁。8–10s 全局窗可能压过其中的及时重规划。三槽终档时全波原迹取消 **238/1133**，发出前冷却 34、仅见 HTTP 回执 71、已有输出 8、阶段未知 125；已证至少 56.989s 冷却申报未走完。A／B／C／D1 先按生命、威胁和作业点复验保留生成后的旧动作，并让最终抢占回执进入防重，保留高危即时接管。 [满血首警](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L53) · [撤退失败](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L64) · [近敌及下降](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl#L74) · [派发入口](../src/mc_agent/harness/agent.py#L1548)
- **D1 比较工具已接 actor 取消分型。** 每集按 actor request ID 记录输出或 HTTP 证据，冷却仍在标称结束前超过 10ms 才判发请求前。既有 Long1、Long2 终档完整重现 29／43／4／129 与 6／17／2／41。Long3 在途切点 **206/971**，分型 **32／63／4／107**，冷却申报 412.9s，已证未走完 53.190s。H1 EP04 首个 DFS 水抢占实为近溺尸，沉头 0.915s，确认释放、上岸并保留泥土 2、砾石 1，未形成持续沉头旧水点提示。request 36 下一工具实向外移约 31.5m，证明离开原作业点。 [新生成实移](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-125202-191391.events.jsonl#L967) · [比较脚本](../../s-triage/scripts/d1_compare.py#L132) · [停前近溺尸](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-125202-191391.events.jsonl#L935)
- **C／D1 复核 OB2 冷却未走完部分。** 61 条申报 **207.468s**，26 次明确在冷却内取消，至少 **36.810s** 的申报窗口未走完。扣除已证未走完的部分后名义额为 170.658s，实际睡眠仍缺独立遥测。200s 验收使用申报额，仍失败。 [单条最大未走完 3.0566s](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125254-507775.events.jsonl#L278) · [对应取消](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125254-507775.events.jsonl#L280)
- **Long3 OB2 首槽终档已单独越 200s 冷却门。** 同名裸装槽 Long2→Long3 取消 **10/110→105/160**，冷却申报 **8.882→207.468s**。Long3 的 105 次取消里，26 次在明确的发出前冷却中，32 次证实已发（31 次仅见 429、1 次已见流片），47 次阶段未知。回场位置和敌情不同，保留为实测对照。 [Long3 终档](../runs/l2_long3_20261122/nju_ob_2_bare/wild-endurance-wild-20261122-123932.json) · [冷却取消](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-124209-388804.events.jsonl#L20) · [有流片的请求](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-124839-825401.events.jsonl#L333)
- **Long3 35 集快照冷却申报已到 316.5s。** OB2 同段 59 条冷却合 **199.500s**，100 次 actor 取消。EP11 actor 实派工具 0、冷却申报 25.391s，仍出现两次真实死亡。把重生点近敌重复与后续新任务派发纳入同一轨迹，冷却仍按发出前、已发出与阶段未知独立统计。 [EP11 原迹](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125648-072609.events.jsonl) · [发请求前计时](../src/mc_agent/harness/model.py#L899)
- **Long3 冷却门已超，主要在一槽。** 门禁切点 0/7 终档，申报 **225.2s**，超 200s。稍后 actor 原迹 68 条合 **241.705s**，其中 OB2 **38 条／128.670s**，本槽 EP09 **35.400s**。申报等待和真实耗时仍按请求阶段对账。 [OB2 EP09 原迹](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125254-507775.events.jsonl) · [可取消等待](../src/mc_agent/harness/model.py#L899)
- **Long3 七槽首 actor 实载 `ab04d59173013193=dc58897`，包含 `166afa5` 的已知湿点派发复查。** 首轮门禁尚 0/7 终档，待同命重复湿航点和干地续采实包。A 的干地重挂候选仍待合入。 [Long3 H1 首集](../runs/l2_long3_20261122/hy_5e_1_midgame/20261122-124014-202849.events.jsonl#L1)
- **Long2 七槽终档，B／C／D1 对齐下轮派发验收。** Long1→Long2 actor 取消 **205/1317→66/1264（15.6%→5.2%）**，Long2 六次冷却中、19 次确定发出（17 个仅见 429、2 个已有首流片）、41 次阶段未知，两波角色及暴露有差异。L2 后独立核两条链：保留生成时，旧排队动作是否在复核生命、威胁和站位后才派发；重开生成时，是否重派同一威胁及原作业点。H1 EP04 实证后者。若补事件，用同集 `scope/request/attempt` 串实际 HTTP 派发与首流片时间；高危即时抢占另验。 [返坑迹](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L550) · [Long2 冷却取消](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114740-678500.events.jsonl#L358) · [已见流片的同一请求](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-115854-141631.events.jsonl#L321) · [HTTP 入口](../src/mc_agent/harness/model.py#L674)
- **C／D1 复核取消统计的证据边界。** Long1 正式终档对应 77 份 actor 原迹，205/1317 为 actor 请求取消率（15.6%）。其中 29 次确定在发请求前的冷却中，47 次确认已发（43 次只有 HTTP 429、4 次已见流式输出），129 次阶段未知。`model.request`／`model.started` 在调用模型之前记录；`model.rate_wait` 只在限流放行后记录。实机复验保留三组计数，死亡、入浆等高危继续即时处理，全局 8–10s 取消节流暂缓。 [actor 请求事件](../src/mc_agent/harness/events.py#L60) · [冷却与限流](../src/mc_agent/harness/model.py#L905) · [发出前取消](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-104456-422594.events.jsonl#L307) · [429 后取消](../runs/l2_long1_20261122/hy_5e_1_midgame/20261122-104511-782540.events.jsonl#L69) · [流片后取消](../runs/l2_long1_20261122/hy_5e_1_midgame/20261122-104511-782540.events.jsonl#L377)
- **A／B 对齐采矿恢复验收。** B 复读已知湿航点的脚格和头格，仍有水就拦下移动。A 从确认的干地续采，旧井路线保留供回撤。A 将 B 提交与本地采矿改动配对跑了 11 个定向用例，均通过；短距干地续采夹具实入 1 粗铁。A 改动仍在工作树，两侧均未实载到 Long2 `1c579b9`。实机看旧湿点实际移动派发为 0、从干地续采有目标矿入包，并确认排干后可重新通过。 [采矿侧续采夹具](../../s-mining/tests/test_mining_dfs.py#L4675) · [Guardian 湿点夹具](../../s-combat/tests/test_generation_guard.py#L15825)
- **B `166afa5` 同命精确湿航点候选。** D1 定向 15 项通过，另用原 DFS 选路夹具，仅设置已确认水点及返回湿脚格。实发两次局部 `mining_water_reentry`，对湿点移动 **0**，同片自行改道并 `quota_reached`、实包 **3 粗铁**。复查缺证按 `mining_footing_unavailable`，排干、换层、换命、换维度均已过精确点夹具。Long2 `1c579b9` 作旧迹，等候选实载及同样 8 步绕路后的派发验收。 [候选精确点夹具](../../s-combat/tests/test_generation_guard.py#L15825) · [原 DFS 路由夹具](../../s-combat/tests/test_generation_guard.py#L14271) · [湿点判断](../../s-combat/src/mc_agent/harness/guardian.py#L5607)
- **Long2 首次水点提示实载，本集仍两次返同水坑。** H1 EP04 初次持续沉头 2.132s、氧气 17，释放真、落干地，L416/L417 明确给 `[352084,-55,357038]`，实收并保留 2 黑曜石。新生成把数量 6→10 后 L462 又向同水格移动；接着实际向东平推 8 步、移 7.9m、入深板岩圆石 14，L550 的后续 DFS 又将同一湿航点 `[352084.5,-55,357038.5]` 作为实际导航目标。两次返坑耗 **50.096s**，没有新增黑曜石回执，独立包为 9。提示已验证，实际派发需要尊重同水点及真实通路，不以参数或换起点视为已改道。 [首次提示](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L417) · [换参返坑](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L462) · [真正平推](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L495) · [DFS 再回湿点](../runs/l2_long2_20261122/hy_5e_1_midgame/20261122-114507-902852.events.jsonl#L550) · [旧图续接](../src/mc_agent/skills/mining_dfs.py#L6001)
- **B `eef30f5` 补首命回包契约。** D1 在 B 分支重跑相同僵尸稳定复核、挖掘超时延后和附近预警共 45 项通过。省略和显式 `null` 都可表示尚无死亡；复查出现新死亡、已有死亡标记消失或整份状态缺证时仍按原危险分支。Long2 仍跑 `1c579b9`，留待后续同类实机。 [候选夹具](../../s-combat/tests/test_generation_guard.py#L2579) · [Long2 实载](../runs/l2_long2_20261122/nju_ob_2_bare/20261122-113700-374535.events.jsonl#L3)
- **B `1853efe` 首次生命契约待补。** L2 OB2 EP06 同一僵尸 899686、同站位且满血，五次零位移撤退仍取消；距离 2.5→2.3×3→2.2m，之后确实击杀。候选保留首次和接近时取消，约 2.3m 需两个相同的实扫样本才保留生成。D1 定向 20 项通过；用同一夹具仅按 API 契约省略首命 `last_death`，由保留生成／验证事件 1 变为取消／事件 0，已发 B。Long2 七槽指纹为 `632f801d792d00d0=1c579b9`，不含 `1853efe`。 [原轨迹首轮](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-104810-414487.events.jsonl#L13) · [后续击杀](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-104810-414487.events.jsonl#L86) · [候选字段校验](../../s-combat/src/mc_agent/harness/guardian.py#L3701)
- **L2 终版区分冷却期间取消与模型生成中断。** 7/7 正式终档，77 份 actor 事件有 205/1317 请求取消（15.6%）。119 条冷却申报 394.274s，其中 29 条在冷却标称结束前已取消，至少 42.864s 申报窗口尚未走完。OB2 EP05 请求 11 申报等 3.979s，0.307s 后遇真实掉血及生命／连接事件取消，尚未走到限流器和模型调用。RFC 复验分开量冷却内取消与真正发出后取消，死亡等高危仍可立即打断。 [冷却开始](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-104456-422594.events.jsonl#L305) · [取消回执](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-104456-422594.events.jsonl#L307) · [等待与限流顺序](../src/mc_agent/harness/model.py#L905)
- **L2 35 集切点，取消冷却请求已超线。** 按逐文件事件计 **129/578** 请求取消，76 条 `model.cancel_cooldown_wait` 请求 **239.221s**；NJU 三槽占 **171.399s**。实际等待可被新危险打断，继续独立核验生成前已排队动作的重新校验，以及收到回执后新生成回旧点。L2 这段还没有可计首次水坑提示的实况。 [贴近重生点的冷却](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-104806-682586.events.jsonl#L82) · [干地三叉戟前的冷却](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-105045-737313.events.jsonl#L219) · [可取消等待](../src/mc_agent/harness/model.py#L905)
- **L2 长测首条实载作业点回执。** OB1 EP01 L365/L366 在苦力怕 872119 抢占时均记录最后派发挖格 `[324994,71,347018]`。L370 新生成后 L398 仍选同一苦力怕，近战被守卫拒，L417 又因落脚不安全无法撤退。该链验证作业点及敌人 ID；最终中断回执的入账、旧动作重验、首次水坑提示后的实际改道分别验收。 [抢占事件](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-103256-396840.events.jsonl#L365) · [新生成](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-103256-396840.events.jsonl#L370) · [拒绝追击](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-103256-396840.events.jsonl#L400) · [撤退失败](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-103256-396840.events.jsonl#L417)
- **W14a H3 EP02 重选同一工具。** L236/259/289 三次同参 `harvest_logs(radius=15,target_logs=12)`，均受击触发 `cover_or_flee`。三次回执最近威胁同为骷髅 809241，回撤约 4.27／1.94／4.80 格，15.621s 工具时间，第三次破格 1 且无入包回执。后两次分别在新生成 request 12/13 后派发。复验需看新生成是否根据解除威胁及换作业区的证据改选，以及在途旧动作派发是否有状态复核。 [首次中断](../runs/l1_w14a_20261122/hy_5e_3_cave_combat/20261122-095822-317366.events.jsonl#L236) · [下一次生成](../runs/l1_w14a_20261122/hy_5e_3_cave_combat/20261122-095822-317366.events.jsonl#L241) · [第三次中断](../runs/l1_w14a_20261122/hy_5e_3_cave_combat/20261122-095822-317366.events.jsonl#L289)
- **D1 复现防重层漏收最终回执。** 固定 W14a 实载 `2c4a34e` 的实际 `_RepeatFailureGuard`，同点同参。内层直接返回 `guardian_preempted` 时第二、三次被拦，内层抛实际 `ReflexPreempted` 时三次均派发，防重记录为 0。注册顺序是先防重，再由外层 Guardian 把异常转回执。B/C 已收到。复验按同威胁、同作业区及实际安全恢复约束，以免同时屏蔽合理的新路线。 [防重注册](../src/mc_agent/harness/agent.py#L1062) · [外层回执入口](../src/mc_agent/harness/agent.py#L1193)
- **W15 实载持续浸没分因，同水坑亦会被异参重选。** OB1 EP01 L276/L307 同一停前坐标 `[309368.5,61,301962.5]`、氧气均 17、满血、无近敌，在沉头 2.050／2.152s 后触发 `sustained_submersion`。两次释放确认，共 52.707s，实际确认圆石 1；后一次是新模型把数量 8 改 10 后派。第二次回执明确提示换入口，之后 L341 实移，L369 采石未再水抢占，但仍破 2 无入包。B `99c5d11` 将最后已派发工作点及触发威胁加入事件，W15 未加载该补证。 [首次停前](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-101108-362534.events.jsonl#L276) · [二次停前](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-101108-362534.events.jsonl#L307) · [提示后实际移动](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-101108-362534.events.jsonl#L341)
- **`6838c45` 首次安全交接即留下水点。** B 新候选在动作已确认释放、同一条命且确认落脚干地时，首次持续沉头也记录 `water_reentry_site` 并提示换入口。D1 在候选分支重跑 21 个定向用例通过，时间线已接该字段。W15 仍用等第二次的旧回执，实机要核首次提示后的实际航点和入包。 [候选夹具](../../s-combat/tests/test_generation_guard.py)
- **W12 H1 EP02 新回执的四个正样本及阈值残口。** 四次同参钻 DFS、停前沉头且氧气20、满血、无近敌，均 `low_oxygen` 且释放真。首片保留17格与泥10、圆石3、砾4，另三片无成果回执，均无钻。`<=140` 的数值仍命中，对波中的真实危险结论需同时保留氧气趋势与垂直运动。本例与已确认头露的浅水用例维持独立验收。 [首片坑前](../runs/l1_w12_guardian_full_20261122/hy_5e_1_deep_water/20261122-091850-204556.events.jsonl#L34) · [部分成果](../runs/l1_w12_guardian_full_20261122/hy_5e_1_deep_water/20261122-091850-204556.events.jsonl#L35) · [第四片](../runs/l1_w12_guardian_full_20261122/hy_5e_1_deep_water/20261122-091850-204556.events.jsonl#L243) · [监视器](../src/mc_agent/harness/guardian.py#L4094)
- **Guardian W12 已实载前两候选。** 7 槽首个 actor 指纹 `b311b28f5786134d=640b8c5`，含浅水与结构化回执 `7bc9e71/5f18372`，近战挖掘超时延后 `63f3a62` 不在本波加载版本。H2 EP03 采钻 DFS 与砍木两次被箭伤触发，实报 `error.code=guardian_preempted`、`stop_reason=tool_interrupted_hurt`、`reflex_mode=cover_or_flee`；两条未确认释放、未报部分采掘。阶段样本，待全波和水况分组。 [DFS 回执](../runs/l1_w12_guardian_full_20261122/hy_5e_2_cave_combat/20261122-091838-584089.events.jsonl#L47) · [砍木回执](../runs/l1_w12_guardian_full_20261122/hy_5e_2_cave_combat/20261122-091838-584089.events.jsonl#L192)
- **跨模型影响**：**128 次 / 13 bots**（`glm_glm_4_enchant_anvil` 24、`hy4_hy_2_bare` 18、`glm_glm_3_village` 14、`hy4_hy_3_iron`/`hy4_hy_8_nether`/`hy4_hy_7_village` 各 12-13）
- **日志证据**：114/128 紧邻 `guardian.incident`（`fresh_hurt 49 / pre_hurt_melee 30 / pre_hurt_ranged 16 / pre_hurt_creeper 15`），每次取消丢弃已生成 token 后整请求重发；另 `model.cancel_cooldown_wait` **80 次 × ~4s = 290.1s 纯睡眠**
- **源码定位**：`harness/guardian.py:246 _detect_pre_hurt_trigger` → 抢占取消在途生成
- **现象与根因**：抢占本身正确，但 `pre_hurt_*` 对同一目标反复触发，每次取消既丢 token 又强制 ~4s 冷却；与工具时长无关（D1 已更正：不存在 >4min 的单次工具）。
- **验收标准**：`pre_hurt_*` 增加 2~3s 去抖/同目标冷却；取消后的 4s 冷却仅在真实受伤后生效；离线单测覆盖同目标连续触发只取消一次。
- **D1 r12 实测（未修复）**：取消 **137 次**、`cancel_cooldown_wait` **272.6s**；`guardian.incident` 179 次中 `pre_hurt_*` 占 63 次（melee 29 / creeper 27 / ranged 7）。与 r11 基本持平，需 Slice B 处理。
- **#1 Integrator 2026-11-22 实测（W11 bare & OB_W3，已开 RFC 讨论）**：`hy_5e_2_bare` 111 次请求被 `guardian.incident` 取消 **43 次（38.7%，650s model + 115s rate_wait）**，`nju_ob_1_bare`/`nju_ob_2_bare` 分别被取消 **59 次 / 84 次（30%+）**；另发现 `guardian.py:3893` `oxy_pre < 180` 疑似与 `oxygen` 满值口径冲突导致浅水 100% 误判 `underwater_danger`（`hy_5e_1_bare` 连断 `mine_blocks` 10 次且 `error: None`）。完整证据、改造提案与开放问题见 **[RFC-guardian-preempt-and-cancel-storm-20261122.md](./RFC-guardian-preempt-and-cancel-storm-20261122.md)**，请 Slice B / D1 / C 直接在该 RFC 末尾追加评审意见。
- **D1 与 B/C/D2 联审。** W11 同实载离线只改浅水氧气 20→300，采石由误停转为正常结束，实机 15 条水抢占仍缺停前状态。H2 纯近战预警取消 16 条均非实杀，EP07 九次战斗超时共 68.419s。六次取消明确在计划冷却内。新版三层建议先补触发、释放与旧工具条件复验，再分别验安全浅水和取消；统一节流需保留真溺水、新敌、生命变化及失证等绕过条件。 [D1 联审与配对门禁](../../s-triage/docs/D1-r11-triage.md#4lc-bug-009-rfc-联审先证触发与动作释放再设节流)
- **联审候选 `7bc9e71`**。Slice B 已在 `fix/s-combat` 限制明确露头的低氧单因触发，保留近溺尸与真实掉血，并记录停前快照。D1 独立重跑 8 条定向用例通过，等匹配波实载和同危害的安全、产出对照。 [候选夹具](../../s-combat/tests/test_generation_guard.py#L14466)
- **联审第二候选 `5f18372`**。触发来源、恢复模式、释放回执和部分成果已分开记录。D1 回放把新旧回执按相同的恢复方式对比，53 项离线通过。实载波的实际释放、输出来历及安全恢复继续对账。 [D1 回执类型与验收](../../s-triage/docs/D1-r11-triage.md#4le-双版本-guardian-抢占按恢复方式对比)
- **W11 近战窄候选 `63f3a62`**。H2 EP07 九次超时端点为 dig 8／status 1，过去 7 轮从上次完成至再次启动仅约 12s；旧件欠逐次敌人 ID。候选保留首轮，最多 24s 内延后确证同威胁的原位重复，D1 定向 18 项通过。验收复查字段缺失、第二敌与真实掉血的恢复路径，实载待确认。 [D1 端点与候选对账](../../s-triage/docs/D1-r11-triage.md#4lf-bug-009-近战挖掘超时候选真实样本仍缺同目标证据)
- **深水候选 `a524f58` 的氧气来源仍待验。** D1 定向 17 项通过。固定满血、沉头且原地不沉，氧气 20 在 2.2s 触发 `sustained_submersion`，140 首帧触发 `low_oxygen`，400 或缺值超过 2.2s 仍未发 `/stop`。B/C 已收到对照结果。实载需覆盖缺值或串值、真危险优先以及上岸后下一片 DFS 的实际井口。 [D1 离线对照](../../s-triage/docs/D1-r11-triage.md#4li-bug-009-深水候选对氧气串值的离线复核)

---

### BUG-091 | P1 | 木石镐混持可循环重铸石镐 | Slice A（挖矿引擎）+ Integrator | 状态: REOPENED（W2 实机证据，两版离线复现）

- **W16 P0 HY9 EP05 的零铁、满包、死亡链。** 四片石镐正差 +5/+4/+9/+3，独立实盘 28 把石镐、25 煤、64 圆石、0 空位、无铁；随后 Y41 死、满包归空。GLM HY8 EP06 即便最终成功，仍在 77.53s 铁片内报真实重铸 10，出炉时带 21 石镐。正例也纳入去重验收。 [HY9 满包](../runs/l1_w16_p0_verify_20261122/hy_5e_9_bare/20261122-195509-422705.events.jsonl#L594) · [HY8 正例](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_8_bare_3600s/20261122-195009-206214.events.jsonl#L176)

- **W16 同槽下一集的实物结果。** 上集四片石镐净增 20，EP04 L36 按名盘点现有 **22 把石镐**。L54 因 36 格满而连第一座炉也无法造，粗铁依然 3。交叉复验应看 A 修复后不再增加冗余镐，以及既有满包中腾位实际落地。 [EP04 盘点](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-194708-068971.events.jsonl#L36) · [满包](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-194708-068971.events.jsonl#L54)

- **W16 GLM HY1 EP03 未加载 A 修复仍大增石镐。** 首三片分别净增石镐 +4／+8／+2，第三片明确 `recrafted_picks=2`；回撤后又一片 +6。三个正差时间出口没有逐把制造回执；四个正差合 +20，不能等作确认重铸数。 [第一片](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L122) · [第二片](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L142) · [第三片](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L161)
- **六个 60m 配置槽新增实物账。** Long1 OB1 EP10 实盘 3 石镐后四片 DFS +10／5／3／1，次集实盘 22，再一片后实盘 23。Long1 OB2 从 3 到终包 23，三片可归 +6／7／5，另 2 把来源缺证。Long2 OB1 EP05 死前 16 把各占独立槽，14 把 `durability_used=5–30`，石镐满耐久 131，空槽只余 2；铁镐仍在副手 45。六槽 10 条 DFS 石镐正差合 +51，正差含拾取及制造，逐把来源缺证。8 个时间出口无重铸字段。 [Long1 前包](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-105621-187460.events.jsonl#L189) · [实盘 23](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-110701-273321.events.jsonl#L266) · [Long2 死前槽位](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114740-678500.events.jsonl#L348)
- **计时边界和候选验收。** 实载三版的预算夹具中，实际制 2 把后若未刷新末包，超时回执正差为 0，刷新后才报 +2。A 用同一离线夹具对比 `b04a430` 和最新未提交快照，木 1／石 2，八步 Y24→16 实制由 8 降至 0，两版均入 1 粗铁。D1 定向 7/7。旧快照在实制后盘点超时时漏报，新快照有 `crafted+gained` 时计制镐 1，缺末包时保持净增未知。合成回执丢失时不认领制镐，夹具只收到一次合成。实载验收沿用独立盘点、实际制镐、主手和入铁回执。 [预算缓存](../src/mc_agent/skills/mining_dfs.py#L407) · [预算回执](../src/mc_agent/skills/mining_dfs.py#L5460) · [候选超时夹具](../../s-mining/tests/test_mining_dfs.py#L1893)
- **Long2 OB1 同集有真实铁产，石镐仍累积。** EP04 初始可见 2 石镐、2 木镐。首片 DFS 84.678s、记录破 69 格、实增 2 粗铁并报石镐 +6，次片 26.945s 在同高度开三格铁矿，实增 3 粗铁、重铸计数 0。同一炉实取 2+3 锭后造出并装备 1 铁镐，后验包仍有 8 石镐。首片的逐把合成来源缺证，保留与分支修复的同场景续片对照。 [前包](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114119-884255.events.jsonl#L268) · [首片](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114119-884255.events.jsonl#L317) · [同层续挖](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114119-884255.events.jsonl#L367) · [后验包](../runs/l2_long2_20261122/nju_ob_1_bare/20261122-114119-884255.events.jsonl#L405)
- **L2 OB2 首终档证实石镐留存在包。** 12 集到计划集数上限，2087.2s／3600s，2 验证、15 分。末集挖铁四片 164.819s、破 94，粗铁 0，石镐记录 +6/+7/+5。Fleet 终包 **23 石镐，粗铁／铁锭／铁镐全 0**。18 把为末集四片包差合计，终包另有先前留存，逐把来源无独立明细。 [末集第三片](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-110259-191669.events.jsonl#L267) · [终包](../runs/l2_long1_20261122/nju_ob_2_bare/wild-endurance-wild-20261122-103254.json#L44)
- **L2 两个裸装槽仍大幅增石镐。** OB1 EP10 两片铁 DFS 合用 169.494s、实破 65，报告石镐包差 +10／+5、煤 +14、粗铁 0。OB2 EP12 首条同类片石镐 +6、粗铁 0。源头逐次合成及独立包需继续核，A 已收到。 [OB1 首片](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-105621-187460.events.jsonl#L272) · [OB1 次片](../runs/l2_long1_20261122/nju_ob_1_bare/20261122-105621-187460.events.jsonl#L311) · [OB2 铁片](../runs/l2_long1_20261122/nju_ob_2_bare/20261122-110259-191669.events.jsonl#L179)
- **W15 OB1 连三片石镐包差 +20，死前实包 19 把。** EP03 L400/L429/L457 的石镐 +6/+9/+5，三次铁目标均无粗铁回执。EP04 首段独立实包 **19 石镐、3 粗铁、0 锭、22 煤**，前集 L321 独立包已见 1 粗铁，额外 2 份仍待逐件归因，这三片铁目标的包差未报粗铁。L96 近战死，L206 独立包石镐和粗铁都为 0。需以一次实制、装备读回、入铁和存活后库存共同验收。 [前集已持 1 粗铁](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-101634-572307.events.jsonl#L321) · [第三片](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-101634-572307.events.jsonl#L457) · [死前实包](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-102705-745774.events.jsonl#L13) · [死后实包](../runs/l1_w15_20261122/nju_ob_1_bare/20261122-102705-745774.events.jsonl#L206)
- **W12 OB1 终包 26 把石镐。** EP01 已实制石镐且混持木镐，EP03 9 个铁 DFS 配对 485.271s、记录破格 202、粗铁 0，五片石镐包差共 +20。EP04 两次显式制工作台及一次熔铜转备料因 `inventory_full` 报 36/36。 [首片正差](../runs/l1_w12_guardian_full_20261122/nju_ob_1_bare/20261122-092334-886059.events.jsonl#L217) · [末片正差](../runs/l1_w12_guardian_full_20261122/nju_ob_1_bare/20261122-092334-886059.events.jsonl#L387) · [满包拦熔炼](../runs/l1_w12_guardian_full_20261122/nju_ob_1_bare/20261122-093420-169330.events.jsonl#L104) · [终包](../runs/l1_w12_guardian_full_20261122/nju_ob_1_bare/wild-endurance-wild-20261122-091655.json#L47)

- **W11 终包确认两槽共持 52 把石镐。** H1／H4 的铁 DFS 包差分别为 +25／+24，终包 27／25。H1 有前段预备及首镐合成，H4 有首镐、铁课备料又制 1，终包比已见 2+24 少 1，逐把去向未记。H4 最后 `empty_slots=0` 并因做不出工作台受阻。实物验收加终包石镐上限及空槽可用，避免只靠 `pickaxe_low=0` 判通过。 [H1 终包](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/wild-endurance-wild-20261122-081434.json#L46) · [H4 终包](../runs/l1_hy4_bare_w11_20261122/hy_5e_4_bare/wild-endurance-wild-20261122-081437.json#L45) · [H4 满包](../runs/l1_hy4_bare_w11_20261122/hy_5e_4_bare/20261122-083426-232705.events.jsonl#L112)
- **W11 `8cf1d18` 裸装铁 DFS 前五片石镐正差 +46、粗铁全零。** H1 EP02 实制石镐时仍持木镐，铁课前三片 **232.449s**、石镐正差 **15+3+7**；H4 EP04 前两片 **172.262s**、石镐正差 **8+13**。同实载用真实 `_dig_stair_down_edge`、`BotSession` 与模拟耐久库存走八步。混持木 1 石 2 实制石镐 **8**、装备 **17**、木镐破普通石 **20**；只调整普通岩石优先选已有可用石镐，实制 **0**、装备 **1**，八步与入粗铁 1 不变。`a20ec5e` 移除工具已有短路后，DFS 错选木镐会真的继续制造。修复优先放在 DFS 的选镐与耐久触发，保持显式需要补新镐时能实制和读回。 [混持包](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/20261122-081821-318531.events.jsonl#L333) · [H1 +15](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/20261122-082139-177378.events.jsonl#L281) · [H1 +3](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/20261122-082139-177378.events.jsonl#L309) · [H1 +7](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/20261122-082139-177378.events.jsonl#L337) · [H4 +8](../runs/l1_hy4_bare_w11_20261122/hy_5e_4_bare/20261122-082407-115581.events.jsonl#L50) · [H4 +13](../runs/l1_hy4_bare_w11_20261122/hy_5e_4_bare/20261122-082407-115581.events.jsonl#L78) · [按最低档选镐](../src/mc_agent/skills/mining_dfs.py#L854) · [下阶与重铸](../src/mc_agent/skills/mining_dfs.py#L4364) · [工具合成快路](../src/mc_agent/skills/library.py#L3837)
- **W3 OB2 另一路回撤后重铸 14。** EP11 已实持石镐 2，第一片净增石镐 1。回撤后下一铁片 70.776s、终止 `cannot_reach_nearest_node`，粗铁 0、铜 7、重铸计数 14。可与同波另一裸装槽的 24 次回执做同配方重放，同样需逐把实制、损耗和主手读回。 [两把实包](../runs/5ease_ob_w3_20261122/nju_ob_2_bare/20261122-070441-299056.events.jsonl#L192) · [首片石镐正差](../runs/5ease_ob_w3_20261122/nju_ob_2_bare/20261122-070441-299056.events.jsonl#L231) · [14 次回执](../runs/5ease_ob_w3_20261122/nju_ob_2_bare/20261122-070441-299056.events.jsonl#L387) · [片内计数](../src/mc_agent/skills/mining_dfs.py#L6749)
- **W3 一片回执重铸 24，紧接的实包只有两把。** OB1 EP03 箭死后先制木镐，再实收 10 圆石及制 1 石镐。L523 的 81.3s 铁片从 Y62 下到 Y40，报本片 `recrafted_picks=24`、石镐净增 1、圆石 +63、粗铁 0。L541 两把石镐实包，显式制镐返回已有、增量 0。检查下探普通石块的实际制造和主手变换，区别真实耐久损耗、成功返回和净包差。 [死后第一把石镐](../runs/5ease_ob_w3_20261122/nju_ob_1_bare/20261122-065632-972307.events.jsonl#L495) · [片内计数](../runs/5ease_ob_w3_20261122/nju_ob_1_bare/20261122-065632-972307.events.jsonl#L523) · [独立盘点](../runs/5ease_ob_w3_20261122/nju_ob_1_bare/20261122-065632-972307.events.jsonl#L541) · [本片重铸差](../src/mc_agent/skills/mining_dfs.py#L6749)
- **三路线 OB2 成功铁片仍报高重铸。** EP07 L183 58.0508s，粗铁净增 9、回执列 4 个矿格、`recrafted_picks=7`。前两片只有一次石镐净增与累计主手切换指标。第七次重铸属于工具计数，缺逐把实际存量和装备来源。把相同 Y19 矿片用现有镐耐久、实际制造和主手读回对照，不应损伤 9 份粗铁的真实入包。 [四矿坐标及重铸](../runs/5ease_ob_w2_20261122/nju_ob_2_bare/20261122-062814-125212.events.jsonl#L183) · [重铸计数](../src/mc_agent/skills/mining_dfs.py#L4048)

- **Wave11 H5 出现短片重铸 9 次。** EP01 两片增粗铁 3、4，EP02 首片 23.3352s 报 `recrafted_picks=9`、无新铁，终版粗铁 7、铁锭 0。已有木石混持夹具应再覆石镐场景带入实际耐久、逐次主手和窗口转移。本次只有合成成功计数，9 把新镐在实包的逐笔流向仍未知。 [初收 3](../runs/5ease_l1_wave11_20261122/hy_5e_5_probe_iron_y16/20261122-054213-664253.events.jsonl#L64) · [再收 4](../runs/5ease_l1_wave11_20261122/hy_5e_5_probe_iron_y16/20261122-054213-664253.events.jsonl#L94) · [首片重铸](../runs/5ease_l1_wave11_20261122/hy_5e_5_probe_iron_y16/20261122-054713-765046.events.jsonl#L31) · [独立终包](../runs/5ease_l1_wave11_20261122/hy_5e_5_probe_iron_y16/wild-endurance-5ease_w11_hy_5e_5_probe_iron_y16-20261122-054201.json#L45) · [重铸计数](../src/mc_agent/skills/mining_dfs.py#L4048)

- **Showcase 首次直接报重铸次数。** OB2 EP07 L174 同时持木镐 1、石镐 2，L205 煤片石镐 +1。随后 L339 同集报 **59.5051s、`recrafted_picks=10`**、煤 +10、列出 4 个煤矿格，该片没有石镐正差。计数由合成成功回执累加，实包净持有另查。用此片与 W2 找铁夹具一起验镐选择，确保只在现有镐缺失或耐久不足时重铸。 [混镐](../runs/5ease_ob_showcase_20261122/nju_ob_2_midgame/20261122-045625-758120.events.jsonl#L174) · [10 次回执](../runs/5ease_ob_showcase_20261122/nju_ob_2_midgame/20261122-045625-758120.events.jsonl#L339) · [计数](../src/mc_agent/skills/mining_dfs.py#L4047)

- **Showcase 已出现同型混持。** OB1 EP05 L45/L55 独立包有木镐 1、石镐 2、工作台 2、圆石 18。OB2 EP07 L174 造木镐后也同时持 1 木、2 石，随后煤 DFS 施工 25 个通用格，净增石镐 1、煤 2。这个样本补充混持覆盖，实际重铸次数仍待低层日志。 [OB1 混包](../runs/5ease_ob_showcase_20261122/nju_ob_1_bare/20261122-045733-906507.events.jsonl#L55) · [OB2 混持后的切片](../runs/5ease_ob_showcase_20261122/nju_ob_2_midgame/20261122-045625-758120.events.jsonl#L205)

- **实机与根因复验**。W2 OB1 EP03 L265 同时持木镐 1 和石镐 2，之后 L305/L346/L469/L511/L539 石镐正差为 **1/1/1/1/7**、粗铁均 0。固定 W2 实载 `2fb1eaf` 和集成 `0731a37`，用真实 DFS 下阶梯边、BotSession 与模拟世界复现。木镐 1、可用石镐 2、每步旁有工作台，8 步下降 Y24→16 均重复造镐 **8** 次、切换装备 **18** 次、拿木镐挖 20 普通石格。移除木镐后造镐 0、改用石镐，两边粗铁均实收 1。保留原库存，仅让普通岩石优先选现有可用石镐，造镐由 8 降为 0、装备由 18 降为 1、粗铁仍为 1。实机日志缺逐次底层合成回执，每把镐的来源仍待证。 [W2 混持盘点](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-024956-442964.events.jsonl#L265) · [W2 单片多出七镐](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-024956-442964.events.jsonl#L539) · [最低档位选择](../src/mc_agent/skills/mining_dfs.py#L854) · [重铸入口](../src/mc_agent/skills/mining_dfs.py#L4014) · [下挖触发](../src/mc_agent/skills/mining_dfs.py#L4360)

- **最小修复与验收**。普通岩石选既有可用石镐，按实际可用库存判定是否重铸；首次重铸成功后确认主手，保留耐久耗尽和石镐缺失的真实升级。原夹具混持时零重铸，只有木镐时至多重铸一次，石镐不足时按实际损耗补造。同井口实测逐格镐档、合成次数、粗铁入包和首采耗时。

- **W2 OB1 40 分钟最终持有数**。EP05 包有石镐 22、满 36 格；EP06 仍回石镐 +2，终版留 **23 把石镐、4 粗铁，0 铁锭**。找木失败，炉务入口未触发。数量已由后验包确认，个别石镐的制造与损耗仍缺逐次证据。 [满包盘点](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-031114-089455.events.jsonl#L42) · [终包](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/wild-endurance-5ease_w2_nju_ob_1_bare-20261122-024709.json#L41)

- **W2 OB1 EP03 单个找铁切片石镐增量 +7**。L265 实查已有 2 石镐，随后七个可读切片粗铁一直 0，L539 施工 30 格、石镐正差 **+7**。较早两片还有原位或零开挖的 +1。时间门回执基于库存首尾，不含每次重铸集内明细，成因和最终持有量待对账。请复查镐的剩余耐久判定以及预算结束时的制造计数。 [已有两镐](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-024956-442964.events.jsonl#L265) · [零开挖的石镐变化](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-024956-442964.events.jsonl#L469) · [单片+7](../runs/5ease_w2_40m_20261122/nju_ob_1_bare/20261122-024956-442964.events.jsonl#L539) · [重铸入口](../src/mc_agent/skills/mining_dfs.py#L4007) · [时间门库存差](../src/mc_agent/skills/mining_dfs.py#L5433)
- **影响**：`5Ease_Bot` 拿到 `iron_pickaxe` 切入 `shiny_gear`（`target='diamond', target_y=-54`）后，连续 3 个 60s 时间片（第 38~40 分钟）均返回 `collar_node_id=null, node_count=0`，在 `Y=20~24` 徘徊 3 分钟未能开井下潜，且石镐爆后切木镐慢挖。
- **现象与根因**：新目标与上一阶段留下的 `exhausted_collars` 距离过近，引擎反复试图选新井口却未真正下切阶梯；同时 `mine_dfs` 内石镐耗尽后未优先就地重铸石镐。
- **最小修复方向**：
  1. 切换 `target`（如 `iron -> diamond`）或 `abs(y - target_y) > 20` 时，若旧 `exhausted_collars` 在不同深度层，允许就地直接建新井口下潜（或自动水平偏移 12~16 格强制落井），避免连续多片 `collar_node_id=null`；
  2. 随身原木+圆石充足且 `pickaxe_tier < 2` 时，在 `mine_dfs` 下潜前自动重铸石镐再挖。

---

### BUG-099 | P1 | 采铁阶梯降至 Y≤16 时回执构造可中止单集 | Slice C + Integrator | 状态: REOPENED（W1 实机 NameError）

- **W1 第二槽实机命中**。同样加载 `75feb68` 的 NJU_OB_1 EP15，L178 北向下阶梯开始，L180 同一 `pickaxe_tier` `NameError`，调用缺完成回执，槽没有终版。EP14 已审到 L530，以集限结束。旧加载两槽因同一错误中止。`ca61ddb` 的 OB2 重启另验，仍需 Y≤16 实机。 [EP15 开始](../runs/5ease_w1_20261122/nju_ob_1_bare/20261122-023630-948888.events.jsonl#L178) · [异常](../runs/5ease_w1_20261122/nju_ob_1_bare/20261122-023630-948888.events.jsonl#L180) · [运行回溯](../runs/5ease_w1_20261122/nju_ob_1_bare/stdout.log#L1197)
- **影响**：EP14（`bootstrap_iron_age`）无铁镐状态下连续调 `mc_skill_staircase(vertical='down')` 一路滑到 `Y=-2` 深板岩层，整集 600s 超时 0 收获。
- **最小修复方向**：在 `bootstrap_iron_age` 或无铁镐下潜到 `y <= 16` 时，返回提示改用水平 `mc_skill_mine_dfs(target='iron', target_y=16)`，禁止继续 `staircase(down)` 滑入深板岩。
- **W1 回归**。`5ease_w1_20261122` NJU_OB_2 EP04 载入 `75feb68`，上一段下行挖 30 格并到 Y25；下一调用启动后 **34.697s** 直接以 `runtime_error` 结束本集，无工具完成或物料回执。`75feb68` 和 `0d45d74` 的提交版 `skills/library.py:7847` 均读未定义局部 `pickaxe_tier`，实际读取放在 `self.bot.pickaxe_tier`。在 Y25 的上个回执未进入该条件，因此不能覆盖。 [上一片](../runs/5ease_w1_20261122/nju_ob_2_midgame/20261122-015205-539540.events.jsonl#L1042) · [调用启动](../runs/5ease_w1_20261122/nju_ob_2_midgame/20261122-015205-539540.events.jsonl#L1060) · [异常](../runs/5ease_w1_20261122/nju_ob_2_midgame/20261122-015205-539540.events.jsonl#L1062) · [镐档读取](../src/mc_agent/skills/library.py#L7383)
- **验收**。下行结束 Y≤16 时，木／石镐收到铁层提示，铁镐和未知档位有正常回执。另过 BUG-055 原有垫柱换招夹具。将两个分支分开，避免下行铁层提示被垫柱文案覆盖。下一波捕获低层 staircase 的配对完成和实际背包。 [回执构造](../src/mc_agent/skills/library.py#L7838) · [垫柱回归夹具](../tests/test_skills_and_providers.py#L2323)

- **`ca61ddb` 固定源码离线复验**。同一公开 `dig_staircase` 从 Y16 下到 Y15。`0d45d74` 的木、石、铁镐三种都 `NameError`。`ca61ddb` 三种都返回 `completed`，木/石镐仍错误提示“已垫高一格”，回执没有垫柱。原有 BUG-055 二次垫柱换招夹具仍失败，保持 REOPENED。 [相邻回执分支](../src/mc_agent/skills/library.py#L7838) · [原夹具断言](../tests/test_skills_and_providers.py#L2371)

---

### BUG-103 | P1 | 包中已齐首 4 钻仍继续挖，未切到穿钻石靴 | Slice D2（课程）+ Slice A（DFS 回执）| 状态: OPEN

- **D1 核近期裸装制靴正例。** W19 OB3 20m EP05 一片确认实入 4，下一笔即制穿，达阈值到派发 **2.6021s**、到脚槽 **9.2380s**，之后才顺采 1 钻。actor 最终超时，课程仍客观验证、终包保靴。W19 OB1 60m EP04 两片各请求 4，实入 1+5，随后下一笔制穿，6 钻耗 4 余 2，确认跨过阈值后 **30.6128s** 穿上。W20 HY7 仅实入 1+2 后死亡、OB1 终包仅 2，均是未达阈值。验收分别核本次申请缺口、真实累计包量、同片超额、穿戴动作与脚槽、真实顺采起点。 [OB3 实入四钻](../runs/l1_w19_20261122/nju_ob_3_bare_20m/20261122-214349-922482.events.jsonl#L120) · [OB3 真穿](../runs/l1_w19_20261122/nju_ob_3_bare_20m/20261122-214349-922482.events.jsonl#L142) · [OB1 累计超额](../runs/l1_w19_20261122/nju_ob_1_bare_60m/20261122-214053-551737.events.jsonl#L149) · [OB1 真穿](../runs/l1_w19_20261122/nju_ob_1_bare_60m/20261122-214053-551737.events.jsonl#L191)

- **W17b OB1 裸装正例。** EP03 第六段在 Y−29 实入 6 钻，约 15.1s 后实制并装备钻石靴。随后三片顺采仍在集预算内，Y−52 另报实入 4。下一集复读脚槽仍穿靴，散钻 7，与已报告 10 入减 4 耗的 6 有 1 颗待归因。课程进度实际从 20 到 95。 [首簇六钻](../runs/l1_w17b_20261122/nju_ob_1_bare/20261122-202931-255188.events.jsonl#L176) · [钻石靴](../runs/l1_w17b_20261122/nju_ob_1_bare/20261122-202931-255188.events.jsonl#L223) · [后验装备](../runs/l1_w17b_20261122/nju_ob_1_bare/20261122-203815-832446.events.jsonl#L14)

- **`cea55e4` 决策门复验**。用真实 `_episode_gate`、模拟独立背包及成就，持铁镐时 3 钻、4 钻有台、4 钻无台均返回无介入；取得穿戴成就后才开始计顺采。L1 H2 EP02 达 4 钻后五次 DFS 中三次实际耗 **62.4683s**，两次被短集尾拦住，至集尾共 **94.6214s**。请在 `shiny_gear` 场景达 4 时安排制靴，依然以实包消耗、装备及成就验收。吞吐探针 `probe_diamond_y54_throughput` 的目标仍是持续挖 16，使用独立对照。 [决策门](../src/mc_agent/harness/curriculum.py#L2622) · [最后实挖](../runs/5ease_l1_probes_20261122/hy_5e_2_probe_diamond_y54/20261122-020911-868201.events.jsonl#L220) · [探针目标](../scripts/l3_wild_endurance_runner.py#L1918)

- **L1 实机**。HY_5E_2 `shiny_gear` EP02 背包已有 1 钻，后两片各入包 1 和 2。首达 4 的回执之后仍有约 **94.62s**，接着 5 次调用 DFS，未调用制靴。终包 4 钻，目标超时。HY_5E_7 同目标终包 **6 钻、1 张工作台**，也未调用制靴。 [已有一钻](../runs/5ease_l1_probes_20261122/hy_5e_2_probe_diamond_y54/20261122-020911-868201.events.jsonl#L68) · [达 4 的回执](../runs/5ease_l1_probes_20261122/hy_5e_2_probe_diamond_y54/20261122-020911-868201.events.jsonl#L131) · [HY_5E_2 终包](../runs/5ease_l1_probes_20261122/hy_5e_2_probe_diamond_y54/wild-endurance-5ease_l1_hy_5e_2_probe_diamond_y54-20261122-020509.json#L50) · [HY_5E_7 终包](../runs/5ease_l1_probes_20261122/hy_5e_7_probe_diamond_y54/wild-endurance-5ease_l1_hy_5e_7_probe_diamond_y54-20261122-020510.json#L49)
- **根因与最小修复**。剧本要求包满 4 钻立即制靴，`mine_dfs` 的 `goal_met` 按单次差额判断，达库存阈值时仍可能为假。保留单次增量，在下一动作前核对防具配方的总库存，达到阈值时切到制靴。工作台未知或缺失按现有前置条件处理。 [剧本](../src/mc_agent/harness/curriculum.py#L477) · [单次增量基准](../src/mc_agent/skills/mining_dfs.py#L6033)
- **验收**。带 1 钻开集，两片分别入包 1 和 2，下一步即尝试制靴。核对 4 钻消耗、钻靴实际入包和装备，以及课程验证。若制靴不可行，记录具体缺口和重试位置。

---

### BUG-105 | P1 | 采矿中段超时恢复抛 TypeError，单集早退 | Slice A（DFS）+ Integrator | 状态: OPEN

- **5Ease 采矿 V4 整波覆盖 `2fb1eaf`**。8 槽、16 集，53 次工具全配对、0 同异常。已观察到外层 `timeout_path=dig` 和同格时间门，仍需实机记录中段恢复、止动结果及同格复查，离线反例保持。 [首片预算门](../runs/5ease_mine_eff_v4_20261122/hy_5e_3_probe_diamond_y54/20261122-024717-616704.events.jsonl#L32) · [短片尾门](../runs/5ease_mine_eff_v4_20261122/hy_5e_2_probe_diamond_y54/20261122-025210-262656.events.jsonl#L154)

- **`2fb1eaf` 离线复验**。相同读超时进入 `ApiError(code=dig_timeout)`，首格 `dug_count=1` 保留。真 DFS 包装内，复查空格时不重挖，实心时重新装备后只重试一次，再次超时以 `dig_failed` 收束。三例均恢复 bot/skills。提交只去掉构造关键字，止动错误仍被捕获，现场需核对释放和完整材料回执。 [错误构造](../src/mc_agent/skills/mining_dfs.py#L383) · [复查及一次重试](../src/mc_agent/skills/mining_dfs.py#L1574)

- **影响与实据**。5Ease 采矿 V3 的 HY_5E_5 EP01 在首次 DFS 开始后以 `runtime_error` 结束，缺完成回执。预期的片内恢复无法返回已完成的挖掘和入包量。该次实际掉落及背包变化未知。实载 `8500883`（`734f2a3f6eaad6b0`）。 [DFS 开始](../runs/5ease_mine_eff_v3_20261122/hy_5e_5_probe_diamond_y54/20261122-023705-994053.events.jsonl#L33) · [异常](../runs/5ease_mine_eff_v3_20261122/hy_5e_5_probe_diamond_y54/20261122-023705-994053.events.jsonl#L35)
- **根因与最小修复**。本片已挖至少一格、剩余 >12s 的 `dig` 读取超时触发止动，随后构造 `ApiError(..., status_code=408)`。构造器接受 `http_status`，所以 `TypeError` 越过局部 `except ApiError`。改正关键字，使局部恢复能复查目标方块和装备并在预算内重试。 [触发与错误构造](../src/mc_agent/skills/mining_dfs.py#L383) · [异常签名](../src/mc_agent/errors.py#L44) · [局部恢复](../src/mc_agent/skills/mining_dfs.py#L1574)
- **验收**。固定提交离线已复现 `dig 成功 → dig ReadTimeout → stop 确认 → TypeError`。修后覆盖已破的格复查、仍在的格至多重试一次和连续超时。实机同类切片有配对回执，保留已完成挖掘、目标入包及终止原因，整集无该 `runtime_error`，同时复核外层截止和止动。
- **同一候选的止动待确认反例**。固定 `2fb1eaf`，真实 BotSession + MockTransport 模拟 `stop` 读超时、HTTP 200 返回 `stopped=false`，两例均复查后重挖。模拟止动超时至重挖只隔 0.100s。若重挖收到 `action_busy` 则回 `dig_failed`。请把未确认释放纳入复验，禁止在动作归属不明时将部分回执当成安全释放。 [止动处理](../src/mc_agent/skills/mining_dfs.py#L394) · [再挖](../src/mc_agent/skills/mining_dfs.py#L1581)

---

### BUG-106 | P1 | 两次无目标产出淘汰矿簇漏算挖掘预算退出 | Slice A（DFS）+ Integrator | 状态: OPEN

- **Wave11 H2 跨集保持同一代表 `[30361,-47,19678]`。** 首片实入 2 钻后，连续三片 **135.1864s**、100 通用破格、钻 0。异段距离约 27.70→9.30→9.30→16.98m。首个靠近片应保留，后两片一次原地一次退远，应反馈换角度或换簇；最后一个拒绝短片不得误计。所有这三个零产片走预算出口，现有近限逻辑应按该样本离线和同矿簇实测。 [实际趋近](../runs/5ease_l1_wave11_20261122/hy_5e_2_probe_diamond_y54/20261122-054211-037673.events.jsonl#L68) · [原地重试](../runs/5ease_l1_wave11_20261122/hy_5e_2_probe_diamond_y54/20261122-054211-037673.events.jsonl#L99) · [跨集又退远](../runs/5ease_l1_wave11_20261122/hy_5e_2_probe_diamond_y54/20261122-054710-506913.events.jsonl#L31) · [预算出口](../src/mc_agent/skills/mining_dfs.py#L5430)

- **黑曜石门户 V1 H8 钻槽预算出口实机补证**。EP01 L32/L62/L95 同锁 `[9815,-52,7382]`，三片 180 个通用破格、钻 0。首片靠近 17.507→5.339m，接着两片各退远到 **12.439m、16.078m**，锁仍在；下一集仍用此锁。H7 同波一直朝另一簇靠近，终包钻 1。配对复验保留「确实趋近」，两次原地或退远才推进换簇。 [H8 首片](../runs/5ease_obsidian_portal_v1_20261122/hy_5e_8_probe_diamond_y54/20261122-035000-640482.events.jsonl#L32) · [H8 第三片](../runs/5ease_obsidian_portal_v1_20261122/hy_5e_8_probe_diamond_y54/20261122-035000-640482.events.jsonl#L95) · [H8 下一集](../runs/5ease_obsidian_portal_v1_20261122/hy_5e_8_probe_diamond_y54/20261122-035500-739396.events.jsonl#L32)

- **`cea55e4` 离线追加出口对照**。正常返回两片仅粗铁 +1、钻石 0，第二片仍淘汰。预算退出的同型两片 `target_gained=0`，计数为 0、锁不掉。模拟单次真实读超时与止动后，正常返回的接近 3m 会清 dry=1，预算返回保留旧计数，导致下一正常零钻片淘汰；预算片实际 +1 钻也会保留旧数。`time_slice_progress` 可单凭非目标入包给出，仍按目标物与同锁距离判矿簇。在 V9 H4 EP01，同代表前三片 183 个通用施工格、238.0373s、0 钻，距离 53.2→42.0→14.2→2.5m，是应保留施工方向的正对照。它在近点还缺目标破块与实收，复验需分开看接近和到点后的回收。 [正常出口](../src/mc_agent/skills/mining_dfs.py#L5399) · [预算出口](../src/mc_agent/skills/mining_dfs.py#L5430) · [V9 同锁第三片](../runs/5ease_virgin_eff_v9_20261122/hy_5e_4_probe_diamond_y54/20261122-033443-877607.events.jsonl#L94)

- **5Ease V8 实机仍跳过干片计数**。HY_5E_1 EP01 L61/L91/L122 与 EP02 L35，连续 4 个预算挖掘出口都锁 `[45,-57,7041]`，共施工 **157 格、193.624s**，钻石全 0。各段实际没有靠近超过 2m，后半还退远，无法落在新增的靠近保护。HY_5E_2 正常返回的 L121 反有距代表 36.091→28.151m 的接近样本，两条路径必须独立验收。 [旧锁首集](../runs/5ease_virgin_eff_v8_20261122/hy_5e_1_probe_diamond_y54/20261122-032542-045784.events.jsonl#L122) · [跨集不换](../runs/5ease_virgin_eff_v8_20261122/hy_5e_1_probe_diamond_y54/20261122-033042-141289.events.jsonl#L35) · [正常返回接近](../runs/5ease_virgin_eff_v8_20261122/hy_5e_2_probe_diamond_y54/20261122-032553-893980.events.jsonl#L121)

- **`e08c586` 离线复验**。正常返回连续两次各靠近同一矿簇 3m 时不再淘汰，恰好靠近 2m 和原地不动仍走两次干片门。正常取得 1 钻清旧计数。实际预算夹具首格成功、次格读超时后正确走止动，但负产、靠近 3m、甚至包差 +1 的三条预算出口均保留旧 dry=1，后一正常零片仍可淘汰。新提交覆盖正常靠近分支，BUG-106 的预算出口继续待修。 [新靠近判定](../src/mc_agent/skills/mining_dfs.py#L5404) · [预算出口](../src/mc_agent/skills/mining_dfs.py#L5428)

- **W2 OB2 EP01 自然生存复现**。实载 `401eb3e`，L519/L550 连续预算退出，保持候选 `[6387,-31,-6474]`、实际下挖、钻石均 0。接着进入目标深度带并换锁，L629 才实有 +2 钻。前两片均从时间门返回，后续换簇的具体原因缺显式回执。 [原锁](../runs/5ease_w2_40m_20261122/nju_ob_2_midgame/20261122-025802-874263.events.jsonl#L519) · [第二个预算退出](../runs/5ease_w2_40m_20261122/nju_ob_2_midgame/20261122-025802-874263.events.jsonl#L550) · [后续实入](../runs/5ease_w2_40m_20261122/nju_ob_2_midgame/20261122-025802-874263.events.jsonl#L629)

- **影响与实据**。采矿 V5 HY_5E_2 连续四片保持雷达候选 `[-5159,-55,-7629]`，施工 **53+58+69+22=202 格**，四片钻石增量均 **0**，旧代表没有被新淘汰逻辑移除。HY_5E_8 的正常返回路径则在连续两次无钻后更换了锁定代表。实载 `401eb3e`（`adcc7d73bef1bd51`）。 [前三片](../runs/5ease_mine_eff_v5_20261122/hy_5e_2_probe_diamond_y54/20261122-025759-216410.events.jsonl#L95) · [第二集同锁](../runs/5ease_mine_eff_v5_20261122/hy_5e_2_probe_diamond_y54/20261122-030259-299913.events.jsonl#L34) · [正常路径换代表](../runs/5ease_mine_eff_v5_20261122/hy_5e_8_probe_diamond_y54/20261122-025802-338265.events.jsonl#L94)
- **源码与离线复现**。`MiningDFSEngine.run` 只在 `_run` 正常返回后更新 `_locked_rep_dry_calls`，`_MiningDeadline` 分支计算目标增量后直接返回。固定 `401eb3e` 的真实包装，给同代表两次正常零产出，第二次标 `stalled_locked_cluster`；两次预算退出时计数始终 0，锁还在。另一个夹具为正常 +0 → 预算退出且背包钻 +1 → 正常 +0，中间的真实入包未清旧计数，第三片仍淘汰。 [计数入口](../src/mc_agent/skills/mining_dfs.py#L5388) · [预算退出](../src/mc_agent/skills/mining_dfs.py#L5411)
- **最小修复与验收**。让同一代表的目标物料判断覆盖正常返回与结构化预算退出，依据真实 `target_gained` 重置；短集尾拒绝而未尝试的调用保持原计数。覆盖同锁两次长切片、跨集持续、确实入包后不淘汰。实测同站、同代表两片无钻后下一片能换角度或候选，首片误判与目标物料入包分别检查。

---

### BUG-108 | P1 | 远端重生被识别为纯漂移，回场后仍为空包 | Integrator（运行场景与死亡恢复）| 状态: OPEN
- **L2 H3 附魔槽有死亡补装的正例，也影响过程归因。** EP05 L123 死，第二次采木执行时 stdout L255 确认回同场景且 `restock=True`，当次只收 4 云杉原木，L145 石器验收通过、集内加分 0；该场景的补装礼包含钻镐，石器真包阈值接受石镐及以上。保留课程实际验收，过程自制与恢复分别记账。 [死亡](../runs/l2_long1_20261122/hy_5e_3_enchant_anvil/20261122-104325-554686.events.jsonl#L123) · [补装](../runs/l2_long1_20261122/hy_5e_3_enchant_anvil/stdout.log#L255) · [当集末工具](../runs/l2_long1_20261122/hy_5e_3_enchant_anvil/20261122-104325-554686.events.jsonl#L142) · [补装配置](../scripts/l3_wild_endurance_runner.py#L1122) · [验证门槛](../src/mc_agent/harness/curriculum.py#L224)

- **冲刺 V2 H8 的恢复坐标日志需再读最终站位。** Watchdog 两条 `death_return` 打印 Y78，但恢复批次先使用场景地表参考点，再调用 `prepare_scene_ground`，后者把铁探针重新放到 Y16。EP01 重生和无镐回执都在 Y16，Fleet 最终也在 Y16，终包石镐 2。这里实证是短时空包、锚内多死及终包恢复。验收记录前后实际站位、恢复批次及背包，勿把日志打印的参考点算作实际回场地层。 [第一条恢复日志](../runs/5ease_bare_sprint_v2_20261122/hy_5e_8_probe_iron_y16/stdout.log#L39) · [首集无镐仍在 Y16](../runs/5ease_bare_sprint_v2_20261122/hy_5e_8_probe_iron_y16/20261122-061821-222590.events.jsonl#L103) · [重建真实入口](../scripts/l3_wild_endurance_runner.py#L1352) · [Y16 定位](../scripts/l3_wild_endurance_runner.py#L783)

- **场景候选 `e5976d4`，固定于 `1220d54` 复验**。真实看门狗和 `BotSession` 死亡账，模拟同一 26,809m 远端复活。已由正常事件通路登记、实际空包时，集成 `cea55e4` 走漂移不补，候选补 1 套；重复登记同 ID 不多补，后续同地点新 ID 可以再补。持续死态也从重复清包改为只补一次。两个缺口仍在：只变化 `status.last_death` 而事件账未入的远端空包、已入死亡账但有 3 件残留，均仍按漂移；同一个旧死态隔正常存活采样再重放，24s 后可再次清掉中间取得的 3 件。普通漂移和已知箱前清包对照保持物料。验收补状态最近死亡信号与事件账共用去重、逐次确认清包及发放，须从实际运行版本取复验。 [V9 远端重生](../runs/5ease_virgin_eff_v9_20261122/hy_5e_7_probe_iron_y16/20261122-033733-469573.events.jsonl#L31) · [候选定门](../../s-scenarios/scripts/l3_wild_endurance_runner.py#L1575) · [死亡账](../../s-scenarios/src/mc_agent/bot.py#L52) · [状态读取](../../s-scenarios/src/mc_agent/bot.py#L254)

- **离线多采样复验**。`6db598c` 与 `bbd64b8` 看门狗针对同一死亡标志保留 20s 冷却，没有消费标记。夹具首次死后补给，于第 24s 重放同一事件再次清包，擦掉中间所得的 3 钻。历史死亡标志下的普通漂移、存箱空包均不清。`f015185` 的 DFS 脚下守卫候选 20/20 通过，集成基线 19/20，差异是深落差支撑仍能发挖掘；V9 H7 末次原子动作缺日志，尚不能区分踩空或挖脚下。补给幂等的验收应包含同死亡重放超过冷却和同坐标不同死亡标志。 [看门狗](../scripts/l3_wild_endurance_runner.py#L1500) · [脚下候选](../../s-combat/src/mc_agent/harness/guardian.py#L4451)

- **实机**。V9 HY_5E_7 EP02 L31 从挖掘段跌死、在远端重生。看门狗写 `reason=drift_26809m restock=False`，确实拉回 Y16。之后放木板、合木及掘进均无装备或材料可用，独立包为空。 [死亡重生](../runs/5ease_virgin_eff_v9_20261122/hy_5e_7_probe_iron_y16/20261122-033733-469573.events.jsonl#L31) · [纯漂移回场](../runs/5ease_virgin_eff_v9_20261122/hy_5e_7_probe_iron_y16/stdout.log#L116) · [空包与木料](../runs/5ease_virgin_eff_v9_20261122/hy_5e_7_probe_iron_y16/20261122-033733-469573.events.jsonl#L156) · [空包与镐](../runs/5ease_virgin_eff_v9_20261122/hy_5e_7_probe_iron_y16/20261122-033733-469573.events.jsonl#L197)
- **判定与离线复现**。固定 `6db598c`，用真实 `SceneWatchdog._tick` 和本地模拟状态。只观察远端重生过渡后 alive/ready、HP 恢复及空包会走 `drift` 且不补给。加上一次被采到的 dead 状态，或同样空包位于场景锚内，则走 `death_return` 并补给。现场缺看门狗的全部采样，回执与离线路径吻合。 [临时未就绪](../scripts/l3_wild_endurance_runner.py#L1496) · [近锚库存骤降](../scripts/l3_wild_endurance_runner.py#L1542) · [回场参数](../scripts/l3_wild_endurance_runner.py#L1563)
- **最小修复与验收**。识别每次未消费的死亡事件 ID，或可重读且去重的最近死亡状态，死亡后远端出生仍在回场时补一套。验收同一死亡重复采样只补一次、普通漂移不补、存箱不补，以及旧装备复拾数字可对账。

---

### BUG-112 | P1 | 背包有铁块，铁器备料仍找粗铁矿 | Slice C（备料）+ Slice D2（课程） | 状态: OPEN

- **场景候选 `21e7876` 增加模型知识。** 固定父与候选，`铁块` 查询及铁器成就说明现在能找到拆锭建议。两版真实显式 actor 加模拟传输均能单次 `mc_craft` 让 1 铁块转 9 铁锭，无需工作台。候选仅改知识库，自动备料的粗铁优先分支仍待回放。 [知识](../../s-scenarios/src/mc_agent/kb/data_1_21_11.py#L729) · [自动入口](../src/mc_agent/skills/ensure.py#L1014)

- **Showcase OB3 EP02–03 实物库存。** EP02 L347 发放铁块只入箱 1，L756 独立包实有铁块 3、铁锭 0。EP03 自动备料先从箱子取出 1 铁锭，接着找燧石失败。本集缺打火石的直接断点是燧石。已有铁块且没有可取锭的分支由下方离线用例独立证实。课程已把铁块每块计 9 铁，真实备料路径的 `iron_ingot` 却直接查粗铁上游。 [独立背包](../runs/5ease_ob_showcase_20261122/nju_ob_3_village/20261122-044552-287845.events.jsonl#L756) · [下一集取锭及找燧石的步骤](../runs/5ease_ob_showcase_20261122/nju_ob_3_village/20261122-044839-054406.json#L16102) · [课程铁储](../src/mc_agent/harness/curriculum.py#L2979) · [备料路径](../src/mc_agent/skills/ensure.py#L1014)
- **隔离掉燧石缺口，`0731a37` 离线复现。** 铁块 3、燧石 1、钻镐，真实前置解析只扫普通或深板岩铁矿，零合成、零炉窗，回 `requires_world_hunt`。用同一模拟世界及真实显式工具，先拆铁块 1 成 9 锭，再合打火石，实包铁块 3→2、净铁锭 +8、打火石 +1，零炉务。最小修复按缺口拆已持铁块，核对铁锭真实入包再回原配方。若拆块不足，沿用剩余缺口的采／炉。若燧石也缺，单独报燧石缺口。用打火石和三锭铁镐分别验实际扣料和成品，另跑没有铁块的粗铁熔炼正例。

---

### BUG-115 | P1 | `mc_skill_mine_dfs` 切片内单次 `movement_not_confirmed` 即中止整片 | Slice A（采矿引擎）| 状态: OPEN

- **W20 60m OB1 钻层回执同时有实收与回节点失败。** EP10 先到 Y−54，下一片在 Y−55 实收两钻后返回 `cannot_reach_nearest_node`，同片另有落脚拒绝；再调用没有新钻，终止码相同。保留入包正进展与后续导航失败，复验当前层按实支撑接回或另走一条安全边。 [到层](../runs/l1_w20_20261122/nju_ob_1_bare_60m/20261122-232732-283976.events.jsonl#L150) · [实收二钻](../runs/l1_w20_20261122/nju_ob_1_bare_60m/20261122-232732-283976.events.jsonl#L184) · [再失败](../runs/l1_w20_20261122/nju_ob_1_bare_60m/20261122-232732-283976.events.jsonl#L215)
- **Long2 OB3 EP08 采掘末片有整体位移，原回溯边仍断。** L560 铁 DFS 耗 51.427s，从 `[-11486.5,-52,11265.5]` 到 `[-11495.5,-46,11273.5]`，铁、钻增量均 0，末步报 `movement_not_confirmed`。L590 下一片欲接 Y−44，回 `backtrack_blocked`，已读目标头脚为空气，踏面缺实读；原地重置也需先回上层。复验当前站位能否短接有效边，不把整片位移和最后一步确认混记。 [移动前后](../runs/l2_long2_20261122/nju_ob_3_village/20261122-115954-440547.events.jsonl#L560) · [下一片回溯](../runs/l2_long2_20261122/nju_ob_3_village/20261122-115954-440547.events.jsonl#L590) · [重置门](../runs/l2_long2_20261122/nju_ob_3_village/20261122-115954-440547.events.jsonl#L621) · [回溯回执源码](../src/mc_agent/skills/mining_dfs.py#L2003)
- **L2 H4 深水 EP02 同集后段仍有一片早停。** L338 先取得有效施工，85.320s、破 35、包增圆石 34、Y44.1→33、钻 0。下一片 L367 15.612s，圆石 3＋安山岩 4、Y33.4→32，随即 `movement_not_confirmed`，切到普通阶梯最低已见 Y19。前案需验单边失证后仍可在片内另试，兼顾有真施工的片和真实入钻。 [有效下降](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L338) · [再次早停](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L367) · [后续阶梯](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L529)
- **L2 H4 深水 EP02 复现钻 DFS 第一次下井早停。** 90s 请求中 L32 实耗 14.561s、施工封口 1、实入圆石 5、钻石 0，Y63.4→61.8 后 `movement_not_confirmed` 整片返回，树仍是同一个井口节点。之后南、北阶梯各实进两阶再报该码，均未进钻层。验实际站位能否在当前片预算内接回有效边，复验钻石按净入包计。 [DFS 回执](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L32) · [北阶梯](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L170) · [南阶梯](../runs/l2_long1_20261122/hy_5e_4_deep_water/20261122-103527-155405.events.jsonl#L197)

- **实证（Wave 10 `a20ec5e` & Wave 11 `8cf1d18`）**。Wave 10（2× 钻 + 2× 铁，6m，0 死亡、0 `shaft_exhausted`）中 3 个切片被 `movement_not_confirmed` 提前打断（`hy_5e_2` 11s/+3 钻、`hy_5e_3` 77s/+13 铁、`hy_5e_4` 73s/+7 铁）；Wave 11 `hy_5e_1_bare` 8 次 `mine_dfs` 中被 `movement_not_confirmed` 中断 **3 次**，累计多付 3 次 LLM 往返与半片预算，最终 20 分钟整点刚好卡在烧铁收尾差 ~30s 未合出铁镐。 [W10 H2](../runs/l1_hy4_w10_20261122/hy_5e_2_probe_diamond_y54/stdout.log) · [W10 H3](../runs/l1_hy4_w10_20261122/hy_5e_3_probe_iron_y16/stdout.log) · [W11 H1](../runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/stdout.log)
- **根因**。`mining_dfs.py` 在巷道掘进或回溯移动中遇到单次 `movement_not_confirmed` 时直接将其作为本片 `stop_reason` 抛回给 LLM，未在切片内部执行 1 次轻量安全脱困（清理头/脚阻挡方块或回退半格重走）。 [采矿引擎](../src/mc_agent/skills/mining_dfs.py#L6190)
- **最小修复与验收**。在 `mining_dfs.py` 内部对单次 `movement_not_confirmed` 做 1 次安全轻脱困并重试当前步，仅当同坐标连续 2 次失败再上报 `movement_not_confirmed`；补离线单测覆盖“首步 `movement_not_confirmed` 脱困后继续掘进”，实测 90s 切片因 `movement_not_confirmed` 提前中断率显著下降。

---

### BUG-117 | P1 | 野外非 Day 1 主选址无安全林地时整槽无备用区域 | Slice D2（场景）+ Integrator（Runner／开波）| 状态: OPEN

- **影响与实据。** W13 H1 专项钻探、H2 专项铁探、OB2 裸装未开任何集，三份 Fleet 均在选址结束为 `not_ready`、0.0s。分别是 slot 530、532、522，报同一类 `Could not find a wooded dry landing`。H4 的村庄和要塞配对另记 SCENE-003。这直接减少了采矿修复的实载暴露。 [H1 钻探](../runs/l1_w13_20261122/hy_5e_1_probe_diamond_y54/stdout.log#L1) · [H2 铁探](../runs/l1_w13_20261122/hy_5e_2_probe_iron_y16/stdout.log#L1) · [OB2 裸装](../runs/l1_w13_20261122/nju_ob_2_bare/stdout.log#L1)
- **定位与最小修复。** 非 Day 1 只对分配中心尝试 9 个偏移，全部筛掉即在开集前终止。日志未保存每个位置是地形、位移、海洋／低河面还是缺原木被筛。用每槽独立的有界备用区复用安全、林地和隔离验算，记录主备区域、筛选原因及是否实进首集。 [候选和筛选](../scripts/l3_wild_endurance_runner.py#L222) · [退场写终档](../scripts/l3_wild_endurance_runner.py#L2565)
- **验收。** 离线覆盖主区九点全部失败后转到合格的独立备用区，以及所有候选都不安全或与活槽重叠时明确失败。同类实测三槽都要有首集指纹和有效时长，保留干地、林地及同波区隔佐证。终档 `not_ready` 仍计作未入场。
- **候选 `7d43095` 已由 D2 提交，未进入 W13。** D1 固定该提交的选址函数，用 522/530/532 模拟首备用成功、次备用成功、27 点无树全耗尽，9/9 通过。待实机确认替补区域的实际开集与隔离。 [候选选址](../../s-scenarios/scripts/l3_wild_endurance_runner.py#L223)
- **W14a 首次实机备用区两槽开跑，铁钻槽仍缺。** HY4_3 洞穴与 OB2 裸装均用 `region=2` 实开集。H1 钻探 27 点全 `unsettled_or_outside`，H2 铁探 21 点同类、6 点缺原木，两槽终档 `not_ready` 及 0s。该标签暂把无干地观测与候选落点偏出合在一起，补同类现场选点与筛选分因。 [洞穴](../runs/l1_w14a_20261122/hy_5e_3_cave_combat/stdout.log#L1) · [裸装](../runs/l1_w14a_20261122/nju_ob_2_bare/stdout.log#L1) · [钻探失败](../runs/l1_w14a_20261122/hy_5e_1_probe_diamond_y54/stdout.log#L1) · [铁探失败](../runs/l1_w14a_20261122/hy_5e_2_probe_iron_y16/stdout.log#L1)
- **W15 实载 `b4984e0`，五个林地选择槽全部开集。** 铁钻各回主区、0 拒绝，门户和 OB1 分别明确有 `wet_landing` 3／5 次后同区成功。该波候选中心和 W14a 不同，待终档再核有效时长。H4 零集为区域配对失败，另计 SCENE-003。 [W15 钻探](../runs/l1_w15_20261122/hy_5e_1_probe_diamond_y54/stdout.log#L1) · [W15 铁探](../runs/l1_w15_20261122/hy_5e_2_probe_iron_y16/stdout.log#L1) · [门户分因](../runs/l1_w15_20261122/hy_5e_3_probe_obsidian_portal/stdout.log#L1) · [OB1 分因](../runs/l1_w15_20261122/nju_ob_1_bare/stdout.log#L1)

- **W19 60m HY7 找到主区和干燥候补仍未入场。** 选址在 region 2 成功，随后另找到 bare reserve。两次正式验证时玩家均实际留在候补位 `151038.5,64,233130.5`，日志确认主世界、着地、无水无岩浆，距期望主点 `151032.5,62,233190.5` 水平 60.30m。官方终档记 `not_ready`、0.0s、0 集。重置调用回执缺失，需记录请求后的实坐标；两次主点不符后，复用已验证候补仍须重新确认可站、边界和资源，再以候补作为场景锚点开始首集。 [候补与两次验证](../runs/l1_w19_20261122/glm_hy_5e_7_bare_60m/stdout.log#L2) · [终档](../runs/l1_w19_20261122/glm_hy_5e_7_bare_60m/wild-endurance-wild-20261122-212857.json#L17) · [重置和验证顺序](../scripts/l3_wild_endurance_runner.py#L3336)

---

### BUG-119 | P1 | 六个 `bare` 槽手动 `mc_skill_staircase` 耗 1608s，铁器课仍绕过 DFS | Slice D2（课程）+ Slice A（采矿）| 状态: OPEN

- **W19 60m HY9 验到保留门的负对照。** EP06 只有木镐，三次分别破两片石、目标入包均 0，回执实际指向低 2–3 格的圆石并建议向下 3–4 步。三次向下均被铁器课门即时挡住，改铁 DFS 又因木镐等级拒绝，主动拾取实入桦木而圆石 0。下一集转石器后真正收足三石，实制石镐。验收保留禁止手动开新铁井，另给未齐石镐时可达的低阶石料流程，以及有已验证安全通道时的短距离拾取；无安全路线时要求换落点。 [建议低位拾取](../runs/l1_w19_20261122/hy_5e_9_bare_60m/20261122-214149-312174.events.jsonl#L230) · [门限](../runs/l1_w19_20261122/hy_5e_9_bare_60m/20261122-214149-312174.events.jsonl#L251) · [镐级拒绝](../runs/l1_w19_20261122/hy_5e_9_bare_60m/20261122-214149-312174.events.jsonl#L279) · [次集复原](../runs/l1_w19_20261122/hy_5e_9_bare_60m/20261122-214751-169195.events.jsonl#L46) · [课程门](../scripts/l3_wild_endurance_runner.py#L2704)

- **W16 GLM HY7 的熔铁前沿绕开硬门。** EP04 目标为 `minecraft:story/smelt_iron`，在铁矿水域四次手动下梯仍实际派发，后续三笔 DFS 零铁。源代码只匹配铁镐成就，需单测等价首铁路径，保留石器阶段的露石下梯和安全撤离。 [手动探矿](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_7_bare_3600s/20261122-195204-198317.events.jsonl#L117) · [铁课绑定](../scripts/l3_wild_endurance_runner.py#L3210)

- **W16 GLM HY1 EP03 实载拦截并接入铁产。** L110 手动下梯回 `iron_descent_requires_dfs`、0 阶，下一动作即铁 DFS。三片分别入粗铁 1/1/1，Y68→35，约 212s 工具时间。首次真实覆盖 `64b0fc2` 在裸装铁课的重定向；仍需正式波统计、其它槽与上行脱困对照。 [拦截](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L110) · [首次入铁](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L122) · [第三次入铁](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_1_bare_1200s/20261122-193704-169391.events.jsonl#L161)

- **实证（L2 #1–#3 六份 `bare`，配置 360 分钟，正式实跑 211.3 分钟）**。手动 `mc_skill_staircase` **1608s**（`down×46` / `up×27`），`mc_skill_mine_dfs` 约 913s（`explore×16` / `climb_out×9`）。D2 按目标拆出铁器课下梯 29 次／729.2s、上梯 17 次／354.6s。石器期下梯另有 14 次／205.6s。Long3 两个裸装共八集铁器课，两槽没有启动任一 DFS 或熔炼，回执未显示挖到粗铁。OB1 EP09 在树冠和空落脚之间多次改梯，三步实下后仍被掉落边缘挡住。 [Long3 实下后停](../runs/l2_long3_20261122/nju_ob_1_bare/20261122-130917-018464.events.jsonl#L446) · [铁器课工具提示](../src/mc_agent/harness/curriculum.py#L455)
- **根因与最小修复**。`bootstrap_iron_age` 仅靠 prompt 文案写“禁止手动 `staircase(down)`”，工具集仍暴露 `mc_skill_staircase` 且无硬拦截；同时地下缺木/缺煤触发 `climb_out` 死循环。建议在 `bootstrap_iron_age` 工具白名单中收缩或硬护栏拦截 `staircase(down)`，引导一律走 `mc_skill_mine_dfs(explore)`。
- **D2 `64b0fc2` 已合 `4af3392`，D1 定向 7/7，待实载。** 首铁镐 `bare` 用真实 compact 注册器验证三种向下参数均在移动前收到 `iron_descent_requires_dfs`，上梯、柱升、石器课及其它工具实例仍调用原 handler。和 BUG-026 联验近敌下的安全脱困，以及正常备齐工具后是否实际改走 DFS 并有粗铁入包。 [集成路径](../scripts/l3_wild_endurance_runner.py#L2435) · [EP09 近敌下梯](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-125254-507775.events.jsonl#L196)

---

### BUG-120 | P1 | `bare` 选址接受红树林沼泽 + `harvest_logs`/`mine_blocks` 20 次 `no_drops_collected` 白挖 | Slice D2（选址）+ Slice C（采集）| 状态: OPEN

- **W16 实载拾取工具可发，地形接近仍失败。** GLM HY5 EP01 破石 2、圆石包差 0，三次实际拾取反复选到同三堆圆石，始终原地 `[109036.5,70,109205.5]`。最近目标低 1 格且实距 2.63m，旧回执却列进 `moved_to`。接着两次裸 XZ 移动同样原位，两处挖泥土、一次挖空都没获圆石。D1 用相同坐标重放，两次 API move 回相同站位时 `moved_to=1`、包差 0；真实贴近对照实入 1。EP02 备料换楼梯路线并造出石镐。验收用实际位移和入包，失败时改安全落脚点或换可达石面，明确目标物与垃圾。 [破石零入](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_5_bare_1200s/20261122-193420-162816.events.jsonl#L70) · [原位拾取](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_5_bare_1200s/20261122-193420-162816.events.jsonl#L86) · [EP02 成镐](../runs/l1_w16_glm_bare_20261122/glm_hy_5e_5_bare_1200s/20261122-193815-344477.json#L38) · [回执投影](../src/mc_agent/skills/library.py#L3115)

- **实证（L2 #1–#3 六份 `bare`）**。Long3 OB1 在红树、树根和泥水地形重复 `no_drops_collected` 与 `on_tree_canopy`，实跑 42.9 分钟没有调用 DFS。终档生态字段空白。六槽切出**砍木 12 次、采石 8 次**成对零目标入包，合耗 **461.044s**、报破 **37 格**。20/20 包含附近掉落点和拾取指引，但 `dc58897` 两套裸装 compact 工具都不注册 `mc_skill_collect_drops`。Long3 OB2 EP01 回执已有低一层的圆石点，走到 XZ 半径内没有实际位移，继续 `dig` 只证实该格是空气。OB1 七次砍木零入中仍有同层掉落，需同时覆盖贴近路线和高度。 [红树零入](../runs/l2_long3_20261122/nju_ob_1_bare/20261122-131700-717876.events.jsonl#L220) · [圆石落点](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-123942-938337.events.jsonl#L155) · [挖空气](../runs/l2_long3_20261122/nju_ob_2_bare/20261122-123942-938337.events.jsonl#L219)
- **最小修复与验收**。D2 选相同隔离规则下的可走干地林区。C／Integrator 给石器和铁器课提供可实际调用的拾取工具，优先实扫掉落和核验安全接近。分开测同层、低层、水中以及隔墙不可达；按独立背包净增验证，无法安全拾取时应结束这趟尝试。 [石器及铁器白名单](../src/mc_agent/harness/curriculum.py#L436) · [掉落提示生成](../src/mc_agent/skills/library.py#L3544)
- **D2 `bfa4756` 已合 `4af3392`，待实载。** 增加红树林和支撑筛选。D1 用两版原件各装一次真实 compact 工具表再 prepare，`dc58897` 石器／铁器的拾取均缺，候选两课均可派发。部署后核对首发落脚、真实掉落实收、首石镐后能否切入铁课。 [集成筛地](../scripts/l3_wild_endurance_runner.py#L319) · [集成拾取](../scripts/l3_wild_endurance_runner.py#L2399)

---

### BUG-127 | P1 | 下界 `obtain_blaze_rod` 63 分钟 0/6：要塞窄桥近战追浮空烈焰人致 `attack_zero_hits×13` + `combat:unsafe_landing×10` | Slice B（战斗）+ Slice D2（课程）| 状态: OPEN

- **实证（L2 #2–#3 `nether_fortress`）**。`nether/find_fortress` 仅 83–93s 即 `+30` 分通关，但随后 6 集 `nether/obtain_blaze_rod`（合计 **3799s = 63 分钟**）**0 次完成**：Bot 已站在要塞内发起 59 次 `mc_skill_combat` + 14 次 `mc_attack`，但因烈焰人浮空且要塞窄桥无护栏，触发 `mc_attack:attack_zero_hits ×13`、`mc_skill_combat:unsafe_landing ×10`、`mc_move_to:movement_not_confirmed ×21`、`mc_skill_tunnel:nether_tunnel_floor_unsupported ×19`。 [L2#2 FORT](../runs/l2_long2_20261122/hy_5e_2_nether_fortress/curriculum-*.json) · [L2#3 FORT](../runs/l2_long3_20261122/hy_5e_2_nether_fortress/curriculum-*.json)
- **最小修复与验收**。对浮空/高处 `blaze` 在 `obtain_blaze_rod` 与 `mc_skill_combat` 中优先使用 `mc_shoot`（弓/弩）远程点杀，仅在同平面且有护栏/实心桥面时近战；无箭矢时引导卡要塞拐角等烈焰人降落再原地 `chase=false` 挥刀。

---

### BUG-128 | P1 | 下界 `obtain_ancient_debris` 68 分钟 0/9：模型用手动 `staircase×43`/`tunnel×14` 替代 `mine_dfs(ancient_debris, Y=15)` | Slice D2（课程）+ Slice A（采矿）| 状态: OPEN

- **实证（L2 #1–#3 `nether_fortress` & `nether_bastion`）**。9 集 `nether/obtain_ancient_debris`（合计 **4109s = 68 分钟**）**0 次完成**；工具统计为 `mc_skill_staircase ×43`、`mc_dig ×21`、`mc_skill_tunnel ×14`，而专用的 `mc_skill_mine_dfs` 仅 19 次，导致大量时间耗在 `staircase:cave_or_drop_ahead ×8`、`tunnel:bridge_gap_ahead ×7`、`nether_tunnel_floor_unsupported ×6`。
- **最小修复与验收**。与 `BUG-119` 同构：`obtain_ancient_debris` 强制收敛到 `mc_skill_mine_dfs(action='explore', target='ancient_debris', target_y=15, max_duration_s=90)`，限制手动 `staircase`/`tunnel` 在下界盲挖。

---

### BUG-133 | P1 | `void_geofence_blocked` 报错未附返回围栏中心方向，致地下 `Y=-54` 反复撞墙 52 次死锁 | Slice D2（Geofence）+ Slice C（技能回执）| 状态: OPEN

- **实证（`L2#2_MID` 60m）**。Bot 在 `[352084, -54, ...]`（`distance=218.2 > safe_radius=212.0`）尝试 `mc_skill_staircase(direction="north", vertical="up")` 与 `mc_skill_tunnel(direction="north"/"east")` 均触发 `void_geofence_blocked`（全组累计 **52 次**），但错误回执未给出“朝哪个基本方向返回中心安全区”，导致模型在地下深层反复朝外侧撞墙死锁。
- **最小修复与验收**。`void_geofence_blocked` 回执必须计算并附带 `inward_direction`（指向围栏中心的合法主方向，如 `"south"`/`"west"`）及明确提示“请改朝 `{inward_direction}` 移动/爬梯返回安全区内”。

---

### BUG-137 | P1 | `mc_skill_scout_surface` 遇河/浅水即 `in_water` 退出（18 次），致 `day2_village_search` 21 集耗 88.6 分钟低效 | Slice C（地表探索）| 状态: OPEN

- **实证（L2 #1–#3 全部 21 集 `day2_village_search`，合计 5316s = 88.6 分钟）**。共调用 `mc_skill_scout_surface` 86 次，第一大失败为 **`in_water ×18`**，连带触发 `mc_skill_unstick:unsafe_landing ×6`、`water_escape_failed ×5`。
- **最小修复与验收**。`scout_surface` 航向遇水域时在技能内部自动沿岸偏转 ±45°/±90° 绕行或换干燥主方向推进，而非直接以 `in_water` 中止返回给 LLM 反复原地重调。

---

### BUG-148 | P1 | 下界堡垒 `mc_skill_combat(mode='auto')` 追击悬空烈焰人连续 5 次撞 `unsafe_landing`，未回退远程/原地反击 | Slice B（战斗）| 状态: OPEN

- **实证（Wave 17b `hy_5e_11_nether_fortress`）**。模型连续 5 次调用 `mc_skill_combat({"mode":"auto","max_time_s":15})`，全部被拒 `ok=False, code='unsafe_landing', message='追击方向缺少安全落脚点，请在有支撑的位置接战'`——原因系烈焰人悬空于堡垒桥外/岩浆上方，`auto` 模式试图近战贴脸追击导致落脚校验必败，却未自动回退为远程弓箭或原地举盾反击。
- **最小修复与验收**。`mc_skill_combat(mode='auto')` 在近战追击方向无安全落脚点且目标为烈焰人/远程悬浮怪、背包有弓箭时，自动切换远程射击或原地掩体反击，而非连续 5 次零动作返回 `unsafe_landing`。

---

### BUG-156 | P1 | 裸装主锚把干地和 48m 有木当作可从空手起步，W20 HY9 在 Y40 开局连死 | Slice D2（场景）+ Integrator | 状态: OPEN

- **W21 HY9 先木的实机正例。** 补跑实载 `273167bef3ccef19` 对应 `8bb199b`。HY9 60m 改场 slot 280，选择阶段挡下三个不稳地面和一个无木点，主锚 Y114，记录首木 Y116、`log_dy=2`。EP01 actor 开包已有 2 云杉木，随后两笔实增各一。史例 W20 同名槽是 slot 250 的 Y40，当前观察验到首木与起步；同一地形和多死候补仍分别验。 [选锚证据](../runs/l1_w21_v1321_20261123/hy_5e_9_bare_60m/stdout.log#L3) · [备料后实包](../runs/l1_w21_v1321_20261123/hy_5e_9_bare_60m/20261123-002529-506615.events.jsonl#L20) · [再收原木](../runs/l1_w21_v1321_20261123/hy_5e_9_bare_60m/20261123-002529-506615.events.jsonl#L186)

- **影响与实据。** W20 `hy_5e_9_bare_60m` slot 250 主点 `236821.5,40,250030.5` 通过入场，另找到 Y66 候补。首集独立背包空；16m 扫木为零，上行试探零步 `missing_step_support`，挖脚下失败。首 actor 第 52.92s 确认近战死亡，回到原锚 Y40.1。下一集又在 8.54s 内近战死。原迹未记录主列顶方，也未验证 Y66 候补可以徒手取得木。 [主点与候补](../runs/l1_w20_20261122/hy_5e_9_bare_60m/stdout.log#L3) · [首集空包](../runs/l1_w20_20261122/hy_5e_9_bare_60m/20261122-224343-191100.events.jsonl#L20) · [扫木与上行](../runs/l1_w20_20261122/hy_5e_9_bare_60m/20261122-224343-191100.events.jsonl#L124) · [首死及重生](../runs/l1_w20_20261122/hy_5e_9_bare_60m/20261122-224343-191100.events.jsonl#L262) · [二次死亡](../runs/l1_w20_20261122/hy_5e_9_bare_60m/20261122-224510-938454.events.jsonl#L33)
- **源码定位与最小修复。** 主选点只查脚下、脚、头三格及 48m 搜木是否非空，没有校验木料高度、可达性或开局通向露天的路线。入场验证确认坐标、干地和人造支撑。应在裸装选锚时对首份木做有界可达性核验，记录高差及探测拒因，再选同槽合格主点或候补。启用候补作为首生点之前重验同一裸装资源门。 [三格支持](../scripts/l3_wild_endurance_runner.py#L286) · [主点搜木](../scripts/l3_wild_endurance_runner.py#L373) · [入场支撑](../scripts/l3_wild_endurance_runner.py#L2188) · [候补首生入口](../scripts/l3_wild_endurance_runner.py#L3403)
- **验收。** 离线复现干地 Y40 且能搜到隔顶或不同层木的假阳性。保留低海拔开阔地和有平缓出口的安全点。实机同类裸装首集应有可达木点或明确改选证据，并用首块实采入包复验，没有验到的候补继续记录为待暴露。
- **D1 离线复现。** 在整合 `a24ea67` 用真实选锚函数配模拟现场，固定 slot 250、足下实石、Y40／41 净空、Y42 起有岩顶，48m 搜到同列 Y70 木。选锚仍返回 Y40，实际块读只有 Y39／40／41 及一次重复 Y39。现场岩顶保留为未观测，离线复现只验证该缺口。

---

### BUG-157 | P1 | 裸装从找钻切到 Y16 找铁，旧待挖楼梯仍向钻层施工 | Slice A（DFS）+ Slice D2（课程）| 状态: OPEN

- **影响与实据。** W20 30m GLM HY6 EP07 找钻下探到 Y−17，收尾未得钻。EP08 改派 `day1_night_iron_prep`，两次实际请求均为铁矿 Y16，雷达也已换成 Y21 铁簇。两次却沿旧井同一父子链从 Y−17 经 Y−35 挖到 Y−52，耗 **170.773s**、破 **112 格**，粗铁零入包，两次都报 `ok=true/time_slice_progress`。第二次末端仍有向西下挖 2 步的待办，正好指向旧钻层 Y−54。该集 `answered`、零验证，下一集仍派备铁。 [上集找钻末段](../runs/l1_w20_20261122/glm_hy_5e_6_bare_30m/20261122-230149-781732.events.jsonl#L157) · [第一片铁请求](../runs/l1_w20_20261122/glm_hy_5e_6_bare_30m/20261122-231047-962092.events.jsonl#L17) · [第二片与旧待办](../runs/l1_w20_20261122/glm_hy_5e_6_bare_30m/20261122-231047-962092.events.jsonl#L35)
- **源码定位。** 从深层切到更浅目标时，现有护栏只在已到旧目标层附近时拒绝；中途 Y−17 高于旧 Y−54＋8，因此接收新目标并更新全局 `target_y`。已有井保留待挖分支，随后挑首个 `pending`，执行优先使用该分支保存的 `branch_target_y`。超时新节点又携带这个层位继续。 [切换护栏与全局层](../src/mc_agent/skills/mining_dfs.py#L5746) · [待办选择](../src/mc_agent/skills/mining_dfs.py#L6262) · [实际下梯派发](../src/mc_agent/skills/mining_dfs.py#L6394) · [续切片](../src/mc_agent/skills/mining_dfs.py#L6578)
- **最小修复与验收。** 切层时保留已验证的旧井回程边，并在派发前核对待挖楼梯与新井层或当前明确锁定的目标矿是否一致。立足于 Y−17 又接铁 Y16，返回显式换层／回程指令，或沿安全已建路线向上；若还要继续找钻，由课程继续派钻并用真实包差验收。使用实迹的旧 Y−54 待办和余量足够的两片复验，铁请求不得执行旧下梯。保留同目标连续下探、已到 Y−54 的保护、铁→钻换层和有真实目标矿证据的偏离层位开采。
- **D1 离线复现。** 用生产 `MiningDFSEngine.run`、原测试体素世界及模拟传输，实跑旧 Y−54 待挖分支。Y−17 给铁 Y16，树目标变 16，仍实破到 Y−54、零粗铁。Y−17 继续找钻的对照仍正确下探；从 Y−54 改铁则报 `layer_switch_upward_refused`，零破格。现有课程夜间备铁门省略当前深度和在挖目标，并明确写入铁 Y16。 [DFS 入口](../src/mc_agent/skills/mining_dfs.py#L5393) · [现有深层反切夹具](../tests/test_mining_dfs.py#L2523) · [夜课派发门](../src/mc_agent/harness/curriculum.py#L1735)

---

> **[环境基线同步 | 2026-11-23 00:10]** `5ease` 侧 `mc-agent-api` 已于 23:50 从 `1.3.19` 升级至上游 **`v1.3.21`（`a9d5ceb`）**（备份 `D:\mc-server\api\src.bak-1.3.19`，`:18900` 正常服务）；`feat/v2-integrated` 已含本地适配 `3f1a148`（`find_blocks` 指定 `types` 支持 `128` 格、`move_to` 支持 `range=0` 精确对位与 `allow_partial`、`createSafeGoto` 及掉落物默认 `range=0.5`）。Wave 21 起所有实机轨迹均基于 `v1.3.21` API。
- **D2 选课器复验。** 同 Y−17 保有铁镐，白天选继续找钻。天黑、开探村且还没做 Day 2 寻村时，缺盾或 `obtain_armor` 任一就改派备铁；两个齐备或关闭探村后继续钻课。正式 HY6 EP07 `cooled_down=false`。转派瞬间缺独立包，保留实际缺装是哪一件待验。 [夜课门与优先级](../src/mc_agent/harness/curriculum.py#L1735)
- **A 候选的 D1 定向复验。** 在 A 工作树 DFS 文件对象 `4bda9a3` 上通过 6 项。Y−17 和 Y−54 向上切铁均零破格，旧节点、旧路径途中点、磁盘重载的三组连续两片都保留回程边并按 Y16 派发，高差内实测铁矿可生成新候选。整合仍为 `7f84599`，本轮暂无实载证明。 [上行保护与换层用例](../../s-mining/tests/test_mining_dfs.py#L3856)
