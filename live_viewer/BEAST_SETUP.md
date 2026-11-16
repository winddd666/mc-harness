# `nju-beast` 部署与无头实景采集指南 (`BEAST_SETUP.md`)

> 安全提示：本文档不包含任何密码、RCON 口令、API Token 或 `.env*` 私密值。所有凭据仅存于受限 `0600` 配置文件中（`/home/xia/mc-beast/.env.beast` / `server.properties` / `api/config.json`）。

---

## 1. 主机与运行环境实测拓扑

- **目标主机**：`nju-beast`（`DESKTOP-TKVRGGF`，Windows 11 Pro Build `26200`）
- **硬件配置**：Intel `i5-13400F`（10C/16T）、`31.78 GB` RAM、`NVIDIA GeForce RTX 5060 Ti`（Driver `32.0.16.1088`）
- **网络地址**：
  - 校园网 LAN：`10.50.248.70/21`
  - Tailscale：`100.93.211.71/32`
- **WSL2 环境**：`Ubuntu-22.04`（用户 `xia`，`.wslconfig` 启用 `networkingMode=mirrored`、`autoProxy=true`、`firewall=true`）
  - Node.js：`v22.14.0`（`/home/xia/mc-beast/runtime/node-v22.14.0-linux-x64/bin/node`）
  - Java：OpenJDK 21（`/usr/bin/java`）
  - Python / 工具：`/home/xia/.local/bin/uv`、`/home/xia/.local/bin/portablemc`（`5.0.6`）
  - 图形与采集：`/usr/bin/Xvfb`、`/usr/bin/ffmpeg`（`4.4.2`）；实测 `Xvfb :99` 支持 **Mesa D3D12（NVIDIA GeForce RTX 5060 Ti）硬件加速 OpenGL 4.2**，无需依赖 Windows 桌面或 RDP 会话，锁屏/断开 RDP 均不会黑屏。

---

## 2. 目录布局与端口分配（`nju-beast` WSL2 `/home/xia/mc-beast`）

```text
/home/xia/mc-beast/
├── leaves/        # Leaves 1.21.11 build 157 (leaves-1.21.11.jar, eula.txt, server.properties[0600])
├── api/           # mc-agent-api v1.3.9 (config.json[0600], npm 依赖已安装)
├── client/        # portablemc 1.21.11 客户端资源 + options.txt
├── mc/            # 同步的项目仓库目录（用于启动 cam_server.js / mc-live / beast_director.py）
├── logs/          # leaves.log, api.log, xvfb.log, beastcam.log
├── runtime/       # Node v22.14.0
└── .env.beast     # 运行时自动生成的 0600 环境配置（对应当前 Leaves RCON 与 API 口令）
```

### 端口规划（重要冲突说明）

> [!WARNING]
> `nju-beast` 的 WSL2 中已有长期运行的 Docker 容器 `local-shell-mcp` 占用 `127.0.0.1:8765->8765/tcp`。**请勿停用或覆盖 `:8765`**，建议将 `mc-live` 绑定到 **`18765`**，并相应在 `beast_director.py` 与 `cam_server.js` 中指向 `18765`。

| 服务 | 绑定端口 | 用途与备注 |
|---|---|---|
| Leaves `1.21.11` | `25565` | 离线模式 MC 服务端（`online-mode=false`, `allow-flight=true`） |
| Leaves RCON | `25575` | 仅本地使用，**切勿在防火墙对外放行** |
| `mc-agent-api` | `0.0.0.0:18900` | `offline` + `direct` 模式 HTTP Bot API |
| `cam_server.js` | `8766` | Prismarine 备选 3D 视角（`LiveCam_01`） |
| `beast_director.py` | `127.0.0.1:8767` | RCON 视角调度 + `ffmpeg x11grab` MJPEG->WS 推流 |
| `mc-live` | `0.0.0.0:18765` | 实时监控面板 HTTP/WS（避让 `:8765` 现有容器） |

---

## 3. 网络与防火墙说明（Tailscale vs 校园网 LAN）

- **Tailscale（已开箱可达）**：`nju-beast` 的 Hyper-V/Windows 防火墙已启用 `Tailscale-In`（`Domain, Private` 允许入站）。从同一 Tailnet 主机（如本机 `100.91.198.86`）可直接访问 `http://100.93.211.71:18900/api/v1/health` 与对应服务端口。
- **代理绕过说明**：`mc_agent.config.should_trust_env()` 已覆盖 RFC1918 私网、链路本地与 Tailscale CGNAT（`100.64.0.0/10`，返回 `False` 直连）。手动用 `curl` 调试时仍建议显式加 `--noproxy '*'`。
- **校园网 LAN (`10.50.248.70`)**：Windows Defender 防火墙当前处于启用状态（WSL2 `firewall=true`），因此 `10.50.248.70` 对外默认拦截上述新增端口。如需从校园网直接访问，请在 `nju-beast` 管理员 PowerShell / CMD 中执行（**仅放行所需服务端口，不放行 RCON `25575`**）：
  ```bat
  netsh advfirewall firewall add rule name=MC-Beast-LAN dir=in action=allow protocol=TCP localport=18900,25565,8766,8767,18765
  ```

---

## 4. 已就绪与实测验证的服务栈

### 4.1 `nju-beast` 实测运行服务（WSL2 `Ubuntu-22.04`）

1. **Leaves `1.21.11` (`:25565`)**：已运行，已设置 `send_command_feedback=false`，并授予 `BeastCam` / `LiveCam_01` OP 与 `spectator` 模式。
2. **`mc-agent-api v1.3.9` (`0.0.0.0:18900`)**：已运行；`BeastBot_01` 在线就绪（`online: true`, `ready: true`），本机可通过 `uv run mc-bot --env-file .env.beast status BeastBot_01` 经 Tailscale 直连查询状态。
3. **`Xvfb :99` (`1280x720x24`) + `BeastCam` (`1.21.11`)**：已连接 `127.0.0.1:25565` 并进入世界（Mesa D3D12 `NVIDIA GeForce RTX 5060 Ti`）。
4. **`cam_server.js` (`0.0.0.0:8766`)**：Prismarine `1.21.11` 旁观视角 `LiveCam_01` 已连接并就绪（`/health` `ok: true, connected: true, spawned: true`）。
5. **`beast_director.py` (`127.0.0.1:8767` -> `ws://127.0.0.1:18765/ws-capture`) + `mc-live` (`0.0.0.0:18765`)**：实景推流与 HTTP/WS 面板已联动；本地 `.env.beast`（`0600`）与共享只读 `.state-beast/tokens.json`（`0600`）已同步。

### 4.2 启停与同步管理（`live_viewer/beast_ctl.py`）

已提供无第三方依赖的控制器 `live_viewer/beast_ctl.py`，在 `nju-beast` WSL2 中执行一条命令即可安装依赖、校验 `BeastBot_01` Token、配置 Leaves 旁观与 `send_command_feedback false`，并重启 `cam_server.js` (`:8766`)、`mc-live` (`:18765`) 与 `beast_director.py` (`:8767`)：

```bash
ssh -n -F /home/xzq/.ssh/config nju-beast wsl -d Ubuntu-22.04 -u xia -- python3 /home/xia/mc-beast/mc/live_viewer/beast_ctl.py start
```

> [!NOTE]
> `mc-agent-api` 在对同一 Bot 调用 `POST /api/v1/auth/login` 时会吊销旧 Token（`REPLACE BeastBot_01`），且 `mc-live` 仅以只读方式加载 `tokens.json`。请勿在启动 `nju-beast` 的 `mc-live` 后再从其他机器对 `BeastBot_01` 重复执行 `mc-bot login`；如需在本机共享只读查询或本地常驻转发，请保持本机 `.state-beast/tokens.json`（权限 `0600`）与 `nju-beast` 同份有效 Token，并使用本仓库托管的 `systemd --user` 常驻服务一键启动本机代理（`:8765` / `:8766`）与 Codex 控制台（`:8770`）：
>
> ```bash
> bash /home/xzq/projects/mc/scripts/mc_services.sh install
> bash /home/xzq/projects/mc/scripts/mc_services.sh status
> bash /home/xzq/projects/mc/scripts/mc_services.sh beast-status
> ```
>
> 等价单步本机转发命令为：
>
> ```bash
> uv run mc-live --host 0.0.0.0 --port 8765 \
>   --env-file .env.beast --bot BeastBot_01 \
>   --upstream-live-url http://100.93.211.71:18765 \
>   --viewer-url http://127.0.0.1:8766 \
>   --viewer-proxy-target 100.93.211.71:8766 \
>   --viewer-proxy-port 8766
> ```

如需手动分步启动或排查，等价命令如下：

1. **安装依赖与 RCON 初始化**：
   ```bash
   cd /home/xia/mc-beast/mc
   /home/xia/.local/bin/uv sync --frozen
   PATH="/home/xia/mc-beast/runtime/node-v22.14.0-linux-x64/bin:$PATH" \
     npm --prefix /home/xia/mc-beast/mc/live_viewer install --omit=optional
   ```
   Leaves RCON / 控制台需确保（1.21.11 使用 snake_case `send_command_feedback`）：
   ```text
   op BeastCam
   op LiveCam_01
   gamerule send_command_feedback false
   gamemode spectator BeastCam
   gamemode spectator LiveCam_01
   ```

2. **分步启动 `cam_server.js` (`:8766`)、`mc-live` (`:18765`) 与 `beast_director.py` (`:8767`)**：
   ```bash
   # 1) Prismarine 备选 3D 伴随视角 (:8766)
   /home/xia/mc-beast/runtime/node-v22.14.0-linux-x64/bin/node \
     /home/xia/mc-beast/mc/live_viewer/cam_server.js \
     --mc-host 127.0.0.1 --mc-port 25565 --cam-user LiveCam_01 \
     --viewer-host 0.0.0.0 --viewer-port 8766 --follow-api http://127.0.0.1:18765/api/telemetry-pos

   # 2) mc-live 实时看板 (:18765，避开已占用的 :8765)
   cd /home/xia/mc-beast/mc
   /home/xia/.local/bin/uv run mc-live \
     --host 0.0.0.0 --port 18765 \
     --env-file /home/xia/mc-beast/.env.beast \
     --bot BeastBot_01 \
     --viewer-url http://127.0.0.1:8766 \
     --director-url http://127.0.0.1:8767

   # 3) BeastCam RCON 导演 + X11 推流 (:8767 -> ws://127.0.0.1:18765/ws-capture)
   /home/xia/.local/bin/uv run python \
     /home/xia/mc-beast/mc/live_viewer/beast_director.py \
     --host 127.0.0.1 --port 8767 \
     --ws-url ws://127.0.0.1:18765/ws-capture \
     --env-file /home/xia/mc-beast/.env.beast \
     --rcon-host 127.0.0.1 --rcon-port 25575 \
     --bot BeastBot_01 --cam-user BeastCam \
     --capture-mode x11grab --display :99 \
     --video-size 1280x720 --fps 15 --mode third_person
   ```

---

## 5. 双视角像素与接口验证清单

在客户端主机运行以下检查（将 `<host>` 替换为 `100.93.211.71` 或放行防火墙后的 `10.50.248.70`）：

1. 检查 `/api/info` 中 `capture_stream.active == true`、`director_available == true`、`camera_mode == "third_person"`：
   ```bash
   curl --noproxy '*' -s http://<host>:18765/api/info | python3 -m json.tool
   ```
2. 抓取 `third_person` 与 `first_person` 帧并校验 JPEG 头、分辨率 `1280x720` 及像素方差（非黑屏且切换视角后画面显著变化）：
   ```bash
   curl --noproxy '*' -sf http://<host>:18765/api/capture/frame.jpg -o /tmp/tp.jpg
   curl --noproxy '*' -sf -X POST http://<host>:18765/api/capture/camera-mode \
     -H 'Content-Type: application/json' -d '{"mode":"first_person"}'
   sleep 1.5
   curl --noproxy '*' -sf http://<host>:18765/api/capture/frame.jpg -o /tmp/fp.jpg
   python3 -c "from PIL import Image, ImageStat; [print(p, Image.open(p).size, ImageStat.Stat(Image.open(p)).mean, ImageStat.Stat(Image.open(p)).stddev) for p in ('/tmp/tp.jpg','/tmp/fp.jpg')]"
   ```
3. 浏览器打开 `http://<host>:18765/`，确认默认 `🎬 实景游戏画面` 标签自动播放，且可在 `👁 第一视角` 与 `🎥 第三视角` 之间无报错切换。

最近一次经 Tailscale (`100.93.211.71`) 实测结果：
- `third_person`：`1280x720` JPEG（`mean=135.21`, `std=78.56`），`seq` 在 `1.0s` 内持续推进（`1795 -> 1812`）
- `first_person`：`1280x720` JPEG（`mean=115.16`, `std=81.18`），与 `third_person` 像素 MAE = `37.49`
- 切回 `third_person`：`1280x720` JPEG（`mean=135.84`, `std=78.07`），`camera_mode` 恢复 `"third_person"`

---

## 6. 常见故障排查

- **`:8765` 绑定失败 (`Address already in use`)**：`nju-beast` 上已有 Docker 容器 `local-shell-mcp` 占用 `127.0.0.1:8765`，请务必使用 `--port 18765`（并给 `beast_director.py` 传入 `--ws-url ws://127.0.0.1:18765/ws-capture`）。
- **Leaves `1.21.11` 切换视角或移动时刷屏 / 踢出**：确认已设置 `allow-flight=true` 并通过 RCON 执行 `op BeastCam`、`gamemode spectator BeastCam`、`gamerule send_command_feedback false`。
- **校园网 `10.50.248.70` 连接超时而 `100.93.211.71` 可通**：检查 Windows 防火墙规则是否已放行 `18900,25565,8766,8767,18765`。
- **`Xvfb :99` 日志提示 `_XSERVTransSocketCreateListener: failed to bind listener`**：WSLg 挂载的 `/tmp/.X11-unix` 限制了常规 UNIX socket 监听，但 X11 客户端（`xdpyinfo -display :99`、`ffmpeg -f x11grab -i :99`、`LWJGL/Minecraft`）通过抽象命名空间 socket 正常工作，不影响渲染与抓帧。
