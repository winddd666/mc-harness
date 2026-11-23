# `mc-agent-api v1.3.14` 细粒度动作与战斗策略人工测试清单

> 本清单覆盖升级至 `mc-agent-api v1.3.14` 后（新增 `/control "wait": false` 实时按住、单进程 Bot 隔离、`55ms` 单 tick `/posture` 切盾等能力）需要实机验证的全部 **5 大类、16 项** 细粒度动作与对怪策略。
> 所有可自动化项已拆分为独立子命令收拢在 [`scripts/probe_micro_combat_v1314.py`](../scripts/probe_micro_combat_v1314.py) 中，**默认不运行，完全按需单项触发，零消耗大模型 API 额度**。

---

## 一、实测环境与前置准备

1. **打开实景旁观**：浏览器访问 [`http://127.0.0.1:8765/`](http://127.0.0.1:8765/)（可选切换第三/第一视角），肉眼实时观察动作流畅度与中箭/击退情况。
2. **确认没有其他 Agent 在运行**：[`http://127.0.0.1:8770/api/state`](http://127.0.0.1:8770/api/state) 中 `running == false`。
3. **测试工具选择**：
   - **纯底层物理/战斗脚本实测（零 LLM 消耗）**：在普通终端用 `uv run python scripts/probe_micro_combat_v1314.py <用例名>` 单项运行（会在高空隔离石台 `(790..830, 119..125, 790..830)` 测试并在结束后自动清理并传回原位/恢复原背包）。
   - **结合 `:8770` Web 控制台测单句指令**：给 Bot 明确指令验证工具调用链；注意这会消耗模型 RPM。

---

## 二、五大类 16 项待测动作与策略全表

### 第一类：`v1.3.14` 实时控制与抢占底座（3 项）

| 编号 | 测试种类 | 核心接口 / 原语 | 预期物理表现与坑点 | 人工测试与判定标准 |
|---|---|---|---|---|
| **1.1** | **`control(wait=false)` 实时保持、无缝变向与急停** | `POST /control` + `GET /status` | 1. 传 `"wait": false` 应约 `1ms + RTT` 立刻返回，同时 `status.held_controls` 非空且角色继续奔跑；<br/>2. 连发新方向 `control` 自动接管，无需先 `/stop`；<br/>3. 传 `{"wait": false}` 立即刹停。 | 运行 `exp1_realtime_baseline`：确认 `control_wait_false_rtt_ms < 80ms`、`speed_during_sprint > 4.5`、`held_after_empty_release == null`。 |
| **1.2** | **`posture` 单 tick 切盾 / 潜行 / 踩水** | `POST /posture` | `v1.3.12+` 从 `150ms` 降到 `55ms`（1 tick）；注意**举盾时移速会跌至 ~20%**，且只有正面朝向射手才格挡。 | 运行 `exp1_realtime_baseline` / `exp7_water_and_stop`：检查 `posture_shield_on_ms`、`blocking: true`，以及水下 `posture(jump=true)` 后是否停在 `submerged: false` 水面。 |
| **1.3** | **`stop` 锁释放后无缝反击** | `POST /stop` -> `POST /control` | `v1.3.8+` `/stop` 等旧动作锁释放后才返回（不再像以前撞 `busy`）。 | 运行 `exp7_water_and_stop`：长 `move_to` 中途发 `/stop` 后立刻发 `control(hit=1)`，验证 `stop_result.released == true` 且不报 `busy`。 |

---

### 第二类：近战微操极限（4 项）

> [!IMPORTANT]
> 实机读取生物真实剩余血量不能看 `get_entities`（服务端不下发），必须看 RCON `data get entity @e[...] Health` 核实真实伤害值（如铁剑平砍 `6.0` vs 暴击 `9.0`；石斧平砍 `9.0` vs 暴击 `13.5`）。

| 编号 | 测试种类 | 核心写法与物理陷阱 | 预期效果与待测重点 | 人工测试与判定标准 |
|---|---|---|---|---|
| **2.1** | **双阶段跳劈暴击（Jump-Crit 1.5x）** | **陷阱**：单次调 `control(jump=true, hit=1)` 第 1 刀在 `t=0`（离地前）就挥出，**必不暴击**；且按住 `sprint` 会变成疾跑击退斩而**取消暴击**。<br/>**正统写法**：`look(entity_id)` -> `control(jump=true, ms=550, wait=false)`（禁 `sprint`）-> 等 `0.34–0.44s` 下落期 -> `control(hit=1, ms=60, wait=false, stop_on=[])` | 在空中最高点（`~0.28s`）后、`vy < 0` 下落期挥刀稳定触发原版 `1.5x` 暴击与粒子。 | 运行 `exp2_jump_crit`：`delay_sweep` 中找出铁剑稳定打出 `9.0`、石斧打出 `13.5` 的最佳 `delay_s`（确认单次调用对照仅 `6.0`、`sprint=true` 对照仅 `6.0`）。 |
| **2.2** | **移动跃进跳劈（Moving Jump-Crit）** | 距怪 `~3m` 时 `forward=true, jump=true, sprint=false, wait=false` -> `sleep(best_d)` -> `look(entity_id)` -> `hit=1` | 验证动态移动下空中锁头追刀的暴击命中率，避免靶子微动挥空。 | 观察 `exp2_jump_crit` 中 `moving_crit_success_rate`（目标 `>= 4/5` 打出 `9.0`）。 |
| **2.3** | **疾跑击退斩（W-Tap Sprint-Knockback）** | `control(forward=true, sprint=true, hit=1, ms=180–200, stop_on=[])` | 把贴脸怪一刀推开 `2.5–3+` 格争取拉扯距离（不可对 `{NoAI:1b}` 实体测，须用 `movement_speed=0` 保留物理击退的生物）。 | 运行 `exp3_knockback_and_creeper`：比较 `knockback_compare.sprint` 与 `normal` 的真实水平击退格数。 |
| **2.4** | **武器原版冷却节奏（Sword vs Axe）** | 剑间隔 `>= 625ms`；石/木/铁斧 `>= 1111–1250ms`；利用 1 格斧头高爆（`9.0` / 暴击 `13.5`）接切剑补刀 | 实测两段 `control(hit=1)` 不同间隔下是否吃满 `100%` 伤害，或“石斧先手暴击 `13.5` + 铁剑 `6.0` 两刀秒 20 血怪”。 | 在靶场用 RCON 对铁傀儡连续砍 2 刀对比 `interval=300ms` vs `650ms`（剑）/ `1300ms`（斧）总伤。 |

---

### 第三类：典型敌对生物专项博弈策略（4 项）

| 编号 | 怪物种类 | 待对比策略与技术要点 | 预期指标与验证方法 | 归属脚本 / 操作方式 |
|---|---|---|---|---|
| **3.1** | **骷髅（小白 Skeleton）4 策略横评** | API 的 `hurt` 事件带 `source_entity` 可精确定位射手；重点测 4 种：<br/>① **A 基线裸冲**：直接 `POST /attack`；<br/>② **B 举盾顶盾推进**：`posture(shield=true)` 盯看骷髅贴到 `<=3.1m` 瞬间放盾挥砍；<br/>③ **C 无盾 `±32°` Z 字折线疾跑**：靠横向切换诱骗小白弹道预判射偏；<br/>④ **D 两格石柱卡视野诱敌**（面前摆 `1x2` 圆石挡射线逼小白走近 `<3.5m`）+ **`/shoot` 弓箭对狙**。 | 统计 4 种打法的 **耗时、中箭次数、总扣血**；重点验证地下前期无盾时用 **1x2 柱卡视野** 或 **Z 字折线** 能不能大幅降损。 | 运行 `exp4_skeleton_strategies` 查看 `4A/4B/4C/4D` 完整对比表。 |
| **3.2** | **苦力怕（Creeper）无伤风筝** | **核心差异**：API 的 `/get_entities` **没有 `fuse` 膨胀计时字段**！不能等看膨胀才撤，必须固定打节奏：**逼近至 `<=3.2m` -> `sprint+hit` 一刀推开并打断蓄爆 -> 立刻反向 `sprint+jump` 拉开 `>4.5m` 至少 `0.7s` 再回头**；有弓时优先 `/shoot`。 | **零引爆（地板无坑）、零受伤、2–3 刀稳杀**；同时对比 `attack(max_hits=1)` 与 `control(sprint+hit)` 哪种击退更稳。 | 运行 `exp3_knockback_and_creeper` 查看 `creeper_kite` 两次实战数据。 |
| **3.3** | **末影人（Enderman）安全收割** | 末影人高 `2.9m` 且被盯看会瞬移：实测**头顶造 `2` 格高矮棚**（地面清 3x3，顶在 `y+2`）站棚下瞄准身体/脚砍杀，或**脚下倒一格水护身圈**防其贴脸。 | 末影人卡在棚外打不到 Bot，不瞬移乱跑，稳定掉落 `ender_pearl`。 | 手动在靶场搭 `3x3`、`y+2` 天花板棚，RCON 刷 `enderman` 测砍杀安全性与珍珠掉率。 |
| **3.4** | **蜘蛛 / 洞穴蜘蛛 / 烈焰人** | 蜘蛛宽 `1.4` 高 `0.9` 且能爬墙：验证**不能只靠 2 格高台躲蜘蛛**（会爬上来，需 3 格或屋檐防爬边），以及洞穴蜘蛛靠 `1x1` 窄口/半格封口堵杀；下界烈焰人靠立柱卡三连火球冷却窗口用 `/shoot` 反杀。 | 确认蜘蛛在无屋檐垫高台上确实能爬上来、有挑檐/半格口则完全被隔绝。 | 人工 RCON 分别放 `spider` 与 `blaze` 实测 `control` 与 `shoot`。 |

---

### 第四类：地形几何杀与防坠微操（3 项）

| 编号 | 测试种类 | 核心物理与坐标机制 | 预期结果与关键风险 | 人工测试与判定标准 |
|---|---|---|---|---|
| **4.1** | **`1x2` 矿道半身封口修脚（Gap Slash）** | **组 A（封头留脚缝）**：在 `y+1` 放石块，`y` 留空；Bot 站距墙 `~1.6m` 并开 **`posture(sneak=true)` 降眼高至 `1.27`** 透下缝斜砍怪物腿部（若站太近、不蹲，`eye=1.62` 会被 `y+1` 头块挡住左键！）。<br/>**组 B（封脚留头缝）**：`y` 放石块，`y+1` 留空，直立平砍。 | 对比两种封口面对 **僵尸+骷髅混合追兵** 时的受击差异：验证“封头留脚缝 + 蹲伏”是否能做到僵尸进不来且骷髅也射不中（几乎 0 伤全清）。 | 运行 `exp5_tunnel_gap_slash`：看 `foot_gap_block_head` vs `head_gap_block_foot` 的 `hits_connected`、`killed_all` 与 `damage_taken`。 |
| **4.2** | **原地起跳垫高 2 格 vs 3 格清群怪（Pillar Cheese）** | 1. 起跳初速 `vy=0.42 b/tick`，脚底完全离开当前格（`dy >= 1.001`）仅在起跳后 **`170–340ms`**！利用 `control(jump=true, wait=false)` + `sleep(0.20s)` + `place` 实测成功率（避免 `blocked_by_self`）；<br/>2. 对比垫 `2` 格与垫 `3` 格面对地面 3 只僵尸时的打/被打双向可达性（3 格高眼 `y+4.62` 距僵尸头 `y+1.95` 约 `2.67m <= 3.0m`）。 | `2` 格时偶尔被僵尸跳起或贴边摸到；`3` 格应实现 **100% 0 受伤** 且低头仍能挥刀击杀全部地面僵尸。 | 运行 `exp6_pillar_cheese`：核对 `pillar_2_blocks` 与 `pillar_3_blocks` 的 `pillar_ok`、`hits_connected`、`damage_taken`。 |
| **4.3** | **潜行安全搭桥（Sneak Bridging）** | `posture(sneak=true)` 激活后，交替执行 `control(back=true, ...)` / `move_to` 到格边 + `place` | 潜行状态下 Bot 在方块边缘不会跌落，可在空岛间/下界岩浆海上稳定水平延伸路桥。 | 在高空搭 `1` 格宽独木桥并设 `posture(sneak=true)` + `place` 往前延伸 10 格，统计 `place` 失败率与掉落次数（要求 `0` 跌落）。 |

---

### 第五类：流体与高风险方块交互（2 项）

| 编号 | 测试种类 | 替代方案与接口细节 | 预期验证重点 | 人工测试与判定标准 |
|---|---|---|---|---|
| **5.1** | **顺水下悬崖回收水 & 1.21 `oxygen` 阈值** | 放弃 100ms 延迟会死的 MLG 落地水，改测 **站崖边 `/interact(water_bucket)` 倒水 -> 顺水流下落 -> 落地再 `/interact(bucket)` 收回水**；同时测 **1.21 水下 `status.oxygen` 真实满值（`20` vs 旧 `300`）**。 | 1. 验证顺水下 20 格悬崖 + 回收水 0 摔伤；<br/>2. 实测水下憋气时 `status.oxygen` 起始数值，确认 `safety.py` 的氧气告警阈值应否从 `<240` 修正为 `<14`。 | 运行 `exp7_water_and_stop` 先看 `water_oxygen_field_when_submerged.before_oxygen`；再在人工崖边实测顺水下崖+收水桶。 |
| **5.2** | **圆石凹槽模具“水+岩浆桶”速浇下界门** | 由于 `/place` 不支持选面且桶倒液体走 `/interact` 视线射线，测试用 **预先砌好的 4 格高圆石凹槽背板 + 地基**，按固定坐标顺序 `/interact(lava_bucket)` + `/interact(water_bucket)` 原位浇筑黑曜石门框并用 `flint_and_steel` 点火。 | 验证在无钻石镐时，仅靠铁桶+熔岩池+模具脚本在 `~20–30s` 内 **100% 一次性浇出合法可点燃的下界传送门**。 | 在沙盘高空搭模具，调用 `/interact` 序列完成 10 格黑曜石门框浇筑与点火进/出检测。 |

---

## 三、人工按需测试快捷命令汇总

> [!TIP]
> 所有下列命令都通过本地 `.state-beast/tokens.json` 复用已有登录态，**不会触发重新登录、也不会消耗大模型费用**；每次运行会在远离主岛的 `(790..830, 119..125, 790..830)` 临时空岛隔离靶场跑完后自动拆除并传回 `BeastBot_01` 原坐标与原背包。

```bash
# 1. 查看所有可测单项（不连服）
uv run python scripts/probe_micro_combat_v1314.py --list

# 2. 按需单项测试（建议每次挑 1~2 项，搭配 http://127.0.0.1:8765/ 实时看画面）
uv run python scripts/probe_micro_combat_v1314.py exp1_realtime_baseline
uv run python scripts/probe_micro_combat_v1314.py exp2_jump_crit
uv run python scripts/probe_micro_combat_v1314.py exp3_knockback_and_creeper
uv run python scripts/probe_micro_combat_v1314.py exp4_skeleton_strategies
uv run python scripts/probe_micro_combat_v1314.py exp5_tunnel_gap_slash
uv run python scripts/probe_micro_combat_v1314.py exp6_pillar_cheese
uv run python scripts/probe_micro_combat_v1314.py exp7_water_and_stop

# 3. 测试完成后结果会保存至 runs/probe-micro-combat-20261116.json
```

> 后续人工扩展 **3.3（末影人矮棚）、3.4（蜘蛛/烈焰人）、4.3（潜行搭桥）、5.1（顺水下崖收水）、5.2（模具浇下界门）** 时，可直接复用 [`scripts/probe_micro_combat_v1314.py`](../scripts/probe_micro_combat_v1314.py) 中的 `setup_arena()` / `teardown_arena()` / `rcon_batch()` 安全模板新增 `exp8_*`~`exp12_*` 注册进 `EXPERIMENTS` 元组即可。
