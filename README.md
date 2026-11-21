# `mc-harness` — Minecraft 1.21.11 竞赛自主智能体底座

基于 **Minecraft Agent API v1.3.14**（[接口规范](agent-api-spec-dist.md) · 上游 [Zyzzyva0381/mc-agent-api](https://github.com/Zyzzyva0381/mc-agent-api)）与赛制规则（[RULES.md](RULES.md)）构建的 Minecraft 自主生存与竞赛 Harness。

> **当前基线（2026-11-20）**：离线单测 **`770 passed`**；已通过 49 波并发实机压测，打通 **Day 1 裸装发育 → Day 2 全白天高速寻村（870–1,404m）与入夜挖钻 → 附魔台（5/5 满分）→ 下界要塞/烈焰人战斗 → 村庄掷末影之眼追要塞进末地** 全链路。

---

## 核心能力一览

| 模块 | 路径 | 说明 |
|---|---|---|
| **自主 Harness 主循环** | `src/mc_agent/harness/` | 有界多轮调度、`ToolScheduler`（观察并发 / 动作互斥）、上下文折叠、运行中 `--interactive` 插队、全链路时间预算钳制 |
| **L3 课程调度与世界记忆** | `harness/curriculum.py`<br>`harness/world_memory.py` | 125 项 1.21.11 进度积分规划、昼夜状态机（白天寻村/地表，入夜下矿/室内）、72m 箱子/熔炉联合库存、村庄/矿脉/地标持久化记忆 |
| **L0 生存反射守护** | `harness/guardian.py`<br>`skills/combat_env.py` | 后台高频监控受伤、落水、岩浆、岩浆块、苦力怕、远程射手，自动抢占执行举盾/反击/逃生/吃食并确认安全落脚 |
| **确定性宏技能库** | `src/mc_agent/skills/` | `mine_dfs`（深井采矿与回程）、`scout_surface`（零 LLM 往返高速寻村/下界斥候）、`combat_v1.0`（多策略战斗）、同树干原地竖砍、熔炼、合成、建门、交易、酿造、绑床 |
| **1.21.11 零膨胀知识库** | `src/mc_agent/kb/` | 别名展开、配方/矿层/怪物/进度剧本按需检索 + 报错即时修正提示（JIT Error Hint） |
| **并发评测与自动化验收** | `scripts/v2_fleet.py`<br>`scripts/l3_wild_endurance_runner.py` | 支持 `bare` / `iron` / `day2_village_near` / `enchant_anvil` / `nether_fortress` / `village_to_stronghold_end` 等多场景并发波次与自动分诊榜单 |
| **只读直播与 Web 控制台** | `src/mc_agent/live/`<br>`src/mc_agent/console/` | 纯只读实景推流/3D 体素/历史轨迹回放看板（`:8765`）与 Codex 风格本地交互控制台（`:8770`） |

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
│   ├── bot.py / manager.py      # MC API 会话、Token 缓存复用、NMO 皮肤站建号
│   ├── cli.py                   # `mc-bot` CLI
│   ├── harness/                 # `mc-harness` 主循环、工具封装、Guardian、课程调度、世界记忆、围栏
│   ├── skills/                  # 宏技能库（library.py / mining_dfs.py / combat_v1.py / combat_env.py）
│   ├── kb/ & data/              # Minecraft 1.21.11 知识库与 125 项进度数据
│   ├── live/                    # `mc-live` 纯只读直播与回放服务
│   └── console/                 # `mc-console` 本地 Web 控制台
├── tests/                       # 770+ 离线单元与回归测试（零网络依赖）
├── scripts/                     # 并发舰队发射器（v2_fleet.py）、场景运行器、分诊与运维脚本
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
- **MC 账号**：`MC_SKIN_EMAIL`、`MC_SKIN_PASSWORD`（NMO 皮肤站首次注册需在网页完成邮箱验证，之后可用 `mc-bot create` 自动建角色）
- **大模型密钥**（任选其一）：
  - 比赛官方 Qwen 网关（默认）：`NJU_API_KEY=...`（`https://api.nju-aia.com/v1`，请求 `model` 自动归一化为 `"default"`，默认限速 `24 RPM`）
  - 百炼 / 自定义兼容接口：`DASHSCOPE_API_KEY=...` 或 `QWEN_API_KEY` + `QWEN_BASE_URL` + `QWEN_MODEL`

### 2. 运行离线全量单测

```bash
uv run --offline pytest -q
```

### 3. 管理 Bot 与启动单机智能体

```bash
# 查看皮肤站积分/角色并签到
uv run mc-bot skin-info --sign

# 新建 Bot 角色、换取 Token 并等待进入游戏服 (ready=True)
uv run mc-bot create MyBot_01

# 查看在线状态 / 缓存 Token / 登出
uv run mc-bot status MyBot_01
uv run mc-bot tokens
uv run mc-bot logout MyBot_01

# 启动自主生存智能体（加 --interactive 支持运行中随时打字插队）
uv run mc-harness --interactive "检查周围环境，采集原木并制作石镐与熔炉。"

# 切换本地仿赛测试服（独立凭据与 Token 缓存，不干扰线上）
uv run mc-harness --env-file .env.local --interactive "探索地表并寻找村庄。"
```

### 4. 多机并发场景评测（`v2_fleet.py`）

```bash
# 示例：5 路并发 20 分钟 Day 1 裸装测试
uv run python scripts/v2_fleet.py launch \
  --wave demo_day1 --bots MoveTest_01,GuiTest_01,HY_1,HY_2,HY_3 \
  --stage-override bare --duration 1200 --stagger 2.0

# 常用 --stage-override 场景：
# bare | iron | day2_village_near | enchant_anvil | nether_fortress | village_to_stronghold_end
```

---

## 直播看板、Web 控制台与常驻运维

| 服务 | 默认端口 | 说明 | 启动 / 管理命令 |
|---|---|---|---|
| **`mc-live`** | `0.0.0.0:8765` | 纯只读实景推流、AI 3D 体素、2D 雷达与 `runs/*.events.jsonl` 轨迹回放（绝不调用登录或动作接口） | `uv run mc-live --host 0.0.0.0 --port 8765` |
| **`mc-console`** | `127.0.0.1:8770` | 本地 Codex 风格交互控制台，复用单一 `BotSession` 与 KV Cache，支持紧急插队与协作停止 | `uv run mc-console --host 127.0.0.1 --port 8770 --live-url http://127.0.0.1:8765` |
| **常驻托管** | `systemd --user` | 与终端窗口解耦的后台服务及远端 `nju-beast` 全栈状态检查 | `bash scripts/mc_services.sh status` / `install` / `restart` / `beast-status` |

---

## 实机开发与比赛防坑速查

1. **单 Token 互斥原则**：`mc-agent-api` 对同一 Bot 每次 `POST /auth/login` 都会立即吊销旧 Token。`mc-live` / `mc-console` 运行时**禁止对同角色重复执行 `mc-bot login`**，直接探活复用 `.state*/tokens.json`。
2. **多角色登录方式**：始终使用 **`<角色名>` + 皮肤站密码** 登录，避免单账号多角色用邮箱登录触发 `401 no_profile`。
3. **比赛 Qwen 网关要点**：`model` 必须传 `"default"`（代码已自动归一化）；关闭思考需传 `chat_template_kwargs: {"enable_thinking": false}`；对局域网、Tailscale 及比赛网关默认 `trust_env=False` 直连。
4. **多人服不依赖睡觉跳夜**：`bind_bed` 仅用于绑定重生点与解锁进度；夜晚统一转入地下挖矿或室内合成/附魔/酿造。
5. **敏感文件红线**：`.env*`、`.state*`、`tokens.json`、`runs/`、`backups/` 保持 `0600` 与 `.gitignore` 忽略，严禁提交入库。

---

## 文档导航

- **开发规范与待办**：[AGENTS.md](AGENTS.md) · [docs/BUG_BOARD.md](docs/BUG_BOARD.md) · [docs/TODO.md](docs/TODO.md)
- **赛制与接口规范**：[RULES.md](RULES.md) · [agent-api-spec-dist.md](agent-api-spec-dist.md) · [docs/advancements-1.21.11.md](docs/advancements-1.21.11.md)
- **架构与系统交接**：[docs/HANDOFF-20261115.md](docs/HANDOFF-20261115.md) · [docs/mc-gameplay-handoff.md](docs/mc-gameplay-handoff.md) · [docs/team-briefing.md](docs/team-briefing.md)
- **战斗与专项模块**：[docs/combat_v1_0_guide.md](docs/combat_v1_0_guide.md) · [docs/combat-env-interface-spec.md](docs/combat-env-interface-spec.md) · [live_viewer/BEAST_SETUP.md](live_viewer/BEAST_SETUP.md)
- **性能压测与历史归档**：[benchmarks/nju-api-20261109/REPORT.md](benchmarks/nju-api-20261109/REPORT.md) · [`docs/archive/`](docs/archive/)
