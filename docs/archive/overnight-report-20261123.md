# 过夜自动闭环晨间总对比报告（2026-11-22 – 2026-11-23）

> **主线分支**：`feat/v2-integrated` @ `b04a430`（`/home/xzq/projects/mc/.worktrees/v2-integrated`）
> **离线回归**：`tests/test_generation_guard.py` + `tests/test_mining_dfs.py` + `tests/test_v2_harness.py` = **`1759 passed, 0 failed`（74.32s）**
> **实机总计**：Wave 12 / 13 / 14a / 15 + **3 轮 7 机 60 分钟 L2 长测**（累计 35+ Bot-小时，双通道 `nju`×3 + `hy4`×4 全程 0 次 429）

---

## 1. 上游 `mc-agent-api` PR 状态
- **PR #4**（`chase=false` 原地挥刀 + `feedback=true` 逐次挥击回执 + `rayBox` 双射线遮挡校验）：**已 MERGED**
- **PR #5**（1.21.11 108 个镐类方块挖矿时间修复）：**已 MERGED**

---

## 2. 本夜合入与根因闭环总览
| 问题 / 方向 | 关键提交 | 实测效果 |
|---|---|---|
| `oxygen` 15× 量纲错配（`0..20` vs `300`）与浅水误抢占 | `7bc9e71`, `547bcce`, `a524f58`, `9a5921e`, `ad26985` | 浅水挖石连断 10 次彻底消失；`deep_water` 60m 仅 1 死、5% 取消 |
| 工具被小脑抢占回执缺结构化错误码 | `5f18372`, `99c5d11` | 统一输出 `error.code="guardian_preempted"` + `stop_reason` + `reflex_mode` |
| `BUG-116`（`_UnsafeMiningLanding` 误炸整片 `mine_dfs`） | `6eaa9be`, `86cf51d`, `ebdd7dd` | `mc_skill_mine_dfs:unsafe_landing` 从 W12 的 **18 次** 降至 W14a/W15/L2#1–#3 全程 **0 次** |
| 战斗/下界/末影人/烈焰人/凋灵骷髅/守尸保护 | `a15c1b7`, `4021c3f`, `669fce4`, `9eefe10`, `f409639`, `2fd39a6`, `fa7e768`, `c57749e` | `nether_fortress` & `nether_bastion` 双创 **`205` 分**，L2 #2 7/7 取消率压至 **`3%–9%`** |
| `BUG-113` / `BUG-114` / 落点与甜浆果丛防护 | `8a52210`, `97b7a71`, `b4984e0`, `7881654`, `5089c2c` | `bare` 出生点不再踩甜浆果丛触发抢占，无暴露石头林区可原地穿土采石 |

---

## 3. 关键场景前后对比
| 维度 | 过夜前基线（W9 / W11 / OB_W3） | 过夜后实测（W12–W15 / L2 #1–#3） |
|---|---|---|
| **Guardian 取消率** | `18%–39%`（`hy_5e_2_bare` 38.7%，`nju_ob_1/2` 30%+） | L2 #2 全员 **`3%–9%`**；`NJU_OB_3` 多次 **`0%`**（142 req / 0 cancel） |
| **`mine_dfs` `unsafe_landing` 炸片** | W12 首次合入爆发 **18 次** | 合入 `6eaa9be` 后连续 5 轮 **0 次** |
| **下界长线（60m）** | 约 `160–175` 分 | `nether_fortress` **`205` 分**、`nether_bastion` **`205` 分**（双双解锁要塞/堡垒） |
| **综合发育（`village` / `midgame` / `enchant_anvil` 60m）** | `120–135` 分 | 稳居 **`135–150` 分**（`NJU_OB_3` 0 死挖 12 钻 + 试炼密室；`midgame` 0 死解锁磁石；`enchant_anvil` 0–1 死附魔） |
| **`deep_water` 水域专项** | 历史 9–10 次三叉戟死亡 | W13 12m **0 死 `40` 分**；L2 #1 60m **仅 1 死 `45` 分** |
| **`bare` 白板开局** | 60m 多死卡 10–15 分 | W12/W14a 取消率压至 `0%`；L2 #2 `NJU_OB_1` 7% 取消打通 `iron_tools`（`20` 分）；防守尸 `c57749e` 已合入待下一轮复测 |

---

## 4. 遗留与次日建议优先项
1. **`fix/s-skills` 合入**：`BUG-090`（熔炼燃料校验）、`BUG-107`（去皮原木合木板）、`BUG-109`（装备确认）因 `library.py` 旧基线有冲突暂未强合，建议 Slice C rebase 到 `b04a430` 后合入；
2. **`BUG-115`（Slice A）**：`mining_dfs.py` 切片内单次 `movement_not_confirmed` 轻脱困重试，进一步减少地下切片早退；
3. **`bare` 守尸复测**：验证刚合入的 `c57749e`（被反复守尸切备用点）+ 进一步压缩 `bare` 从铁镐到首 4 钻的耗时。
