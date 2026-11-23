# `mc-harness` — Minecraft 1.21.11 自主智能体底座

基于 **Minecraft Agent API v1.3.14**（[接口规范](agent-api-spec-dist.md) · 上游 [Zyzzyva0381/mc-agent-api](https://github.com/Zyzzyva0381/mc-agent-api)）与赛制规则（[RULES.md](RULES.md)）构建的 Minecraft 自主生存与竞赛 Harness。

> **当前基线（2026-11-21）**：离线单元与回归测试 **`1,000+ tests`**；已完成多轮本地仿赛并发评测与正式服（`survivaltest`）实机验证。

---

## 核心模块一览

| 模块 | 路径 | 说明 |
| :--- | :--- | :--- |
| **自主 Harness 主循环** | `src/mc_agent/harness/` | 有界多轮调度、读写分流、上下文折叠、运行中 `--interactive` 插队 |
| **课程调度与世界记忆** | `harness/curriculum.py`<br>`harness/world_memory.py` | 1.21.11 成就积分规划、本地持久化世界记忆与容器记录 |
| **生存守护层** | `harness/guardian.py`<br>`skills/combat_env.py` | 后台状态监测与紧急生存响应 |
| **宏技能库** | `src/mc_agent/skills/` | 采矿、地表探索、采集、熔炼、合成、战斗、交易、酿造等确定性宏技能 |
| **1.21.11 知识库** | `src/mc_agent/kb/` | 别名解析、配方/生态/进度按需检索 |
| **运行与评测脚本** | `scripts/run_official_bot.py`<br>`scripts/v2_fleet.py` | 零 RCON 单机长跑脚本与多机并发场景评测、自动分诊 |
| **只读直播与 Web 控制台** | `src/mc_agent/live/`<br>`src/mc_agent/console/` | 纯只读实景推流/3D 体素/回放看板（`:8765`）与本地交互控制台（`:8770`） |

---

## 仓库目录结构

```text
mc-harness/
├── README.md                    # 项目总览与快速上手（本文件）
├── AGENTS.md                    # 编码 Agent 协作与安全操作规范
├── RULES.md                     # 比赛积分与成就规则
├── agent-api-spec-dist.md       # MC Agent API v1.3.14 接口文档
├── pyproject.toml / uv.lock     # Python >= 3.11 依赖与 CLI 入口定义
├── .env.example                 # 环境变量模板（真实 .env* 严禁入库）
├── src/mc_agent/                # 核心源码包
│   ├── bot.py / auth.py         # MC API 会话、Token 缓存复用、NMO 皮肤站建号
│   ├── cli.py                   # `mc-bot` CLI
│   ├── harness/                 # `mc-harness` 主循环、工具封装、Guardian、课程调度、世界记忆、围栏
│   ├── skills/                  # 宏技能库
│   ├── kb/ & data/              # Minecraft 1.21.11 知识库与进度数据
│   ├── live/                    # `mc-live` 纯只读直播与回放服务
│   └── console/                 # `mc-console` 本地 Web 控制台
├── tests/                       # 1,000+ 离线单元与回归测试（零网络依赖）
├── scripts/                     # 单机运行器、并发舰队评测、分诊与运维脚本
├── docs/                        # 架构文档、Bug 看板、交接与历史归档
├── live_viewer/                 # 无头实景推流与 Prismarine 3D 旁观
├── deploy/                      # systemd 常驻服务单元与 Windows SSH 辅助脚本
├── tools/teammate_combat_kit/   # 战斗技能独立测试套件
├── server_patches/              # 仿赛测试服后端补丁
├── benchmarks/                  # 大模型网关吞吐与延迟压测报告
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

# 正式服零 RCON 长跑
uv run python scripts/run_official_bot.py \
  --bot MyBot_01 --provider nju --rpm-budget 10 --duration-s 3600 --tag match_run
```

### 4. 多机并发场景评测（`v2_fleet.py`）

```bash
uv run python scripts/v2_fleet.py launch \
  --wave demo_wave --bots MoveTest_01,GuiTest_01 \
  --stage-override bare --duration 1200 --stagger 5.0
```

---

## 直播看板、Web 控制台与常驻运维

| 服务 | 默认端口 | 说明 | 启动 / 管理命令 |
| :--- | :--- | :--- | :--- |
| **`mc-live`** | `0.0.0.0:8765` | 纯只读实景推流、AI 3D 体素、2D 雷达与 `runs/*.events.jsonl` 轨迹回放 | `uv run mc-live --host 0.0.0.0 --port 8765` |
| **`mc-console`** | `127.0.0.1:8770` | 本地 Web 交互控制台，复用单一 `BotSession` 与 KV Cache | `uv run mc-console --host 127.0.0.1 --port 8770 --live-url http://127.0.0.1:8765` |
| **常驻托管** | `systemd --user` | 与终端窗口解耦的后台服务及远端状态检查 | `bash scripts/mc_services.sh status` / `install` / `restart` / `beast-status` |

---

## 实机开发注意事项

1. **单 Token 互斥原则**：`mc-agent-api` 对同一 Bot 每次 `POST /auth/login` 都会立即吊销旧 Token。服务运行时避免对同角色重复 `mc-bot login`，直接复用 `.state*/tokens.json`。
2. **多角色登录方式**：始终使用 `<角色名>` + 皮肤站密码登录，避免单账号多角色用邮箱登录触发 `401 no_profile`。
3. **敏感文件红线**：`.env*`、`.state*`、`tokens.json`、`runs/`、`backups/` 保持 `0600` 与 `.gitignore` 忽略，严禁提交入库。

---

## 文档导航

- **开发规范与待办**：[AGENTS.md](AGENTS.md) · [docs/BUG_BOARD.md](docs/BUG_BOARD.md) · [docs/TODO.md](docs/TODO.md)
- **赛制与接口规范**：[RULES.md](RULES.md) · [agent-api-spec-dist.md](agent-api-spec-dist.md) · [docs/advancements-1.21.11.md](docs/advancements-1.21.11.md)
- **架构与交接文档**：[docs/HANDOFF-20261115.md](docs/HANDOFF-20261115.md) · [docs/mc-gameplay-handoff.md](docs/mc-gameplay-handoff.md) · [docs/team-briefing.md](docs/team-briefing.md)
- **性能压测与历史归档**：[benchmarks/nju-api-20261109/REPORT.md](benchmarks/nju-api-20261109/REPORT.md) · [`docs/archive/`](docs/archive/)

---

## Acknowledgements

- **[AltoClef](https://github.com/gaucho-code/altoclef)**：本项目在资源前置依赖求解与全局库存管理的设计上参考了 AltoClef 的相关思路，并在此基础上针对纯外部 HTTP API 与 LLM 混合调度场景做了独立实现。
- **[Zyzzyva0381/mc-agent-api](https://github.com/Zyzzyva0381/mc-agent-api)**：本项目对接的 Minecraft 1.21.11 Agent HTTP API 服务端协议规范。
- **[PrismarineJS / Mineflayer](https://github.com/PrismarineJS/mineflayer)** 与 **[Prismarine Viewer](https://github.com/PrismarineJS/prismarine-viewer)**：支撑底层 Bot 生态与本仓库 3D 实时旁观能力。
