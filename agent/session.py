"""单用户 Agent 会话：独立历史 / 独立长期记忆 / 独立锁 / 独立持久化。

持久化到 agent_state/<user_id>.json；长期记忆存 agent_state/memory/<user_id>/memory.md
（由 memory 工具读写，这里只读摘要注入 system）。
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

DEFAULT_SYSTEM = """## 命名

你是一个 AI 助手，名字是：汽水西瓜。通过 iMessage 和用户一对一聊天。

## 限制

注意注意：政治类的聊天（中国大陆领导人，再加上金正恩），一律回复：你想吃紫蛋了？其他问题不限。

如果用户问到你的功能，请使用表格形式总结你的能力，发送给用户。

默认使用用户说话的语言，严格按照此要求，英文缩写和专有名词不限制。

实时问题可以使用功能中的工具，在回复中说明信息来源、工具来源。

可以使用 emoji 表情。

当有人问你是什么模型的时候统一答复：我是一个随负载自动切换的智能模型（基于 DeepseekV3、GPT-4o、Grok2、Gemini 2 Flash Exo）。不要主动提起这件事。

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
MEMORY_DIGEST_LIMIT = 1500


def memory_dir_for(state_dir: str, user_id: str) -> str:
    return os.path.join(state_dir, 'memory', user_id)


def read_memory_digest(state_dir: str, user_id: str) -> str:
    path = os.path.join(memory_dir_for(state_dir, user_id), MEMORY_FILENAME)
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                txt = f.read().strip()
            return txt[-MEMORY_DIGEST_LIMIT:] if txt else ''
    except Exception:
        pass
    return ''


class AgentSession:
    def __init__(self, phone: str, user_id: str, state_dir: str, history_limit: int = 24):
        self.phone = phone
        self.user_id = user_id
        self.state_dir = state_dir
        self.history_limit = history_limit
        self.lock = threading.Lock()
        self.path = os.path.join(state_dir, f'{user_id}.json')
        self.history: list[Message] = []
        self.created_at = datetime.now().isoformat()
        self.last_active: Optional[str] = None
        self._load()

    # ---- 持久化 ----
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.history = [Message.from_dict(m) for m in data.get('history', [])]
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
                    'history': [m.to_dict() for m in self.history],
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"保存会话 {self.user_id} 失败: {e}")

    # ---- 组装 system ----
    def _system_message(self, cfg: dict) -> Message:
        parts = [cfg.get('system_prompt') or DEFAULT_SYSTEM]
        digest = read_memory_digest(self.state_dir, self.user_id)
        if digest:
            parts.append("你记录过的关于这位用户的长期记忆：\n" + digest)
        parts.append("当前时间：" + datetime.now().strftime('%Y-%m-%d %H:%M:%S %A'))
        return Message(role='system', content='\n\n'.join(parts))

    def _trim(self):
        if len(self.history) <= self.history_limit:
            return
        # 从 len-limit 处“向回退”到最近的 user 边界：保证保留完整轮次、且以 user 开头。
        # 关键：向回退（而不是向前）——若截断点之后没有 user 轮，向前会一路走到末尾把历史清空。
        # 向回退最坏只是多留几条（一整轮很长时），绝不会清空。
        cut = len(self.history) - self.history_limit
        start = cut
        while start > 0 and self.history[start].role != 'user':
            start -= 1
        self.history = self.history[start:]

    # ---- 主流程：处理一条用户消息，返回给用户的回复文本 ----
    def process(self, user_text: str, images: list[str], provider: LLMProvider,
                registry: ToolRegistry, cfg: dict, services: dict,
                log: Optional[Callable[[str], None]] = None) -> str:
        with self.lock:
            sys_msg = self._system_message(cfg)
            user_msg = Message(role='user', content=user_text or '', images=images or [])
            base = [sys_msg] + self.history + [user_msg]
            ctx = ToolContext(self.user_id, self.phone, self.state_dir, services)
            text, appended = run_agent(provider, base, registry, ctx, int(cfg.get('max_iters', 8)), log)

            # 入历史：user 去掉图片字节（只留文字/占位，避免每轮重发和文件失效）
            hist_text = user_text or ('[图片]' if images else '')
            self.history.append(Message(role='user', content=hist_text))
            self.history.extend(appended)
            self._trim()
            self.last_active = datetime.now().isoformat()
            self._save()
            return text

    # ---- 主动事件（提醒等）：让 agent 生成一句主动发给用户的话 ----
    def process_event(self, event_text: str, provider: LLMProvider, registry: ToolRegistry,
                      cfg: dict, services: dict, log: Optional[Callable[[str], None]] = None) -> str:
        with self.lock:
            sys_msg = self._system_message(cfg)
            ev_msg = Message(role='user', content=f"[系统事件] {event_text}\n请据此主动给用户发一句话。")
            base = [sys_msg] + self.history + [ev_msg]
            ctx = ToolContext(self.user_id, self.phone, self.state_dir, services)
            text, appended = run_agent(provider, base, registry, ctx, int(cfg.get('max_iters', 8)), log)
            self.history.append(Message(role='user', content=f"[系统事件] {event_text}"))
            self.history.extend(appended)
            self._trim()
            self.last_active = datetime.now().isoformat()
            self._save()
            return text

    def summary(self) -> dict:
        mem_path = os.path.join(memory_dir_for(self.state_dir, self.user_id), MEMORY_FILENAME)
        mem_bytes = os.path.getsize(mem_path) if os.path.exists(mem_path) else 0
        return {
            'phone': self.phone,
            'user_id': self.user_id,
            'history_len': len(self.history),
            'memory_bytes': mem_bytes,
            'last_active': self.last_active,
            'created_at': self.created_at,
        }
