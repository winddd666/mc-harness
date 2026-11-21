# `mc-harness` — Minecraft 1.21.11 自主智能体与竞赛底座

基于 **Minecraft Agent API v1.3.14**（[接口规范](agent-api-spec-dist.md) · 上游 [Zyzzyva0381/mc-agent-api](https://github.com/Zyzzyva0381/mc-agent-api)）与竞赛积分规则（[RULES.md](RULES.md)）构建的 **L3 战略规划 + L1/L2 确定性宏技能 + L0 高频反射守护** 三层混合 Minecraft 自主智能体 Harness。

> **当前验证基线（2026-11-21）**：离线单元与回归测试 **`982 tests`**；已通过 60+ 波并发实机压测与多轮正式服（`survivaltest`）实测，在 **0 崩溃 / 0 慢工具调用** 前提下跑通 **Day 1 铁层过夜 → Day 2 天亮探村与入夜挖钻 → 附魔台 → 下界要塞/烈焰棒 → 两眼三角定位进主世界要塞** 全链路。

---

## 阶段性实测成果

| 赛道 / 场景 | 验证环境 | 核心实测表现 | 关键技术能力 |
| :--- | :--- | :--- | :--- |
| **正式服生存** (`survivaltest`) | 零 RCON 官方服 | 累计多轮 40m **0 断线 / 0 崩溃**；`GLM_1` 实测破百至 **`110 pts`**（地下挖齐 7 钻、0-LLM 极限躲苦力怕并穿戴钻石靴 + 盾牌 + 铁甲） | `run_official_bot.py` 自动 TLS/断线重连护盾、秃地强冲外迁、防地下断镐 |
| **Day 1 → Day 2 裸开** (`bare`) | 本地仿赛服（0 分重置） | 30m 裸开 `HY_3` **0 死 `95 pts`**；45m 完整昼夜连跑 `HY_10` 达成 **`95 pts`**（严格走出 `铁层过夜 → Day 2 天亮探村 → 入夜挖钻`），单轮地表探村狂奔 **513–1,404m** | 昼夜课程状态机、`day1_night_iron_prep` 铁层防装成型、`scout_surface` 高速斥候 |
| **附魔 / 铁砧** (`enchant_anvil`) | 本地仿赛服 | **8/8（100%）** 稳定完成 `story/enchant_item`（最高 `180 pts`，NJU 主模型 0 死亡） | 72m 联合箱子/熔炉记忆 + 确定性窗口交互 |
| **下界要塞 / 烈焰棒** (`nether_fortress`) | 本地仿赛服 | 连续多轮 **80%（4/5）** 稳进要塞，`HY_8` 多次稳定 **`240–250 pts (+85~+95)`**（打通 `find_fortress` + `obtain_blaze_rod` / `obtain_ancient_debris`），死亡数较早期压降 **70%+** | 下界主线 `tier=0` 调度、`BUG-080` 下界安全探路、火球/岩浆/岩浆块/举盾 L0 守护 |
| **要塞 → 末地** (`village_to_stronghold_end`) | 本地仿赛服 | 跨地图稳定复现：`HY_1`（`165/+70`, `160/+65`）与 `HY_2`（`160/+65`, **`180/+85`**）均 **0 死亡** 进要塞解锁 `follow_ender_eye (+60)`，且实测扫出全部 12 块 `end_portal_frame` | **0-LLM 两眼自动三角定位** + 跨 episode 坐标记忆 + 钻地检测 + `enter_the_end` 剧本 |

---

## 三层混合架构

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  L3 战略与课程督导 (harness/curriculum.py · world_memory.py)            │
│  · 125 项 1.21.11 成就前沿规划与积分排序                                 │
│  · 昼夜状态机：Day 1 铁层过夜 → Day 2 天亮必探村 → Day 2 夜深潜挖钻      │
│  · 72m 箱子/熔炉联合库存、村庄/矿脉/要塞三角坐标持久化记忆               │
├─────────────────────────────────────────────────────────────────────────┤
│  L2 极简 Agent Harness (harness/agent.py · scheduler.py · mc_tools.py)  │
│  · 有界多轮工具循环、观察并发 / 动作互斥调度、上下文折叠与时间预算钳制   │
│  · 0-LLM 前置备料 (pre_ensure) + 两眼三角定位解算 + JIT 报错即时修正提示  │
├─────────────────────────────────────────────────────────────────────────┤
│  L1 确定性宏技能库 (skills/library.py · mining_dfs.py · combat_v1.py)   │
│  · mine_dfs 深井鱼骨采矿与回程 · scout_surface 零 LLM 往返高速探图       │
│  · 原地竖砍原木 · 熔炼 · 合成 · 建门 · 交易 · 酿造 · 绑床                │
├─────────────────────────────────────────────────────────────────────────┤
│  L0 高频生存反射守护 (harness/guardian.py · skills/combat_env.py)       │
│  · 后台高频监测受伤/落水/岩浆/岩浆块/苦力怕/远程射手/火球击退            │
│  · 0-LLM 毫秒级抢占执行举盾/躲避/封井/吃食/上岸并确认安全落脚            │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 相关开源项目与致谢（Related Work & Acknowledgements）

`mc-harness` 是面向纯 HTTP REST API 与开放权重推理模型的独立工程实现，在架构设计过程中深受以下开源社区与学术项目的启发，特此说明关系并致谢：

### 1. 与 `nano-agent` 及极简 Agent Harness 范式的关系
- **借鉴与灵感**：在底层 Agent 主循环（`src/mc_agent/harness/`）的设计上，我们借鉴了 `nano-agent` 等极简 Agent Harness 的核心哲学——**把编排层做薄、做确定、做可观测**：不引入重型多框架抽象，而是通过干净的 Tool Calling 主循环、读写分流调度器（`ToolScheduler`）、流式结构化事件日志（`.events.jsonl`）、上下文自动折叠与 `--interactive` 运行中插队，把省下来的 token 与延迟预算全部留给实时游戏决策。
- **差异化演进**：针对 Minecraft 实时生存的强时间约束，我们在极简循环外围补齐了全链路墙钟预算钳制（杜绝 `>=60s` 慢调用）、LLM 限流退避与 **L0 后台异步抢占守护（`GenerationGuard`）**，使模型在思考或限流等待期间依然具备毫秒级生存反射。

### 2. 与 `AltoClef` 的关系
- **借鉴与灵感**：[AltoClef](https://github.com/gaucho-code/altoclef)（及其社区维护分支）是 Minecraft 全自动速通领域的标杆项目。我们在 **分层任务求解与资源管理** 上深受 `AltoClef` 启发，包括：
  - **物理前置依赖求解（`skills/ensure.py`）**：类似 AltoClef 的资源树拆解，将高层目标（如 `iron_pickaxe`）自动拆解为原木→木板→工作台→木镐→圆石→石镐→粗铁→熔炼的 0-LLM 确定性链条；
  - **全局联合库存与容器记忆（`InventoryManager` + `WorldMemory`）**：跟踪背包与周边 72m 箱子/熔炉的联合库存，避免重复伐木与盲目返贫；
  - **将高频低层控制下沉为确定性宏技能**：将鱼骨挖矿（`mine_dfs`）、地表高速斥候（`scout_surface`）、要塞两眼三角定位以及战斗/岩浆/水体脱困彻底从 LLM 剥离，由 Python 确定性代码保证执行可靠性。
- **核心区别**：`AltoClef` 是基于 Java/Fabric 客户端 Mod 与 Baritone 的纯硬编码状态机；而 `mc-harness` 运行在 **纯外部 HTTP REST API（无客户端 Mod 注入）** 之上，采用 **LLM 课程/战略调度 + 确定性宏技能 + L0 反射守护** 的混合范式，能够泛化覆盖 125 项 1.21.11 成就、应对多人服动态环境并支持人类实时自然语言插队控制。

### 3. 上游协议与生态致谢
- **[Zyzzyva0381/mc-agent-api](https://github.com/Zyzzyva0381/mc-agent-api)**：本项目对接的 Minecraft 1.21.11 Agent HTTP API 服务端协议规范。
- **[PrismarineJS / Mineflayer](https://github.com/PrismarineJS/mineflayer) & [Prismarine Viewer](https://github.com/PrismarineJS/prismarine-viewer)**：为底层 Bot 协议生态及本项目 `live_viewer/` 3D 实时体素旁观看板提供基础能力。
- **[MineDojo / Voyager](https://github.com/MineDojo/Voyager)**：在“课程驱动探索（Curriculum）”与“技能库复用”范式上提供了早期概念启发。

---

## 仓库目录结构

```text
mc-harness/
├── README.md                    # 项目总览、实测成果与开源致谢（本文件）
├── AGENTS.md                    # 编码 Agent 协作与安全操作规范
├── RULES.md                     # 比赛积分与成就规则
├── agent-api-spec-dist.md       # MC Agent API v1.3.14 接口文档
├── pyproject.toml / uv.lock     # Python >= 3.11 依赖与 CLI 入口定义
├── .env.example                 # 环境变量模板（真实 .env* 严禁入库）
├── src/mc_agent/                # 核心源码包
│   ├── bot.py / auth.py         # MC API 会话、Token 缓存复用、NMO 皮肤站建号
│   ├── cli.py                   # `mc-bot` CLI
│   ├── harness/                 # `mc-harness` 主循环、工具封装、Guardian、课程调度、世界记忆、围栏
│   ├── skills/                  # 宏技能库（library.py / mining_dfs.py / combat_v1.py / combat_env.py / ensure.py）
│   ├── kb/ & data/              # Minecraft 1.21.11 知识库与 125 项进度数据
│   ├── live/                    # `mc-live` 纯只读直播与回放服务
│   └── console/                 # `mc-console` 本地 Web 控制台
├── tests/                       # 982 项离线单元与回归测试（零网络依赖）
├── scripts/                     # 正式服零 RCON 运行器（run_official_bot.py）、并发舰队（v2_fleet.py）与分诊脚本
├── docs/                        # 核心架构、Bug 看板、交接与专题指南
│   ├── prompts/                 # 场景提示词模板
│   └── archive/                 # 历史分阶段实测报告与旧版文档归档
├── live_viewer/                 # 无头实景推流（BeastCam / director）与 Prismarine 3D 旁观
├── deploy/                      # systemd 常驻服务单元与 Windows SSH 辅助脚本
├── tools/teammate_combat_kit/   # 队友战斗技能独立测试套件
├── server_patches/              # 仿赛测试服后端补丁
├── benchmarks/                  # 比赛大模型网关吞吐与延迟压测报告
└── experiments/                 # 历史受控实验夹具与后端快照
```

---

## 快速开始

### 1. 安装依赖与配置凭据

```bash
# 安装本地虚拟环境 (.venv) 与四大 CLI
uv sync

# 复制配置模板（权限必须为 0600，切勿提交 .env* 或 .state*）
cp .env.example .env
chmod 600 .env
```

在 `.env` 中按需填入：
- **MC 账号**：`MC_SKIN_EMAIL`、`MC_SKIN_PASSWORD`（NMO 皮肤站首次注册需完成邮箱验证，之后可用 `mc-bot create` 自动建角色）
- **大模型密钥**（任选其一）：
  - 比赛官方 Qwen 网关（默认）：`NJU_API_KEY=...`（`https://api.nju-aia.com/v1`，请求 `model` 自动归一化为 `"default"`）
  - 百炼 / OpenAI 兼容接口：`DASHSCOPE_API_KEY=...` 或 `QWEN_API_KEY` + `QWEN_BASE_URL` + `QWEN_MODEL`

### 2. 运行离线全量单测

```bash
uv run --offline pytest -q
```

### 3. 管理 Bot 与运行智能体

```bash
# 查看皮肤站积分/角色并签到
uv run mc-bot skin-info --sign

# 新建 Bot 角色、换取 Token 并等待进入游戏服 (ready=True)
uv run mc-bot create MyBot_01

# 查看在线状态 / 缓存 Token / 登出
uv run mc-bot status MyBot_01
uv run mc-bot tokens
uv run mc-bot logout MyBot_01

# 启动单机交互式自主智能体（支持运行中随时打字插队）
uv run mc-harness --interactive "检查周围环境，采集原木并制作石镐与熔炉。"

# 正式服零 RCON 长跑（自带 TLS/网络抖动与死亡冷却自动重连护盾）
uv run python scripts/run_official_bot.py \
  --bot MyBot_01 --provider nju --rpm-budget 10 --duration-s 3600 --tag match_run
```

### 4. 多机并发场景评测（`v2_fleet.py`）

```bash
# 示例：并发 30 分钟 Day 1→Day 2 裸装评测
uv run python scripts/v2_fleet.py launch \
  --wave demo_day12 --bots DS_2,HY_3,HY_4,HY_10 \
  --stage-override bare --duration 1800 --stagger 5.0

# 常用 --stage-override 场景：
# bare | iron | day2_village_near | enchant_anvil | nether_fortress | village_to_stronghold_end
```

---

## 直播看板、Web 控制台与常驻运维

| 服务 | 默认端口 | 说明 | 启动 / 管理命令 |
| :--- | :--- | :--- | :--- |
| **`mc-live`** | `0.0.0.0:8765` | 纯只读实景推流、AI 3D 体素、2D 雷达与 `runs/*.events.jsonl` 轨迹回放（绝不调用登录或动作接口） | `uv run mc-live --host 0.0.0.0 --port 8765` |
| **`mc-console`** | `127.0.0.1:8770` | 本地 Codex 风格交互控制台，复用单一 `BotSession` 与 KV Cache，支持紧急插队与协作停止 | `uv run mc-console --host 127.0.0.1 --port 8770 --live-url http://127.0.0.1:8765` |
| **常驻托管** | `systemd --user` | 与终端窗口解耦的后台服务及远端 `nju-beast` 全栈状态检查 | `bash scripts/mc_services.sh status` / `install` / `restart` / `beast-status` |

---

## 实机开发与比赛防坑速查

1. **单 Token 互斥原则**：`mc-agent-api` 对同一 Bot 每次 `POST /auth/login` 都会立即吊销旧 Token。`mc-live` / `mc-console` 运行时**禁止对同角色重复执行 `mc-bot login`**，直接探活复用 `.state*/tokens.json`。
2. **多角色登录方式**：始终使用 **`<角色名>` + 皮肤站密码** 登录，避免单账号多角色用邮箱登录触发 `401 no_profile`。
3. **比赛 Qwen 网关要点**：`model` 必须传 `"default"`（代码已自动归一化）；关闭思考需传 `chat_template_kwargs: {"enable_thinking": false}`；对局域网、Tailscale 及比赛网关默认 `trust_env=False` 直连。
4. **多人服不依赖睡觉跳夜**：`bind_bed` 仅用于绑定重生点与解锁进度；夜晚统一转入地下铁层/矿道或室内合成/附魔/酿造。
5. **敏感文件红线**：`.env*`、`.state*`、`tokens.json`、`runs/`、`backups/` 保持 `0600` 与 `.gitignore` 忽略，严禁提交入库。

---

## 文档导航

- **开发规范与待办**：[AGENTS.md](AGENTS.md) · [docs/BUG_BOARD.md](docs/BUG_BOARD.md) · [docs/TODO.md](docs/TODO.md)
- **赛制与接口规范**：[RULES.md](RULES.md) · [agent-api-spec-dist.md](agent-api-spec-dist.md) · [docs/advancements-1.21.11.md](docs/advancements-1.21.11.md)
- **架构与系统交接**：[docs/HANDOFF-20261115.md](docs/HANDOFF-20261115.md) · [docs/mc-gameplay-handoff.md](docs/mc-gameplay-handoff.md) · [docs/team-briefing.md](docs/team-briefing.md)
- **战斗与专项模块**：[docs/combat_v1_0_guide.md](docs/combat_v1_0_guide.md) · [docs/combat-env-interface-spec.md](docs/combat-env-interface-spec.md) · [live_viewer/BEAST_SETUP.md](live_viewer/BEAST_SETUP.md)
- **性能压测与历史归档**：[benchmarks/nju-api-20261109/REPORT.md](benchmarks/nju-api-20261109/REPORT.md) · [`docs/archive/`](docs/archive/)
