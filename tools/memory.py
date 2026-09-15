"""长期记忆工具（每用户独立）。

记忆存 agent_state/memory/<user_id>/memory.md；写入后刷新该用户 memory BM25。
召回优先走 BM25，关键词 substring 作 fallback。
"""
from __future__ import annotations

import os
from datetime import datetime

from tools.base import Tool, ToolContext
from agent.session import memory_dir_for, MEMORY_FILENAME
from agent.rag import KIND_MEMORY, get_rag_store

MAX_MEMORY_BYTES = 8000


def _mem_path(ctx: ToolContext) -> str:
    d = memory_dir_for(ctx.state_dir, ctx.user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, MEMORY_FILENAME)


def _memory_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _refresh_memory_index(ctx: ToolContext, text: str):
    try:
        store = get_rag_store(ctx.state_dir)
        store.rebuild(ctx.user_id, KIND_MEMORY, _memory_lines(text))
    except Exception as e:
        print(f"刷新 memory 索引失败 ({ctx.user_id}): {e}")


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
        _refresh_memory_index(ctx, existing)
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
        # 优先 BM25（仅本用户 memory 索引）
        try:
            hits = get_rag_store(ctx.state_dir).search(ctx.user_id, KIND_MEMORY, q, top_k=8)
            if hits:
                return '\n'.join(hits)
        except Exception:
            pass
        # fallback：子串匹配
        lines = [ln for ln in text.splitlines() if q.lower() in ln.lower()]
        return '\n'.join(lines) if lines else f'没有匹配“{q}”的记忆。'


def make_memory_tools() -> list:
    return [RememberTool(), RecallTool()]
