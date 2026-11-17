# `mc-agent-api v1.3.14` 战斗与微操测试包（给同学的 1 分钟上手指南）

这套测试包把 **`v1.3.14` 底层所有新增的微操动作与对怪策略** 封装成了 **零第三方依赖的纯 Python 标准库工具**，不调用大模型、不花 API 额度，只需要 **Python 3.10+** 就能直接在 Windows / macOS / Linux 上跑起来实测。

---

## 一、包里有什么（重要：先看哪几份文档）

1. **`nju-beast-服务器与OP实操手册.md`（必读！）**：完整服务器 IP/端口表（`10.50.248.70` / Tailscale `100.93.211.71`）、安全避让规则、以及作为 **Level 4 管理员（OP）** 进服一键建测试房 + 刷怪 + `/data get` 读真实血量的复制即用指令；
2. **`testkit.env`**：已为你预填好 `nju-beast` 校园网连接信息、口令与独立 Bot 账号 **`FightBot_01`**（若你在校外走 Tailscale，只需把其中的 `10.50.248.70` 改为 `100.93.211.71`）；
3. **`run_combat_tests.py`**：零依赖核心脚本（联机交互台 `--interactive` + 顶部集中的毫秒数/角度调参区 + 可选 RCON 全自动靶场 `exp1`~`exp7`）；
4. **`micro-action-test-plan.md`**：5 大类 16 项待测动作清单、物理避坑说明与调参回传记录表；
5. **`API-v1.3.14.md`**：最新版上游 HTTP API 规范说明书。

---

## 二、30 秒开测（解压即跑，无需 `pip install`）

> 提示：测试服 `nju-beast` 我已升级至 **`mc-agent-api v1.3.14`**；请保持 `testkit.env` 里的 **`MC_BOT_USER=FightBot_01`**，不要改成 `BeastBot_01`！

1. **打开游戏客户端**：用你的 OP 离线账号（Minecraft Java **`1.21.11`**）多人游戏连接 `10.50.248.70:25565`。
2. **打开终端启动交互控制台**：
   ```bash
   python run_combat_tests.py --interactive
   ```
   看到 `FightBot_01>` 提示符后，直接照着 **`nju-beast-服务器与OP实操手册.md`** 第 3 节复制指令在高空 `(810, 120, 810)` 建测试房、放木桩与各类怪物开测即可！

### 常用交互命令速览
- `crit 380` / `crit 340` / `crit 420` —— 调不同毫秒数试双阶段跳劈，配合游戏内 `/data get ... Health` 找 100% 暴击（铁剑 `9.0` / 石斧 `13.5`）窗口；
- `mcrit 380` —— 3 格外移动跃进跳劈；
- `wtap` —— 疾跑强击退一刀；
- `creeper` —— 苦力怕一刀强击退 + 反向 `sprint+jump` 拉扯循环；
- `skel naive` / `skel shield` / `skel zigzag` / `skel pillar` / `skel bow` —— 小白 5 种战术横评；
- `pillar 3` / `gap foot` —— 原地跳垫高 3 格清群怪 / `1x2` 矿道蹲姿修脚。

---

## 三、测完之后

请把你测出来的 **最佳延时（如 `DEFAULT_CRIT_DELAY_S` / `DEFAULT_PILLAR_WAIT_S`）、小白/苦力怕/矿道哪套打法最稳、以及物理坑点** 填进 `micro-action-test-plan.md` 底部的表格（连同你调整后的 `run_combat_tests.py` 或 `combat_test_report.json`）发回给我，我们马上合并进二阶段确定性技能库！
