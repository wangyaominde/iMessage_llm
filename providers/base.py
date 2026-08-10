"""Provider 无关的归一化数据结构 + LLMProvider 抽象基类。

harness 全程只用这里的 Message / ToolCall / LLMResponse；每个具体 provider 负责把它们
翻译成自家 API 的请求格式，并把响应翻回这里的结构。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    """模型发起的一次工具调用（客户端工具，需要 harness 执行）。"""
    id: str
    name: str
    arguments: dict

    def to_dict(self) -> dict:
        return {'id': self.id, 'name': self.name, 'arguments': self.arguments}

    @classmethod
    def from_dict(cls, d: dict) -> 'ToolCall':
        return cls(id=d['id'], name=d['name'], arguments=d.get('arguments') or {})


@dataclass
class Message:
    """归一化的一条消息。

    role:
      - 'system'    系统提示
      - 'user'      用户输入（可带本地图片路径 images）
      - 'assistant' 模型输出（可带 tool_calls）
      - 'tool'      某次工具调用的结果（配 tool_call_id）
    raw / raw_provider:
      assistant turn 的 provider 原生表示，用于同 provider 内忠实回放
      （保留 Anthropic 的 thinking / server-tool 块、OpenAI 的 tool_calls 结构等）。
    """
    role: str
    content: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    images: list[str] = field(default_factory=list)  # 本地图片文件路径（仅 user turn）
    raw: Any = None
    raw_provider: Optional[str] = None

    def to_dict(self) -> dict:
        d = {'role': self.role, 'content': self.content}
        if self.tool_calls:
            d['tool_calls'] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d['tool_call_id'] = self.tool_call_id
        if self.name:
            d['name'] = self.name
        if self.raw is not None:
            d['raw'] = self.raw
            d['raw_provider'] = self.raw_provider
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Message':
        return cls(
            role=d['role'],
            content=d.get('content') or '',
            tool_calls=[ToolCall.from_dict(t) for t in d.get('tool_calls', [])],
            tool_call_id=d.get('tool_call_id'),
            name=d.get('name'),
            raw=d.get('raw'),
            raw_provider=d.get('raw_provider'),
        )


@dataclass
class LLMResponse:
    """provider.chat() 的返回。

    assistant_message: 已成型的 assistant 轮，harness 直接 append 进历史（含 raw 供回放）。
    tool_calls:        本轮模型请求的客户端工具调用（server 端工具已被 provider 内部消化）。
    text:              最终可读文本（无工具调用时即最终回复）。
    """
    assistant_message: Message
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ''
    finish_reason: str = ''
    usage: dict = field(default_factory=dict)


class LLMProvider(ABC):
    """所有后端实现这个接口。"""

    name: str = 'base'

    @abstractmethod
    def chat(self, messages: list[Message], tools: Optional[list[dict]] = None) -> LLMResponse:
        """发起一次对话补全。

        messages: 归一化消息列表（含 system）。
        tools:    provider 无关的工具 schema 列表，每个形如
                  {'name','description','parameters'(JSON Schema)}。
                  联网搜索等 provider 原生能力不在这里，由 provider 自行按配置附加。
        """
        raise NotImplementedError


def sanitize_history(messages: list[Message], allowed_client_names: set) -> list[Message]:
    """把引用了“当前不可用的客户端工具”的 assistant 轮降级为纯文本，并丢弃它对应的
    tool 结果消息。

    为什么需要：历史里持久化的 assistant 轮可能带 tool_use/tool_calls，一旦用户在后台
    关掉某个工具，请求里就不再声明该工具，但回放历史仍带着它的调用块 → API 直接 400，
    该用户之后每条消息都失败。降级后丢掉 raw，provider 会用纯文本重建这一轮，历史仍可用。
    （降级只丢工具调用细节，文本回复保留。）
    """
    out: list[Message] = []
    dropped_ids: set = set()
    for m in messages:
        if m.role == 'assistant' and m.tool_calls:
            if any(tc.name not in allowed_client_names for tc in m.tool_calls):
                for tc in m.tool_calls:
                    dropped_ids.add(tc.id)
                out.append(Message(role='assistant', content=m.content or '（此前用过的工具现已关闭）'))
                continue
        if m.role == 'tool' and m.tool_call_id in dropped_ids:
            continue
        out.append(m)
    return out


def build_provider(cfg: dict) -> LLMProvider:
    """根据 config 里的 provider 字段构造对应实现。未配置好时抛 ValueError。"""
    provider = (cfg.get('provider') or '').strip().lower()
    if provider == 'anthropic':
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(cfg)
    if provider in ('openai', 'openai_compatible', 'openai-compatible'):
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(cfg)
    raise ValueError(f"未配置或不支持的 provider: {provider!r}（请在后台选择 anthropic 或 openai）")
