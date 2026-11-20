# Minecraft 战斗系统 v1.0：技能介绍与参数说明

> 对应文件：`combat_v1_0.py`。适用环境：Minecraft Java **1.21.11**、`mc-agent-api v1.3.14`。文档整理日期：2026-11-20。
>
> 这是当前战斗初版的使用说明。代码只依赖 Python 标准库，不调用大模型；技能通过比赛 HTTP API 观察与操作 bot。参数以本版实际代码为准，不沿用旧测试包中的过时数值。

---

## 一、30 秒上手

把 `combat_v1_0.py` 和已有的 `testkit.env` 放在同一目录，用 Python 3.10 或更高版本运行：

```bash
python combat_v1_0.py --interactive
```

也可以指定配置文件：

```bash
python combat_v1_0.py --env ./testkit.env --interactive
```

配置中的 `MC_API_URL`、`MC_BOT_USER`、`MC_BOT_PASS` 分别提供 API 地址、bot 账号和密码。沿用现有配置即可，不需要把口令写进技能代码。交互战斗不需要 RCON；装备和耗材需要预先准备，本文件不会自动合成或补充。

看到 `FightBot_01>` 后输入技能命令。例如：

```text
melee zombie
rush skeleton
crit 350
pillar 3
```

这些命令应分别按场景测试，不是一次粘贴后同时执行。当前交互台按顺序运行技能；技能执行期间，同一终端不会读取下一条输入。

## 二、当前有哪些战斗 skill

| 命令 | 作用 | 目标 | 主要物资 |
|---|---|---|---|
| `crit [delay_ms]` | 原地跳劈一次 | 默认选 8 格内最近敌对实体 | 斧优先，没有斧则用剑 |
| `mcrit [delay_ms]` | 向前跳劈一次，不开启疾跑 | 同上 | 斧优先，剑替代 |
| `wtap` | 向前疾跑并挥击一次，尝试增强击退 | 默认选 6 格内最近敌对实体 | 剑优先 |
| `melee [mob_name]` | 持续追击一个目标并普通近战 | 不填则选 24 格内最近敌对实体 | 剑优先，斧替代 |
| `rush [mob_name]` | 择机突进，近身转剑盾攻击 | 不填则从 32 格内最近敌对实体确定怪物种类 | 剑与盾 |
| `creeper` | 苦力怕风筝，无法可靠逃跑时尝试盾防 | 32 格内苦力怕，选中后跟踪其 ID | 剑；盾用于备用防御 |
| `skel shield` | 原有骷髅剑盾打法 | 骷髅 | 剑与盾 |
| `skel bow` | 调用比赛 `shoot` 接口射箭 | 骷髅 | 弓与箭 |
| `pillar [height]` | 搭高后清理附近怪物 | 柱顶观察范围 12 格 | 圆石、剑；有盾则使用 |
| `gap foot` / `gap head` | 在已准备的矿道缝隙中攻击 | 8 格内最近敌对实体 | 剑；需人工或上层脚本准备地形 |

`mob_name` 使用英文实体名，如 `zombie`、`skeleton`、`spider`、`blaze`；也接受 `minecraft:zombie`。指定名称时按该名称查询，不填时才默认筛选敌对实体。通用入口表示可选择该怪物，不表示同一战术适合所有怪物。

旧的 `skel naive` 已改为 `melee`，旧的 `skel rush` 已改为 `rush`；`skel zigzag` 和旧掩体等待版 `skel pillar` 已删除。现在的 `pillar 2/3` 是独立的原地搭高技能。

## 三、共同的武器与攻击节奏

普通近战优先选剑，背包没有剑时再选斧；`crit/mcrit` 则相反。每一类内部按照代码预设的材质顺序选择，不会根据附魔重新计算最优武器。

攻击冷却使用客户端本地计时和原版武器基础数值，不是实时读取服务端冷却条。普通攻击额外留 **50ms** 余量；不考虑急迫、挖掘疲劳等效果。典型值如下：

| 武器 | 代码基础冷却 | 加余量后的普通攻击最短间隔估计 |
|---|---:|---:|
| 剑 | 625ms | 675ms |
| 铁斧 | 1112ms | 1162ms |
| 木斧、石斧 | 1250ms | 1300ms |

网络请求会进一步增加实际间隔。跳劈会把起跳后的计划等待时间计入冷却准备，减少在地面空等；不会把不稳定的网络延迟当成可提前攻击的保证。

---

## 四、各技能的实现与参数

### 1. `crit` / `mcrit`：双阶段跳劈

实现流程：选武器与准备冷却 → 看向目标 → 发出跳跃 → 等待 `delay_s` → 再次瞄准、观察竖直速度 → 单次挥刀。`mcrit` 的跳跃阶段额外按住前进，两个版本都不主动开启疾跑。

| 参数 | 默认值 | 如何设置 |
|---|---:|---|
| 跳跃到攻击阶段的计划等待 | 350ms | 控制台 `crit 350` / `mcrit 350` |
| `entity_id` | 自动选择 | Python 参数，指定某个实体 ID |
| `delay_s` | `0.35` 秒 | Python 参数；必须有限且非负 |
| `moving` | `False` | Python 参数；`mcrit` 对应 `True` |

函数：`action_jump_crit(bot, entity_id=None, delay_s=0.35, moving=False)`。

返回的 `pre_hit_on_ground` 和 `pre_hit_vy` 有助于判断出刀时是否处于下落阶段，但采样与真正攻击之间仍有请求延迟，不能单凭这些字段保证暴击。

### 2. `wtap`：单次疾跑击退攻击

先等待武器冷却、看向目标，然后在一个 `control` 请求中发送前进、疾跑和一次挥刀。它是单次操作，不会自动接后撤或持续追击。击退效果还取决于服务器判定和当时的疾跑状态。

| 参数 | 默认值 | 如何设置 |
|---|---:|---|
| `entity_id` | 自动选择 | Python 参数 |
| 前进与疾跑时长 | 190ms | 代码常量 `WTAP_HIT_MS` |

函数：`action_wtap_knockback(bot, entity_id=None)`。控制台直接输入 `wtap`。

### 3. `melee`：持续普通近战

开始时选定一个实体 ID，持续更新它的位置；目标消失后结束，不自动改追另一只怪物。进入技能时会清除已有举盾、蹲伏等持久姿态，避免普通近战被原有举盾状态挡住。

远处把 `move_to` 分成短时寻路段；接近后改用短 `control` 步进。根据相邻观测中的距离变化估算接近速度，把请求耗时计入刹车判断。进入攻击距离后先释放前进键，冷却未好时继续观察，不按住前进等冷却。

| 参数或阈值 | 当前值 | 设置位置 |
|---|---:|---|
| `target_name` | `None`，自动选敌对实体 | 控制台 `melee zombie` 或 Python 参数 |
| `max_time_s` | 22 秒 | Python 参数 |
| 出刀次数上限 | 20 次 | 内部 `reactive_melee(max_hits=20)`；控制台未开放 |
| 远处寻路启用距离 | 大于 8 格 | `reactive_melee` 内部值 |
| 单段寻路超时 / 停靠范围 | 最多 300ms / 6 格 | 同上 |
| 近处移动时长 | 50–120ms，6 格内最多 80ms | 同上，按接近速度动态缩短 |
| 近战循环最小间隔 | 75ms | 同上；不是实际网络刷新率保证 |
| 暂缓前进的观测耗时阈值 | 大于 300ms | 同上 |
| 尝试攻击的实体距离 | 不超过 3.05 格 | 同上；仍由 API 做实际射线检测 |

函数：`action_melee(bot, target_name=None, max_time_s=22.0)`。

近距离步进本身不是绕障寻路；复杂墙角、高差和危险地面不能只靠这个技能解决。本文件没有后台地形扫描或持久地形记忆。

### 4. `rush`：突进与剑盾近战

沿用 `melee` 的短时寻路、实时距离更新和提前刹车方法，加入举盾状态切换。检测到新弹射物或观测变慢时转入防御；近身后在观察、等待冷却时举盾，只在单次攻击窗口放盾。循环会跟踪选定实体 ID。

| 参数或阈值 | 当前值 | 设置位置 |
|---|---:|---|
| `target_name` | `None`，自动选怪物种类 | 控制台 `rush zombie` 或 Python 参数 |
| `max_time_s` | 22 秒 | Python 参数 |
| 出刀次数上限 | 100 次 | 内部值；仍受 22 秒时限约束 |
| 进入近身防御 / 恢复远处突进 | 5 格 / 6 格 | `RUSH_FAR_DISTANCE` / `RUSH_RESUME_DISTANCE` |
| 新弹射物触发的防御保持 | 0.9 秒 | `RUSH_GUARD_S` |
| 弹射物名称 | `arrow`、`spectral_arrow`、`small_fireball`、`fireball` | `RUSH_PROJECTILES` |

函数：`action_rush(bot, target_name=None, max_time_s=22.0)`。

结束或报错时尝试恢复进入前的蹲伏、举盾、跳跃姿态，并停止本次移动。`posture_restored` 表示恢复请求是否成功。箭矢出现才举盾存在反应延迟，且盾只能防御有效朝向的攻击；此版本不是多方向弹射物拦截系统。

### 5. `creeper`：苦力怕风筝与盾防备用方案

技能包括接近、后撤、举盾防御三个阶段。进入攻击距离且武器冷却完成时，尝试疾跑击退一刀；收到挥刀响应后立即转向远离苦力怕，使用短时间的疾跑跳跃后撤。达到安全距离并持续保持一段时间后才再次接近。

| 参数 | 当前值 | 设置位置 |
|---|---:|---|
| `max_time_s` | 25 秒 | Python 参数；控制台只接受 `creeper` |
| 攻击距离 | 2.8 格 | `CREEPER_HIT_DIST` |
| 过近阈值 | 1.8 格 | `CREEPER_PANIC_DIST` |
| 安全距离 / 连续保持时间 | 6 格 / 1 秒 | `CREEPER_SAFE_DIST` / `CREEPER_SAFE_HOLD_S` |
| 目标轮询周期 / 过旧阈值 | 100ms / 250ms | `CREEPER_POLL_S` / `CREEPER_STALE_S` |
| 单次移动最大时长 | 150ms | `CREEPER_STEP_MS`；近处接近时进一步缩短 |
| 后撤进展检查 | 每 300ms 检查约 0.25 格进展 | `CREEPER_ESCAPE_CHECK_S` / `CREEPER_ESCAPE_MIN_PROGRESS` |

函数：`action_kite_creeper(bot, max_time_s=25.0)`。

饱食度不超过 6、存在缓慢效果、后撤进展不足、未拉开距离、过近或观察过旧时，会尝试备用防御。有盾则转向苦力怕并持续举盾；进入这阶段后不再继续风筝出刀。没有盾时记录原因并继续尝试后撤，不等于能成功逃脱。

这里的后撤是直线短步控制，没有读取苦力怕引信计时，也没有调用独立逃跑规划模块。若触发盾防，技能退出后仍保留举盾；这一点与 `rush/pillar` 的姿态恢复不同。

### 6. `skel shield`：原版保留的骷髅剑盾

函数：`action_skeleton_duel(bot, strategy="shield", max_time_s=22.0)`。

仅选择骷髅，在观察和接近时举盾，用最多 120ms 的短步前进；实体距离不超过 2.6 格后停止移动，冷却完成再短暂放盾出刀。`max_time_s` 默认 22 秒，只能通过 Python 参数调整。

这个保留分支没有改成新版 `melee/rush` 的接近速度预测。它结束后仍举盾；需要释放时输入 `posture off`。

### 7. `skel bow`：API 弓箭射击

函数：`action_skeleton_duel(bot, strategy="bow", max_time_s=22.0)`。

在 28 格范围寻找骷髅，将目标 ID 交给官方 `shoot` 接口，指定 `weapon="bow"`、`charge_ms=1000`。最多射 8 次，同时受 22 秒总时限限制。蓄力时间、箭数上限目前是分支内部值，控制台没有相应参数。

这层脚本没有自行实现移动靶速度预测；弹道和瞄准依赖 API。仅当 API 同时提供射击前后数值血量且血量下降，才增加 `hits_landed`；血量缺失不能当成命中或死亡。退出时停止动作并清除持久姿态。

### 8. `pillar`：搭高清怪

```text
pillar 2
pillar 3
```

函数：`action_pillar_up_and_clear(bot, height=3, block_item="cobblestone")`。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `height` | `3` | 控制台可填整数，代码接受 1–16；当前主要实测 2、3 格 |
| `block_item` | `cobblestone` | Python 参数；使用普通完整方块，需备足数量 |

搭建时持续下蹲，提前拿好方块，用持久跳跃状态避开放置接口释放临时按键的问题；观察实际高度后择机放置，逐层确认方块与落地位置。每层最多尝试两次放置；中断后重新核实现场，最多进行两次搭建恢复。能确认原柱就续搭，被击离原柱后只有确认新落点支撑安全、上方为空才重建。

搭高 2 格后站在原位置攻击；恰好搭高 3 格后保持下蹲，根据目标位置朝柱顶边缘小步靠近。靠边目标范围为柱中心横向各轴约 ±0.72 格，步长 50–100ms；它是蹲伏支撑范围的保守估计，不是允许离开高柱追怪。

攻击时本地估计怪物碰撞箱，优先瞄准上部，并检查约 3 格的射线距离和自身柱体遮挡。怪物尺寸是估计值；不会检查所有周边方块。

| 清怪行为参数 | 当前值 |
|---|---:|
| 周围实体观察范围 | 12 格 |
| 锁定目标持续未命中后的切换时间 | 4 秒 |
| 目标暂时消失的等待时间 | 0.8 秒 |
| 放弃目标后的暂避时间 | 3 秒 |
| 每只目标的出刀上限 | 24 次 |
| 正常结束条件 | 连续 3 秒未观察到活怪 |
| 活动预算 | 初始 20 秒；新目标出现或选中实体的攻击反馈会续期 |
| 总安全超时 | 180 秒 |

没有整群共用的固定挥刀上限。苦力怕优先于普通怪物，并可打断普通怪物锁定；选中一只苦力怕后仍保持锁定，避免频繁换怪。全部剩余目标都耗尽尝试次数、没有进展或离开柱顶等情况也会提前结束，并给出原因。

有盾时，柱顶观察、冷却和移动期间举盾，只在攻击时放盾。无论正常结束还是失败，技能最终都会尝试恢复进入前的姿态。失败备用格挡也不会在技能返回后覆盖这个恢复规则。

### 9. `gap foot/head`：矿道缝隙攻击

函数：`action_gap_slash(bot, mode="foot", max_time_s=15.0)`。

| 参数 | 当前值 | 说明 |
|---|---|---|
| `mode` | `foot` | 控制台 `gap foot` 或 `gap head`，其他值报错 |
| `max_time_s` | 15 秒 | Python 参数 |
| `foot` 的瞄准高度 / 眼高估计 | 0.35 / 1.27 格 | 下蹲，从低缝攻击 |
| `head` 的瞄准高度 / 眼高估计 | 1.45 / 1.62 格 | 站立，从高缝攻击 |
| 尝试出刀的实体距离 | 3.5 格 | 仍需 API 实际射线命中 |

技能只做选目标、调整姿态和挥刀，不会自动建造矿道或封口。它仍使用较简单的固定瞄准逻辑，不能视为已经完成与 pillar 同等程度的射线筛选。退出时清除持久姿态。

---

## 五、从其他 Python 脚本调用

控制台只开放命令速览中的参数。例如 `rush zombie 30` **不会**设置 30 秒时限；需要调整 `max_time_s` 等参数时，应直接调用 Python 函数：

```python
from pathlib import Path
import combat_v1_0 as combat

cfg = combat.load_env_file(Path("testkit.env"))
bot = combat.SimpleBotClient(
    cfg["MC_API_URL"], cfg["MC_BOT_USER"], cfg["MC_BOT_PASS"]
)
bot.ensure_ready()

result = combat.action_rush(bot, target_name="zombie", max_time_s=30.0)
print(result)
```

`crit/mcrit` 的 Python 时间参数单位是秒，控制台延迟参数单位是毫秒。其余函数中的 `max_time_s` 也都是秒。一次只让一个战斗技能控制同一 bot；本版不包含多技能抢占调度器。

## 六、辅助命令与结果怎么看

| 命令 | 作用 |
|---|---|
| `status` | 查看位置、血量、速度与姿态 |
| `entities [radius]` | 查看周围实体，默认范围 16 格 |
| `inv` | 查看背包与装备 |
| `equip iron_sword hand` | 装备主手物品 |
| `equip shield off-hand` | 装备副手盾牌 |
| `posture off` | 清除持久蹲伏、举盾与跳跃 |
| `stop` | 停止动作并清除姿态；在终端有空读取输入时才会执行 |
| `raw <path> [JSON]` | 直接调用 API，绕过技能逻辑及部分节奏控制 |
| `help` / `quit` | 查看帮助 / 退出 |

重点回传以下字段即可：

| 场景 | 重点字段 |
|---|---|
| 接近后贴脸、移动过冲 | `observed_distances`、`brakes`、`poll_gap_mean_ms/max_ms`、`slow_observations` |
| 放盾窗口 | `unshielded_request_windows_ms`：请求窗口时长，不是服务器已生效的格挡时长 |
| 搭建失败 | `build_reason`、`layers_completed`、`layers`、`recovery_events` |
| 频繁切目标、某怪一直打不到 | `target_events`、`target_attempts`、`miss_details` |
| 姿态未恢复 | `posture_before`、`posture_restored`、`posture_restore_error`（如有） |
| 苦力怕退不出去 | `transitions`、`minimum_distance`、`shield_fallback`、`guard_reason` |

`hits_connected`、`ray_connection_rate` 主要统计 API 的射线是否选中了实体，不等于实际伤害次数。`target_missing` 表示观察不到目标；部分旧分支的 `killed_all` 也只表示范围内没有目标，不能直接作为死亡证明。精确伤害测试仍应使用已有服务端读血量方法。

## 七、本版整理与测试范围

- 新文件名为 `combat_v1_0.py`；旧 `run_combat_tests.py` 保留对照。
- 修正帮助中跳劈默认 310ms 的过时文字，实际仍为 350ms；删除已不参与逻辑的旧搭高等待常量和旧固定攻击等待常量。
- 普通 `melee` 开始时主动放下继承的盾与姿态，避免看似挥刀却仍在举盾。
- `skel bow` 不再把缺失血量当成命中或死亡；非法跳劈等待值、非法 `gap` 模式会在动作前被拒绝。
- 没有旧地形记忆服务依赖。搭高期间必要的 `get_block` 查询仍保留；独立逃跑模块尚未接入这些技能。
- 保留 `--list` 和 RCON 自动靶场入口。RCON 实验会修改测试场地、清理并重新发放测试装备，属于测试工具，不能作为普通战斗命令运行。

本文所配 v1.0 已通过 **81 项离线测试**，包括接近运动目标、举盾切换、姿态恢复，以及此次修正的参数校验和弓箭反馈；命令列表也已检查。离线通过不表示所有怪物、地形和比赛延迟均已实服验证。
