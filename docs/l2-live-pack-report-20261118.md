# `[L2-ZeroLLM-Live-Pack]` 实机验收报告（nju-beast / Minecraft 1.21.11）

> 日期：2026-11-18 夜间 ~ 2026-11-19 凌晨
> 分支：`feat/l2-live-pack`（独占 worktree `/home/xzq/projects/mc/.worktrees/l2-live-pack`）
> 实机角色：`MoveTest_01`（Token 独立缓存于 `/home/xzq/projects/mc/.state-move-test/tokens.json`，`0600`）
> LLM 调用：**0 次**（全部通过 Python 直连 `mc-agent-api` + RCON 确定性驱动）
> 靶场空域：`x 5160..5340, y 145..180, z 5200..5340`（实测结束已 `forceload remove` 并清理船只/掉落物）

---

## 1. 交付物

| 类型 | 文件 |
|---|---|
| 移动原语 | `src/mc_agent/skills/movement.py` |
| 物品栏大脑 | `src/mc_agent/skills/inventory_manager.py` |
| 容器自愈 + 多炉分拆 | `src/mc_agent/harness/world_memory.py`、`src/mc_agent/skills/library.py`、`src/mc_agent/harness/mc_tools.py`（仅限窗口同步 / 挖箱销账 / 新增技能） |
| 离线单测 | `tests/test_movement_terrain.py`、`tests/test_inventory_manager.py`、`tests/test_window_and_smelt_split.py` |
| 实机脚本 | `scripts/l2_pack_live_test.py`（RCON/连接/场地公共层）、`scripts/l2_move_live_test.py`（M1–M13）、`scripts/l2_inv_window_live_test.py`（INV/WIN/SM） |
| 实机数据 | `runs/l2-move-full.json`、`runs/l2-inv-window-full2.json`、`runs/l2-live-pack-20261119-002222.json`（`runs/` 按仓库约定不入库） |

## 2. 离线单测

```
PYTHONPATH=.worktrees/l2-live-pack/src  .venv/bin/pytest .worktrees/l2-live-pack/tests -q
245 passed, 1 skipped   （其中新增 45 条：movement 15 / inventory 17 / window+smelt 13）
```

覆盖重点：面包屑采样间距/维度隔离/瞬移断段/倒序回退、软陷阱 `stop_on=["death"]`、细雪优先桶舀、木门/栅栏门、潜行搭桥悬空沙拦截与 `finally` 释放潜行、贴岸下船与船顶跳岸、`navigate_segmented` 最多两次恢复；
99+ 重要物白名单、燃料白名单（禁床/木工具/工作台/船/烈焰棒）、四级清杂（含 96 保底 / 192 圆石封顶 / 木镐淘汰 / 缺粮免死）、穿甲升阶与副手盾、双视图、满箱自建新箱；
窗口空箱自愈（`sections.container: []` → `{}`）、`ApiError.context` 窗口快照、挖箱双表销账、`conversion_jobs` 持久化、`smelt_start` 双/三炉并联与 `smelt_collect` 销账、`smelt_items` 签名向后兼容。

## 3. 实机移动关卡（M1–M13，全部 `PASS`，0 死亡）

| 关卡 | 场景 | 耗时 | 血量 | 关键证据 |
|---|---|---|---|---|
| M1 | 10m 浆果丛+蛛网+密竹走廊 | 4.5s | 20→20 | 4 轮 `stop_on=["death"]` 破障，无刺伤打断 |
| M2 | 3×3×2 细雪坑（无皮靴） | 7.4s | 20→20 | 桶舀 `powder_snow_bucket` 成功脱出 |
| M3 | 关门木屋 + 1.5 格栅栏院（带门） | 3.6s | 20→20 | 实测 `oak_fence_gate`/`oak_door` 由 `interact` 打开（`open: false → true`） |
| M4 | 8 格无底深渊潜行搭桥 | 12.7s | 20→20 | 8 块圆石逐格贴放，`sneaking` 结束时为 `false`，0 坠落 |
| M5 | 7 格石崖上山 + 断崖下山 | 30.4s | 20→20 | `dig_staircase_up` 实抬 8 格；下山挖阶梯实降 7 格，0 摔伤 |
| M6 | 3 层厚树叶封路 | 3.0s | 20→20 | 自动破叶，无 `stalled` 死锁 |
| M7 | 40m 水域渡船（1 格高石岸） | 14.1s | 20→20 | 放船→折线 4 段→贴岸 `dismount`→跳岸→拆船回收 |
| M8 | L 形双层盲肠洞面包屑回退 | 2.9s | 20→20 | 栈内 4 点倒序回退到入口开阔平台 |
| M9 | 66m 单宽 bedrock 走廊 + 6 格无底缺口 | 27.9s | 0 伤 | `navigate_segmented` 报 `path_not_found` → 恢复诊断 `gap_6_at_9` → 自动搭桥 6 格 → 到达终点（`segmented: true`，3 段） |
| M10 | 5 级 1 格台阶 + 顶部平台（禁挖） | — | 20→20 | `step_climb` 实抬 4 格，0 摔伤 |
| M11 | 6 格高台水桶下降（禁挖） | — | 20→20 | `water_descend` 156→150，0 摔伤 |
| M12 | 8 格深渊仅给 2 块垫脚石 | — | 20→20 | 明确拒绝 `missing_item`，留在崖沿且潜行已释放（不冒险） |
| M13 | 2 格高石岸渡船 | — | 20→20 | 水中垫 1 块台阶后登岸，船已回收 |

补充：M9 证明"恢复只在 `navigate_segmented` 断点触发、最多 2 次、成功后交回 `move_to`"的状态机在真实服务端生效；M12 证明材料不足时不会无限造桥。

## 4. 实机物品栏 / 容器 / 熔炉（16 组全部 `PASS`）

| 组 | 场景 | 实测结论 |
|---|---|---|
| INV1 | 36/36 满包 + 钻石/绿宝石/铁镐 | 背抛后走开 2 步，丢 4 格杂物，**0 回吸**，贵重物全保留 |
| INV2 | `/equip` 槽位名探测 | **`chest` 非法**（`ERR_ASSERTION: invalid destination: chest`）；`head/legs/feet/torso/off-hand` 有效；副手盾成功，护甲值 15 |
| INV3 | 双视图计数 | 随身 3 石 vs 可达 9 石（箱 0 + 在途 6），铁锭 7/煤 5 仅出现在可达视图 |
| INV4 | 满箱归仓 | 27 槽满箱 → 自动合成并放置新箱，钻石/绿宝石入库并同步 `WorldMemory` |
| INV5 | 刷怪笼地牢箱 | 6 格内有 `spawner` 的箱子被跳过并返回 `no_safe_container` |
| INV6 | 35 杂 + 1 面包（饥饿<16） | Tier1 清杂，面包**未进丢弃计划**，保留 1 |
| INV7 | 300 圆石 + 泥土 + 凝灰岩 | Tier2 生效：丢掉装饰石/泥沙与超额圆石，剩余 128（≥96 保底，≤192 封顶） |
| INV8 | 满箱再存 | `section_full` 真实报错，`ApiError.context` 含 `done/failed_op/window`，快照 27 槽全部回写（1728 石） |
| INV9 | 自动升阶穿甲 | 皮革→铁胸甲（`torso` 别名）、钻石头盔、副手盾，全部一次成功 |
| WIN1 | 空箱 `missing_item` 自愈 | 取走红石后缓存立刻变 `{}`；再取报 `missing_item`（`nothing matching redstone to take from container`），缓存仍 `{}`；挖箱后 `containers`/`pois` 双表删除 |
| WIN2 | 天然箱摸底 | 木桶建档 `{coal:3}`；刷怪笼旁箱子标记 `dungeon_spawner` 跳过 |
| SM1 | `smelt_start("cobblestone", 12)` | 3.0s 内返回（含两次开窗投料），自动并联 2 炉各 6 块，`non_blocking=true`，账本 `{"stone": 12}` |
| SM2 | 等待约 65s 后 `smelt_collect` | 收齐 `stone×12`（`done[].took` 6+6），账本归零、`jobs_remaining=0` |
| SM3 | 非法燃料 + 20 块 | `white_bed` 被拒（`missing_fuel`）；20 块自动分 3 炉并联 |
| SM4 | 无炉且圆石不足 16 保底 | 优雅返回 `need_furnace`，不乱花材料 |
| SM5 | 无炉 + 自带木板 | `craft_gear` 先造工作台→补造 2 台熔炉并排→投料，账本 12 |

## 5. 实机抓到的服务端怪癖与修复（已写回代码）

1. **`/equip` 槽位名**：真实服务端不认 `chest`（`ERR_ASSERTION: invalid destination: chest`），只认 `head/torso/legs/feet/off-hand/hand`。`auto_equip_best` 改为 `torso` 优先并记录成功的别名。
2. **窗口失败载荷**：`window/act` 失败时服务端仍返回**执行后的完整窗口快照**，位于 `error.window`，并带 `failed_op`/`done`；项目 `ApiError` 把这些放进 `err.context`。`container_batch`、`mc_window_view`、`mc_window_act` 现在成功/失败双分支都覆写 `WorldMemory.containers`。
3. **空箱语义**：`sections.container: []` 必须写 `{}`，否则旧红石/铁锭永远残留（WIN1/INV8 实证）。
4. **`/drop` 回吸**：只背向转头不够，海带/海草/蛛网会从脚下被自动吸回；首次丢弃后需向背向走 1–2 步（`control forward` 两次）才稳定不回吸。
5. **栅栏门门洞**：门格上方若仍是 `oak_fence`，即使 `open=true` 也过不去（1.5 格碰撞只开下半）；建场必须清空门上方一格。另：同一门被 `interact` 两次会反向关上，现已按坐标去重并先读 `open` 状态。
6. **放船位置**：`use_item` 必须瞄准**最上层水方块**；瞄准水面之上的空气返回 `no_water`。下船后常站在船碰撞箱顶（`on_ground=false`），需 `jump+forward` 登岸；**2 格高石岸**还需先在水中垫 1 块台阶（M13 实证）。
7. **1 宽桥过冲**：`/control` 长脉冲会把身体推到桥沿，下一步 `/place` 直接 `no_support`。改为按距离比例短脉冲（目标 0.32m 容差）后，M4/M9 稳定 0 失败。
8. **寻路跳跃能力**：`move_to` 能自行跨越 **3 格**宽缺口（M9 早期版本被它绕过），**6 格**无底缺口才 `path_not_found`；恢复诊断需向前扫 16 格，且当缺口在 1 格外时先走到崖边再搭桥。
9. **`dig_staircase` 回执**：物理挖完仍可能返回 `requires_replan`（`missing_step_support` / `cave_or_drop_ahead`）；`climb_or_descend_cliff` 以真实 Y 位移判成功（M5 上山 +8、下山 -7）。
10. **熔炉"假完成"**：燃烧中途会出现 `burning=false` 且 `input>0` 的瞬间；提前取物会把账本做穿。现仅在 `input==0 且 not burning`（或等待预算到期）时收取。
11. **取物数量**：`done[].took` 才是服务端实际取出的数量，不能用计划数量入账（否则账本虚增、炉里剩货变成幽灵）。
12. **补造熔炉**：仅靠 `/craft` 会 `need_crafting_table`；`smelt_start` 改走 `craft_gear`（自带工作台）并保留 ≥16 圆石。
13. **区块命令静默失败**：未 `forceload` 的区块里 `/fill`/`setblock` 无任何报错也不生效；旧关卡残留地板会让机器人"走在空气上"（实为旧方块），因此每关先在更大范围清 `air`。
14. **幽灵工单**：熔炉被拆/替换后，`smelt_collect` 在 `window/view` 失败且 `get_block` 非熔炉时自动销账，避免 `in_conversion_counts` 长期虚高。

## 6. 边界与已知限制

- `BreadcrumbTrail` 只在 `navigate_segmented` 成功推进/恢复后采样；`move_to` 内部绕行路径不逐点入栈，因此 M8 用"分段 `move_to` + 手动采样"验证倒序回退。
- `smelt_start` 的 `ready_at` 是按 10s/块 + 5s 点火的确定性估算，真实服务端会有 ±1–2s 抖动；`smelt_collect` 已做 `wait_remaining_s` 收尾。
- 熔炉多炉上限 3、单炉输入格上限 64；`count>=8` 才并联（更小批量保持单炉，减少开窗次数）。
- `InventoryManager` 目前由实机脚本/上层直接调用；按白名单要求未改动 `ensure.py`/`curriculum.py` 的接线点，也未在 `mc_tools.py` 新增工具名（只做窗口同步与挖箱销账）。
- 归仓 `stash_surplus_nearby` 满箱自建箱依赖随身木板 ≥8 与附近可放置工作面；无木板时明确返回 `no_safe_container`。

## 7. 复现命令

```bash
# 离线
PYTHONPATH=/home/xzq/projects/mc/.worktrees/l2-live-pack/src \
  /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/l2-live-pack/tests -q

# 实机（0 LLM；自动建场/清理；Token 复用 .state-move-test）
cd /home/xzq/projects/mc/.worktrees/l2-live-pack
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_move_live_test.py --only all --report runs/l2-move-full.json
PYTHONPATH=$PWD/src /home/xzq/projects/mc/.venv/bin/python scripts/l2_inv_window_live_test.py --only all --report runs/l2-inv-window-full2.json
```
