"""工具声明、JSON Schema 生成与参数校验分发。（基于 winddd666/nano-agent 扩展 number/object_array）"""

import json
from dataclasses import dataclass
from threading import Event
from typing import Callable

VALID_KINDS = ("string", "integer", "number", "boolean", "string_array", "string_map", "object_array")


@dataclass(frozen=True)
class Parameter:
    description: str
    kind: str = "string"
    required: bool = True

    def __post_init__(self):
        if self.kind not in VALID_KINDS:
            raise ValueError(f"不支持的参数类型：{self.kind}")

    def json_schema(self) -> dict:
        if self.kind == "string_array":
            schema: dict = {"type": "array", "items": {"type": "string"}}
        elif self.kind == "object_array":
            schema = {"type": "array", "items": {"type": "object"}}
        elif self.kind == "string_map":
            schema = {"type": "object", "additionalProperties": {"type": "string"}}
        else:
            schema = {"type": self.kind}
        if self.description:
            schema["description"] = self.description
        return schema


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Parameter]
    handler: Callable[..., str]
    concurrency: str = "exclusive"
    interrupt: str = "wait"
    compact_schema: bool = False

    def __post_init__(self):
        if self.concurrency not in ("shared", "exclusive", "control"):
            raise ValueError("concurrency 必须是 shared、exclusive 或 control")
        if self.interrupt not in ("wait", "cancel", "background"):
            raise ValueError("interrupt 必须是 wait、cancel 或 background")
        if self.interrupt == "cancel" and self.concurrency != "shared":
            raise ValueError("协作取消工具必须声明 shared，并接收 cancellation 参数")
        if self.interrupt == "cancel" and "cancellation" in self.parameters:
            raise ValueError("cancellation 是运行时保留参数")

    def schema(self):
        req = [name for name, spec in self.parameters.items() if spec.required]
        params_obj: dict = {
            "type": "object",
            "properties": {
                name: parameter.json_schema()
                for name, parameter in self.parameters.items()
            },
        }
        if self.compact_schema:
            if req:
                params_obj["required"] = req
        else:
            params_obj["required"] = req
            params_obj["additionalProperties"] = False
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params_obj,
            },
        }


@dataclass(frozen=True)
class PreparedTool:
    tool: Tool
    arguments: dict

    def invoke(self, cancellation=None):
        if self.tool.interrupt == "cancel":
            return self.tool.handler(**self.arguments, cancellation=cancellation)
        return self.tool.handler(**self.arguments)


class ToolRegistry:
    def __init__(self, tools):
        self._tools = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"工具重复注册：{tool.name}")
            self._tools[tool.name] = tool

    def schemas(self):
        return [tool.schema() for tool in self._tools.values()]

    def select(self, *names):
        return ToolRegistry(self._tools[name] for name in names)

    def with_tools(self, *tools):
        return ToolRegistry([*self._tools.values(), *tools])

    def map_tools(self, transform):
        return ToolRegistry([transform(tool) for tool in self._tools.values()])

    def prepare(self, call):
        try:
            function = call["function"]
            name = function["name"]
            raw_args = function["arguments"]
        except (KeyError, TypeError):
            return "错误：工具请求格式不正确，缺少 function.name 或 function.arguments。"
        if not isinstance(name, str):
            return "错误：工具名称必须是字符串。"
        if not isinstance(raw_args, str):
            return "错误：工具请求格式不正确，arguments 必须是 JSON 字符串。"
        raw_stripped = raw_args.strip()
        if not raw_stripped:
            args = {}
        else:
            try:
                args = json.loads(raw_stripped)
            except json.JSONDecodeError as exc:
                args = None
                try:
                    args = json.loads(raw_stripped, strict=False)
                except json.JSONDecodeError:
                    if raw_stripped.endswith("}}"):
                        try:
                            args = json.loads(raw_stripped[:-1], strict=False)
                        except json.JSONDecodeError:
                            args = None
                if args is None:
                    snippet = raw_args[max(0, exc.pos - 24) : exc.pos + 24]
                    return (
                        f"错误：工具 {name} 参数 JSON 解析失败（{exc.msg}，位置 char {exc.pos}，"
                        f"附近片段 {snippet!r}）；请检查括号闭合与引号转义。"
                    )

        if not isinstance(args, dict):
            return "错误：工具参数必须是 JSON 对象。"
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：没有注册工具 {name}。"
        # Drop explicit nulls on optional parameters so models passing null for omitted args work cleanly
        cleaned_args = {
            k: v
            for k, v in args.items()
            if not (v is None and k in tool.parameters and not tool.parameters[k].required)
        }
        required = {pname for pname, spec in tool.parameters.items() if spec.required}
        if not required <= set(cleaned_args) or not set(cleaned_args) <= set(tool.parameters):
            expected = ", ".join(tool.parameters) or "无参数"
            return f"错误：{name} 参数不匹配，允许字段：{expected}（必填：{', '.join(sorted(required)) or '无'}）。"
        for parameter, spec in tool.parameters.items():
            if parameter not in cleaned_args:
                continue
            value = cleaned_args[parameter]
            if spec.kind == "string" and not isinstance(value, str):
                return f"错误：{name} 的 {parameter} 参数必须是字符串。"
            if spec.kind == "integer":
                if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
                    value = int(value)
                    cleaned_args[parameter] = value
                if type(value) is not int:
                    return f"错误：{name} 的 {parameter} 参数必须是整数。"
            if spec.kind == "number" and (type(value) is bool or not isinstance(value, (int, float))):
                return f"错误：{name} 的 {parameter} 参数必须是数字（int 或 float）。"
            if spec.kind == "boolean" and type(value) is not bool:
                return f"错误：{name} 的 {parameter} 参数必须是布尔值。"
            if spec.kind == "string_array" and (
                not isinstance(value, list) or any(not isinstance(item, str) for item in value)
            ):
                return f"错误：{name} 的 {parameter} 参数必须是字符串数组。"
            if spec.kind == "object_array" and (
                not isinstance(value, list) or any(not isinstance(item, dict) for item in value)
            ):
                return f"错误：{name} 的 {parameter} 参数必须是对象数组。"
            if spec.kind == "string_map" and (
                not isinstance(value, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items())
            ):
                return f"错误：{name} 的 {parameter} 参数必须是字符串映射。"
        return PreparedTool(tool, cleaned_args)

    def execute(self, call):
        prepared = self.prepare(call)
        if isinstance(prepared, str):
            return prepared
        if prepared.tool.interrupt == "cancel":
            return prepared.invoke(Event())
        return prepared.invoke()
