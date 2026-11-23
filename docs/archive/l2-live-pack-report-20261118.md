# `[L2-ZeroLLM-Live-Pack]` 实机验收报告（nju-beast / Minecraft 1.21.11）

> 日期：2026-11-18 夜间 ~ 2026-11-19 凌晨
> 分支：`feat/l2-live-pack`（独占 worktree `/home/xzq/projects/mc/.worktrees/l2-live-pack`）
> 实机角色：`MoveTest_01`（Token 缓存于 `/home/xzq/projects/mc/.state-move-test/tokens.json`，`0600`）
> LLM 调用：**0 次**（全部 Python 直连 `mc-agent-api` + RCON 确定性驱动）
> 靶场：高空隔离区 `x 5160..5340, y 145..180, z 5200..5340` + 真实自然地形（plains / forest / river / stony_shore）
> 实机结束已 `forceload remove`、清理船只/掉落物，并保留 5×5 高空停机坪防止坠落

---

## 1. 交付物

| 类型 | 文件 |
|---|---|
| 移动原语 | `src/mc_agent/skills/movement.py` |
| 物品栏大脑 | `src/mc_agent/skills/inventory_manager.py` |
| 容器自愈 + 多炉分拆 | `src/mc_agent/harness/world_memory.py`、`src/mc_agent/skills/library.py`、`src/mc_agent/harness/mc_tools.py` |
| 离线单测 | `tests/test_movement_terrain.py`（17）、`tests/test_inventory_manager.py`（19）、`tests/test_window_and_smelt_split.py`（14） |
| 实机脚本 | `scripts/l2_pack_live_test.py`（RCON/连接/场地公共层）、`scripts/l2_move_live_test.py`（M1–M26）、`scripts/l2_inv_window_live_test.py`（INV/WIN/SM）、`scripts/l2_soak_live_test.py`（自然地形长跑） |
| 实机数据 | `runs/l2-move-full9.json`（31 关）、`runs/l2-inv-window-full6.json`（21 组）、`runs/l2-soak-forest*.json`、`runs/l2-soak-shore2.json`、`runs/l2-live-pack-20261119-044404.json` |

## 2. 离线单测

```
PYTHONPATH=.worktrees/l2-live-pack/src .venv/bin/pytest .worktrees/l2-live-pack/tests -q
253 passed, 1 skipped      （新增 53 条：movement 17 / inventory 22 / window+smelt 14）
```

覆盖：面包屑采样/维度隔离/瞬移断段/倒序回退、软陷阱 `stop_on=["death"]`、细雪优先桶舀、木门/栅栏门、船顶跳岸、`navigate_segmented` 最多两次恢复与 wall/unstick 兜底；99+ 重要物、燃料白名单、保护栈、四级清杂、满包回吸防护、穿甲升阶、双视图、满箱自建箱；窗口空箱自愈、`ApiError.context` 快照、挖箱双表销账、`conversion_jobs` 持久化、双/三炉并联与 `done[].took` 记账、`smelt_items` 向后兼容。

## 3. 实机移动关卡（M1–M27/M29/M30/M31 + M5b，单次全跑 31/31 PASS，0 死亡）

| 关卡 | 场景 | 耗时 | 血量 | 关键证据 |
|---|---|---|---|---|
| M1 | 10m 浆果丛+蛛网+密竹走廊 | 4.8s | 20→20 | 4 轮 `stop_on=["death"]` 破障 |
| M2 | 3×3×2 细雪坑（无皮靴） | 7.4s | 20→20 | 桶舀 `powder_snow_bucket` 脱出 |
| M3 | 关门木屋 + 1.5 格栅栏院 | 4.4s | 20→20 | 栅栏门/木门均由 `interact` 打开（`open:false→true`） |
| M4 | 8 格无底深渊潜行搭桥 | 12.9s | 20→20 | 8 块圆石逐格贴放，结束 `sneaking=false`，0 坠落 |
| M5 | 7 格石崖登顶 | 30.2s | 20→20 | `dig_staircase_up` 实抬 8 格 |
| M5b | 全新 8 格石崖下山 | — | 20→20 | `dig_staircase_down` 实降 8 格，0 摔伤 |
| M6 | 3 层厚树叶封路 | 2.9s | 20→20 | 自动破叶，无 `stalled` |
| M7 | 40m 水池渡船（1 格高岸） | 21.5s | 20→20 | 折线 4 段→贴岸下船→跳岸→拆船回收 |
| M8 | L 形双层盲肠洞面包屑回退 | 2.9s | 20→20 | 栈内 4 点倒序回退入口 |
| M9 | 66m 单宽 bedrock 走廊 + 6 格无底缺口 | 27.6s | 0 伤 | `path_not_found`→`gap_6_at_9`→自动搭桥→到达 |
| M10 | 5 级 1 格台阶 + 平台（禁挖） | — | 20→20 | `step_climb` 实抬 4 格 |
| M11 | 6 格高台水桶下降（禁挖） | — | 20→20 | `water_descend` 156→150 |
| M12 | 8 格深渊仅 2 块垫脚石 | — | 20→20 | 明确拒绝 `missing_item`，不冒险 |
| M13 | 2 格高石岸渡船 | — | 20→20 | 水中垫 1 块台阶后登岸 |
| **M14** | **真实 plains 80m 穿越** | 21.4s | 20→20 | 自然地形 88m，无伤害 |
| **M15** | **真实 river 渡船** | 27.9s | 20→20 | 找岸 27m→乘船 75m→远岸下船 |
| **M16** | **真实 forest 70m 穿越** | 19.0s | 20→20 | 树木/树叶/坡地，实际 68m |
| M17 | 铁门（无红石） | 0.16s | — | 拒绝开启、0 动作、不盲跳 |
| M18 | 岩浆上方潜行搭桥 | — | 20→20 | 4 块砖跨 6 格岩浆，`in_lava=false` |
| M19 | 12 格断崖（无阶梯/水/镐） | — | 20→20 | 明确拒绝，原地不动 |
| M20 | 6 格断崖 + 中途 3 格台阶 | — | 20→20 | 两次 3 格安全跳（156→153→150） |
| M21 | 1×1×3 竖井 | — | 20→20 | 挖阶梯脱困到 Y+5 |
| M22 | 冰盖下溺水 | — | 20→20 | 头顶 `ice` 破除成功 |
| M23 | 蛛网堵门的木屋 | — | 20→20 | 先破蛛网再开门 |
| M24 | 无门栅栏 | — | 20→20 | 挖穿两格栅栏通过（不盲跳） |
| M25 | y=30 地下盲肠 + bedrock | — | 20→20 | `navigate_segmented` 两次 `backtrack_breadcrumbs` 后结构化报错 |
| M26 | 真实 river 往返 3 次 | 75.3s | 20→20 | 3 次渡船全部下船+回收，0 伤害 |
| M27 | 河中 `recover()` 宽水诊断 | 13.5s | 20→20 | 浮面→3 格外放船→33m 渡河→上岸，`diagnosis=wide_water` |
| M29 | 无船无木料渡水 | — | — | 明确 `missing_item`，0 移动 |
| M30 | 陆地目标（无水） | — | — | 明确 `no_water`，0 移动 |
| M31 | 河中连续 3 次宽水恢复 | 152.4s | 20→20 | 3 次 `recover()` 均 `wide_water→渡船→上岸` |

## 4. 自然地形长跑（0 LLM，持续导航）

| 场景 | 时长 | 尝试 | 到达 | 死亡 | 恢复 | 里程 |
|---|---|---|---|---|---|---|
| plains 环线 | 2 min | 10 | 10 | 0 | 0 | 428m |
| forest 环线 | 10 min | 36 | 35 | 0 | 2 | 1673m |
| forest 环线（加固后） | 6 min | 21 | 20 | 0 | 2 | 1041m |
| stony_shore 环线（加固后） | 5 min | 14 | 12 | 0 | 0 | 518m |
| forest 环线（大半径） | 20 min | 47 | 43 | 0 | 0 | 2867m |
| forest 环线（超时修复后） | 12 min | 34 | 33 | 0 | 2 | 1973m |

唯一失败点为森林边缘的**浮冰/冰面水塘**：`packed_ice` 边缘 + 悬空，路径 `stalled`，两次 `dig_level_tunnel` 无效后按契约返回阻塞原因（不死亡、不乱挖）。冰面水塘策略（优先绕行或 `escape_water`）列入后续。

## 5. 实机物品栏 / 容器 / 熔炉（21 组全部 PASS）

| 组 | 场景 | 实测结论 |
|---|---|---|
| INV1 | 36/36 满包 + 钻石/绿宝石/铁镐 | 背抛+走开 2 步，丢 4 格杂物，0 回吸，贵重物全保留 |
| INV2 | `/equip` 槽位探测 | `chest` 非法（`ERR_ASSERTION`），`head/legs/feet/torso/off-hand` 有效；护甲值 15 |
| INV3 | 双视图计数 | 随身 3 石 vs 可达 9 石（在途 6），箱内铁 7/煤 5 |
| INV4 | 满箱归仓 | 27 槽满箱→自动造新箱→钻石/绿宝石入库并同步记忆 |
| INV5 | 刷怪笼地牢箱 | 6 格内有 `spawner` 的箱子被跳过，返回 `no_safe_container` |
| INV6 | 35 杂 + 1 面包（饥饿<16） | 只清杂，面包不进丢弃计划 |
| INV7 | 300 圆石 + 泥土 + 凝灰岩 | Tier2 保留 ≥96、圆石 ≤192 |
| INV8 | 满箱再存 | `section_full` + `error.window` 27 槽快照，缓存回写 1728 石 |
| INV9 | 自动升阶 | 皮革→铁胸甲（torso）、钻石头盔、副手盾，一次成功 |
| WIN1 | 空箱 `missing_item` 自愈 | 二次取物报错，缓存 `{}`；挖箱后双表销账 |
| WIN2 | 天然箱摸底 | 木桶建档 `{coal:3}`，刷怪笼旁箱子跳过 |
| SM1 | `smelt_start(cobblestone,12)` | 3.0s 返回，双炉各 6，账本 12 |
| SM2 | 65s 后 `smelt_collect` | 收齐 stone×12，账本归零 |
| SM3 | 非法燃料 + 20 块 | 床被拒（`missing_fuel`）；20 块自动三炉 |
| SM4 | 无炉且圆石不足 16 | 优雅 `need_furnace` |
| SM5 | 无炉 + 自带木板 | 自建工作台→补造 2 炉并排→投料 |
| SM6 | 满包收取 | `inventory_full`（"no room ... nothing was moved"），炉内存货不丢；腾格后收齐 |
| INV10 | 下界金甲规则 | 钻靴+金靴同槽时仍保留并穿上金靴，铁胸甲不受影响 |
| INV11 | 归仓保留随身装备 | 只存钻石/绿宝石；铁镐/盾/面包 8/圆石 64/水桶全部留在身上 |
| INV12 | 满包工具汰弱留强 | 木镐与多余石镐被淘汰，铁镐+2 石镐保留（Tier3） |
| SM7 | 熔炼中熔炉被拆 | `no_window`（"air did not open a GUI"），账本按契约销账为 `{}` |

## 6. 实机服务端怪癖与修复（已写回代码）

1. **`/equip` 槽位名**：真实服务端不认 `chest`（`ERR_ASSERTION: invalid destination: chest`）；只认 `head/torso/legs/feet/off-hand/hand`。`auto_equip_best` 以 `torso` 优先并记录成功别名。
2. **窗口失败载荷**：`window/act` 失败仍返回**执行后完整窗口快照**（`error.window` + `failed_op`/`done`），项目侧落在 `ApiError.context`；成功/失败双分支都覆写 `WorldMemory.containers`。
3. **空箱语义**：`sections.container: []` 必须写 `{}`，否则旧红石/铁锭残留（WIN1/INV8 实证）。
4. **`/drop` 回吸**：只背向转头不够，海带/海草/蛛网会被脚边吸回；首批丢弃后需背向走 1–2 步。
5. **栅栏门门洞**：门格上方若仍是 `oak_fence`（1.5 格碰撞），即使 `open=true` 也过不去；建场需清空门上方一格。同一门被 `interact` 两次会反关，已按坐标去重并先读 `open`。
6. **放船距离**：`use_item` 目标必须是**最上层水方块**；距玩家 **1–2 格会挂起 >15s**，**≥3 格**稳定成功；挂起后必须 `/stop` 释放动作锁。
7. **游泳态放船会挂起**：改为先 `escape_water`/游到岸，否则返回 `cannot_reach_bank`（不冒险）。
8. **拆船会把 Bot 拽回水里**：`attack(boat)` 自动寻路回水；回收后需再执行一次上岸（`escape_water` + 冲刺跳）。
9. **自然河岸判定**：岸边常是浅水（脚下水、下方泥土），`firefly_bush`/草可站立，水不可；`locate biome river` 的坐标常在水中央，需按 8 方向找干岸并把镜像目标吸附到真正干列。
10. **1 宽桥过冲**：`/control` 长脉冲会推到桥沿，下一步 `/place` 直接 `no_support`；改为比例短脉冲（0.32m 容差）。
11. **寻路跳跃能力**：`move_to` 能自行跨 **3 格**宽缺口（M9 早期被绕过），**6 格**无底缺口才 `path_not_found`；恢复需向前扫 16 格并在缺口 >1 格时先走到崖边。
12. **分段推进的“假进展”**：24m 跳点只挪 1–2m 必须判为停滞并交给局部恢复，否则会耗尽跳数后直接抛错（M25 实证）。
13. **`dig_staircase` 回执**：物理挖完仍可能 `requires_replan`（`missing_step_support` / `cave_or_drop_ahead`）；`climb_or_descend_cliff` 以真实 Y 位移判定。
14. **台阶下降**：必须在**每一级**先走到台沿（不能只在开始时走一次），并在读高度前等待落地（否则读到半空 Y）。
15. **熔炉“假完成”**：燃烧中途 `burning=false` 且 `input>0` 的瞬间不能取物；仅在 `input==0 且 not burning`（或等待预算到期）时收取。
16. **取物数量**：`done[].took` 才是真实数量，计划数量会把账本做穿；SM6 证明满包时 `inventory_full` 不丢炉内存货。
17. **补造熔炉**：裸 `/craft` 会 `need_crafting_table`；`smelt_start` 走 `craft_gear`（自带工作台）并保底 ≥16 圆石。
18. **区块命令静默失败**：未 `forceload` 的区块 `/fill`/`setblock` 无报错也不生效；旧关卡残留地板会造成“走在空气上”的假象。
19. **自然落点**：`tp` 到高空需 `slow_falling` + **稳定落点判定**（`on_ground`/`in_water` + Y 连续两次不变）；刚 tp 后 `in_water` 可能是旧值。
20. **区块流式加载**：落地瞬间 `get_block` 常 `not_loaded`，探测需 retry；否则干岸扫描会误判。
21. **动作锁级联**：一次挂起会让后续全部 `busy`；`/stop` 失败时用 `disconnect → connect → wait_ready` 兜底（shore 长跑实证，恢复后继续）。
22. **未分类阻塞兜底**：`recover` 最后尝试 `dig_level_tunnel`（2 步）或 `unstick`，仍不行才返回结构化阻塞原因。
23. **Tier4 误淘汰主工具（饱和测试抓到并修复）**：旧实现在绝境腾格时只保护"手持/已穿"的装备，背包里的铁镐与备用石镐会被当成散件丢掉。现先计算 **工具/护甲保留集**（镐 1–2 主镐 + ≤2 石镐、剑斧锹锄各 1、未穿最佳护甲每槽 1），Tier4 永不触碰。
24. **水下放船挂起**：在水中且沉在水面下 2 格时放船会 `use_item` 超时；先 `posture{jump}` 浮到水面，再在 **3–5 格**外放船即可稳定成功（M27 实证）。
25. **`move_to` 会游泳**：实测服务端寻路可以直接游过 70m+ 河面，因此"宽水恢复"只在 `navigate_segmented` 失败或显式 `recover()` 时触发（M27 用后者验证派发）。
26. **长距离直连超过 HTTP 超时**：100m 直连 `move_to` 会跑满 45s 客户端超时；现给直连/跳点都带服务端 `timeout`（30s/25s），客户端超时也转换为失败并自动切分段跳点（20 分钟森林长跑从 4 次超时降到 0–1 次）。

## 7. 已知限制

- 浮冰/冰面水塘边缘：`packed_ice` + 悬空会 `stalled`，当前兜底无效（不死亡）；后续应优先绕行/破冰入水再用船。
- `smelt_start` 的 `ready_at` 是 10s/块 + 5s 点火的估算，实机 ±1–2s 抖动；`smelt_collect` 已带收尾等待。
- 多炉上限 3、单炉输入上限 64；`count>=8` 才并联。
- `BreadcrumbTrail` 只在 `navigate_segmented` 成功推进/恢复后采样；`move_to` 内部绕行不逐点入栈。
- 按白名单未改 `ensure.py`/`curriculum.py` 接线，也未在 `mc_tools.py` 新增工具名（只做窗口同步/拆箱销账）。

## 8. 复现命令

```bash
# 离线
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l2-live-pack/src \
  /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/l2-live-pack/tests -q

# 实机（0 LLM；自动建场/清理；Token 复用 .state-move-test）
cd /home/xzq/projects/mc/.worktrees/l2-live-pack
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_move_live_test.py --only all --report runs/l2-move-full9.json
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_inv_window_live_test.py --only all --report runs/l2-inv-window-full6.json
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_soak_live_test.py --minutes 10 --biome forest --report runs/l2-soak-forest.json
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_soak_live_test.py --minutes 5 --biome stony_shore --report runs/l2-soak-shore2.json
```
