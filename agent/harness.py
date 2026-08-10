"""工具调用循环（provider 无关）。

run_agent 拿一段已组装好的 base_messages（system + 历史 + 本轮用户输入），
反复调 provider.chat；模型要工具就执行并回填结果，直到产出纯文本或到达迭代上限。
返回 (最终文本, 本轮新增的消息列表 appended)，appended 由调用方并入历史。
"""
from __future__ import annotations

from typing import Callable, Optional

from providers.base import LLMProvider, Message
from tools.base import ToolRegistry, ToolContext

_TIMEOUT_REPLY = '（这个问题有点复杂，我一时没能处理好，换个说法再问我一次？）'
_EMPTY_REPLY = '（我这边没生成出内容，换个说法再问我一次？）'


def run_agent(
    provider: LLMProvider,
    base_messages: list[Message],
    registry: Optional[ToolRegistry],
    ctx: ToolContext,
    max_iters: int = 8,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[str, list[Message]]:
    appended: list[Message] = []
    tools = registry.schemas() if (registry and not registry.is_empty()) else None
    last_text = ''

    for _ in range(max_iters):
        resp = provider.chat(base_messages + appended, tools)
        appended.append(resp.assistant_message)
        last_text = resp.text or last_text

        if not resp.tool_calls:
            # 空文本兜底：thinking 把预算耗尽/命中 max_tokens 时 resp.text 可能为空，
            # 不能让用户收到一片沉默。
            final = resp.text or last_text or _EMPTY_REPLY
            return final, appended

        for tc in resp.tool_calls:
            result, is_err = registry.dispatch(ctx, tc.name, tc.arguments)
            if log:
                tag = 'ERR ' if is_err else ''
                log(f"工具 {tc.name} -> {tag}{result[:120]}")
            appended.append(Message(role='tool', tool_call_id=tc.id, content=result, name=tc.name))

    # 到达上限仍在要工具：补一个干净的 assistant 收尾，保证历史结构完整
    fallback = last_text or _TIMEOUT_REPLY
    appended.append(Message(role='assistant', content=fallback))
    if log:
        log(f"工具循环达上限 {max_iters}，收尾返回")
    return fallback, appended
