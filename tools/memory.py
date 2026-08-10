"""长期记忆工具（每用户独立）。

记忆存 agent_state/memory/<user_id>/memory.md —— 与 AgentSession 注入 system 的 digest
是同一个文件，所以 remember 写进去后，下一轮对话模型就能在 system 里看到。
"""
from __future__ import annotations

import os
from datetime import datetime

from tools.base import Tool, ToolContext
from agent.session import memory_dir_for, MEMORY_FILENAME

MAX_MEMORY_BYTES = 8000


def _mem_path(ctx: ToolContext) -> str:
    d = memory_dir_for(ctx.state_dir, ctx.user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, MEMORY_FILENAME)


class RememberTool(Tool):
    name = 'remember'
    description = (
        "把关于这位用户值得长期保留的事实或偏好记下来（如称呼、喜好、重要背景），"
        "跨对话生效。只在确有长期价值时使用；闲聊内容不要记。"
    )
    parameters = {
        'type': 'object',
        'properties': {'fact': {'type': 'string', 'description': '要长期记住的一句话'}},
        'required': ['fact'],
    }

    def run(self, ctx: ToolContext, fact: str = '') -> str:
        fact = (fact or '').strip()
        if not fact:
            return '没有内容可记。'
        path = _mem_path(ctx)
        existing = ''
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                existing = f.read()
        if fact in existing:
            return '这条已经记过了。'
        existing += f"- [{datetime.now().strftime('%Y-%m-%d')}] {fact}\n"
        if len(existing.encode('utf-8')) > MAX_MEMORY_BYTES:
            lines = existing.splitlines(keepends=True)
            while len(''.join(lines).encode('utf-8')) > MAX_MEMORY_BYTES and len(lines) > 1:
                lines.pop(0)
            existing = ''.join(lines)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(existing)
        return f'已记住：{fact}'


class RecallTool(Tool):
    name = 'recall'
    description = "检索之前记住的关于这位用户的长期信息。留空 query 返回全部记忆。"
    parameters = {
        'type': 'object',
        'properties': {'query': {'type': 'string', 'description': '检索关键词，可留空'}},
    }

    def run(self, ctx: ToolContext, query: str = '') -> str:
        path = _mem_path(ctx)
        if not os.path.exists(path):
            return '还没有关于该用户的长期记忆。'
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        if not text:
            return '还没有关于该用户的长期记忆。'
        q = (query or '').strip()
        if not q:
            return text[-2000:]
        hits = [ln for ln in text.splitlines() if q.lower() in ln.lower()]
        return '\n'.join(hits) if hits else f'没有匹配“{q}”的记忆。'


def make_memory_tools() -> list:
    return [RememberTool(), RecallTool()]
