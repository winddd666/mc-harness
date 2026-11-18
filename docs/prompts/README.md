# 地表探索 / 找村庄提示词

`mc-harness` 新增 `--system-prompt-file`，可直接覆盖默认的采矿优先 system prompt。

## 1. 地表广域探索（推荐，目标扩大 WorldMemory）

```bash
cd /home/xzq/projects/mc
MC_STATE_DIR=.state-maptest \
MC_WORLD_MEMORY_PATH=.state-maptest/world_memory_MapTest_01.json \
uv run mc-harness --env-file .env.beast --bot MapTest_01 \
  --system-prompt-file docs/prompts/surface-explore-system.txt \
  --tool-profile compact --hud-mode ephemeral --context-mode sliding \
  --max-calls 300 --run-timeout-s 5400 \
  "$(cat docs/prompts/surface-explore-task.txt)"
```

特点：

- 明确只做地表移动，不挖矿/不下潜；
- 单步 48–72m，连续走 4–6 步再观察；
- 目标写死“至少 1500m、覆盖 60 个新区块”；
- 每 2–4 步扫描方块/实体，让 `WorldMemory` 记录 POI/栖息地；
- 卡洞穴时只允许 `mc_skill_staircase(vertical="up")` 脱困。

## 2. 找村庄专项

```bash
cd /home/xzq/projects/mc
MC_STATE_DIR=.state-maptest \
MC_WORLD_MEMORY_PATH=.state-maptest/world_memory_MapTest_01.json \
uv run mc-harness --env-file .env.beast --bot MapTest_01 \
  --system-prompt-file docs/prompts/surface-explore-system.txt \
  --tool-profile compact --hud-mode ephemeral --context-mode sliding \
  --max-calls 300 --run-timeout-s 5400 \
  "$(cat docs/prompts/find-village-task.txt)"
```

特点：

- 目标明确为“至少找到 1 个村庄”；
- 定向扫描 `bell / lectern / composter / hay_block / farmland / torch`；
- 同步扫描 `villager / iron_golem / cat`；
- 找到后记录钟/床/箱子/农田，绑定床并扩大周边区块；
- 15–20 步无线索才换方向，避免原地转圈。

## 3. 注意

- `--system-prompt-file` 只影响 `mc-harness` 的 plain task 模式；curriculum 模式仍使用内置课程提示词。
- 专用 system prompt 不能物理禁用挖矿工具，但会明确禁止调用；如果需要硬限制，可再增加一个 `explore` tool profile。
- 跑之前建议先用 `mc-bot connect <bot>` 确认在线，并清空/归档旧 `world_memory_*.json`。
