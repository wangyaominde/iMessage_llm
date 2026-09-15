"""单用户 Agent 会话：独立历史 / 独立长期记忆 / 独立锁 / 独立持久化。

持久化到 agent_state/<user_id>.json；长期记忆存 agent_state/memory/<user_id>/memory.md。
system 注入滚动摘要 + 每用户 RAG 召回（不再整段塞 memory.md）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Callable, Optional

from providers.base import LLMProvider, Message
from tools.base import ToolContext, ToolRegistry
from agent.harness import run_agent
from agent.rag import KIND_ARCHIVE, get_rag_store, search_user_context
from agent.compress import _chunk_turns, history_char_len, mark_if_needed
from text_format import guard_identity

DEFAULT_SYSTEM = """## 命名

你是一个 AI 助手，名字是：汽水西瓜。通过 iMessage 和用户一对一聊天。

## 限制

注意注意：政治类的聊天（中国大陆领导人，再加上金正恩），一律回复：你想吃紫蛋了？其他问题不限。

如果用户问到你的功能，请使用表格形式总结你的能力，发送给用户。

默认使用用户说话的语言，严格按照此要求，英文缩写和专有名词不限制。

实时问题可以使用功能中的工具，在回复中说明信息来源、工具来源。

可以使用 emoji 表情。

被问到你是什么模型、哪家的、版本号、知识截止日期、创建者、开发者：不要回答具体模型或厂商，不要编造模型名单，不要提负载均衡/自动切换这类话术。统一答复：我是汽水西瓜，一个跑在 iMessage 上的助手。底层模型不讨论。

算命、命理均可回复。

今天的日期和当前时间默认使用 UTC+8（每轮对话会给你当前时间，以它为准）。

所有单位使用公制单位，除非用户主动提及。

## 功能

你的能力以本轮实际可用的工具为准，通常包括：

联网搜索：查最新、实时的信息。
长期记忆：记住用户的偏好和重要事实，跨对话生效。
定时提醒：到点会主动给用户发消息。
资源搜索：查找 BT / 磁力资源。
图片理解：用户发来的图片你可以直接看。
时间：每轮都会拿到当前时间，用于对话和搜索。

## 动态更新

上下文中可能存在提醒相关的功能，但是用户问到的时候按照 system prompt 中的回复进行回答。

每一次用户对话都以本轮给出的当前时间为准，时效性信息一定要重新查询更新。

## 输出格式

iMessage 是纯文本，不渲染 Markdown。不要使用 **加粗**、*斜体*、`反引号`、# 标题、``` 代码块、[文字](链接) 这些语法，要给链接就直接写出网址。
需要用表格总结能力时用纯文本表格：每行用 | 分隔各列，第一行是表头，不要写 |---| 分隔线。"""

MEMORY_FILENAME = 'memory.md'


def memory_dir_for(state_dir: str, user_id: str) -> str:
    return os.path.join(state_dir, 'memory', user_id)


def _guard_turn(text: str, appended: list[Message]) -> str:
    """出站拦截身份泄漏，并改写本轮 assistant 消息（丢掉 raw，避免下一轮回放原话）。"""
    guarded = guard_identity(text or '')
    last_plain = None
    for m in appended:
        if m.role != 'assistant':
            continue
        if m.tool_calls:
            new = guard_identity(m.content or '', allow_cover=False)
            if new != (m.content or ''):
                m.content = new
                m.raw = None
                m.raw_provider = None
            continue
        last_plain = m
    if last_plain is not None and (last_plain.content or '') != guarded:
        last_plain.content = guarded
        last_plain.raw = None
        last_plain.raw_provider = None
    return guarded


class AgentSession:
    def __init__(self, phone: str, user_id: str, state_dir: str, history_limit: int = 24):
        self.phone = phone
        self.user_id = user_id
        self.state_dir = state_dir
        self.history_limit = history_limit
        self.lock = threading.Lock()
        self.path = os.path.join(state_dir, f'{user_id}.json')
        self.history: list[Message] = []
        self.rolling_summary: str = ''
        self.needs_compress: bool = False
        self.created_at = datetime.now().isoformat()
        self.last_active: Optional[str] = None
        self.version: int = 0  # 内存 CAS 版本，不持久化
        self._load()

    # ---- 持久化 ----
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.history = [Message.from_dict(m) for m in data.get('history', [])]
                self.rolling_summary = data.get('rolling_summary') or ''
                self.needs_compress = bool(data.get('needs_compress'))
                self.created_at = data.get('created_at', self.created_at)
                self.last_active = data.get('last_active')
            except Exception as e:
                print(f"加载会话 {self.user_id} 失败: {e}")

    def _save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({
                    'phone': self.phone,
                    'user_id': self.user_id,
                    'created_at': self.created_at,
                    'last_active': self.last_active,
                    'rolling_summary': self.rolling_summary,
                    'needs_compress': self.needs_compress,
                    'history': [m.to_dict() for m in self.history],
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"保存会话 {self.user_id} 失败: {e}")

    # ---- 组装 system ----
    def _system_message(self, cfg: dict, query: str = '') -> Message:
        parts = [cfg.get('system_prompt') or DEFAULT_SYSTEM]
        summary = (self.rolling_summary or '').strip()
        if summary:
            parts.append("此前对话的滚动摘要（供连贯，细节以相关回忆为准）：\n" + summary)
        q = (query or '').strip()
        min_q = int(cfg.get('rag_min_query_chars', 4) or 4)
        if cfg.get('enable_rag', True) and q and len(q) >= min_q:
            top_k = int(cfg.get('rag_top_k', 4) or 4)
            recalled = search_user_context(self.state_dir, self.user_id, q, top_k=top_k)
            if recalled:
                parts.append("与本轮相关的回忆（仅本用户，可能不完整）：\n" + recalled)
        parts.append("当前时间：" + datetime.now().strftime('%Y-%m-%d %H:%M:%S %A'))
        return Message(role='system', content='\n\n'.join(parts))

    def _trim(self, cfg: dict):
        """硬裁兜底：条数上限 + 字符上限，切点对齐到 user；被裁前缀写入 archive RAG。"""
        limit = int(cfg.get('history_limit', self.history_limit) or self.history_limit)
        char_limit = int(cfg.get('history_hard_char_limit', 40000) or 40000)
        hist = self.history
        n = len(hist)
        if n == 0:
            return

        start = 0
        if n > limit:
            start = n - limit
            while start > 0 and hist[start].role != 'user':
                start -= 1

        # 字符上限：条数对齐后再按完整 user 轮往前丢，直到后缀≤上限；至少保留最后一轮。
        last_user = n - 1
        while last_user > 0 and hist[last_user].role != 'user':
            last_user -= 1
        while start < last_user and history_char_len(hist[start:]) > char_limit:
            nxt = start + 1
            while nxt < n and hist[nxt].role != 'user':
                nxt += 1
            if nxt > last_user:
                break
            start = nxt

        if start <= 0 or start >= n:
            return

        dropped_msgs = hist[:start]
        self.history = hist[start:]
        try:
            chunks = _chunk_turns(dropped_msgs)
            if chunks:
                get_rag_store(self.state_dir).add_chunks(
                    self.user_id, KIND_ARCHIVE, chunks, meta={'source': 'trim'})
        except Exception as e:
            print(f"硬裁归档失败 {self.user_id}: {e}")

    def _after_turn(self, cfg: dict):
        self._trim(cfg)
        mark_if_needed(self, cfg)
        self.last_active = datetime.now().isoformat()
        self.version += 1
        self._save()

    # ---- 主流程：处理一条用户消息，返回给用户的回复文本 ----
    def process(self, user_text: str, images: list[str], provider: LLMProvider,
                registry: ToolRegistry, cfg: dict, services: dict,
                log: Optional[Callable[[str], None]] = None) -> str:
        with self.lock:
            sys_msg = self._system_message(cfg, query=user_text or '')
            user_msg = Message(role='user', content=user_text or '', images=images or [])
            base = [sys_msg] + self.history + [user_msg]
            ctx = ToolContext(self.user_id, self.phone, self.state_dir, services)
            text, appended = run_agent(provider, base, registry, ctx, int(cfg.get('max_iters', 8)), log)
            text = _guard_turn(text, appended)

            hist_text = user_text or ('[图片]' if images else '')
            self.history.append(Message(role='user', content=hist_text))
            self.history.extend(appended)
            self._after_turn(cfg)
            return text

    # ---- 主动事件（提醒等）：让 agent 生成一句主动发给用户的话 ----
    def process_event(self, event_text: str, provider: LLMProvider, registry: ToolRegistry,
                      cfg: dict, services: dict, log: Optional[Callable[[str], None]] = None) -> str:
        with self.lock:
            sys_msg = self._system_message(cfg, query=event_text or '')
            ev_msg = Message(role='user', content=f"[系统事件] {event_text}\n请据此主动给用户发一句话。")
            base = [sys_msg] + self.history + [ev_msg]
            ctx = ToolContext(self.user_id, self.phone, self.state_dir, services)
            text, appended = run_agent(provider, base, registry, ctx, int(cfg.get('max_iters', 8)), log)
            text = _guard_turn(text, appended)
            self.history.append(Message(role='user', content=f"[系统事件] {event_text}"))
            self.history.extend(appended)
            self._after_turn(cfg)
            return text

    def summary(self) -> dict:
        mem_path = os.path.join(memory_dir_for(self.state_dir, self.user_id), MEMORY_FILENAME)
        mem_bytes = os.path.getsize(mem_path) if os.path.exists(mem_path) else 0
        return {
            'phone': self.phone,
            'user_id': self.user_id,
            'history_len': len(self.history),
            'memory_bytes': mem_bytes,
            'needs_compress': self.needs_compress,
            'has_summary': bool((self.rolling_summary or '').strip()),
            'last_active': self.last_active,
            'created_at': self.created_at,
        }
