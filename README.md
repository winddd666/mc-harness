# Minecraft Competition Agent (`mc-agent`)

基于 [agent-api-spec-dist.md](agent-api-spec-dist.md)（Minecraft Agent API v1.3.7）、[docs/HANDOFF-20261115.md](docs/HANDOFF-20261115.md)（**2026-11-15 全栈架构、常驻后台运维与实机修复完整归档**）、[docs/endurance-findings-20261116.md](docs/endurance-findings-20261116.md)（**2026-11-16 65 分钟长程深潜压测复盘与思考吞吐实测**）、[docs/TODO.md](docs/TODO.md)（**当前阶段成绩基线、底座修复待办与 AltoClef 脚本化二阶段路线图**）、[AGENTS.md](AGENTS.md)（**项目操作与沙箱边界规范**）、[docs/mc-gameplay-handoff.md](docs/mc-gameplay-handoff.md)（**游戏侧专版：盲玩机制、现成技能库与知识库/成就路线共建指南**）、[docs/team-briefing.md](docs/team-briefing.md)（**接口能力与真机踩坑速览**）、[docs/expedition-20261114.md](docs/expedition-20261114.md)（2 小时托管远征与双模型实测报告）、[docs/advancements-1.21.11.md](docs/advancements-1.21.11.md)（`1.21.11` 全部 125 项原版进度表）与 [benchmarks/nju-api-20261109/REPORT.md](benchmarks/nju-api-20261109/REPORT.md) 构建的竞赛 Bot 客户端底座，当前已完成 **NMO 皮肤站角色管理**、**MC Agent API 认证与会话守护**、**1.21.11 知识库与宏技能库**、**多模型自主远征 Harness**、**实景直播看板（`mc-live`）** 及 **Codex 风格 Web 控制台（`mc-console`）**。

> **2026-11-16 最新阶段基线**：`pytest` **`132 passed`**；前期托管累计解锁 **`5 / 125`** 项原版进度；在 `BeastBot_01` 上完成 **`65m 04s` / `168` 轮**纯目标自主深潜（零复读死循环、合出 4 把铁镐、最深 `Y=-37`、开矿道贴近 `deepslate_diamond_ore` 仅 `0.7` 格，`qwen3.6` 思考中位数 `5.66s`、均频 `2.58 RPM`），详细复盘与下阶段清单见 [`docs/TODO.md`](docs/TODO.md)。

## 关键接口与现状摘要

1. **MC Bot 控制接口（v1.3.7）**
   - Base URL：`https://apiin.nmo.net.cn/api/v1`（校园网直连默认，keep-alive 实测 ~9–11ms；校外备选公网隧道：`https://mc-api.windywindie.top/api/v1`，~164–177ms）
   - 认证：`POST /auth/login` 换取 Bearer Token；**同一角色重复登录会使旧 Token 立即失效**，因此本地通过 `.state/tokens.json`（权限 `0600`）按角色缓存并探活复用。
   - 连接与就绪：登录后自动连接并切往 `survivaltest` 比赛服；需轮询 `GET /status` 直至 `result.ready == true` 后才可调用动作接口（否则返回 `wrong_server`）。断线重连间隔 ≥ 30s；60s 内死亡 3 次触发 5 分钟 `death_loop_cooldown`。
   - 动作互斥：观察接口（`/status`、`/inventory`、`/find_blocks`、`/get_block`、`/get_entities`、`/events`、`/advancements`）与 `/posture` 不占动作锁；其余动作互斥，并发调用返回 `busy`。
2. **NMO 皮肤站（Blessing Skin 6.0.2 + Yggdrasil）与单账号多 Bot 机制**
   - 地址：`https://skin.nmo.net.cn/`
   - **首次注册账号须在网页手动完成一次**：`/auth/register` 仅开放 `@smail.nju.edu.cn` / `@nju.edu.cn` 邮箱且带图片验证码，注册后需点击南大邮箱验证链接。
   - **新建多个 Bot 角色无需重复注册邮箱**：账号验证后，可通过本项目的 `mc-bot create <角色名>` 直接调用皮肤站接口查询积分、自动签到并创建新角色（Player Profile）。
   - **避免 `no_profile`**：NMO 皮肤站已开启 `feature.non_email_login: true`。当单账号下有多个角色时，用邮箱调 `/auth/login` 会因无法自动选定角色报 `401 no_profile`；本项目统一使用具体的 **`<角色名>` + 皮肤站密码** 向 MC API 登录，每个 Bot 独立持有各自的 Bearer Token。
3. **比赛大模型接口（实测结论见 `benchmarks/nju-api-20261109/REPORT.md`）**
   - 地址：`https://api.nju-aia.com/v1`，请求 `model` 必须填 `"default"`（后端返回 `qwen3.6-35b-a3b`）。
   - 限流：服务端 30 次/分钟（含失败），客户端建议共享限速 24 RPM；暂不支持图片输入；关闭思考使用 `chat_template_kwargs: {"enable_thinking": false}`。

## 快速开始

```bash
# 1. 安装项目虚拟环境与 CLI
uv sync

# 2. 配置凭据（切勿提交 .env）
cp .env.example .env
chmod 600 .env
# 编辑 .env 填入 MC_SKIN_EMAIL 与 MC_SKIN_PASSWORD

# 3. 查看皮肤站积分与已有角色（可加 --sign 每日签到）
uv run mc-bot skin-info --sign

# 4. 一键新建 Bot 角色 + 换取 Token + 连接比赛服并等待 ready=true
uv run mc-bot create MyBot_01

# 5. 已有 Bot 复用缓存 Token 查看实时状态 / 登录 / 登出
uv run mc-bot status MyBot_01
uv run mc-bot tokens
uv run mc-bot logout MyBot_01
```

## Python 调用示例

```python
from mc_agent import BotManager

manager = BotManager()

# 若角色不存在则在皮肤站新建，随后换取 Token 并等待进入 survivaltest (ready=True)
provisioned = manager.create_bot("MyBot_01")
with provisioned.session as bot:
    st = bot.status()
    print(st["username"], st["pos"], st["health"], st["ready"])
    blocks = bot.call("find_blocks", {"radius": 16})
```

## 本地仿赛测试服（Leaves `1.21.11` + `mc-agent-api` `v1.3.7`）

已在队友机器（SSH 别名 `5ease`，`D:\mc-server\` 绿色自包含部署）搭建与线上比赛同款的 **Leaves `1.21.11` + 开源 `mc-agent-api`（`offline` + `direct` 模式）**，支持任意合法 Bot 名（`3–16` 位字母/数字/下划线）免积分秒进服。

- **连接地址**：
  - **校园网局域网**：API `http://10.50.249.97:18900/api/v1` ｜ 游戏服 `10.50.249.97:25565`
  - **Radmin VPN（跨网段队友）**：API `http://26.14.9.42:18900/api/v1` ｜ 游戏服 `26.14.9.42:25565`
- **本地凭据配置**：已保存在受限文件 `.env.local`（权限 `0600`，Token 独立缓存于 `.state-local/`，与线上比赛服互不干扰）。队友使用时新建自己的 `.env.local` 填入 `MC_API_BASE_URL` 与共享 `MC_SKIN_PASSWORD` 即可。
- **CLI / Python 切换本地测试服**：
  ```bash
  # 登录并自动生成任意离线测试 Bot 进入本地服
  uv run mc-bot --env-file .env.local login LocalBot_01
  uv run mc-bot --env-file .env.local status LocalBot_01
  uv run mc-bot --env-file .env.local logout LocalBot_01
  ```
  ```python
  from mc_agent import BotManager, Settings

  mgr = BotManager(Settings.load(dotenv_path=".env.local"))
  with mgr.open_bot("LocalBot_01").session as bot:
      print(bot.status())
  ```
- **服务端日常管理**（`D:\mc-server\`，Windows 后台静默计划任务 `MC-Leaves-Server` / `MC-Agent-API`）：
  - 查看状态：`ssh 5ease "D:\mc-server\status.bat"`
  - 安全保存并停服：`ssh 5ease "D:\mc-server\stop.bat"`（通过本机 RCON 执行 `save-all` + `stop`）
  - 启动服务：`ssh 5ease "D:\mc-server\start.bat"`
  - 本机发 OP / 改时间等 RCON 指令：`ssh 5ease "D:\mc-server\runtime\node\node.exe D:\mc-server\rcon.js \"op <玩家名>\""`

## 轻量级 Qwen Harness（`mc-harness`，基于最新 `nano-agent` 改造）

位于 `src/mc_agent/harness/`，保留了 `winddd666/nano-agent` 的有界主循环、`ToolScheduler`（`shared` 观察并发 / `exclusive` 动作串行 + 前序失败自动熔断连招）、`RequestContext` 长输出自动折叠、`InputInbox` 运行中实时插队指令以及 `runs/*.events.jsonl` 审计日志，并针对比赛接口与本地测试服做了以下实测优化：

- **自动绕过本地代理（`should_trust_env`）**：对局域网 / Radmin VPN（`10.*`、`26.*` 等）及国内比赛接口默认 `trust_env=False` 直连；对 `api.nju-aia.com` 默认绑定 IPv4 走香港 `HKG` Cloudflare 边缘节点（约 `0.28s`），并将读取超时对齐为 `95s` 防止源站排队时客户端误断重排。
- **比赛 Qwen 网关防坑与提速要点**：
  - 网关请求体必须传 `model="default"`（直传底层权重名 `qwen3.6-35b-a3b` 会返 `HTTP 403`，代码已自动归一化）；关闭思考需传 `chat_template_kwargs: {"enable_thinking": false}`（`/no_think` 标签不生效）。
  - 关闭 `<think>` 后，提示词保留“每轮先输出一句 <=30 字倒推规划再调工具”，可显著减少漏算材料/空手挖石头的试错轮数，并触发单轮多步连招批处理（如单轮连挖 3 块石头）。
  - `mc-agent-api` 的 `craft.count` 参数语义为**目标产物总数量**（如合 12 块木板传 `count=12`），而非配方执行次数。

### 1. 配置 Qwen API 密钥

在 `.env`（或 `.env.local`）中填入以下任一 Qwen 密钥（`mc-harness` 在使用 `--env-file .env.local` 时也会自动回退读取 `.env` 中的模型密钥，无需重复配置）：

- **比赛官方 Qwen 接口（默认）**：填入 `NJU_API_KEY=...`（默认连接 `https://api.nju-aia.com/v1`，`model="default"`，限速 `24 RPM`）
- **阿里云百炼 DashScope / 自定义 Qwen 接口**：填入 `DASHSCOPE_API_KEY=...`（自动默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-plus`）或同时设置 `QWEN_API_KEY`、`QWEN_BASE_URL`、`QWEN_MODEL`

### 2. 启动智能体执行任务

```bash
# 连同学电脑的本地测试服驱动 TestBot_01 执行生存任务（加 --interactive 可在运行中随时打字追加新指令或 /now 紧急插队）
uv run mc-harness --env-file .env.local --interactive \
  "找到玩家 Lovecraft 走到他身边打个招呼，然后在附近砍 3 块橡木原木并合成 1 个工作台。"

# 连线上比赛服驱动默认 Bot
uv run mc-harness "检查周围环境，采集原木并制作一把木镐。"
```

## 1.21.11 知识库、宏技能与分阶段压测（[`docs/expedition-20261114.md`](docs/expedition-20261114.md)）

- **1.21.11 零膨胀知识库（[`src/mc_agent/kb/`](src/mc_agent/kb/)）**：内置 `ITEM_ALIASES`、`RECIPES_1_21`、`ORES_1_21`、`MOBS_AND_MECHANICS` 与 `ADVANCEMENT_PLAYBOOKS`（配 [`src/mc_agent/data/advancements_1_21_11.json`](src/mc_agent/data/advancements_1_21_11.json) 125 项进度目录），通过 `mc_kb_lookup`、`mc_advancement_frontier` 按需检索，并通过 `build_jit_error_hint` 在工具报错时注入针对性修正提示。
- **L0 守护与宏技能（[`src/mc_agent/skills/`](src/mc_agent/skills/)）**：提供水下浮力自适应与自动保命 `check_and_stabilize`，以及 `mc_skill_harvest_logs`、`mc_skill_mine_blocks`（跳过水下矿与 `interrupted` 黑名单）、`mc_skill_craft_gear`、`mc_skill_smelt`（支持狭小石室掏墙龛置炉）、`mc_skill_collect_drops`、`mc_skill_unstick`（含岸崖掘洞登岸 `escape_water`）、`mc_skill_bunker`、`mc_skill_staircase` 等高层封装；并在 [`src/mc_agent/harness/mc_tools.py`](src/mc_agent/harness/mc_tools.py) 内置分页 `/events` 游标追平、三维射线 `hostile_blocked_by_wall` 墙体遮挡威胁评估与原子 `mc_dig` 矿镐等级防护（详见 [`docs/HANDOFF-20261115.md`](docs/HANDOFF-20261115.md)）。
- **多阶段自主托管 CLI（[`scripts/expedition.py`](scripts/expedition.py)）**：支持 `deepseek` / `nju` / `dashscope` 路由、`--min-start-hp` / `--bunker-first` 开局保护及分阶段 diff 摘要：
  ```bash
  # 环境侦察、执行单阶段自主远征、安全断开
  uv run python scripts/expedition.py scout
  uv run python scripts/expedition.py stage --name my-stage --provider deepseek --max-calls 30 --max-tokens 2048 --goal "..."
  uv run python scripts/expedition.py disconnect
  ```
- **2026-11-14 长时自主测试实测总结**：详见 [`docs/expedition-20261114.md`](docs/expedition-20261114.md)（含 DeepSeek-flash 与校内 `qwen3.6-35b-a3b` 延迟/KV Cache 对比、5 项进度解锁记录、7 项真机工程修复与单元测试覆盖）。

## 实时直播与远征复盘看板（`mc-live`）

位于 [`src/mc_agent/live/`](src/mc_agent/live/)，提供纯只读旁路监控 Web 服务（严格不调用 `/auth/login` 或任何动作接口，绝不抢占活跃 `mc-harness` 会话 Token 或动作锁），支持 **服务端无头实景推流 / 上游代理转发**、**本机浏览器窗口采集备选**、**AI 思考与宏技能/熔断/知识库提示流**、**背包与 125 项进度前沿抽屉** 以及 **XZ 轨迹 + Y 海拔联动历史日志回放（`runs/**/*.events.jsonl`）**。

```bash
# 1. 监控线上比赛服（默认读取 .env 与 .state/tokens.json，监听 0.0.0.0:8765，支持离线纯日志回放）
uv run mc-live --host 0.0.0.0 --port 8765

# 2. 监控本地 5ease 仿赛测试服（读取 .env.local 与 .state-local/tokens.json）
uv run mc-live --env-file .env.local --bot LocalBot_01 --port 8765

# 3. 本机代理挂载 nju-beast 实景直播 + Prismarine 旁观转发（队友纯 HTTP 打开即看）
uv run mc-live --host 0.0.0.0 --port 8765 \
  --env-file .env.beast --bot BeastBot_01 \
  --upstream-live-url http://100.93.211.71:18765 \
  --viewer-url http://127.0.0.1:8766 \
  --viewer-proxy-target 100.93.211.71:8766 \
  --viewer-proxy-port 8766
```

- **默认主视图「🎬 实景游戏画面」（自动播放 + 双视角实时切换）**：
  - 页面打开默认直达 **「🎬 实景游戏画面」**（`local_capture`）。当 `beast_director.py`（通过 `--director-url` / `MC_CAPTURE_DIRECTOR_URL`）或上游 `--upstream-live-url`（`MC_UPSTREAM_LIVE_URL`）推流就绪时，浏览器直接自动播放 `/api/capture/stream.mjpg`（纯 HTTP 局域网免操作观看，无需 HTTPS 或手动点选窗口；仅在无服务端流且浏览器支持 `getDisplayMedia` 时于占位卡片提供本机窗口采集备选）。
  - 实景顶栏提供 **「👁 第一视角 / 🎥 第三视角」** 切换按钮：前端调用 `POST /api/capture/camera-mode`（`{"mode": "first_person" | "third_person"}`，另有 `GET /api/capture/camera-mode` 查询），由 `mc-live` 经 `--director-url` 或 `--upstream-live-url` 透传至 `beast_director.py`（`/mode`），通过 Source RCON 在 `/spectate` 第一视角与 ~6 Hz 追尾第三视角之间实时切换，并通过 `/ws-capture` 同步刷新分辨率、FPS 与 `camera_mode` 状态。
  - 右上 **「👁 叠加层」** 一键显隐画面四角的坐标/危急提醒/`busy` 锁叠加层；也可切换 **「🧊 AI 体素 (第三视角 / 第一视角) / 🧊 自由鸟瞰 / 📡 2D 战术雷达」** 查看 AI 盲玩记忆体素与历史回放（[`src/mc_agent/live/static/viewer3d.js`](src/mc_agent/live/static/viewer3d.js)，零侵入、无需在比赛服登录任何旁观小号）。
- **`nju-beast` 无头实景推流与跨机代理拓扑（详细步骤见 [`live_viewer/BEAST_SETUP.md`](live_viewer/BEAST_SETUP.md)）**：
  - `nju-beast`（Tailscale `100.93.211.71`，校园网 LAN `10.50.248.70`）WSL2 内已部署 Leaves `1.21.11`（`:25565`）、`mc-agent-api`（`:18900`）、`Xvfb :99`（Mesa D3D12 硬件加速）+ `BeastCam`、`cam_server.js`（`:8766`）、`beast_director.py`（`:8767`）与 `mc-live`（`:18765`，避让宿主已有容器占用的 `:8765`）；可使用 [`live_viewer/beast_ctl.py`](live_viewer/beast_ctl.py)（`start` / `stop` / `status`）统一启停。
  - 跨机代理模式下，`mc-live` 支持 `--upstream-live-url`（转发 `/api/info` `capture_stream`、`/api/capture/frame.jpg`、`/api/capture/stream.mjpg` 与 `GET/POST /api/capture/camera-mode`）以及内置异步 TCP 中继 `--viewer-proxy-target` / `--viewer-proxy-port` / `--viewer-proxy-host`（对应环境变量 `MC_UPSTREAM_LIVE_URL`、`MC_CAPTURE_DIRECTOR_URL`、`MC_VIEWER_PROXY_TARGET`、`MC_VIEWER_PROXY_PORT`、`MC_VIEWER_PROXY_HOST`）。`mc_agent.config.should_trust_env()` 已对 Tailscale CGNAT（`100.64.0.0/10`）及局域网私网自动直连，不受本机代理变量干扰。
  - **共享只读 Token 注意事项**：`.env.beast` 与 `.state-beast/tokens.json`（均设为 `0600`）已与 `nju-beast` 对齐同一有效会话 Token；切勿在 `mc-live` 运行时重复执行 `mc-bot login BeastBot_01`（重新登录会使旧 Token 立即失效）。执行实时任务请直接运行 `uv run mc-harness --env-file .env.beast ...`（自动探活并复用缓存 Token）。
- **「🟢 Prismarine 备选 3D」旁观流（[`live_viewer/cam_server.js`](live_viewer/cam_server.js)，实测报告见 [`live_viewer/PROBE_RESULT.md`](live_viewer/PROBE_RESULT.md)）**：
  - 适用于 **`5ease` / `nju-beast` 等本地离线测试服**作为备选 WebGL 3D 旁观视角（默认 `:8766`），内置 `1.21.11 -> 1.21.4` 方块 `stateId` 重映射，并在 `createRemappedSocketProxy` 过滤 `item` / `experience_orb` 及 `1.16.4` 不支持的实体避免前端渲染报错；顶部控制栏支持 **「👁 第一视角 / 🎥 第三视角」** 瞬时切换（调用 `GET /api/mode?mode=first_person|third_person`，含 CORS 支持），前端自动将 `127.0.0.1`/`localhost` 等回环地址重写为当前访问主机名以便局域网队友同看。

## Codex 风格 Web 控制台（`mc-console`）

位于 [`src/mc_agent/console/`](src/mc_agent/console/)，为 `mc-harness` 提供本地交互式对话与指令操控前端（默认监听仅本机回环 `http://127.0.0.1:8770`，通过 `--live-url` 联动 `:8765` 只读实景看板）：

```bash
# 手动在前台/调试模式启动（常驻运行建议直接使用下方 scripts/mc_services.sh）
uv run --offline mc-console --env-file .env.beast --bot BeastBot_01 --provider nju --host 127.0.0.1 --port 8770 --live-url http://127.0.0.1:8765
```

- **主要特性**：持久复用单个 `BotSession` 与 LLM KV Cache；运行中支持普通排队（`Enter`）与高优先级紧急插队（`Ctrl/Cmd+Enter` 或按钮，`immediate: true`）以及安全协作停止（`POST /api/stop`）；完整事件落盘 `runs/*.events.jsonl` 与 `:8765` 复盘同步。

## 常驻后台服务管理（与 CLI 窗口解耦）

本机已将 `mc-live` 跨机代理（`:8765` + `:8766`）与 `mc-console`（`:8770`）托管为宿主 `systemd --user` 常驻单元（[`deploy/systemd/`](deploy/systemd/)），关闭终端或 `agy` 窗口后服务持续后台运行：

```bash
# 安装/启用本机两个常驻服务，并查看 systemd 与 HTTP 健康状态
bash scripts/mc_services.sh install
bash scripts/mc_services.sh status

# 重启 / 查看本机服务日志 / 查看或重启远端 nju-beast 全套栈
bash scripts/mc_services.sh restart
bash scripts/mc_services.sh logs 80
bash scripts/mc_services.sh beast-status
bash scripts/mc_services.sh beast-restart
```

- **同局域网只读观战**：开放本机 `8765/tcp`、`8766/tcp` 后，同学直接访问 `http://<本机局域网IP>:8765` 即可观看实景画面、AI 体素视角与轨迹复盘（仅本机 `127.0.0.1:8770` 控制端带写入操作权限）。完整拓扑与排障清单见 [`docs/HANDOFF-20261115.md`](docs/HANDOFF-20261115.md)。
