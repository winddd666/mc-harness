# `mc-agent` 项目 Agent 操作规范

> 本文件供在该仓库工作的编码 Agent 与辅助模型优先遵循；与全局用户规则配合生效。

## 1. 仓库定位与核心入口

- **工作目录**：`/home/xzq/projects/mc`（本地 Git 仓库已初始化；提交前务必确认 `.env*`、`.state*`、`runs/`、`backups/` 保持忽略，严禁将凭据或大体积日志提交入库）
- **Python 包管理工具**：`uv` + 项目本地虚拟环境 `.venv`（Python >= 3.11）。离线单测统一使用 `uv run --offline pytest -v`。
- **四大 CLI 入口**（见 [`pyproject.toml`](pyproject.toml)）：
  - `mc-bot`（[`src/mc_agent/cli.py`](src/mc_agent/cli.py)）：NMO 皮肤站建号/签到、MC API Token 签发、Bot 状态查询与退出。
  - `mc-harness`（[`src/mc_agent/harness/agent.py`](src/mc_agent/harness/agent.py)）：命令行版自主生存 Harness，支持 `--interactive` 插队。
  - `mc-live`（[`src/mc_agent/live/server.py`](src/mc_agent/live/server.py)）：纯只读实时直播与历史回放服务（`0.0.0.0:8765`），支持实景 MJPEG 代理、Prismarine 3D TCP 转发（`:8766`）与前端叠加层控制。
  - `mc-console`（[`src/mc_agent/console/server.py`](src/mc_agent/console/server.py)）：本地 Codex 风格 Web 交互控制台（`127.0.0.1:8770`）。

## 2. 常驻后台服务管理（与 CLI 窗口解耦）

本机核心后台服务已托管至宿主 `systemd --user`，通过统一脚本 [`scripts/mc_services.sh`](scripts/mc_services.sh) 管理，**不要再在临时会话里用后台进程占用 `:8765` / `:8766` / `:8770`**：

```bash
# 查看本机常驻服务与 HTTP 健康状态
bash /home/xzq/projects/mc/scripts/mc_services.sh status

# 安装/更新 unit 或重启服务
bash /home/xzq/projects/mc/scripts/mc_services.sh install
bash /home/xzq/projects/mc/scripts/mc_services.sh restart

# 查看远端 nju-beast 全套服务状态（Leaves / API / Xvfb+BeastCam / cam_server / mc-live / director）
bash /home/xzq/projects/mc/scripts/mc_services.sh beast-status
```

## 3. 沙箱执行规范（重要：避免无意义的沙箱报错）

本机 Agent 默认运行在带网络/挂载隔离的终端沙箱中：

1. **直接在沙箱内运行（`BypassSandbox: false`）**：
   - `uv run --offline pytest -v`
   - 工作区内纯本地文件的 `rg` / `ls` / `chmod` / 离线 Python 脚本（如只读分析 `runs/*.events.jsonl`）。
2. **直接请求沙箱外运行（`BypassSandbox: true`），严禁先在沙箱内“明知故犯”试跑一次再重试**：
   - 访问本机回环或局域网常驻服务端口（`http://127.0.0.1:8765`、`8766`、`8770`）
   - 执行 `systemctl --user`、`journalctl --user` 或 `bash /home/xzq/projects/mc/scripts/mc_services.sh ...`
   - 通过 `~/.ssh/config` 连接 Tailscale 远端主机 `nju-beast` (`100.93.211.71`) 或 `5ease`
3. **保持命令形状干净以便前缀匹配授权**：优先直接使用单一字面命令（例如 `bash /home/xzq/projects/mc/scripts/mc_services.sh status`），避免在外部命令中混用 `$()`、反引号、`{a,b}`、`$VAR` 或 `&&`/`;` 串联特权指令与纯读取指令。

## 4. 凭据、Token 与实机安全红线

- **单 Token 互斥原则**：`mc-agent-api` 对同一 Bot 每次 `POST /auth/login` 都会立即吊销旧 Token。当 `mc-live` 或 `mc-console` 正在运行并复用 `.state-beast/tokens.json` 时，**禁止对同角色（如 `BeastBot_01`）再次执行 `mc-bot login`**，直接探活复用既有 Token。
- **凭据保密**：`.env*`、`.state*/tokens.json` 与 `nju-beast` 侧 `server.properties` / `api/config.json` 必须保持 `0600` 权限，严禁将其内容打印到日志、文档、回复或提交记录中。
- **总览、阶段实测与待办文档**：系统全景架构与实机修复归档于 [`docs/HANDOFF-20261115.md`](docs/HANDOFF-20261115.md)；2026-11-16 65 分钟压测复盘与待修路线图见 [`docs/endurance-findings-20261116.md`](docs/endurance-findings-20261116.md) 与 [`docs/TODO.md`](docs/TODO.md)。
