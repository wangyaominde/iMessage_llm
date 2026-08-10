"""客户端工具的基类与注册表。

Tool 是 provider 无关的：name/description/parameters(JSON Schema) + run(ctx, **kwargs)->str。
harness 拿 registry.schemas() 交给 provider；模型发起调用后 harness 用 registry.dispatch() 执行。
每次调用带 ToolContext（当前 user_id/phone 及共享服务），实现每用户隔离。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolContext:
    """一次工具调用的上下文。"""
    user_id: str
    phone: str
    state_dir: str = 'agent_state'
    # 由 app 注入的共享服务（可选）：主动发消息、访问 reminder store 等
    services: dict = field(default_factory=dict)


class Tool(ABC):
    name: str = 'tool'
    description: str = ''
    parameters: dict = {'type': 'object', 'properties': {}}

    def schema(self) -> dict:
        return {'name': self.name, 'description': self.description, 'parameters': self.parameters}

    @abstractmethod
    def run(self, ctx: ToolContext, **kwargs) -> str:
        """执行工具，返回给模型看的字符串结果。异常由 registry 兜底。"""
        raise NotImplementedError


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def is_empty(self) -> bool:
        return not self._tools

    def dispatch(self, ctx: ToolContext, name: str, arguments: dict) -> tuple[str, bool]:
        """执行工具，返回 (结果字符串, is_error)。任何异常都转成错误字符串，绝不抛出。"""
        tool = self._tools.get(name)
        if tool is None:
            return (f"错误：未知工具 {name}", True)
        try:
            result = tool.run(ctx, **(arguments or {}))
            if result is None:
                result = ''
            return (str(result), False)
        except TypeError as e:
            return (f"错误：工具 {name} 参数不对: {e}", True)
        except Exception as e:
            return (f"错误：工具 {name} 执行失败: {e}", True)
