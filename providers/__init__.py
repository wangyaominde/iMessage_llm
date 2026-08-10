"""LLM Provider 抽象层：把不同后端（Anthropic / OpenAI 兼容）归一化成同一套接口，
供 agent harness 调用。harness 只跟 providers.base 里的数据结构打交道。"""
from .base import LLMProvider, Message, ToolCall, LLMResponse, build_provider

__all__ = ['LLMProvider', 'Message', 'ToolCall', 'LLMResponse', 'build_provider']
