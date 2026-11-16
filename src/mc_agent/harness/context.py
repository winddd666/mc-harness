"""从原始历史生成模型请求视图；支持 legacy 分块折叠与 sliding 有界动作账本 + 实时 HUD 注入。"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from typing import Any

from .tool_registry import Parameter, Tool

DEFAULT_TOOL_OUTPUT_CHARS = 3200
_COORD_RE = re.compile(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]")


def _compact_args(raw_args: str | None) -> str:
    if not raw_args:
        return ""
    try:
        parsed = json.loads(raw_args)
    except ValueError:
        return str(raw_args)[:40]
    if not isinstance(parsed, dict) or not parsed:
        return ""
    parts: list[str] = []
    for k, v in list(parsed.items())[:3]:
        sval = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)
        parts.append(f"{k}={sval[:22]}")
    return ",".join(parts)


def _parse_leading_json(raw_content: str) -> dict[str, Any] | None:
    first_line = raw_content.splitlines()[0] if raw_content else ""
    for candidate in (raw_content, first_line):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _compact_tool_outcome(raw_content: str) -> str:
    data = _parse_leading_json(raw_content)
    if data is None:
        return raw_content.replace("\n", " ")[:80]
    ok = data.get("ok")
    if ok is False:
        err = data.get("error")
        perr = data.get("partial_error")
        lerr = data.get("last_error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("reason") or "err"
        elif isinstance(err, str) and err:
            code = err
        elif isinstance(perr, dict) and perr.get("code"):
            code = str(perr["code"])
        elif data.get("stop_reason"):
            code = str(data["stop_reason"])
        elif isinstance(lerr, dict) and lerr.get("code"):
            code = str(lerr["code"])
        elif data.get("reason"):
            code = str(data["reason"])
        else:
            code = "failed"
        details: list[str] = []
        steps_done = data.get("steps_done")
        if isinstance(steps_done, int) and steps_done > 0:
            details.append(f"steps={steps_done}")
        blocker = data.get("blocker")
        if isinstance(blocker, dict) and blocker.get("block"):
            bpos = blocker.get("pos")
            bpos_s = f"[{bpos[0]},{bpos[1]},{bpos[2]}]" if isinstance(bpos, list) and len(bpos) >= 3 else str(bpos)
            details.append(f"{blocker['block']}@{bpos_s}")
        vdirs = data.get("viable_directions")
        if isinstance(vdirs, list):
            details.append(f"viable={','.join(str(d) for d in vdirs) or 'none'}")
        vtun = data.get("viable_tunnel_directions")
        if isinstance(vtun, list) and (vdirs is None or not vdirs):
            if vtun or vdirs is None:
                details.append(f"tunnel={','.join(str(d) for d in vtun) or 'none'}")
        vup = data.get("viable_up_directions")
        if (not vdirs) and (not vtun) and isinstance(vup, list) and vup:
            details.append(f"up={','.join(str(d) for d in vup)}")
        if data.get("viable_pillar_up") is True:
            details.append("pillar_up=True")
        suffix = f"({','.join(details)})" if details else ""
        return f"ERR:{code}{suffix}"[:120]
    bits: list[str] = ["ok"]
    if isinstance(data.get("gained"), dict) and data["gained"]:
        g = ",".join(f"{k}+{v}" for k, v in list(data["gained"].items())[:3])
        bits.append(g)
    elif isinstance(data.get("collected"), list) and data["collected"]:
        col_bits = [
            f"{str(it.get('name') or '').removeprefix('minecraft:')}+{int(it.get('count') or 0)}"
            for it in data["collected"][:3]
            if isinstance(it, dict) and it.get("name") and int(it.get("count") or 0) > 0
        ]
        if col_bits:
            bits.append(",".join(col_bits))
    elif isinstance(data.get("dug_count"), int):
        bits.append(f"dug={data['dug_count']}")
    elif isinstance(data.get("steps_done"), int):
        bits.append(f"steps={data['steps_done']}({data.get('stop_reason', '')})")
    elif isinstance(data.get("crafted"), str):
        bits.append(f"+{data['crafted']}")
    elif isinstance(data.get("pos_after"), list):
        bits.append(f"pos={data['pos_after']}")
    return " ".join(bits)[:88]


def _failed_group_signature(grp: list[dict[str, Any]]) -> str | None:
    if any(m.get("role") == "user" and m.get("name") != "runtime" for m in grp):
        return None
    assistants = [m for m in grp if m.get("role") == "assistant"]
    if len(assistants) != 1:
        return None
    tcs = assistants[0].get("tool_calls") or []
    if not tcs:
        return None
    tool_msgs = [m for m in grp if m.get("role") == "tool"]
    if not tool_msgs:
        return None
    pos_by_cid: dict[str, str] = {}
    code_by_cid: dict[str, str] = {}
    for tm in tool_msgs:
        txt = str(tm.get("content") or "")
        if txt.startswith("错误："):
            continue
        parsed = _parse_leading_json(txt)
        if parsed and parsed.get("ok") is False:
            cid = str(tm.get("tool_call_id") or "")
            err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            err_code = str(
                (err_obj.get("code") or err_obj.get("reason") if err_obj else (parsed.get("error") if isinstance(parsed.get("error"), str) else None))
                or parsed.get("stop_reason")
                or parsed.get("reason")
                or ""
            )
            if err_code:
                code_by_cid[cid] = err_code
            pbefore = parsed.get("pos_before") or parsed.get("pos") or err_obj.get("pos") or err_obj.get("from")
            if isinstance(pbefore, (list, tuple)) and len(pbefore) >= 3:
                try:
                    bx = math.floor(float(pbefore[0]))
                    by = math.floor(float(pbefore[1]))
                    bz = math.floor(float(pbefore[2]))
                    pos_by_cid[cid] = f"@[{bx},{by},{bz}]"
                except (TypeError, ValueError):
                    pass
            continue
        return None
    drift_fold_tools = {"mc_dig", "mc_move_to", "mc_place", "mc_interact"}
    drift_fold_codes = {"out_of_reach", "path_not_found", "nothing_to_dig", "repeated_identical_failure_blocked"}
    sig_parts: list[str] = []
    for tc in tcs:
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        cid = str(tc.get("id") or "")
        err_code = code_by_cid.get(cid, "")
        if name in drift_fold_tools and err_code in drift_fold_codes:
            sig_parts.append(f"{name}:ERR={err_code}")
            continue
        raw_args = str(fn.get("arguments") or "")
        try:
            parsed_args = json.loads(raw_args) if raw_args.strip() else {}
            canon = json.dumps(parsed_args, sort_keys=True, ensure_ascii=False)
        except ValueError:
            canon = raw_args.strip()
        sig_parts.append(f"{name}:{canon}{pos_by_cid.get(cid, '')}")
    return "|".join(sig_parts)


def extract_failed_coords(history: list[dict[str, Any]], *, max_items: int = 6) -> list[str]:
    """从最近工具失败返回中提取坐标黑名单提醒，防止模型原地重试。"""
    coords: list[str] = []
    seen: set[str] = set()
    by_call_id: dict[str, dict[str, Any]] = {}
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            cid = tc.get("id")
            if cid:
                by_call_id[cid] = tc

    for msg in reversed(history):
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            continue
        text = msg["content"]
        parsed: dict[str, Any] | None = None
        first_line = text.splitlines()[0] if text else ""
        for raw_candidate in (text, first_line):
            try:
                cand = json.loads(raw_candidate)
            except ValueError:
                cand = None
            if isinstance(cand, dict):
                parsed = cand
                break
        is_failed = (
            '"ok": false' in text
            or '"ok":false' in text
            or '"error"' in text
            or bool(parsed and (parsed.get("ok") is False or "error" in parsed or parsed.get("all_directions_blocked") is True))
        )
        if not is_failed:
            continue
        err_dict = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else {}
        if parsed and (parsed.get("error") == "bad_args" or err_dict.get("code") == "bad_args"):
            continue
        tc = by_call_id.get(msg.get("tool_call_id") or "")
        coord_tag = None
        fn_name = ""
        args: dict[str, Any] = {}
        if tc:
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name") or "")
            try:
                raw_loaded = json.loads(fn.get("arguments") or "{}")
                if isinstance(raw_loaded, dict):
                    args = raw_loaded
            except ValueError:
                args = {}
            if all(k in args for k in ("x", "z")):
                y_part = f",{args['y']}" if "y" in args and args["y"] is not None else ""
                coord_tag = f"[{args['x']}{y_part},{args['z']}]"
        dir_val = (parsed.get("direction") if parsed else None) or args.get("direction")
        if coord_tag is None and parsed:
            for p_cand in (parsed.get("pos_after"), parsed.get("pos"), err_dict.get("pos"), parsed.get("pos_before")):
                if isinstance(p_cand, list) and len(p_cand) >= 3:
                    try:
                        fx, fy, fz = (
                            math.floor(float(p_cand[0])),
                            math.floor(float(p_cand[1])),
                            math.floor(float(p_cand[2])),
                        )
                        if fn_name == "mc_skill_staircase":
                            has_alt = (
                                (isinstance(parsed.get("viable_directions"), list) and len(parsed["viable_directions"]) > 0)
                                or (isinstance(parsed.get("viable_up_directions"), list) and len(parsed["viable_up_directions"]) > 0)
                                or err_dict.get("code") == "repeated_identical_failure_blocked"
                            )
                            if parsed.get("all_directions_blocked") is not True and dir_val and has_alt:
                                coord_tag = f"[{fx},{fy},{fz}:{dir_val}]"
                            else:
                                coord_tag = f"[{fx},{fy},{fz}]"
                        elif fn_name == "mc_skill_tunnel":
                            has_alt = (
                                (isinstance(parsed.get("viable_tunnel_directions"), list) and len(parsed["viable_tunnel_directions"]) > 0)
                                or (isinstance(parsed.get("viable_up_directions"), list) and len(parsed["viable_up_directions"]) > 0)
                                or parsed.get("viable_pillar_up") is True
                                or err_dict.get("code") == "repeated_identical_failure_blocked"
                            )
                            if dir_val and has_alt:
                                coord_tag = f"[{fx},{fy},{fz}:{dir_val}]"
                            else:
                                coord_tag = f"[{fx},{fy},{fz}]"
                        else:
                            coord_tag = f"[{fx},{fy},{fz}]"
                        break
                    except (TypeError, ValueError):
                        pass
        if coord_tag is None:
            m = _COORD_RE.search(text)
            if m:
                coord_tag = f"[{m.group(1)},{m.group(2)},{m.group(3)}]"
        if coord_tag and coord_tag not in seen:
            seen.add(coord_tag)
            coords.append(coord_tag)
            if len(coords) >= max_items:
                break
    coords.reverse()
    return coords


def _append_ephemeral_hud(messages: list[dict[str, Any]], hud_text: str) -> None:
    if not hud_text or not messages:
        return
    last = messages[-1]
    existing = last.get("content")
    if isinstance(existing, str) and existing.strip():
        last["content"] = f"{existing}\n\n{hud_text}"
    elif existing is None or existing == "":
        last["content"] = hud_text
    else:
        messages.append({"role": "user", "name": "hud", "content": hud_text})


class RequestContext:
    def __init__(
        self,
        tool_output_chars: int = DEFAULT_TOOL_OUTPUT_CHARS,
        *,
        archive_after_tools: int = 8,
        archive_step: int = 8,
        mode: str = "legacy",
        keep_recent_turns: int = 6,
        max_ledger_entries: int = 16,
    ) -> None:
        if type(tool_output_chars) is not int or tool_output_chars < 256:
            raise ValueError("tool_output_chars 必须是至少 256 的整数")
        if type(archive_after_tools) is not int or archive_after_tools < 0:
            raise ValueError("archive_after_tools 必须是非负整数")
        if type(archive_step) is not int or archive_step < 1:
            raise ValueError("archive_step 必须是正整数")
        if mode not in {"legacy", "sliding"}:
            raise ValueError("mode 必须是 'legacy' 或 'sliding'")
        if type(keep_recent_turns) is not int or keep_recent_turns < 1:
            raise ValueError("keep_recent_turns 必须是正整数")
        if type(max_ledger_entries) is not int or max_ledger_entries < 1:
            raise ValueError("max_ledger_entries 必须是正整数")
        self.tool_output_chars = tool_output_chars
        self.archive_after_tools = archive_after_tools
        self.archive_step = archive_step
        self.mode = mode
        self.keep_recent_turns = keep_recent_turns
        self.max_ledger_entries = max_ledger_entries
        self._outputs: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []

    def _truncate_tool_message(self, message: dict[str, Any], limit: int, *, is_old: bool) -> dict[str, Any] | None:
        content = message.get("content")
        if message.get("role") != "tool" or not isinstance(content, str) or len(content) <= limit:
            return None
        result_id = sha256(content.encode("utf-8")).hexdigest()[:16]
        self._outputs[result_id] = content
        tag = "旧轮次工具输出已折叠" if is_old else "工具输出节选"
        header = (
            f"[{tag}，原文共 {len(content)} 字符；result_id={result_id}。"
            "如需查看隐藏段落请调 read_tool_result(result_id, offset)。]\n"
        )
        separator = "\n[…中间已省略…]\n"
        available = max(32, limit - len(header) - len(separator))
        head, tail = available // 2, available - available // 2
        message["content"] = header + content[:head] + separator + content[-tail:]
        return {
            "tool_call_id": message.get("tool_call_id"),
            "result_id": result_id,
            "original_chars": len(content),
            "sent_chars": len(message["content"]),
        }

    def _append_group_to_ledger(
        self,
        grp: list[dict[str, Any]],
        ledger_lines: list[str],
        turn_no: int,
        *,
        collapsed_repeats: int = 0,
    ) -> int:
        tool_results: dict[str, str] = {
            m.get("tool_call_id", ""): str(m.get("content") or "")
            for m in grp
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        suffix = f" (连续重复失败×{collapsed_repeats}已折叠)" if collapsed_repeats > 0 else ""
        for m in grp:
            role = m.get("role")
            if role == "assistant":
                turn_no += 1
                tcs = m.get("tool_calls") or []
                if tcs:
                    steps = []
                    for tc in tcs[:3]:
                        fn = tc.get("function") or {}
                        name = fn.get("name") or "?"
                        arg_str = _compact_args(fn.get("arguments"))
                        cid = tc.get("id") or ""
                        out_str = _compact_tool_outcome(tool_results[cid]) if cid in tool_results else "skipped"
                        steps.append(f"{name}({arg_str})->{out_str}")
                    if len(tcs) > 3:
                        steps.append(f"+{len(tcs) - 3}call")
                    ledger_lines.append(f"#{turn_no} " + " ; ".join(steps) + suffix)
                elif isinstance(m.get("content"), str) and m["content"].strip():
                    ledger_lines.append(f"#{turn_no} text:{m['content'].strip()[:60]}")
            elif role == "user":
                txt = str(m.get("content") or "").strip().replace("\n", " ")
                if txt:
                    ledger_lines.append(f"[事件/提示] {txt[:72]}")
        return turn_no

    def _build_sliding(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """保留固定前缀 messages[:2] 与最近 keep_recent_turns 轮，早先轮次及连续重复失败压缩为动作账本。"""
        previews: list[dict[str, Any]] = []
        if len(messages) <= 2:
            return messages, previews

        prefix = messages[:2]
        tail = messages[2:]

        # 将 tail 按 assistant 轮次分组（保证 assistant + 后续所有 tool 消息成对不裂开）
        groups: list[list[dict[str, Any]]] = []
        cur_group: list[dict[str, Any]] = []
        for msg in tail:
            if msg.get("role") == "assistant" and cur_group and any(m.get("role") == "assistant" for m in cur_group):
                groups.append(cur_group)
                cur_group = [msg]
            else:
                cur_group.append(msg)
        if cur_group:
            groups.append(cur_group)

        # 对连续无成功间隔的失败段（含连续相同失败与跨步周期性乒乓失败）去重：每个签名仅保留最后 1 次活跃结果，早先重复并入账本
        tagged_groups = [(grp, _failed_group_signature(grp)) for grp in groups]
        active_groups: list[list[dict[str, Any]]] = []
        collapsed_records: list[tuple[int, list[dict[str, Any]], int]] = []
        idx = 0
        while idx < len(tagged_groups):
            grp, sig = tagged_groups[idx]
            if sig is None:
                active_groups.append(grp)
                idx += 1
                continue
            end = idx + 1
            while end < len(tagged_groups) and tagged_groups[end][1] is not None:
                end += 1
            span = tagged_groups[idx:end]
            sig_total: dict[str, int] = {}
            sig_first_grp: dict[str, list[dict[str, Any]]] = {}
            sig_last_offset: dict[str, int] = {}
            for offset, (s_grp, s_sig) in enumerate(span):
                assert s_sig is not None
                sig_total[s_sig] = sig_total.get(s_sig, 0) + 1
                sig_first_grp.setdefault(s_sig, s_grp)
                sig_last_offset[s_sig] = offset
            for offset, (s_grp, s_sig) in enumerate(span):
                assert s_sig is not None
                if offset == sig_last_offset[s_sig]:
                    if sig_total[s_sig] >= 2:
                        collapsed_records.append((len(active_groups), sig_first_grp[s_sig], sig_total[s_sig] - 1))
                    active_groups.append(s_grp)
            idx = end

        if len(active_groups) <= self.keep_recent_turns:
            old_count = 0
            recent_groups = active_groups
        else:
            old_count = len(active_groups) - self.keep_recent_turns
            recent_groups = active_groups[-self.keep_recent_turns :]

        ledger_lines: list[str] = []
        turn_no = 0
        collapsed_by_pos: dict[int, list[tuple[list[dict[str, Any]], int]]] = {}
        for pos_idx, c_grp, c_repeats in collapsed_records:
            collapsed_by_pos.setdefault(pos_idx, []).append((c_grp, c_repeats))

        for active_idx in range(len(active_groups)):
            for c_grp, c_repeats in collapsed_by_pos.get(active_idx, ()):
                turn_no = self._append_group_to_ledger(c_grp, ledger_lines, turn_no, collapsed_repeats=c_repeats)
            if active_idx < old_count:
                turn_no = self._append_group_to_ledger(active_groups[active_idx], ledger_lines, turn_no)

        # 剥离较早轮次中与更新一轮完全相同的 assistant 口号正文，切断滑动窗口下的自模仿复读锁死
        seen_slogans: set[str] = set()
        for grp in reversed(recent_groups):
            for m in grp:
                if m.get("role") == "assistant" and isinstance(m.get("content"), str):
                    stripped = m["content"].strip()
                    if stripped:
                        if stripped in seen_slogans:
                            m["content"] = ""
                        else:
                            seen_slogans.add(stripped)

        older_limit = min(self.tool_output_chars, 1400)
        last_group_idx = len(recent_groups) - 1
        recent_msgs: list[dict[str, Any]] = []
        for g_idx, grp in enumerate(recent_groups):
            msg_limit = self.tool_output_chars if g_idx == last_group_idx else older_limit
            for m in grp:
                p = self._truncate_tool_message(m, msg_limit, is_old=False)
                if p:
                    previews.append(p)
                recent_msgs.append(m)

        out_messages = list(prefix)
        if ledger_lines:
            trimmed = ledger_lines[-self.max_ledger_entries :]
            out_messages.append(
                {
                    "role": "user",
                    "name": "ledger",
                    "content": "[已完成动作账本（旧轮次摘要，勿重复失败操作）]\n" + "\n".join(trimmed),
                }
            )
        out_messages.extend(recent_msgs)
        return out_messages, previews

    def build(
        self,
        history: list[dict[str, Any]],
        *,
        record: bool = True,
        ephemeral_hud: str | None = None,
    ) -> list[dict[str, Any]]:
        """只改请求副本；支持 legacy 量化折叠或 sliding 有界账本，并将 ephemeral_hud 仅贴到请求尾部。"""
        messages = deepcopy(history)
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "assistant" and "reasoning_content" in m:
                m.pop("reasoning_content", None)
        if self.mode == "sliding":
            messages, previews = self._build_sliding(messages)
            if ephemeral_hud:
                _append_ephemeral_hud(messages, ephemeral_hud)
            if record:
                self.requests.append(
                    {
                        "mode": "sliding",
                        "message_count": len(messages),
                        "truncated_tool_results": previews,
                    }
                )
            return messages

        previews = []
        tool_indices = [
            idx
            for idx, m in enumerate(messages)
            if m.get("role") == "tool" and isinstance(m.get("content"), str)
        ]
        if self.archive_after_tools == 0:
            old_set = set(tool_indices)
        elif len(tool_indices) >= self.archive_after_tools + self.archive_step:
            fold_count = ((len(tool_indices) - self.archive_after_tools) // self.archive_step) * self.archive_step
            old_set = set(tool_indices[:fold_count])
        else:
            old_set = set()

        for idx, message in enumerate(messages):
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            is_old = idx in old_set
            limit = min(self.tool_output_chars, 600) if is_old else self.tool_output_chars
            preview = self._truncate_tool_message(message, limit, is_old=is_old)
            if preview:
                previews.append(preview)
        if ephemeral_hud:
            _append_ephemeral_hud(messages, ephemeral_hud)
        if record:
            self.requests.append({"message_count": len(messages), "truncated_tool_results": previews})
        return messages

    def read_result(self, result_id: str, offset: int = 0) -> str:
        content = self._outputs.get(result_id)
        if content is None:
            return f"错误：没有找到工具结果 {result_id}。"
        if offset < 0 or offset > len(content):
            return f"错误：offset 超出范围，原文长度为 {len(content)} 字符。"
        window = max(128, self.tool_output_chars - 128)
        end = min(len(content), offset + window)
        more = "；仍有后续内容" if end < len(content) else "；已到末尾"
        return f"[工具结果原文 {result_id}：{offset}..{end}/{len(content)}{more}]\n" + content[offset:end]

    def result_tool(self, *, compact: bool = False):
        if compact:
            return Tool(
                name="read_tool_result",
                description="读折叠输出",
                parameters={
                    "result_id": Parameter(""),
                    "offset": Parameter("", kind="integer", required=False),
                },
                handler=self.read_result,
                concurrency="shared",
                compact_schema=True,
            )
        return Tool(
            name="read_tool_result",
            description="按 result_id 与字符偏移 offset 读取被折叠或节选的工具完整返回内容。",
            parameters={
                "result_id": Parameter("节选提示中的 result_id。"),
                "offset": Parameter("起始字符偏移，从 0 开始。", kind="integer", required=False),
            },
            handler=self.read_result,
            concurrency="shared",
        )
