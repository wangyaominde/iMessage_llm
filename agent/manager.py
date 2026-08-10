"""AgentManager：每 phone 一个 AgentSession，用线程池实现「不同用户并发、同用户串行」。

替换旧的 UserSessionManager（以 Dify conversation_id 为核心）。迁移时从 user_sessions.json
只取 phone→user_id 映射，丢弃 conversation_id，历史从空开始。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from providers.base import build_provider
from agent.session import AgentSession, memory_dir_for


class AgentManager:
    def __init__(self, state_dir: str, config: dict,
                 registry_factory: Callable[[], object],
                 deliver: Callable[[str, str], None],
                 log: Callable[[str, str], None],
                 max_workers: int = 6):
        self.state_dir = state_dir
        self.config = config                      # 引用同一个 config 字典（实时反映后台改动）
        self.registry_factory = registry_factory  # ()->ToolRegistry（按当前配置构造）
        self.deliver = deliver                    # (phone, text) -> None（发送 + 失败重试）
        self.log = log                            # (msg, level) -> None
        self.index_path = os.path.join(state_dir, 'index.json')
        self.index: dict[str, str] = {}           # phone -> user_id
        self._sessions: dict[str, AgentSession] = {}
        self._sessions_lock = threading.Lock()
        # 每 phone 一个串行队列：同用户消息 FIFO、串行处理，且每用户至多占 1 个 worker
        # （避免单用户连发把线程池占满、饿死其他用户）。
        self._pending: dict[str, deque] = {}
        self._draining: set = set()
        self._queue_lock = threading.Lock()
        self.max_pending_per_user = 100
        # 缓存 provider / registry（SDK client 自带连接池，别每条消息重建）；
        # 按配置签名缓存，后台改配置后签名变化会自动重建。
        self._provider_cache = None
        self._registry_cache = None
        self._provider_lock = threading.Lock()
        self._registry_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='agent')
        # 供工具使用的共享服务；reminder 等在 app 里往里塞
        self.services: dict = {'manager': self, 'deliver': deliver, 'config': config, 'log': log}
        os.makedirs(state_dir, exist_ok=True)
        self._load_index()

    # ---- 索引（phone -> user_id）----
    def _load_index(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    self.index = json.load(f)
                return
            except Exception as e:
                print(f"加载 index 失败: {e}")
        # 首次：尝试从旧 user_sessions.json 迁移 phone->user_id（丢弃 conversation_id）
        legacy = 'user_sessions.json'
        if os.path.exists(legacy):
            try:
                with open(legacy, 'r', encoding='utf-8') as f:
                    old = json.load(f)
                for phone, sess in old.items():
                    uid = sess.get('user_id') or self._generate_user_id(phone)
                    self.index[phone] = uid
                self.log(f"已从 user_sessions.json 迁移 {len(self.index)} 个用户映射", 'info')
            except Exception as e:
                print(f"迁移旧会话失败: {e}")
        self._save_index()

    def _save_index(self):
        try:
            with open(self.index_path, 'w', encoding='utf-8') as f:
                json.dump(self.index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存 index 失败: {e}")

    @staticmethod
    def _generate_user_id(phone: str) -> str:
        h = hashlib.md5((phone or '').encode()).hexdigest()[:8]
        return f"imessage-user-{h}"

    # ---- provider / registry 缓存（按配置签名）----
    def _provider_signature(self) -> tuple:
        c = self.config
        return (c.get('provider'), c.get('anthropic_model'), c.get('anthropic_api_key'),
                c.get('anthropic_base_url'), c.get('openai_model'), c.get('openai_base_url'),
                c.get('openai_api_key'), c.get('openai_search_param'),
                bool(c.get('enable_web_search')), c.get('max_tokens'),
                c.get('anthropic_max_tokens'), c.get('anthropic_thinking', True))

    def get_provider(self):
        sig = self._provider_signature()
        with self._provider_lock:
            if self._provider_cache and self._provider_cache[0] == sig:
                return self._provider_cache[1]
            prov = build_provider(self.config)  # 未配置好会抛，调用方 catch；失败不写缓存
            self._provider_cache = (sig, prov)
            return prov

    def get_registry(self):
        c = self.config
        sig = (bool(c.get('enable_web_search')), bool(c.get('enable_memory')),
               bool(c.get('enable_reminder')), bool(c.get('enable_torrent')))
        with self._registry_lock:
            if self._registry_cache and self._registry_cache[0] == sig:
                return self._registry_cache[1]
            reg = self.registry_factory()
            self._registry_cache = (sig, reg)
            return reg

    def get_or_create(self, phone: str) -> AgentSession:
        with self._sessions_lock:
            if phone in self._sessions:
                return self._sessions[phone]
            uid = self.index.get(phone)
            if not uid:
                uid = self._generate_user_id(phone)
                self.index[phone] = uid
                self._save_index()
            sess = AgentSession(phone, uid, self.state_dir, int(self.config.get('history_limit', 24)))
            self._sessions[phone] = sess
            return sess

    # ---- reader 回调入口：接收一批消息，按用户入队串行处理 ----
    def dispatch_batch(self, messages: list[dict]) -> bool:
        for msg in messages:
            if msg.get('is_from_me'):
                continue
            text = msg.get('text') or ''
            atts = msg.get('attachments') or []
            images = [a['path'] for a in atts if a.get('exists') and a.get('path')]
            phone = msg.get('contact')
            if not phone:
                continue
            if not text.strip() and not images:
                continue
            self._enqueue(phone, text, images)
        return True  # 已接住并入队，reader 可推进游标

    def _enqueue(self, phone: str, text: str, images: list[str]):
        start = False
        with self._queue_lock:
            q = self._pending.setdefault(phone, deque())
            q.append((text, images))
            if len(q) > self.max_pending_per_user:
                q.popleft()  # 单用户堆积过多，丢最旧的，防内存无界增长
                self.log(f"{phone} 待处理消息过多，丢弃最旧一条", 'warning')
            if phone not in self._draining:
                self._draining.add(phone)
                start = True
        if start:
            self.pool.submit(self._drain, phone)  # 每用户至多一个在跑的 drain 任务

    def _drain(self, phone: str):
        while True:
            with self._queue_lock:
                q = self._pending.get(phone)
                if not q:
                    self._draining.discard(phone)
                    return
                text, images = q.popleft()
            self._process(phone, text, images)  # 顺序处理，保证同用户 FIFO 串行

    def _process(self, phone: str, text: str, images: list[str]):
        try:
            provider = self.get_provider()
        except Exception as e:
            self.log(f"provider 未配置好，跳过来自 {phone} 的消息: {e}", 'warning')
            return
        try:
            registry = self.get_registry()
            sess = self.get_or_create(phone)
            preview = (text[:30] + '...') if text else '[图片]'
            self.log(f"处理 {phone}: {preview}", 'success')
            reply = sess.process(text, images, provider, registry, self.config, self.services,
                                 log=lambda m: self.log(f"[{phone}] {m}", 'info'))
            if reply and reply.strip():
                self.deliver(phone, reply)
                self.log(f"已回复 {phone}", 'success')
            else:
                self.log(f"{phone} 无有效回复，跳过发送", 'warning')
        except Exception as e:
            self.log(f"处理 {phone} 出错: {e}", 'error')

    # ---- 主动事件（提醒等）在池里跑一轮并发送 ----
    def run_event(self, phone: str, event_text: str, on_success=None, on_failure=None):
        def _done(ok: bool):
            cb = on_success if ok else on_failure
            if cb:
                try:
                    cb()
                except Exception as e:
                    print(f"事件回调出错: {e}")

        def _job():
            try:
                provider = self.get_provider()
            except Exception as e:
                self.log(f"provider 未配置，无法执行事件({phone}): {e}", 'warning')
                _done(False)
                return
            try:
                registry = self.get_registry()
                sess = self.get_or_create(phone)
                reply = sess.process_event(event_text, provider, registry, self.config, self.services,
                                           log=lambda m: self.log(f"[{phone}] {m}", 'info'))
                if reply and reply.strip():
                    self.deliver(phone, reply)  # 发送失败会进重试队列，交付所有权已转移
                    self.log(f"主动事件已发送给 {phone}", 'success')
                    _done(True)
                else:
                    self.log(f"事件({phone})无有效回复", 'warning')
                    _done(False)
            except Exception as e:
                self.log(f"执行事件({phone})出错: {e}", 'error')
                _done(False)
        self.pool.submit(_job)

    # ---- 后台管理 ----
    def list_sessions(self) -> list[dict]:
        with self._sessions_lock:  # 持锁快照，避免遍历时 index 被池线程改动 → RuntimeError
            index_snapshot = list(self.index.items())
            cached = dict(self._sessions)
        out = []
        for phone, uid in index_snapshot:
            sess = cached.get(phone)
            if sess is None:
                sess = AgentSession(phone, uid, self.state_dir, int(self.config.get('history_limit', 24)))
            out.append(sess.summary())
        out.sort(key=lambda s: s.get('last_active') or '', reverse=True)
        return out

    def reset_session(self, phone: str) -> bool:
        """清空该用户的对话历史（保留长期记忆）。"""
        sess = self.get_or_create(phone)
        with sess.lock:
            sess.history = []
            sess._save()
        return True

    def _purge_user_files(self, uid: str):
        """删除该 user_id 的历史文件和长期记忆目录。"""
        try:
            p = os.path.join(self.state_dir, f'{uid}.json')
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        try:
            mem = memory_dir_for(self.state_dir, uid)  # 关键：删记忆，否则确定性 uid 会让记忆复活
            if os.path.isdir(mem):
                shutil.rmtree(mem, ignore_errors=True)
        except Exception:
            pass

    def delete_session(self, phone: str) -> bool:
        uid = self.index.get(phone)
        with self._sessions_lock:
            self._sessions.pop(phone, None)
            self.index.pop(phone, None)
            self._save_index()
        if uid:
            self._purge_user_files(uid)
        return uid is not None

    def clear_all(self) -> int:
        n = len(self.index)
        with self._sessions_lock:
            uids = list(self.index.values())
            self._sessions.clear()
            self.index.clear()
            self._save_index()
        for uid in uids:
            self._purge_user_files(uid)
        return n
