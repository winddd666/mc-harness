# `nju-beast` 测试服连接信息与 OP 联机实操手册

> 你当前已拥有 **`nju-beast`**（Leaves `1.21.11` 离线测试服 + `mc-agent-api v1.3.14`）的 **Level 4 管理员（OP）权限**。压缩包内的 **`testkit.env`** 已为你预填好连接参数与测试服口令，解压即可开测！

---

## 一、服务器连接参数速查表

| 服务 / 用途 | 校园网直连地址 | 校外 Tailscale 地址 | 说明 |
|---|---|---|---|
| **MC 游戏客户端** | `10.50.248.70:25565` | `100.93.211.71:25565` | Minecraft Java **`1.21.11`**（`online-mode=false` 离线模式，用你的 OP 离线账号直连） |
| **Bot HTTP API** | `http://10.50.248.70:18900/api/v1` | `http://100.93.211.71:18900/api/v1` | 已升级到最新 **`v1.3.14`**（支持 `/control wait:false` 实时控制与每 Bot 独立进程） |
| **实景直播只读看板** | `http://10.50.248.70:18765/` | `http://100.93.211.71:18765/` | 浏览器直接打开可查看实景画面、状态与事件 |
| **Prismarine 3D 视角** | `http://10.50.248.70:8766/` | `http://100.93.211.71:8766/` | WebGL 备选 3D 伴随旁观视角 |
| **Source RCON** | `10.50.248.70` 端口 `25575` | `100.93.211.71` 端口 `25575` | 仅跑 `exp1`~`exp7` 全自动靶场测伤时用到；口令已预填在包内 `testkit.env` |

> **如果你在校园网内**：无需改动 `testkit.env`，直接运行脚本即可。  
> **如果你在校外通过 Tailscale 访问**：把 `testkit.env` 里的 `10.50.248.70` 改为 `100.93.211.71` 即可（仅改 IP，端口与口令不变）。

---

## 二、两条安全红线（务必遵守）

1. **严禁占用的保留角色名**（同一角色重新登录会立刻把原有进程踢掉）：
   - `BeastBot_01`：主开发控制台与直播面板正在使用的主 Bot（脚本已拦截防误填）
   - `BeastCam`、`LiveCam_01`：服务器旁观相机账号
   - 请始终使用默认的 **`FightBot_01`**（如多人同时测可改成 `FightBot_02`）。
2. **严禁破坏 `-158, 76, -374` 主岛地形与误杀全局实体**：
   - 放怪、测苦力怕爆炸或测熔岩桶前，务必传送到 **`(810, 120, 810)`** 高空隔离房；
   - 游戏内清怪只能带坐标框限定范围（如 `/kill @e[x=790,y=118,z=790,dx=40,dy=10,dz=40,type=!player]`），**禁止执行无范围限制的 `/kill @e[type=!player]`**。

---

## 三、4 步 OP 联机实测完整流程（指令可直接复制）

### 第 1 步：双开进服（游戏客户端 + 终端控制）

1. 打开 MC Java `1.21.11`，用你的管理员账号直连 **`10.50.248.70:25565`**。
2. 在本解压目录打开终端，直接启动联机交互台（零第三方依赖，Python 3.10+ 即可）：
   ```bash
   python run_combat_tests.py --interactive
   ```
   看到 `FightBot_01>` 提示符说明 Bot 已作为独立子进程登录进入服务器。

### 第 2 步：在远离主岛的高空一键搭建隔离测试房

进游戏聊天栏按顺序复制执行（屋顶铺海晶灯既照明，又能防止白天骷髅/僵尸自燃）：

```mcfunction
/tp @s 810 120 810
/fill 795 119 795 825 125 825 polished_andesite hollow
/fill 798 125 798 822 125 822 sea_lantern
/tp FightBot_01 810.5 120 810.5
/give FightBot_01 iron_sword 1
/give FightBot_01 stone_axe 1
/give FightBot_01 shield 1
/give FightBot_01 bow 1
/give FightBot_01 arrow 64
/give FightBot_01 cobblestone 64
```

### 第 3 步：按优先级逐项实测与调参

> 提示：调参时直接编辑 `run_combat_tests.py` 最顶部的常量（`DEFAULT_CRIT_DELAY_S`、`DEFAULT_PILLAR_WAIT_S`、`ZIGZAG_ANGLE_RAD`、`CREEPER_RETREAT_MS` 等），退出重开 `--interactive` 立即生效。

#### A. 校准「双阶段跳劈暴击（1.5x）」最佳毫秒窗口
1. 游戏内刷木桩铁傀儡（`100` 血，不反击）：
   ```mcfunction
   /summon iron_golem 810.5 120 808.2 {NoAI:1b,PersistenceRequired:1b,Tags:["dummy"]}
   ```
2. 终端切换武器试砍，同时肉眼看是否冒出**金色暴击星星粒子**：
   - `equip iron_sword hand` -> 依次试 `crit 340`、`crit 380`、`crit 420`
   - 把 `FightBot_01` 拉远到 3 格外试 `mcrit 380`（移动跃进跳劈）
3. **用 OP 命令读取服务端权威真实血量**（不要信客户端 `/get_entities` 血量）：
   ```mcfunction
   /data get entity @e[tag=dummy,limit=1,sort=nearest] Health
   ```
   - 铁剑平砍掉 **`6.0`**，真暴击掉 **`9.0`**；换 `equip stone_axe hand` 真暴击掉 **`13.5`**！测完清桩 `/kill @e[tag=dummy]`。

#### B. 横评「打小白（Skeleton）」5 种策略
每轮先帮 Bot 回满血：`/effect give FightBot_01 instant_health 1 10`，再在 12 格外放一只真小白：
```mcfunction
/summon skeleton 810.5 120 798.5 {PersistenceRequired:1b}
```
终端分 5 轮各试一次，每轮结束后打 `status` 记录剩余血量：
1. `skel naive`（直接 `attack` 裸冲基线）
2. `equip shield off-hand` 后跑 `skel shield`（举盾推进贴脸放盾斩）
3. `skel zigzag`（无盾 `±32°` Z 字折线疾跑骗预判）
4. `skel pillar`（面前 2 格手摆 1×2 圆石柱遮挡射线，验证小白会不会停射自己走到 `<3.5m` 送死）
5. `skel bow`（直接 `/shoot` 服务端弹道提前量对狙）

#### C. 验证「苦力怕无伤风筝」阈值
在 9 格外放真苦力怕：`/summon creeper 810.5 120 801.5 {PersistenceRequired:1b}`，终端跑 **`creeper`**。连测 3 只观察是否做到 **0 引爆、0 掉血**（极限的话在代码顶部适当调大 `CREEPER_RETREAT_MS`）。

#### D. 验证「地形几何杀」与自由扩展测试
- **原地垫高 3 格清群怪**：放 3 只僵尸 `/summon zombie ...`，终端跑 **`pillar 3`**（对比 `pillar 2`），验证起跳后 `210ms` 窗口放方块成功率及 3 格高是否 0 受伤；
- **`1x2` 矿道半身封口修脚**：搭一段 1×2 窄道，出口只封头顶那格（`y+1` 圆石、`y` 留空），外面放 1 僵尸 + 1 小白，终端跑 **`gap foot`**（脚本自动开启 `sneak=true` 降眼高至 `1.27`）；
- **其他特色怪物**：可按 `micro-action-test-plan.md` 自己搭 2 格高矮棚放末影人、或放蜘蛛测无屋檐高台爬墙。

### 第 4 步：收尾清理（或跑 RCON 全自动靶场脚本）

1. 测完后进游戏清理高空测试房（只清测试区，不影响主岛）：
   ```mcfunction
   /kill @e[x=790,y=118,z=790,dx=40,dy=10,dz=40,type=!player]
   /fill 795 119 795 825 125 825 air
   ```
   终端输入 `quit` 退出交互台；如需让 `FightBot_01` 离线也可先运行 `raw disconnect {}`。
2. 如不想手动在游戏里摆木桩测伤害，只要你测的机器能通 `nju-beast` 的 RCON 端口（若 `25575` 暂未从校园网侧放行则优先用上面 `--interactive` + OP 命令），也可直接跑：
   ```bash
   python run_combat_tests.py exp2_jump_crit
   python run_combat_tests.py exp4_skeleton_strategies
   ```
   详细待测项与调参回传记录表见同目录下的 [`micro-action-test-plan.md`](./micro-action-test-plan.md)。
