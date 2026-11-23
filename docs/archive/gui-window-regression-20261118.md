# GUI / 窗口接口全量实机回归（2026-11-18）

> 环境：nju-beast · Leaves `1.21.11` · `mc-agent-api v1.3.14`  
> 测试 Bot：`GuiTest_01`（独立 `.state-gui-test` Token，不触碰 `BeastBot_01` / `mc-live` / `mc-console`）  
> 总结果：**A–F 六组 33 项检查全部通过**  
> 最终报告：[`runs/gui-window-full-A-B-C-D-E-F-20261118-120840.json`](../runs/gui-window-full-A-B-C-D-E-F-20261118-120840.json)  
> 复跑脚本：[`scripts/gui_window_full_live_test.py`](../scripts/gui_window_full_live_test.py)

---

## 1. 测试方法

测试台位于 nju-beast 主世界远离基地的天空平台（约 `3096, 150, 3096`）。通过 RCON 搭建固定夹具：

- 容器：箱子、木桶、漏斗、潜影盒、投掷器
- 工作台、熔炉、高炉、烟熏炉、砂轮、织布机、信标（3×3 铁块底座）
- 附魔台 + 15 个书架（y+1 环绕，留北侧入口）
- 酿造台、铁砧、锻造台、切石机
- 流浪商人 + 自定义双材料图书管理员（`NoAI` 固定，避免走动）

每次运行前重置玩家背包、经验、效果和天气；测试结束后清理效果并保留测试台，便于复跑。

---

## 2. 分组结果

### A. 容器与 WorldMemory（10/10）

- 箱子：指定槽位批量 `put`（32 圆石槽 0、16 原木槽 5）
- `WorldMemory.update_container_from_window` 快照正确写入 `.state-gui-test/world_memory_GuiTest_01.json`
- 批量 `take` 10 圆石；随后 `window/view` 回读为 22 圆石 + 16 原木
- 缺失物品负例正确返回 `missing_item`
- 木桶、漏斗、潜影盒、投掷器均完成 put/take

### B. 顺序敏感合成台 GUI（4/4）

- 错误槽位组合（木板 + 木棍对角）正确返回 `no_output`
- 2×2 四木板 → 工作台
- 竖排 2 木板（槽 1、4）→ 4 木棍
- 3×3 木镐：3 木板（槽 0/1/2）+ 2 木棍（槽 4/7）→ 木镐

### C. 附魔 / 铁砧 / 锻造 / 砂轮 / 织布机（7/7）

- 附魔台 option 0 / 1 / 2 分别消耗 1 / 2 / 3 青金石并成功附魔
- 铁砧合并两把受损铁剑；铁砧改名成功（`xp_spent >= 1`）
- 锻造台：下界合金升级模板 + 钻石胸甲 + 下界合金锭 → 下界合金胸甲
- 锻造台：Dune 纹饰模板 + 铁胸甲 + 金锭 → 带纹饰铁胸甲
- 砂轮去除附魔并取回物品
- 织布机：白色旗帜 + 红色染料 + `select(0)` → 带图案旗帜

### D. 酿造 / 熔炉三件套 / 切石机（6/6）

- 酿造两段连续：3 水瓶 + 下界疣 → 3 粗制药水；再加糖 → 3 迅捷药水，`brew_time_left=0`
- 熔炉：1 粗铁 + 煤 → 铁锭
- 高炉：1 粗铁 + 煤 → 铁锭
- 烟熏炉：1 生牛肉 + 煤 → 熟牛肉
- 切石机：石头 → 石砖、石头 → 石台阶；非法 option 99 返回 `bad_option`

### E. 村民交易（5/5）

- 流浪商人 `window/view` 读取 options，按 `get_item` 匹配
- 连续 `select(option, times=3)`：1 绿宝石 → 8 深色橡木原木，连续 3 次成功
- 不可用选项负例返回 `option_unavailable`
- 自定义图书管理员双材料交易：5 绿宝石 + 1 书 → 1 附魔书，`payment1/payment2 -> output` 正确

### F. 信标（1/1）

- 3×3 铁块底座 + 信标
- `put iron_ingot -> payment` + `select(0)` 顺序正确
- 随后 `window/view` 读取 `beacon_level=1`、`primary=speed`

---

## 3. 上游 API 发现：连续附魔的窗口背包陈旧

在 `mc-agent-api v1.3.14` 上复现：

1. 第一次 `enchant_item_once` 成功，背包出现附魔物品。
2. 紧接着第二次/第三次调用，`window/act` 的 `done` 仍显示 `select` 命中附魔、`take` 取回成功。
3. 但 `/inventory` 中背包不增加新的附魔物品；表现为窗口背包快照陈旧/取回物品未真正落包。
4. 同类型物品重复附魔、不同类型物品连续附魔均可触发；`1s`/`3s` 等待不能修复。
5. **断开重连后立即恢复正常**；不同物品类型之间偶尔可连续成功，但不稳定。

结论：

- 这是上游 mineflayer 窗口/背包同步问题，不是 `enchant_item_once` 的参数或 sections 错误。
- 生产 harness 不建议直接在循环里连续调用原始 `window/act` 或 `enchant_item_once`。
- 已在 `McToolset.mc_skill_enchant` 中加入**默认落包校验 + 自动恢复**，见下一节。

复现记录：`runs/gui-window-full-C-20261118-113450.json`（未重连，仅 1 件附魔落包）、`runs/gui-window-full-C-20261118-114254.json`（显式重连后 3 件全部落包）、`runs/gui-window-full-C-20261118-115956.json`（自动保护触发 `reconnected_for_sync` 且未双附魔）、`runs/gui-window-full-A-B-C-D-E-F-20261118-120840.json`（全量 33/33，自动保护下 C 组钻石剑触发一次 `reconnected_for_sync`）。

---

## 4. 如何避免：默认校验 + 按需重连

`mc_skill_enchant` 现在默认：

- `verify=True`：附魔前后读取 `/inventory`，要求目标物品的“已附魔数量”至少 +1；
- `reconnect_on_stale=True`：校验失败时执行一次受控 `disconnect -> connect -> ready`，然后**先重新读取背包**：
  - 如果重连后发现附魔其实已经落包，直接返回成功，**不会再次附魔**，避免双附魔；
  - 如果确实是失败，才在重连后的干净会话里重试一次；
  - 重试仍失败则返回 `stale_window: true` / `requires_replan: true`，交给上层决策。
- 返回字段：
  - `inventory_verified`：是否通过背包校验；
  - `reconnected_for_sync`：是否通过重连恢复（附魔已生效）；
  - `retried_after_reconnect`：是否在重连后重试了一次；
  - `stale_window` / `requires_replan`：最终仍失败时的明确标记。

因此调用方只需要：

```text
mc_skill_enchant(item='iron_sword', option=2)
```

不需要自己写“附魔后重连”的循环。若明确希望关闭保护，可传 `verify=false` 或 `reconnect_on_stale=false`，但不建议在连续附魔场景关闭。

补充建议：

1. 连续附魔多件物品时，优先使用 `mc_skill_enchant` 的默认保护，不要直接手写 `mc_window_act` 的 `put/select/take` 循环。
2. 附魔台附近保持安全（重连会短暂离开游戏），或先回到基地/掩体再批量附魔。
3. 如果同一会话中还执行其他窗口操作，发现“`done` 成功但背包不变”时，统一按 `stale_window` 处理：先重连刷新，再判断是否需要重试。
4. 其他 GUI（村民、锻造、切石等）目前未复现同类问题；仍建议关键产物以 `/inventory` 实际落包为准，不要只看 `done`。

---

## 5. 复跑

```bash
# 单组
uv run --offline python scripts/gui_window_full_live_test.py --groups A
uv run --offline python scripts/gui_window_full_live_test.py --groups E

# 全量 A-F
uv run --offline python scripts/gui_window_full_live_test.py --groups ABCDEF
```

脚本期望 `GuiTest_01` 的 Token 已存在于 `.state-gui-test/tokens.json`：

```bash
MC_STATE_DIR=.state-gui-test uv run mc-bot --env-file .env.beast login GuiTest_01
```

> 注意：`GuiTest_01` 与 `BeastBot_01` 使用不同 Bot 名和独立 Token 文件，不会触发单 Token 互斥。

---

## 6. 轨迹深审后的生产收口补丁（2026-11-18）

逐帧复核 `runs/gui-*.json` 全部 26 份原始轨迹后，已合入以下 5 项生产加固（单测见 [`tests/test_gui_atomic_skills.py`](../tests/test_gui_atomic_skills.py)，全量 `195 passed`）：

1. **`compact` 默认生产模式按需解锁安全 GUI 技能**（[`agent.py`](../src/mc_agent/harness/agent.py)、[`curriculum.py`](../src/mc_agent/harness/curriculum.py)）：任务或进度涉及附魔、交易、酿造、箱子仓储、锻造/纹饰、铁砧、切石时自动暴露对应 `mc_skill_*`，保持单轮 schema `< 3000 tokens`。
2. **统一接入 `_run_skill(...)` 容错闭环**（[`mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）：7 个新增 `mc_skill_*` 工具全部走断线/隔离自动恢复与 `pos_after` 位置刷新。
3. **比赛服 30s 重连冷却兼容**（[`mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）：`_reset_window_session()` 快重连若遇服务端冷却限制（`ApiError`），自动回退为 `connect(respect_reconnect_interval=True)` 等待达标重连。
4. **无坐标容器自动补全 `pos` 同步 `WorldMemory`**（[`library.py`](../src/mc_agent/skills/library.py)、[`mc_tools.py`](../src/mc_agent/harness/mc_tools.py)）：服务端 `window` 返回体不含坐标，当调用方仅传 `block="chest"` / `"barrel"` 未传 `(x, y, z)` 时，开窗成功后自动通过 6 格内 `find_blocks` 补全容器坐标写入 `WorldMemory.containers`。
5. **HTTP 空闲连接自愈与铁砧防呆**（[`bot.py`](../src/mc_agent/bot.py)、[`library.py`](../src/mc_agent/skills/library.py)）：修复 `120453` 第 27 项 Node keep-alive 空闲挥手导致的 `httpx.RemoteProtocolError`（重建连接池重试 1 次）；为 `mc_skill_anvil` 增加 `left_enchanted` / `right_enchanted` 筛选及 `114143` 左槽满耐久/互斥附魔 `no_output` 诊断提示。
