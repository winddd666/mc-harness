# `mc-agent-api v1.3.14` 细粒度动作与战斗实测指引（队友分享版）

> 本文档对应工具包里的 [`run_combat_tests.py`](./run_combat_tests.py) 与 [`API-v1.3.14.md`](./API-v1.3.14.md)。所有动作已按底层 HTTP + Mineflayer 约束拆好，**零大模型 API 额度消耗**。

---

## 一、核心底层物理坑点（调参前必读）

移植 AltoClef 思路或编写脚本时，务必记住我们的底层接口和客户端 Mod 的 5 个关键差异：

1. **单次 `control(jump=true, hit=1)` 第 1 刀默认打不出暴击！**
   - `POST /control` 的第 1 下挥砍在 `t_ms = 0`（起跳离地前）立即发出，此时还在地上无法暴击。
   - 必须使用 `v1.3.14` 的 **`"wait": false` 分两段发**：先跳（且**绝对不能开 `sprint`**，Java 版疾跑会强制变为击退斩而取消暴击），等约 **`340–420ms`** 进入下落期再挥砍。
2. **苦力怕 `/get_entities` 没有 `fuse` 膨胀计时字段**
   - 不能等“快炸了”才跑；只能用固定距离节奏：`<= 3.3m` 疾跑强击退一刀打断 -> 立刻反向 `sprint+jump` 拉开 `> 4.5m` 再回头。
3. **只有 `status.nearest_hostile` 和 `/hurt` 事件的 `source_entity` 可靠**
   - **客户端拿不到普通怪物真实剩余血量**（不要信 `get_entities` 里的 `health` 算伤害），实测精确伤害数值必须靠 RCON 读取服务端 `Health`。
4. **举盾时移速骤降至 ~20%，且必须正面朝向射手才格挡**
   - 举盾靠近小白要持续刷新 `look(entity_id)` 对正射手；贴脸后务必 `posture({})` 放盾才能出刀。
5. **`1x2` 矿道隔缝修脚（封头留脚缝）必须开潜行！**
   - 站立眼高 `1.62` 会被头顶的阻挡块拦住左键射线；开启 `posture(sneak=true)` 降眼高至 `1.27` 才能顺利透下缝斜砍怪物脚部。

---

## 二、五大类 16 项待测动作与调参目标

| 编号 | 类别 | 测试项 | `--interactive` 实时命令 | RCON 靶场命令 | 重点验证/调参问题（测完填下表） |
|---|---|---|---|---|---|
| 1.1 | **控制底座** | `control(wait=false)` 按住/接管/急停 | `raw control {"forward":true,"ms":600,"wait":false}` -> `status` | `exp1_realtime_baseline` | RTT 下实测接管与急停是否干脆？ |
| 1.2 | **控制底座** | `posture` `55ms` 秒切盾 / 潜行 / 水中踩水 | `posture shield` / `posture sneak` / `posture off` | `exp1` / `exp7` | 1.21 `oxygen` 水下起始满值是不是 `20`？ |
| 1.3 | **控制底座** | `/stop` 释放动作锁无缝接反击 | 先 `raw move_to ...` 再输入 `stop` | `exp7_water_and_stop` | `/stop` 后连发挥刀会不会撞 `busy`？ |
| 2.1 | **近战微操** | **双阶段跳劈暴击（1.5x）** | `crit 340` / `crit 380` / `crit 420` | `exp2_jump_crit` | **铁剑打出 9.0 / 石斧 13.5 暴击率最高的 `delay_ms` 是多少？** |
| 2.2 | **近战微操** | 移动跃进跳劈（3m 助跑） | `mcrit 380` | `exp2_jump_crit` | 移动靶空中 `look` + 出刀命中率如何？ |
| 2.3 | **近战微操** | W-Tap 疾跑击退斩 | `wtap` | `exp3_knockback_and_creeper` | 疾跑第一刀能把怪推开多少格？ |
| 2.4 | **近战微操** | 斧（高爆 `1111-1250ms`）+ 剑（`625ms`）冷却节奏 | 游戏内手感实测 / `raw control {"hit":...}` | 可手动扩展 `exp2` | “石斧破盾/爆伤第一刀 + 切铁剑补刀”好不好用？ |
| 3.1 | **怪物对策** | **小白（Skeleton）5 战术横评** | `skel naive` / `shield` / `zigzag` / `pillar` / `bow` | `exp4_skeleton_strategies` | **哪种打法掉血最少？无盾 `zigzag` 推荐角度 `ZIGZAG_ANGLE_RAD`？** |
| 3.2 | **怪物对策** | 苦力怕（Creeper）无伤风筝 | `creeper`（在苦力怕旁边输入） | `exp3_knockback_and_creeper` | `CREEPER_RETREAT_MS`（默认 650ms）能否 0 引爆稳杀？ |
| 3.3 | **怪物对策** | 末影人 `2` 格矮棚 / 脚下倒水 | 自己在面前搭 3x3 `y+2` 矮棚放怪砍 | 人工实测 | 高 2.9m 末影人卡在棚外时，是否会触发瞬移？ |
| 3.4 | **怪物对策** | 蜘蛛爬墙 / 烈焰人三连火球掩体 | 放蜘蛛测试 2~3 格高台能否挡住 | 人工实测 | 验证蜘蛛无屋檐会爬上纯高台、加 1 圈凸檐能否完全卡住？ |
| 4.1 | **地形微操** | **`1x2` 矿道半身封口修脚 vs 修头** | `gap foot` / `gap head` | `exp5_tunnel_gap_slash` | **修脚（`foot` + `sneak`）能否同时防住僵尸+小白 0 伤清怪？** |
| 4.2 | **地形微操** | 原地起跳垫高 2 格 vs 3 格清群怪 | `pillar 2` / `pillar 3`（可调 `DEFAULT_PILLAR_WAIT_S`） | `exp6_pillar_cheese` | **`DEFAULT_PILLAR_WAIT_S` 最稳几 ms？2 格还是 3 格完全不挨打？** |
| 4.3 | **地形微操** | `posture(sneak=true)` 悬崖/虚空安全搭桥 | `posture sneak` + `raw control` 边缘移动 + `raw place` | 人工实测 | 高空连搭 10 格独木桥能否 0 跌落？ |
| 5.1 | **流体交互** | 顺水下悬崖回收水（替代 MLG 落地水） | `raw interact {"pos":...,"item":"water_bucket"}` | 人工实测 | 水桶倒水顺流下去再收回是否稳定不摔伤？ |
| 5.2 | **流体交互** | 圆石凹槽模具“水+岩浆桶”速浇下界门 | `raw interact` 按顺序对着模具格倒岩浆/水 | 人工实测 | 固定凹槽模板能否一次成功浇出 10 格黑曜石门并点燃？ |

---

## 三、实测结论反馈记录模板（测完填这里回传）

- **测试环境**：（本地 `5ease` / `nju-beast` / 校园网，RTT 大约 `___ ms`）
- **最佳参数结论**：
  - `DEFAULT_CRIT_DELAY_S`（站定暴击最佳延时）：`_____ s`（暴击命中率：`___ / ___`）
  - `DEFAULT_PILLAR_WAIT_S`（原地垫高最佳 `/place` 时机）：`_____ s`
  - 打小白最稳推荐：前期无盾用 `[zigzag / pillar / ...]`；有盾用 `[shield / bow / ...]`
  - 矿道遭遇战：`gap foot`（封头留脚缝+潜行）是否无伤？`[是 / 否，现象：___]`
- **意外坑点 / 不可行动作记录**：
  - （例如：某延迟下出现什么报错、蜘蛛爬墙情况、某种站位会被小白穿过等）
