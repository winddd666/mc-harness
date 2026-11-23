# 2026-11-14 自主托管远征与技术栈实测报告

- **测试对象与账号**：`5Ease_Bot` @ `https://apiin.nmo.net.cn/api/v1`（校园网直连 MC Agent API `v1.3.7`）
- **测验产物目录**：[`runs/expedition-20261114/`](../runs/expedition-20261114/)
- **停机与离线状态**：已通过 [`scripts/expedition.py`](../scripts/expedition.py) `disconnect` 验证 `{"ok": true, "already_offline": true}`；Bot 最终重生并安全离线停泊于主世界出生点附近 `[-1.5, 107, 1.5]`（`health=20`、`food=20`，末阶段在最终总结等待期间受冻伤/环境伤害清空背包）。

---

## 1. 端点与双模型对比实测

| 对比维度 | DeepSeek (`deepseek-flash@https://api.deepseek.com`) | 校内 Qwen (`default@https://api.nju-aia.com/v1` -> `qwen3.6-35b-a3b`) |
| --- | --- | --- |
| **38 工具全量单轮耗时** | 典型工具轮 `1.09s–1.61s`（`stage5a` 收尾长文 `3.56s`） | 首轮无缓存 `24.59s`（TTFT `~20.8s`）；Req #2 达 `72.85s`；KV 命中后降至 `8.77s–19.72s` |
| **24 工具基线对比** | — | 11-12 基线日志 [`runs/20261112-073921-292729.events.jsonl`](../runs/20261112-073921-292729.events.jsonl) 首字约 `4.3s`（`38` 工具升至 `~20.8s` TTFT） |
| **Prefix KV Cache 实测** | `stage5a` 立即命中且持续上升（`384/7637` -> `7552/8339` -> `14336/14505`，最高 `98.8%`） | `stage5b` Req #1–#3 `cached_tokens=0`；Req #4–#7 稳定命中 `8168 -> 10834 -> 12524 -> 13415`（最高 `93.7%`）；Req #8 因收尾切换 `tool_count=0` 前缀失效重置为 `0` |
| **工具调用稳定性** | 原生 `tool_calls` 稳定，支持并发/多步规划 | 提示词含“`tool_calls`”字样时曾触发正文伪造 JSON 代码块（`stage4c` `finish_reason="stop"`）；移除触发词并加入 `_extract_text_tool_calls` 兜底后，`stage5b` 全程使用原生 `tool_calls` |
| **限流与速率** | 无客户端额外等待，适合高频迭代探测 | 服务端 30 RPM；客户端 `24 RPM` 节流下 `stage5b` 8 轮均 `wait_s=0.0` |

---

## 2. 分阶段远征台账（`runs/expedition-20261114/`）

| 阶段 | 模型 / 状态 | 请求 / 耗时 | 进度数 / 新解锁 | 关键实测结论 |
| --- | --- | --- | --- | --- |
| `scout` | `apiin` / 只读侦察 | `20261113-212326` | `0 / 125` | 出生 `[-6.5, 104, -7.5]`，周边有橡木/白桦、已有工作台 `[-1, 106, -4]`、牛与流浪商人 |
| `stage0-escape-and-wood` | `deepseek` / `answered` | `18` / `183.22s` | `0` | 采集 `birch_log×4, oak_log×2`，合成工作台与木镐；手挖 11 圆石掉落未能拾取 |
| `stage1-stone-age-and-food` | `deepseek` / `answered` | `28/28` / `111.6s` | `1`（含 `story/root`） | 圆石掉落沉入带水深坑触发 `no_drops_collected` |
| `stage2-iron-age` | `deepseek` / `answered` | `32/32` / `87.36s` | `1` | 负坐标 `int()` 截断问题导致角色卡在 `[-3.3, 102, -5.3]` 方块碰撞箱 |
| `stage2b-unstick-and-iron` | `deepseek` / `answered` | `32/32` / `103.64s` | `2` (`+adventure/root`) | 成功脱困位移至 `[5.5, 107, -7.7]`；后续战斗死亡清空背包 |
| `stage3a-kb-progression` | `deepseek` / `runtime_error` | `7/35` / `86.9s` | `2` | 远程跑图至 `[59.5, 112, -25.5]` 遇袭，`low_health=5.33` 触发 L0 紧急停机保命 |
| `stage3b-bunker-stone-age` | `deepseek` / `answered` | `30/30` / `175.65s` | `3` (`+story/mine_stone`) | 开局残血被骷髅击杀；复活后验证 `mc_skill_bunker` 自埋掩体，下挖至 `Y=102` 采石解锁石器时代 |
| `stage3c` (Run 1) | `deepseek` / `runtime_error` | `7` / — | `3` | `--max-tokens 1024` + 16 并发 `mc_dig` 触发 `finish_reason=length` |
| `stage3c` (Run 2) | `deepseek` / `answered` | `32/32` / `112.81s` | `4` (`+story/upgrade_tools`) | `--max-tokens 2048` 恢复；验证 `mc_kb_lookup`、采圆石 21、合成石镐/石剑/熔炉并下探至 `Y=95` |
| `stage4a-surface-and-iron` | `deepseek` / `answered` | `32/32` / `162.41s` | `4` | 向上阶梯掘进遇塌方沙砾（500ms `hurt` 窒息打断 `/dig`）于 Turn #7 `[-2.5, 96.1, -1.5]` 死亡；随后安全复活回地表 `[-0.4, 109, 1.1]` |
| `stage4b-surface-frontier` | `deepseek` / 中断草稿 | `12` / `25.57s` | — | 中途停止，仅保留 `.events.jsonl` |
| `stage4b-surface-iron-and-farming` | `deepseek` / `answered` | `34/34` / `334.9s` | `4` | 验证 `harvest_logs` 背包增量计数与分离式 `iron_ore` 扫描（定位暴露铁矿 `[-27, 120, 28]`）；受 `[0, 101, -2]` 附近 7 个被埋工作台 `windowOpen` 超时阻塞 |
| `stage4c-nju-kb-verify` | `nju` / `answered` | `1/12` / `36.87s` | `4` | 复现 38 工具下 Qwen3.6 正文输出 `tool_calls:` 代码块的问题 |
| `stage5a-iron-and-husbandry` | `deepseek` / `answered` | `30/30` / `168.71s` | `5` (`+adventure/kill_a_mob`) | `mc_skill_unstick` + `mc_skill_staircase` 逃离坑洞至 `[-20.5, 122, 28.5]`；在干净地表一次合成石镐/石剑/熔炉；击杀骷髅解锁怪物猎人 |
| `stage5b-nju-kb-live` | `nju` / `answered` | `8/8` / `333.66s` | `5` | 实证 Qwen3.6 原生并行调用 `mc_kb_lookup('bucket')` + `mc_advancement_frontier(8)`，执行采矿/扫描/脱困/跑图/自稳；暴露 `[-27, 120, 28]` 触发 `protected_region`、`[-60.5, 128, 43.5]` 细雪冻伤与 `/events` 历史死亡事件回放问题 |

**累计已解锁原版进度（5 / 125）**：`minecraft:story/root`、`minecraft:adventure/root`、`minecraft:story/mine_stone`、`minecraft:story/upgrade_tools`、`minecraft:adventure/kill_a_mob`。

---

## 3. 技术栈与知识库架构沉淀

1. **三层“零膨胀” 1.21.11 知识库与按需检索**：
   - **静态结构化底座**（[`src/mc_agent/kb/data_1_21_11.py`](../src/mc_agent/kb/data_1_21_11.py) & [`src/mc_agent/data/advancements_1_21_11.json`](../src/mc_agent/data/advancements_1_21_11.json)）：覆盖 `ITEM_ALIASES`、`RECIPES_1_21`、`ORES_1_21`、`MOBS_AND_MECHANICS` 与 `ADVANCEMENT_PLAYBOOKS`，不塞入大段 System Prompt，稳住 KV Cache 前缀。
   - **按需查表 + 进度前沿规划**（[`src/mc_agent/kb/lookup.py`](../src/mc_agent/kb/lookup.py) `lookup_kb`、[`src/mc_agent/skills/advancements.py`](../src/mc_agent/skills/advancements.py) `compute_advancement_frontier`）：通过 `mc_kb_lookup` 与 `mc_advancement_frontier` 按需返回配方、工具等级、交互禁忌及当下可解锁目标与做法。
   - **JIT 错因微提示**（[`src/mc_agent/kb/lookup.py`](../src/mc_agent/kb/lookup.py#L115-L166) `build_jit_error_hint`）：针对 `need_block_target`、`path_not_found`（提示水平跑图省略 `y`）、`no_recipe` / `missing_ingredients`、`use_window_api`、`stalled`、`protected_region` 直接在工具报错体注入 `kb_hint`。
2. **L0 确定性保命与宏技能库**：
   - **L0 守护**（[`src/mc_agent/skills/safety.py`](../src/mc_agent/skills/safety.py) `check_and_stabilize`）与 **CLI 启停守护**（[`scripts/expedition.py`](../scripts/expedition.py) `--min-start-hp`、`--bunker-first`、`30s` 重连冷却退避）。
   - **宏技能**（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）：`harvest_logs`、`mine_blocks`（保护区位置黑名单、连续无掉落止损）、`craft_gear`、`ensure_crafting_table`、`smelt`、`collect_drops`、`unstick`、`dig_safety_bunker`、`dig_staircase`。

---

## 4. 实测暴露的问题与已落地的 7 项工程修复

1. **负坐标截断卡死**：`unstick` 原使用 `int()` 截断负坐标，改用 `math.floor` 精准定位脚下与碰撞箱格子，并在后退失败时切入前向掘进清除（见 [`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py)）。
2. **垂直挖坑降高困难**：新增 `dig_staircase` / `mc_skill_staircase` 支持上下双向阶梯掘进并在上行缺脚垫石；`ensure_crafting_table` 限制纵向检索 `abs(y - py) <= 2` 并在狭小矿道通过 `_carve_and_place_table` 就近雕槽放置。
3. **塌方沙砾窒息打断 `/dig`**：`/dig` 默认 `stop_on=["hurt","death"]`，500ms 窒息伤害会打断破除头顶沙砾；`unstick` 与 `dig_staircase` 改为 `stop_on=["death"]` 并支持 Falling Gravel 最多 2 次复挖。
4. **`harvest_logs` 掉落静默拾取计数遗漏**：补充调用前背包基线 `inv_before` 差值统计。
5. **夜战残血出生循环**：新增 `mc_skill_bunker`（原地自埋封闭掩体）与 `--min-start-hp` / `--bunker-first` 启动保护。
6. **`find_blocks(limit=20)` 杂块稀释**：避免一次性扫描混淆结果，支持按 `types` 定向扫描暴露矿脉/工作台，且修复 `mc_tools.py` 中 `integer` / `boolean` Schema 类型标记。
7. **被埋工作台 `windowOpen` 劫持 + 校内 Qwen 正文伪调用**：
   - `craft_gear` 遇到 `TABLE_RECIPES` + `"windowOpen"` 时，自动用 `stop_on=["death"]` 清理半径 `5` 内最近 `2` 个疑似被埋工作台、确保背包持有工作台并调用 `_carve_and_place_table` 在身边 `1` 格重放后重试一次（[`src/mc_agent/skills/library.py`](../src/mc_agent/skills/library.py#L554-L587)）。
   - 修改 [`src/mc_agent/harness/agent.py`](../src/mc_agent/harness/agent.py#L38) 提示词去掉诱发正文的 `tool_calls` 字样，并在 [`src/mc_agent/harness/model.py`](../src/mc_agent/harness/model.py#L337-L423) 增加 `_extract_text_tool_calls` 确定性代码块兜底解析（严格校验 `mc_*` 命名与 JSON dict 参数）。
   - 单元测试 `uv run pytest -v` **22 passed**（含新增 [`test_text_tool_calls_fallback_and_window_open_adjacent_table_recovery`](../tests/test_skills_and_providers.py#L802-L923)）。

---

## 5. 遗留问题与后续建议

1. **`/events` 重连历史事件回放**：`stage5a` 与 `stage5b` 首轮工具返回后均注入了先前阶段的历史 `[死亡 pos=...]` / `[受伤 ...]` 事件，导致模型误判“刚死亡并清空背包”。建议在 `BotSession` 重连就绪后主动排水消费一次历史 `/events` 作为基线，仅将新产生事件送入上下文。
2. **保护区铁矿与高寒冻伤防护**：主世界出生点周边半径约 40 格地表 `[-27, 120, 28]` 等暴露 `iron_ore` 在实测中返回 `protected_region`（触发 `protected_blacklisted: 3`）；且雪山顶部 `[-60.5, 128, 43.5]` 存在细雪 `freeze` 持续扣血，L0 保命检查与探索策略需将持续不明原因扣血/冻伤纳入脱离触发条件，优先引导至远离出生点的平缓森林/矿洞区域采集铁矿与开展农牧业。
