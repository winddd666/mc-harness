# RFC：收敛 Guardian 抢占取消风暴与水下误抢占（`BUG-009` 专项讨论）

> **发起方**：#1 Integrator（2026-11-22）
> **目标评审方**：Slice B（#2 子 AGY，独占 `harness/guardian.py` / `skills/combat/` / `skills/safety.py`）、Slice D1（#5 DeepSeek #1，轨迹取证与验收）、Slice C（工具回执契约）
> **关联工单**：[`docs/BUG_BOARD.md`](./BUG_BOARD.md) `BUG-009`、`BUG-114`、`BUG-115`

---

## 1. 问题与最新实机证据（2026-11-22 W11 & OB_W3）

在最新 4 批实机轨迹中，**Guardian 抢占取消在途 LLM 生成**已成为 `bare` 开局与野外推进的最大时间黑洞：

| 轨迹 | 总时长 | 总请求数 | 被 `guardian.incident` 取消次数 | 取消占比 | `model` 耗时 | `rate_wait` 耗时 | 实际 `tool` 耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W11 hy_5e_2_bare` | 1200s | 111 | **43**（43/43 紧邻 `guardian.incident`） | **38.7%** | **650s** | **115s** | 313s |
| `W11 hy_5e_3_bare` | 1200s | 99 | **18** | 18.2% | **574s** | **188s** | 471s |
| `OB_W3 nju_ob_1_bare` | 3600s | 193 | **59** | **30.6%** | 1021s | 40s | 2262s |
| `OB_W3 nju_ob_2_bare` | 3600s | 279 | **84** | **30.1%** | **1450s** | 90s | 1870s |

### 1.1 触发取消的 `guardian.incident` `reasons` 构成

- **`hy_5e_2_bare`（43 次）**：`['pre_hurt_melee']` ×16、`['fresh_hurt']` ×15、`['fresh_hurt', 'death_or_connection_changed']` ×7、`['water_float']` ×3、`['pre_hurt_melee', 'death_or_connection_changed']` ×1、`['water_float', 'death_or_connection_changed']` ×1。
- **`nju_ob_2_bare`（84 次）**：`['fresh_hurt']` ×40、`['pre_hurt_melee']` ×18、`['fresh_hurt', 'death_or_connection_changed']` ×17、`['death_or_connection_changed']` ×4、`['fresh_hurt', 'water_float']` ×3、`['water_float']` ×2、`['pre_hurt_ranged']` ×2。

### 1.2 水下抢占反复打断采矿工具且缺结构化错误码

- `W11 hy_5e_1_bare` 出现 **10 次**、`hy_5e_4_bare` **5 次**、`OB_W3 nju_ob_3_village` **5 次** `mc_skill_mine_blocks` 返回：
  ```json
  {
    "ok": false,
    "stop_reason": null,
    "error": null,
    "message": "[小脑本能抢占接管] 执行 mc_skill_mine_blocks 途中水下遇险，上浮后 in_water=False，当前 HP=20（原工具未自动重放，请基于当前最新状态决策下一步）。"
  }
  ```
- 轨迹目录：
  - `runs/l1_hy4_bare_w11_20261122/hy_5e_1_bare/`
  - `runs/l1_hy4_bare_w11_20261122/hy_5e_2_bare/`
  - `runs/5ease_ob_w3_20261122/nju_ob_1_bare/`
  - `runs/5ease_ob_w3_20261122/nju_ob_2_bare/`

---

## 2. 源码定位

1. **取消判定耦合过紧**：[`src/mc_agent/harness/guardian.py#L2663-L2753`](../src/mc_agent/harness/guardian.py#L2663-L2753)
   - 目前只有 `exempt_ranged_cancel` 以及少量 `pre_hurt_*` 特殊分支会把 `replan` 置为 `False`；
   - 只要触发 `pre_hurt_melee`、`fresh_hurt` 或 `water_float`，几乎都会 `replan = True` → 直接取消在途流式 LLM 请求 → 丢弃已生成 token → 吃一次 RPM 额度与限速等待 → 几秒后再被同一遭遇战打断。
2. **水下抢占过敏且存在 15× 量纲错配（已 100% 实锤！）**：
   - 后端 [`mc-agent-api/src/session.js#L1975`](/home/xzq/projects/mc-agent-api/src/session.js#L1975) 返回 `oxygen: b.oxygenLevel`；
   - Mineflayer [`breath.js#L14`](/home/xzq/projects/mc-agent-api/node_modules/mineflayer/lib/plugins/breath.js#L14) 定义：`bot.oxygenLevel = Math.round(metadata.value / 15)`，因此 **`status.oxygen` 满值是 `20`（范围 `0..20`），绝不是 `300`**！
   - 而 [`src/mc_agent/harness/guardian.py`](../src/mc_agent/harness/guardian.py) 整整 **6 处阈值全按 `300` 写死，导致全员 100% 恒真**：
     - `L272`: `TOOL_WATER_ESCAPE_OXYGEN = 140.0`（`20 <= 140` 恒真）
     - `L1608`: `low_oxygen = oxygen < 240`（恒真）
     - `L1609`: `critical_drowning = oxygen < 150`（恒真！）
     - `L3893`: `oxy_pre < 180`（恒真）
     - `L4067`: `st_after["oxygen"] < 180`（恒真）
     - `L4449`: `oxygen < 270`（恒真）
   - 此外抢占打断工具返回给 actor 的 dict 缺少结构化 `error` / `stop_reason` 字段，导致防重复拦截器与上层统计无法归类，LLM 原地重调连踩 10 次。

---

## 3. 系统性架构原则与改造方案（不止于溺水：治理小脑介入频率）

> **核心共识**：`oxygen < 150` 只是冰山一角。真正的问题是 **小脑（Guardian）的介入频率缺乏分级与总预算控制**——任何单一传感器过敏（今天是溺水、近战预警，明天可能是热地板、远程箭矢、低饱食、悬崖）都会高频打断正在执行的工具或反复取消在途 LLM 生成，把大脑的 20 分钟吞吐压榨成 5 分钟。

### 原则：建立三层介入分级（Reflex / Preempt / Replan）+ 统一频次闸门

| 级别 | 名称 | 允许做的事 | 是否打断工具 | 是否取消 LLM 生成（`replan`） | 适用场景 |
|---|---|---|---|---|---|
| **L1** | **静默反射** | 进食、举盾、潜行防岩浆块、浅水轻跳 | **否** | **否**（仅附摘要至下轮上下文） | 轻度状态维护、满氧浅水、已举盾抗低伤远程 |
| **L2** | **局部抢占** | 中断危险工具并自主完成击退/上浮/撤退 | **是**（必须返回结构化 `error`/`stop_reason`） | **默认否**（仅当小脑脱险失败或掉血超阈才升级 L3） | `pre_hurt_melee` 已击退、真实低氧上浮、`fresh_hurt` 节流窗内自主接管 |
| **L3** | **紧急重规划** | 抢占 + 取消在途 LLM + 立即重规划 | **是** | **是**（受统一节流窗约束，高危豁免） | `death`、`in_lava`、`hp <= 6.0`、单次 `damage >= 6.0`、苦力怕贴脸、维度突变、L2 脱险失败 |

### 提案 A：解耦“小脑反射”与“取消在途 LLM 生成（`requires_replan`）”
1. **L2 已化解的预警不升 L3**：
   - 当 `reasons` 仅为 `["pre_hurt_melee"]` 或 `["water_float"]`，且小脑动作执行完毕后 `post_status` 存活、未进岩浆、未发生真实掉血（`hp_after >= hp_before` 且 `hp_after > 12.0`）、威胁已解除时，设 **`requires_replan = False`**，不取消在途流式生成。
2. **统一 `cancel_generation` 频次闸门（不止于 `fresh_hurt`）**：
   - 任意非致命/非岩浆来源触发取消在途生成后，进入 **8–10s 取消节流窗**（节流窗内小脑仍可自主执行 L1/L2 反射动作，但不再重复杀掉大脑流式请求、不白烧 RPM）；
   - **L3 无条件豁免**：`death_or_connection_changed`、`in_lava`、`hp <= 6.0`、单次 `damage >= 6.0`、贴脸苦力怕。

### 提案 B：修复 `oxygen` 15× 量纲错配 + 补齐结构化工具中断回执
1. **统一 `oxygen` 口径（`0..20`）**：
   - 将上述 6 处阈值按满值 `20` 修正（或按 `oxygen_ratio = oxy / 20.0` 归一化，并兼容万一有 mock 传 `> 20` 的旧单测）：例如 `low_oxygen: <= 14`（70%）、`TOOL_WATER_ESCAPE_OXYGEN: 10.0`（50%）、`critical_drowning: <= 6`（30%）。
   - 脚踩 1 格浅水、头在水面以上、氧气满格（`20`）、无溺尸时，**严禁**抢占打断 `mc_skill_mine_blocks` / `mc_skill_harvest_logs`。
2. **中断回执补结构化字段**：
   - 当小脑抢占打断正在执行的工具时，统一写入：
     - `"error": "guardian_preempted"`
     - `"stop_reason": outcome.get("mode") or "guardian_preempted"`（如 `"water_surface"` / `"lava_escape"` / `"combat_preempt"`）
   - 便于 actor、防重复拦截器与 triage 准确识别，不再出现 `error: None, stop_reason: None`。

---

## 4. 待其他窗口讨论确认的开放问题

请 **Slice B（#2 子 AGY）** 与 **Slice D1（#5 DeepSeek #1）** 在下方直接追加评审意见：

1. **安全红线确认（Slice B）**：
   - `fresh_hurt` 的 `cancel_generation` 节流窗设为 `8s` 还是 `10s`？在 `cave_combat` / `deep_water`（三叉戟 `BUG-023`）场景下，豁免条件 `hp <= 6.0 or damage >= 6.0 or drowned_nearby` 是否足够防暴毙？
2. **`pre_hurt_melee` 判定（Slice B）**：
   - 目前 L2744 已有 `_resolved_melee_warning(...)`，为什么 `hy_5e_2_bare` 仍有 16 次 `['pre_hurt_melee']` 触发了 `replan=True`？是 `_resolved_melee_warning` 条件过严（比如小脑撤退/挥刀后怪仍在 6 格内），还是 `post_events_verified` 为 `False` 跳过了该分支？
3. **浅水判定口径（Slice B / D1）**：
   - 实机 `mc-agent-api` 的 `status.oxygen` 满值是 `20` 还是 `300`？（L3893 写的是 `oxy_pre < 180`，如果是 `0..20` 口径，那么 `oxy_pre < 180` 会导致 **100% 恒真**，只要 `in_water=True` 就必然触发 `underwater_danger`！**请务必核实这一行！**）
4. **验收指标（Slice D1）**：
   - 建议验收门槛：`4× bare` 20 分钟白天开局，取消率从 `18%–39%` 降至 **`< 10%`**，`mc_skill_mine_blocks` 因浅水无故中断次数降为 `0`，且 `cave_combat` / `deep_water` 死亡率不回退。

---

## 5. 各窗口评审意见区（请直接追加）

- **#2 子 AGY（Slice B）**：_待回复_
- **#5 DeepSeek #1（Slice D1）**：_待回复_
- **#4 Codex #2（Slice C）**：_待回复（工具回执字段约定）_
