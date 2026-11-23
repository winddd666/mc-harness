# Slice D1 交接说明（轨迹深挖与根因压缩官）

> 供接手 agent 直接读取执行。最后更新：2026-11-20 14:05（6 波收官后）
> 配套报告：`docs/D1-r11-triage.md`（节 4by / 4bz / 4bz 之后继续追加）

## 一、身份与硬边界

- **写入目录**：`/home/xzq/projects/mc/.worktrees/s-triage`（脚本、报告、commit）
- **只读分析**：`/home/xzq/projects/mc/.worktrees/v2-integrated/runs/`
- **共享看板**：`/home/xzq/projects/mc/.worktrees/v2-integrated/docs/BUG_BOARD.md`
  - D1 可以改，但**不要 commit**（v2-integrated 那棵树归 integrator）
- **严禁**：修改 `src/`、连接 nju-beast/RCON、启动在线 bot、打印 `.env*` / `tokens.json`
- **Python**：`/home/xzq/projects/mc/.venv/bin/python`

## 二、已完成状态（不要重做）

6 波全部收官，共 **1,581 episode / 40 条 bot 轨迹**：

| 波次 | 配置 | 时长 | 集数 | 429 | 总分 |
|---|---|---|---|---|---|
| `overnight_12bot` | 12 路 nju/glm/ds | 4h | 582 | 9 | 1900 |
| `overnight_12bot_part2` | 12 路 | 3.9h | 596 | 52 | 1945 |
| `hy4_audit_r25` | 4 路 + rpm=6 | 20min | 17 | 0 | 335 |
| `hy4_audit_r26` | 4 路 + rpm=6 | 20min | 92 | 0 | 535 |
| `hy4_audit_r27` | 4 路 + rpm=6 | 3h | 126 | 0 | 520 |
| `hy4_audit_r28` | 4 路 + rpm=6 | 2.5h | 167 | 0 | 645 |

**三条核心结论**：

1. **91.1% 的 episode 得分为 0**，真实成交率仅 7.0%（`answered` ≠ 成功）
2. hy4 的 **4 路 + rpm=6** 在 6.2h / 402 集里 **0 次 429**，可定为默认配置
3. Qwen 通道 5 路 = 17~25% retry、**18.1% 墙钟耗在退避**，是唯一的问题通道

## 三、看板现状

- 编号范围：**BUG-001..063 / P2-1..40**
- D1 已立：BUG-042..048、BUG-056..060、P2-33..40
- 并行 slice 已立：BUG-049..055（课程层：下界派主世界 bootstrap、缺前置强派、地下派地表任务、缺工作台/满包、pillar 自锁）——**不要重复立票**
- **BUG-063 明确指派给 Slice D1**（评测口径：Fleet `Deaths` 双计），优先处理
- 新票从 **BUG-064 / P2-41** 起；立票前先 grep 确认无重号

## 四、现成工具（`scripts/`，均已 commit）

```bash
# 1) 全量逐集抽取（主力）
python scripts/d1_episode_audit.py --wave overnight_12bot --wave hy4_audit_r28 \
    --json /tmp/d1_eps.jsonl
# 输出：状态分布 / 每类错误签名 / 逐 bot 卡点表

# 2) 单集事件时间线（定位具体机制）
python scripts/d1_ep_trace.py --wave W --bot B --ep N [--tail 40 | --all]

# 3) 门禁验收
python scripts/d1_acceptance.py --wave W

# 4) 过夜低频值守（收官或每 45 分钟退出一次）
python scripts/d1_overnight_watch.py --every-min 45 \
    --prefix overnight_12bot --prefix hy4_audit_

# 5) 前后对照 / 失败排行 / 专项
python scripts/d1_compare.py W1 W2
python scripts/d1_triage.py
python scripts/d1_geofence_check.py / d1_stick_check.py / d1_budget_check.py
```

## 五、测量口径（错一次会得出相反结论）

- events 里的 `elapsed` 是**该 episode 内**的相对时间，跨集不可相加
- 单次调用耗时 = `completed.elapsed - started.elapsed`
- **`answered` ≠ 成功**：只有 `goal_satisfied` 或 `objective_verified` 才算成交
- 波次级统计用 `wild-endurance-*.json` 的 `curriculum_summary`，不要用进行中快照
- 死亡计数目前存在双计（`guardian.incident` + `mc.notable_events`）→ 见 BUG-063

## 六、已证伪的假线索（不要再追）

- ❌ "hy4 过载是上下文膨胀" → 失败集 `actor_requests` 中位 3，是通道级饱和
- ❌ "缩小历史能缓解 429" → 只有降并发 / 加 RPM / 改重试策略有效
- ❌ "max_tokens 截断是主要报废源" → 65 次截断 64 次被 `length_recovered` 救回
- ❌ "unstick 有 55s 慢调用" → 55s 正常；真问题是 404s 的被中止调用（BUG-047）
- ❌ "staircase `stop_reason=None` 是 bug" → 三类正常错误路径（围栏 / 反射抢占 / 重复失败保护）

## 七、当前任务（按顺序）

1. **BUG-063（指派给 D1）**：统一死亡口径（区分 death / respawn / connection_changed；有事件 ID 按 Bot+ID 去重），给离线夹具，并对账 `r26/hy4_6` 榜单 51 vs JSONL 20 条
2. 重新武装 watcher（见上），每次收到通知后必须重新武装
3. 新波次出现时：`d1_acceptance.py --wave W` → `d1_episode_audit.py` 逐集过 → `d1_ep_trace.py` 定位机制
4. 修复验收：BUG-042 / 045 / 046 / 047 / 056 / 057 / 058 / 059 / 060 的"验收标准"逐条核对
5. 修复上线后用 `d1_compare.py 前波 后波` 出前后对照，把板子状态改成 FIXED + 实测数据

## 八、交付规范

- 每个结论可追溯：波次 / bot / EP / 文件路径 / 源码行号
- 报告追加在 `## 4bz` 之后，新节号从 `## 4ca` 起
- commit 格式：`docs(d1): <一句话结论> — <关键数字>`
- 发现 P0/P1：先写 BUG_BOARD（影响 / 实据 / 源码定位 / 最小修复 / 验收标准），再在报告展开
- 空闲时不要连续轮询，等 watcher 通知或用户指令

## 九、待修优先级（按能救回多少零分集）

| # | 票 | 卡点 | 规模 |
|---|---|---|---|
| 1 | **BUG-056** (P0) | `escape_water` 无 deadline | 19 集整集报废 / 4.69h |
| 2 | **BUG-049/050** (P0) | 下界派主世界 bootstrap、缺前置强派 | 494 集（31.2%）花在 0% 目标 |
| 3 | **BUG-042/045** | 429 重试预算 + Qwen RPM | 61 集 + 18% 墙钟 |
| 4 | **BUG-057** | `hot_floor` 无反射 | 2,129 次伤害 / 34 死 |
| 5 | **BUG-058** | `path_not_found` 往返横跳 | 1,137 次 / 446 集 |
| 6 | **BUG-059 + 051** | `climb_out` 7 秒/格 + 地下派地表 | 58 集回程超时 |
| 7 | **BUG-048 / 055** | 围栏自锁 / pillar 自锁 | 307 / 213 次 |
| 8 | **BUG-030 / 037** | `depth_reset` / 软方块路由 | 41 / 592 次 |
