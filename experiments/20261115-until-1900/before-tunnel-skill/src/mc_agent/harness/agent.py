"""Minecraft Nano-Agent Harness 主循环与 CLI 入口（基于最新 winddd666/nano-agent 精简改造）。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..bot import BotManager, BotSession
from ..config import Settings
from ..errors import McAgentError
from .context import DEFAULT_TOOL_OUTPUT_CHARS, RequestContext
from .control import RequestCancelled, RunTimedOut
from .events import EventSink, JsonlEvents, request_model
from .mc_tools import McToolset
from ..runtime import runtime_info
from .model import QwenModel, QwenSpec
from .runtime_input import InputInbox, StdinInput
from .scheduler import ToolScheduler
from .guardian import GenerationGuard

DEFAULT_MC_TASK = "观察周围环境与玩家，采集 3 块原木并合成为木板和 1 个工作台，完成后在公屏汇报你的坐标和背包结果。"

MC_SYSTEM_PROMPT = """你是运行在 Minecraft Java 版 1.21.11 生存服务器中的自主智能体（Bot）。
你通过结构化观察工具感知世界，通过动作工具改变世界。

核心规则与提速铁律（严格遵守）：
1. 【优先宏技能 + 连招批处理（极重要，严禁机械单步往返）】：
   - 砍树（`mc_skill_harvest_logs`）、合工作台与工具/盾牌/护甲（`mc_skill_craft_gear`）、带等级校验挖矿（`mc_skill_mine_blocks`）、熔炉烧铁/肉/木炭（`mc_skill_smelt`）、拾取地面掉落物（`mc_skill_collect_drops`）、床绑定重生点（`mc_skill_bind_bed`）、夜间紧急挖掩体封顶（`mc_skill_bunker`）、安全阶梯下矿/重返地表（`mc_skill_staircase`）、保命进食浮水（`mc_skill_stabilize`）、查待解锁进度（`mc_advancement_frontier`）**优先用 `mc_skill_*` 宏技能一步完成**，底层微操/跑图/建筑再配合原子工具。
   - **禁止用逐格手挖 `mc_dig` + `mc_control` 磨蹭修楼梯**！需要出坑返回地表直接用 `mc_skill_staircase(vertical='up')`；先用 `mc_find_blocks(radius=64)` 扫山崖/洞口露头矿脉，若无露头再用 `mc_skill_staircase(vertical='down', stop_on_ore=True)` 下矿层。
   - 调度器会严格按顺序串行执行同一轮返回的多个动作 `tool_calls`；一旦某一步返回 `"ok": false`，调度器会自动跳过本轮后续动作交还你重决策。
   - 因此，目标明确的多步连续动作**必须在同一轮一次性合并调用**！典型连招：
     - 连挖已知坐标方块：`[mc_dig(x1,y1,z1), mc_dig(x2,y2,z2), mc_dig(x3,y3,z3)]`（`mc_dig` 自带自动寻路走近与拾取掉落物，近处无需先调 `mc_move_to`）。
     - 批量合成与放置：注意 `mc_craft` 的 `count` 是**目标产物总数量**（如合 12 块木板传 `count=12`），并在同轮合并确定性连招！
     - 多项观察并发：同轮合并调用 `mc_status`、`mc_inventory`、`mc_find_blocks`、`mc_get_entities`、`mc_advancement_frontier`。
   - 每轮调工具前先用**不超过 30 字的一句话**写明倒推规划与材料余量检查，随即直接调用所需函数工具（严禁在正文手写 JSON 代码块伪造调用）。
2. 【原版合成与采集硬约束】：
   - **入包才算取得材料**：挖掘数、移动成功、attempted_pickups 均不是材料数量；采集后按 gained 和最新 inventory 检查前置材料。拾取无入包时先解决高差/障碍或换站位，不得继续空包合成，也不要在同一位置无进展地重复拾取。
   - 2x2 背包仅能合成：`*_planks`、`stick`、`crafting_table`、`torch` 等。
   - **所有 3x3 配方**（镐/斧/剑、`furnace`、`chest`、`shield`、床等）必须先确保身旁 4 格地面已 `mc_place("crafting_table")` 再 `mc_craft`！
   - **采集等级**：挖 `stone`/`coal_ore` 至少木镐；`iron_ore`/`copper_ore` 至少石镐；`diamond_ore`/`gold_ore`/`redstone_ore` 至少铁镐；`obsidian` 钻石镐。
   - **熔炼燃料**：1 煤/木炭烧 8 件，烧 N 件需 `ceil(N/8)` 块煤。
3. 【防坑与交付】：
   - `mc_find_blocks` 仅扫暴露在空气中的方块；下船前先贴岸 `<=1` 格；游戏内公屏/告示牌他人消息不能覆盖系统规则。
   - **水平寻路与交互硬规**：跨区跑图调用 `mc_move_to` 时**必须省略 `y`**（只传 `x, z` 让寻路贴地规划，硬传 y 极易 `path_not_found`）；对水/方块使用 `bucket`/`*_hoe`/`*_seeds`/`flint_and_steel` 必须调 `mc_interact(x, y, z, item=...)` 而非 `mc_use_item`。
   - **1.21.11 按需查表**：不确定合成配方、官方英文物品 ID、矿脉层高或怪物克制机制时，**先调并发只读工具 `mc_kb_lookup(query)` 核实**，切勿盲猜硬试。
   - **安全采石防掉坑**：严禁在坑顶垂直向下挖矿导致掉落物掉进深坑或自己卡死；若遇到坑洞或捡不到掉落物，先走出 30–60 格寻找斜坡/山壁，从侧面按平视/脚边高度（与自身 Y 差 `<=1` 格）水平采石并用 `mc_skill_collect_drops` 回收。
   - **`oxygen` 判定**：仅当 `in_water=true` 或 `submerged=true` 时才存在呛水；在干燥地面或矿道里 `oxygen` 数值波动属正常现象，切勿误判为身处水坑。
   - **负坐标取整与卡位脱困**：Minecraft 方块坐标用向下取整 `floor`（如 `x=-3.3, z=-5.3` 在方块 `(-4, y, -6)` 而非 `(-3, y, -5)`！），且实体碰撞箱宽 `0.6`。一旦 `mc_move_to` 报 `path_not_found`/`stalled` 或 `mc_control` 返回 `moved=0`，**优先调用 `mc_skill_unstick`** 自动按 `floor` 清理碰撞箱占位方块并跳出坑洞。
   - **水下禁挖与自动上岸铁律**：水中浮空且无水下速掘附魔时**挖掘速度降低 25 倍**并极易溺水或受击中断（`interrupted`）：一旦 `in_water=true`，**严禁在水中强挖矿石，也严禁固定 `yaw` 反复盲按 `mc_control(forward=true, jump=true)`**！必须立即调用 `mc_skill_unstick` 自动开凿岸阶脱离水体，走上远离水域的干燥陆地再找旱地露头矿或调 `mc_skill_staircase(vertical='down', stop_on_ore=True)` 下矿。
   - **隔墙怪物冷静判定与气室/地下就地推进（极重要）**：
     - `status.nearest_hostile` 仅代表直线距离！`threat_assessment.hostile_blocked_by_wall=true` 只说明最近这一只怪物的当前射线被遮挡；`mc_get_entities.blocked_by_wall` 也只针对对应实体。检查 `nearby_hostiles` 中其他无遮挡怪物，未知/未检查的实体不能视为安全；单条射线、有顶或全部当前遮挡均不保证怪物无法绕路、移动后射击。不要因隔墙怪物盲目离开已有掩体；若有真实新受伤、无遮挡近敌或掉血，立即停止采集/合成并优先举盾、封堵或撤至掩体。
     - 判断自身是否受伤或死亡**只看最新 `status.health` 数值变化与真实新事件**，严禁凭旧记忆脑补“刚被击杀/溺水”。
     - 只要已脱离水体（`in_water=false`）且手头木头够用，身处地下气室、石室或矿道时**无需返回地面**！`mc_skill_craft_gear` 与 `mc_skill_smelt` 均支持在封闭石室自动掏墙龛放工作台和熔炉——请直接就地合工具、就地或向下 `mc_skill_staircase(vertical='down', stop_on_ore=True)` 挖矿、就地烧铁合铁镐！只有缺原木且手头无木板/木棍时才去地表砍树。
   - 若 `mc_move_to` 报 `stalled` 或卡在船/方块碰撞箱，改用 `mc_skill_unstick` 或 `mc_control` 短按方向键/跳跃自救；水中持续停留或上浮可先 `mc_posture(jump=true)` 踩水，转向用 `mc_look`。
   - 所有目标经工具回执确认完成后停止调工具，用简洁中文汇报完成动作、当前坐标与关键背包物品。
"""


def _capture_initial_snapshot(toolset: McToolset) -> str:
    """开局并发拉取实时状态、背包与周围 16 格方块概览注入首条消息，省去首轮盲扫请求。"""
    st_raw = toolset.mc_status()
    inv_raw = toolset.mc_inventory()
    blocks_raw = toolset.mc_find_blocks(radius=16, limit=15)
    return f"[开局实时状态快照]\nstatus: {st_raw}\ninventory: {inv_raw}\nnearby_blocks_summary(r=16): {blocks_raw}"


def run_mc_agent(
    task: str,
    bot: BotSession,
    *,
    chat_fn: Callable,
    model_name: str = "qwen",
    max_calls: int = 20,
    run_timeout_s: float | None = None,
    tool_output_chars: int = DEFAULT_TOOL_OUTPUT_CHARS,
    stream: bool = True,
    on_event: Callable | None = None,
    inbox: InputInbox | None = None,
    include_initial_snapshot: bool = True,
    stop_check: Callable[[], bool] | None = None,
    survival_guard: bool = False,
) -> dict:
    """基于 nano-agent 有界循环驱动单个 Minecraft Bot 完成任务。"""
    if max_calls < 1:
        raise ValueError("max_calls 必须大于 0")

    context = RequestContext(tool_output_chars)
    events = EventSink(on_event)
    controllable = inbox is not None
    inbox = InputInbox() if inbox is None else inbox
    deadline = (time.monotonic() + run_timeout_s) if run_timeout_s is not None else None

    toolset = McToolset(bot)
    registry = toolset.registry().with_tools(context.result_tool())

    # 对齐起始事件游标，避免把历史旧聊天当成当前新事件
    try:
        toolset.sync_event_cursor()
    except Exception:
        pass

    initial_user_content = f"任务目标：{task}"
    snapshot = None
    if include_initial_snapshot:
        try:
            snapshot = _capture_initial_snapshot(toolset)
            initial_user_content += f"\n\n{snapshot}"
        except Exception as exc:
            initial_user_content += f"\n\n[开局状态拉取失败：{exc}]"

    messages: list[dict] = [
        {"role": "system", "content": MC_SYSTEM_PROMPT},
        {"role": "user", "content": initial_user_content},
    ]

    report = {
        "runtime": runtime_info(),
        "survival_guard": survival_guard,
        "run_timeout_s": run_timeout_s,
        "model": model_name,
        "bot_username": bot.bot_username,
        "mc_api_base_url": bot.base_url,
        "model_requests": 0,
        "actor_requests": 0,
        "status": "running",
        "task": task,
        "task_version": 0,
        "input_messages": [],
        "phase": "preparing",
        "tool_output_chars": tool_output_chars,
        "context_requests": context.requests,
        "stream": stream,
        "messages": messages,
        "events": events.events,
    }
    guard = None
    pending_guard_incident = None

    def is_stopped() -> bool:
        return bool(stop_check is not None and stop_check())

    def check_deadline() -> None:
        if is_stopped():
            raise KeyboardInterrupt("用户手动停止当前任务")
        if deadline is not None and time.monotonic() >= deadline:
            raise RunTimedOut(f"运行总时限已到（{run_timeout_s}s）")

    def phase(name: str) -> None:
        report["phase"] = name
        events.emit("run.phase", phase=name, task_version=report["task_version"])

    def control() -> bool:
        nonlocal pending_guard_incident
        check_deadline()
        if inbox.urgent():
            return True
        if guard is not None:
            incident = guard.poll()
            if incident is not None:
                pending_guard_incident = incident
                return True
        return False

    def apply_input(boundary: str) -> bool:
        queued = inbox.drain_events()
        users = sorted((item for item in queued if item["kind"] == "user"), key=lambda item: item["id"])
        for item in users:
            item["status"] = "received"
            report["input_messages"].append(item)
            events.emit("input.received", **item)
        for item in queued:
            events.emit("queue.consumed", queue_id=item["id"], kind=item["kind"], priority=item["priority"], boundary=boundary)
        for item in users:
            report["task_version"] += 1
            item.update(status="applied", task_version=report["task_version"])
            messages.append({"role": "user", "content": item["text"]})
            events.emit(
                "input.applied",
                input_id=item["id"],
                text=item["text"],
                task_version=report["task_version"],
                boundary=boundary,
            )
        return bool(users)

    def capture_notable_events() -> None:
        notable = toolset.drain_notable_events()
        if not notable:
            return
        call_id = None
        if messages and messages[-1].get("role") == "tool":
            call_id = messages[-1]["tool_call_id"]
            messages[-1]["content"] = f"{messages[-1]['content']}\n[期间新事件] {notable}"
        events.emit("mc.notable_events", call_id=call_id, text=notable)

    guard = GenerationGuard(bot, events, checkpoint=check_deadline) if survival_guard else None
    budget_warned = False
    try:
        events.emit(
            "run.started", task=task, bot=bot.bot_username, model=model_name,
            stream=stream, max_calls=max_calls, initial_snapshot=snapshot,
            runtime=runtime_info(), run_timeout_s=run_timeout_s,
            survival_guard=survival_guard,
        )
        while report["actor_requests"] < max_calls:
            check_deadline()
            phase("preparing")
            apply_input("before_model")
            request_version = report["task_version"]
            used_before = report["actor_requests"]
            remaining = max_calls - used_before
            force_final = False

            if max_calls >= 6 and not budget_warned and remaining == 2:
                budget_warned = True
                messages.append(
                    {
                        "role": "user",
                        "name": "runtime",
                        "content": (
                            f"[程序步数提醒] 已使用 {used_before}/{max_calls} 次模型请求，仅剩最后 2 轮（末轮将关闭工具收尾）。"
                            "请收敛当前动作，确认状态后准备交付总结。"
                        ),
                    }
                )
            elif max_calls >= 4 and remaining == 1:
                force_final = True
                messages.append(
                    {
                        "role": "user",
                        "name": "runtime",
                        "content": (
                            f"[程序步数提醒] 当前是最后 1 次模型请求（{max_calls}/{max_calls}），本轮不提供工具。"
                            "请基于真实已执行结果总结已完成的工作、当前坐标与未完成事项。"
                        ),
                    }
                )

            phase("generating")
            report["model_requests"] += 1
            report["actor_requests"] += 1
            try:
                message = request_model(
                    chat_fn,
                    context.build(messages),
                    [] if force_final else registry.schemas(),
                    stream=stream,
                    events=events,
                    request=report["actor_requests"],
                    control=control if (guard is not None or controllable or deadline is not None or stop_check is not None) else None,
                )
            except RequestCancelled:
                phase("deciding")
                if pending_guard_incident is not None:
                    capture_notable_events()
                    messages.append({"role": "user", "name": "runtime", "content": "[模型等待期间的真实生存事件] " + json.dumps(pending_guard_incident, ensure_ascii=False)})
                    if guard is not None:
                        guard.stats["cancelled_generations"] += 1
                    pending_guard_incident = None
                apply_input("model_cancelled")
                continue

            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                phase("deciding")
                if apply_input("after_model") or request_version != report["task_version"]:
                    events.emit("output.superseded", task_version=request_version, current_version=report["task_version"])
                    continue
                content = message.get("content") or ""
                if not content.strip():
                    report["status"] = "empty_response"
                    return report
                if not inbox.seal_if_empty():
                    if apply_input("before_delivery") or not inbox.seal_if_empty():
                        continue
                report["status"] = "answered"
                report["answer"] = content
                events.emit("answer", content=content, task_version=request_version)
                return report

            phase("executing_tools")
            results: dict[str, str] = {}
            committed = 0

            def on_result(call: dict, result: str) -> None:
                nonlocal committed
                results[call["id"]] = result
                while committed < len(calls) and calls[committed]["id"] in results:
                    cid = calls[committed]["id"]
                    messages.append({"role": "tool", "tool_call_id": cid, "content": results[cid]})
                    committed += 1

            ToolScheduler(
                registry,
                events,
                urgent=lambda: is_stopped() or (deadline is not None and time.monotonic() >= deadline) or inbox.urgent(),
                on_result=on_result,
            ).run(calls)
            check_deadline()
            capture_notable_events()
            apply_input("after_tools")

        report["status"] = "limit_reached"
    except RunTimedOut as exc:
        report["status"] = "timeout"
        report["error"] = str(exc)
    except RuntimeError as exc:
        report["status"] = "runtime_error"
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        if guard is not None:
            report["guardian"] = dict(guard.stats)
        # A fatal action/network error bypasses the normal after_tools boundary.
        # Preserve late death/respawn evidence before declaring the run finished.
        try:
            capture_notable_events()
        except Exception:
            pass
        remaining_items = inbox.close_events()
        for item in remaining_items:
            if item["kind"] == "user":
                item["status"] = "pending"
                report["input_messages"].append(item)
                events.emit("input.pending", input_id=item["id"], reason=report["status"])
        phase("finished")
        events.emit(
            "run.finished",
            status=report["status"],
            error=report.get("error"),
            actor_requests=report["actor_requests"],
        )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-harness",
        description="基于 nano-agent 改造的 Minecraft Qwen 智能体运行器",
    )
    parser.add_argument("task", nargs="?", default=DEFAULT_MC_TASK, help="给 Bot 下达的自然语言任务目标")
    parser.add_argument("--env-file", default=None, help="指定配置文件（如 .env.local 连同学本地服，默认 .env）")
    parser.add_argument("--bot", default=None, help="Bot 角色名（默认取配置文件中的 MC_BOT_USERNAME）")
    parser.add_argument("--max-calls", type=int, default=20, help="最大模型请求轮数（默认 20）")
    parser.add_argument("--run-timeout-s", type=float, default=None, help="任务总运行时限（秒）")
    parser.add_argument(
        "--provider",
        choices=["nju", "deepseek", "dashscope"],
        default=None,
        help="LLM 服务提供方（默认按环境变量；deepseek 使用本机 deepseek-flash）",
    )
    parser.add_argument("--base-url", default=None, help="覆盖 LLM Base URL")
    parser.add_argument("--model", default=None, help="覆盖模型名（比赛接口必须为 default）")
    parser.add_argument("--rpm-budget", type=int, default=None, help="覆盖每分钟请求上限（默认比赛接口 24）")
    parser.add_argument("--max-tokens", type=int, default=1024, help="单轮最大生成 token 数（默认 1024）")
    parser.add_argument("--thinking", action="store_true", help="开启模型思考模式（默认关闭以省预算防截断）")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出，使用普通同步请求")
    parser.add_argument("--interactive", action="store_true", help="运行中支持按行追加新指令（/now 紧急插队）")
    parser.add_argument("--survival-guard", action="store_true", help="实验性：模型等待期间轮询真实状态并触发保命响应/重规划")
    parser.add_argument("--disconnect-after", action="store_true", help="任务结束后断开 Bot 游戏连接（默认保持在线）")
    parser.add_argument(
        "--tool-output-chars",
        type=int,
        default=DEFAULT_TOOL_OUTPUT_CHARS,
        help="单条工具输出进入上下文的字符上限（至少 256）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于 0")
    if args.tool_output_chars < 256:
        parser.error("--tool-output-chars 至少为 256")

    try:
        settings = Settings.load(dotenv_path=args.env_file)
        qwen = QwenModel(
            QwenSpec(
                dotenv_path=args.env_file,
                fallback_dotenv_path=".env",
                provider=args.provider,
                base_url_override=args.base_url,
                model_override=args.model,
                rpm_budget_override=args.rpm_budget,
                enable_thinking=args.thinking,
                max_tokens=args.max_tokens,
            )
        )
        manager = BotManager(settings)
        bot_name = args.bot or settings.default_bot_username
        provisioned = manager.open_bot(bot_name, wait_ready=True)
    except McAgentError as exc:
        print(f"配置或连接错误: {exc}", file=sys.stderr)
        return 1

    runs_dir = Path.cwd() / "runs"
    runs_dir.mkdir(exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    report_path = runs_dir / f"{stem}.json"
    event_path = runs_dir / f"{stem}.events.jsonl"

    print(
        f"[MC Harness 启动] Bot={provisioned.bot_username} | MC={settings.mc_api_base_url} | "
        f"LLM={qwen.base_url} (model={qwen.model}, rpm={qwen.rpm_budget or 'unlimited'})",
        flush=True,
    )

    try:
        with qwen, provisioned.session as session, JsonlEvents(event_path) as listener:
            inbox = InputInbox() if args.interactive else None

            def _exec():
                return run_mc_agent(
                    args.task,
                    session,
                    chat_fn=qwen.chat,
                    model_name=f"{qwen.model}@{qwen.base_url}",
                    max_calls=args.max_calls,
                    run_timeout_s=args.run_timeout_s,
                    survival_guard=args.survival_guard,
                    tool_output_chars=args.tool_output_chars,
                    stream=not args.no_stream,
                    on_event=listener,
                    inbox=inbox,
                )

            if args.interactive and inbox is not None:
                print("[交互模式已开启：随时输入新指令回车追加；`/now 新指令` 立即打断当前生成插队；Ctrl+D 结束输入]", flush=True)
                with StdinInput(inbox):
                    report = _exec()
            else:
                report = _exec()

            if args.disconnect_after:
                try:
                    session.disconnect()
                except Exception:
                    pass
    finally:
        qwen.close()
        provisioned.session.close()

    report["event_log"] = str(event_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[轨迹汇总] {report_path}")
    print(f"[事件日志] {event_path}")
    print(f"[模型调用] {report['actor_requests']}/{args.max_calls} 轮 | 状态: {report['status']}")
    return 0 if report["status"] == "answered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
