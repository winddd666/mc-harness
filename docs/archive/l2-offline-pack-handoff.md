# `[L2-ZeroLLM-Live-Pack]` 移动跨越 + AltoClef 物品栏 + 容器/多炉分拆 三合一开发与 `nju-beast` 实机验收规范

> 更新日期：2026-11-18  
> 适用对象：独立编码与实机验证 Agent（**0 LLM 配额消耗**，但**直连 `nju-beast` 真实 Minecraft 1.21.11 服务器跑确定性实机靶场验收 + 本地离线 `pytest` 双重验证**）  
> 隔离工作树：`/home/xzq/projects/mc/.worktrees/l2-live-pack`（分支 `feat/l2-live-pack`）

---

## 1. 隔离边界、实机连接与安全红线（开工必读！）

1. **Git Worktree 严格隔离**：
   - 主工作区 `/home/xzq/projects/mc` 正在跑单并发挖矿压测，`.worktrees/l3-sched` 由主循环调度组独占。你的一切代码读写、测试和报告**必须在独立 worktree `/home/xzq/projects/mc/.worktrees/l2-live-pack` 中完成**：
     ```bash
     git -C /home/xzq/projects/mc worktree add /home/xzq/projects/mc/.worktrees/l2-live-pack -b feat/l2-live-pack
     ```
2. **严禁污染共享 `.venv` 的 editable `.pth`**：
   - **严禁执行** `uv pip install -e .`、`pip install -e .` 或不带 `PYTHONPATH` 的 `uv run`！
   - 运行单测与实机验收脚本**一律显式指定本工作树 `PYTHONPATH`**：
     ```bash
     PYTHONPATH=/home/xzq/projects/mc/.worktrees/l2-live-pack/src \
       /home/xzq/projects/mc/.venv/bin/pytest /home/xzq/projects/mc/.worktrees/l2-live-pack/tests -v

     PYTHONPATH=/home/xzq/projects/mc/.worktrees/l2-live-pack/src \
       /home/xzq/projects/mc/.venv/bin/python /home/xzq/projects/mc/.worktrees/l2-live-pack/scripts/<your_live_test>.py
     ```
3. **`nju-beast` 实机测试 Bot 与高空靶场隔离（0 LLM 消耗）**：
   - **严禁发起任何 LLM 请求**（不要调 `QwenModel` / `mc-harness`），所有实机验收均通过 Python 直接驱动 `BotSession` / `McToolset` / `MacroSkills` 连接 `nju-beast`（配置读取只读路径 `/home/xzq/projects/mc/.env.beast`，严禁打印任何密码或 Token！）。
   - **严禁触碰在跑角色**：严禁连接或重新登录 `BeastBot_01`、`MapTest_01`、`FightBot_01`、`L3Test_01`、`MineDFS_*`！
   - **你的专用测试角色**：
     - 方案 A（推荐）：创建/使用专用角色 `MoveTest_01`，状态目录指向独立 `/home/xzq/projects/mc/.state-move-test`（权限 `0600`，若 `tokens.json` 已存在有效 Token 则直接复用，切勿重复 `login`）；
     - 方案 B：若需现成缓存 Token，只读参考 `/home/xzq/projects/mc/scripts/gui_window_full_live_test.py` 第 `678–714` 行的连接写法与第 `67–90` 行的 `rcon_batch(...)` RCON 建场写法。
   - **你的专用高空隔离靶场空域**：统一使用 **`(5160..5340, 145..180, 5200..5340)`**（远离 `FightBot_01` 的 `800,120,800`、`GuiTest_01` 的 `3096,150,3096` 与 `MineDFS_*` 野地），实测完毕后在 `finally` 中移除 `forceload` 并清理测试残留实体（如船、掉落物）。
4. **严格文件修改白名单（确保与其他并行线 0 合并冲突）**：
   - ✅ **新建代码文件**：
     - `src/mc_agent/skills/movement.py`（局部地形跨越、软陷阱破除、搭桥、陡崖、渡船、面包屑）
     - `src/mc_agent/skills/inventory_manager.py`（AltoClef 保护栈、四级清杂、自动穿甲、双视图与归仓）
   - ✅ **允许修改既有文件（仅限这三个文件）**：
     - `src/mc_agent/skills/library.py`（接入 `navigate_segmented` 有限次断点恢复；`container_batch` 异常分支窗口同步 + 天然箱摸底；拆分非阻塞多炉并联 `smelt_start` / `smelt_collect` 并兼容 `smelt_items`；可选在清杂处对接 `InventoryManager`）
     - `src/mc_agent/harness/world_memory.py`（新增 `invalidate_container` 双表销账 + `conversion_jobs` / `get_in_conversion_counts` 在途熔炼账本及持久化）
     - `src/mc_agent/harness/mc_tools.py`（仅限 `mc_window_view`、`mc_window_act` 的失败分支窗口同步与容器被挖 `invalidate_container` 销账）
   - ✅ **新建单测、实机脚本与报告**：
     - `tests/test_movement_terrain.py`、`tests/test_inventory_manager.py`、`tests/test_window_and_smelt_split.py`
     - `scripts/l2_pack_live_test.py`（或拆分为 `scripts/move_obstacle_live_test.py` + `scripts/inv_window_smelt_live_test.py`）
     - `runs/l2-live-pack-<timestamp>.json` 与 `docs/l2-live-pack-report-20261118.md`
   - 🚫 **严禁触碰**：`harness/agent.py`、`harness/model.py`、`harness/guardian.py`、`harness/curriculum.py`、`skills/ensure.py`、`skills/mining_dfs.py`、`run_combat_tests.py`。

---

## 2. 模块一：局部移动、地形跨越与 `navigate_segmented` 接力（`movement.py` + `library.py`）

完整细节请先完整阅读主仓只读文档 **`/home/xzq/projects/mc/docs/move-terrain-handoff-guide.md`**！核心交付包括：

1. **五大局部无策略原语（`src/mc_agent/skills/movement.py`）**：
   - **`BreadcrumbTrail`**：按维度隔离的环形路点栈（容量 `48`，`on_ground=True` 且非水非陷阱时每 `6~20m` 压栈；瞬移 `>48m` 自动新开段）+ `backtrack_breadcrumbs(steps=3, max_dist=24.0)` 倒序回退；
   - **`clear_traps_and_doors`**：遭遇 `sweet_berry_bush` / `cobweb` / `bamboo` / `*_leaves` / `powder_snow` 时，强制携带 **`stop_on=["death"]`**（防止浆果刺伤/冻伤打断自身挖掘！有空桶遇细雪优先舀取）清除阻挡；水下头顶 `ice` 向上破冰；正前方关闭的木门 `*_door` 或 `*_fence_gate` 自动调 `/interact` 开门；`1.5` 格高栅栏优先找门或垫 1 块翻越，不原地盲跳；
   - **`sneak_bridge`**：校验非保护实心非重力垫脚块充裕 + **校验起桥点脚下非悬空 `sand/gravel`** -> 调 `/posture {"sneak": True}` 锁潜行逐格向对岸贴砖推进（`max_steps=20`）-> 对岸着陆及任何异常退出时 **`finally` 强制调用 `/posture {}` 释放潜行**；
   - **`climb_or_descend_cliff`**：可挖实心山体复用 `staircase(up/down)`；外壁无山体采用稳态 **`2×1` 交替台阶垫升（Step-Climb）**（严禁高空裸跳或帧级落地水 MLG）；遭遇瀑布 `flowing_water` 逆流停滞时侧向 `±90°` 脱出；
   - **`cross_water_with_boat`**：`>24m` 宽水域放船（或现合木船）`/interact` 乘船 -> `10~14m` 分段直线航行 -> **距实心岸边 `<= 1.2m` 停船 `/dismount`** -> 若站在船碰撞箱上立即补朝岸 `control(forward=True, jump=True, ms=350)` 登岸 -> 拆船回收。
2. **接入 `MacroSkills.navigate_segmented`（有限恢复，防死循环）**：
   - 常规分段 `move_to` 成功时沿途记录 `BreadcrumbTrail`；当 `move_to` 及 `±35°` 绕行均失败时，诊断面前 `1~8` 格最多触发 **2 次** 局部原语跨越，跨过后交回 `move_to`，仍不通则结构化返回阻塞原因。
3. **实机必测关卡（`nju-beast` 高空隔离区 `5200..5320, 150, 5200..5320`，M1–M8 全部实机跑通）**：
   - `M1` 软陷阱走廊（浆果丛+蛛网+密竹）、`M2` 无皮靴细雪坑脱困、`M3` 关门木屋与栅栏门院子、`M4` 8 格深渊潜行搭桥（含岸边悬空沙防塌与上岸 `sneaking==False` 核验）、`M5` 7 格陡崖登顶与 8 格断崖无伤下山、`M6` 3 层厚树叶迷障、`M7` 40m 深水池乘船渡海贴岸下船回收、`M8` L 形双层盲肠洞面包屑回退。

---

## 3. 模块二：AltoClef 物品栏、保护栈、自动穿甲与归仓模块（`inventory_manager.py`）

完整设计背景请阅读 **`/home/xzq/projects/mc/docs/l3-feedback-and-altoclef-inventory-design.md` 第 5 节**。实现 `src/mc_agent/skills/inventory_manager.py`：

1. **静态常量与判据**：
   - `IMPORTANT_ITEMS` + `is_important_item(name)`（覆盖 1.21.11 99 项成就关键物、矿产锭块、`*_bucket`、`*_bed`、`*_shulker_box`、`*_smithing_template`、`music_disc_*`、`enchanted_book`、工作站、弓弩盾三叉戟重锤旋风棒磁石指南针等）；
   - `SUPPORTED_FUELS` + `is_safe_fuel(name, protected)`（仅允煤/木炭/煤块/干海带块/竹子/木棍/未保护木板，**严禁**烧床、木工具、工作台、箱子、船、弓弩、盾牌、烈焰棒）；
   - `FOOD_HUNGER_VALUES` 与按丢弃优先级排序的跨维度垫脚池 `BUILDING_BLOCK_PRIORITY`。
2. **`ProtectionStack` 与四级垃圾裁决纯函数 `plan_garbage_drops`**：
   - 支持 `with inv_mgr.protect(items):` 动态栈保护，且自动保护带 `enchantments` / `custom_name` 的实例；
   - 四级腾格：Tier 1 非方块纯杂物清零（含富粮 `>=16` 时清腐肉/蜘蛛眼） -> Tier 2 全维度垫脚石超额裁剪（保底 `reserved_blocks=96`，圆石+深板岩封顶 `192`） -> Tier 3 未附魔旧工具/旧护甲汰弱留强（剑斧锹锄各留最高阶 1 把；镐留 `1~2` 主镐 + 最多 `2` 石镐，有石镐及以上时优先汰除木镐/金镐/多余石镐） -> Tier 4 绝境低量散件按 `count` 升序腾格（总饥饿分 `<16` 时安全食物免死）。
3. **实机动作与仓储协同（在 `nju-beast` 实机验证！）**：
   - `free_slots(min_free=4)`：首次 `/drop` 前读取 `status.yaw` 调 `look(yaw + math.pi)` 背抛防回吸（**在实机中验证真实服务端槽位索引与不回吸效果！**）；
   - `auto_equip_best(dimension="overworld", preserve_offhand=False)`：四槽护甲升阶穿戴（下界无金甲时保/穿 1 件金甲）+ 空副手挂 `shield`（**在实机中验证 `/equip` 真实部位参数名如 `head/chest/legs/feet/off-hand` 的实际行为！**）；
   - 双视图 `get_inventory_only_counts` / `get_accessible_counts`（含容器与在途熔炼 `in_conversion_counts`）+ 近箱归仓 `stash_surplus_nearby`（过滤 6 格内含 `spawner` 地牢箱与上方压死箱；满箱且木料 `>=8` 木板时自动合新箱放置并同步 `WorldMemory`）。

---

## 4. 模块三：容器窗口异常自愈与熔炉两段式多炉并联（`world_memory.py` / `library.py` / `mc_tools.py`）

完整背景请阅读 **`/home/xzq/projects/mc/docs/l3-feedback-and-altoclef-inventory-design.md` 第 2.4 与第 3 节**：

1. **`[L2-Window-Sync]` 容器全路径快照同步、拆箱销账与天然箱摸底**：
   - 注意本项目 `ApiError`（`src/mc_agent/errors.py`）将服务端附加载荷存在 **`err.context`** 中；
   - 在 `library.py` 的 `container_batch`（正常分支与 `except ApiError` 分支）以及 `mc_tools.py` 的 `mc_window_view` / `mc_window_act`（成功与失败分支）中：**只要载荷携带 `window`（哪怕 `sections.container` 是空列表 `[]`），一律调用 `update_container_from_window` 覆写 `WorldMemory.containers`（空箱写 `{}` 清空旧红石/旧铁锭幽灵缓存）**；
   - 新增 `world_memory.invalidate_container(pos, dim=...)`：当目标方块变为 `air` / 非容器（或被 `mc_dig` 挖除）时，同步从 `containers` 与 `pois` 双表删除；
   - 新增 `MacroSkills.probe_unknown_nearby_containers(radius=8.0, max_probes=2)`：顺路对附近尚未建档库存且 6 格内无 `spawner` 的天然 `chest`/`barrel` 调 `window/view` 建档。
   - **实机验证**：在 `nju-beast` 实机放置箱子放入 1 个红石 -> 取出 -> 再次调 `container_withdraw` 触发真实服务端 `missing_item` 报错，核对真实 `ApiError.context` 字段结构，确认 `WorldMemory.containers` 已自愈为 `{}`；挖掉箱子确认双表销账。
2. **`[L2-Smelt-Split]` 熔炉非阻塞分拆、多炉并联与在途转换槽账本**：
   - `WorldMemory` 新增 `conversion_jobs`（支持持久化）与 `register_conversion_job` / `complete_conversion_job` / `get_in_conversion_counts`；
   - `MacroSkills` 新增 **`smelt_start(input_item, count=1, fuel_item=None, allow_multi_furnace=True)`**：
     - 严格使用安全燃料白名单；
     - 当 `allow_multi_furnace=True` 且 `smelt_n >= 8` 时，自动复用附近现有熔炉或用富余圆石（扣合炉消耗后保底仍剩 `>=16` 圆石）补造并排放置 `2~3` 台熔炉（`desired_k = min(3, max(1, smelt_n // 6))`），将原料均分投料点火，登记 `conversion_jobs`，**0 阻塞立即返回** `furnaces` 坐标列表、`output_item`、`estimated_wait_s` 与 `ready_at`；
   - 新增 **`smelt_collect(furnaces=..., furnace_pos=..., wait_remaining_s=15.0)`**：按炉坐标靠近、必要时短等收尾、满包保护检查、取出 `output` 并销账 `conversion_jobs`；
   - 原有同步 `smelt_items` 保持签名与返回字典 **100% 向后兼容**。
   - **实机验证**：在 `nju-beast` 给 Bot 圆石与煤，调用 `smelt_start("cobblestone", count=12)` 实测自动摆出双炉各分 6 块并毫秒级返回，核验 `get_in_conversion_counts` 显示 `{"stone": 12}`，真实等待约 62s 后调 `smelt_collect` 实测收齐 `stone×12` 且转换账本归零！

---

## 5. 最终交付标准

1. **全量离线单测全绿**：新增 3 套单测 + 既有全套 `pytest` 全部 `0 failed`；
2. **`nju-beast` 实机靶场全绿**：
   - Move M1–M8 八关实机全过；
   - AltoClef 满包清杂不回吸、四槽穿甲+副手挂盾、近箱归仓+满箱造箱+地牢箱跳过实机全过；
   - 空箱 `missing_item` 报错自愈、拆箱销账、双炉并联烧 12 石头非阻塞分拆实机全过；
   - 实机测试中抓到的任何服务端真实边缘情况（如槽位名、错误码格式、船只碰撞箱细节）全部现场修进代码并写入报告 `docs/l2-live-pack-report-20261118.md` 与 `runs/l2-live-pack-<timestamp>.json`；
3. **干净提交**：确认未修改任何白名单外文件、靶场已清理，提交至 `feat/l2-live-pack` 分支汇报结果。
