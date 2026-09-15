#!/usr/bin/env python3
"""P0 / P1 / 安全契约回归（C1–C11）。

不依赖真实 LLM。Flask 相关用例在 import 失败时 SKIP。
运行：
  python3 tests/test_p0_p1_security.py
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import pickle
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers.base import Message, LLMResponse, ToolCall, LLMProvider
from agent.rag import (
    get_rag_store, KIND_ARCHIVE, tokenize, _BM25, rag_dir_for,
)
from agent.compress import (
    _chunk_turns, _format_dialog, compress_session, scheduler_tick,
    history_char_len, _SUMMARIZE_PROMPT,
)
from agent.session import AgentSession
from agent.manager import AgentManager
from tools.base import ToolContext, ToolRegistry
from tools.web import WebFetchTool
from config import DEFAULT_CONFIG

PASS = 0
FAIL = 0
SKIP = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = ''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  OK  {name}')
    else:
        FAIL += 1
        msg = f'  FAIL  {name}' + (f' — {detail}' if detail else '')
        print(msg)
        ERRORS.append(msg)


def skip(name: str, detail: str = ''):
    global SKIP
    SKIP += 1
    print(f'  SKIP  {name}' + (f' — {detail}' if detail else ''))


def section(title: str):
    print(f'\n== {title} ==')


class FakeProvider:
    """同时实现 chat / complete；可记录调用。"""

    def __init__(self, reply: str = '好的', summary: str = '摘要：用户偏好简洁。', fail: bool = False):
        self.reply = reply
        self.summary = summary
        self.fail = fail
        self.chat_calls: list = []
        self.complete_calls: list = []

    def chat(self, messages, tools=None):
        last = (messages[-1].content or '') if messages else ''
        self.chat_calls.append({'n': len(messages), 'last': last[:80]})
        if self.fail:
            raise RuntimeError('fake provider down')
        if '对话压缩助手' in last or '待压缩的新对话' in last:
            text = self.summary
        else:
            text = self.reply
        am = Message(role='assistant', content=text)
        return LLMResponse(assistant_message=am, tool_calls=[], text=text)

    def complete(self, prompt, max_tokens=1024) -> str:
        self.complete_calls.append({'prompt': (prompt or '')[:80], 'max_tokens': max_tokens})
        if self.fail:
            raise RuntimeError('fake provider down')
        return self.summary


class SlowCompressProvider:
    """摘要路径 sleep(2)，用于验证压缩不阻塞热路径。chat 与 complete 都慢，覆盖新旧实现。"""

    def __init__(self, summary: str = '摘要：慢压缩完成。', delay: float = 2.0):
        self.summary = summary
        self.delay = delay
        self.complete_calls = []
        self.chat_calls = []

    def chat(self, messages, tools=None):
        last = (messages[-1].content or '') if messages else ''
        self.chat_calls.append(last[:40])
        if '对话压缩助手' in last or '待压缩的新对话' in last:
            time.sleep(self.delay)
            text = self.summary
        else:
            text = '好的'
        am = Message(role='assistant', content=text)
        return LLMResponse(assistant_message=am, tool_calls=[], text=text)

    def complete(self, prompt, max_tokens=1024) -> str:
        self.complete_calls.append(prompt[:40] if prompt else '')
        time.sleep(self.delay)
        return self.summary


def base_cfg(**over):
    c = {**DEFAULT_CONFIG, 'is_running': True, 'provider': 'openai',
         'enable_rag': True, 'enable_auto_compress': True,
         'history_soft_limit': 12, 'history_char_budget': 8000,
         'history_keep_recent': 4, 'history_limit': 40,
         'rag_top_k': 4, 'compress_idle_seconds': 90, 'max_iters': 3}
    c.update(over)
    return c


def fill_history(n_user_turns: int, tag: str = 'msg') -> list[Message]:
    out = []
    for i in range(n_user_turns):
        out.append(Message(role='user', content=f'{tag}-user-{i}'))
        out.append(Message(role='assistant', content=f'{tag}-asst-{i}'))
    return out


def make_manager(td, cfg, deliveries=None, logs=None):
    deliveries = deliveries if deliveries is not None else []
    logs = logs if logs is not None else []
    mgr = AgentManager(
        td, cfg,
        registry_factory=lambda: ToolRegistry(),
        deliver=lambda phone, text: deliveries.append((phone, text)),
        log=lambda m, level='info': logs.append((level, m)),
        max_workers=4,
    )
    mgr.get_provider = lambda: FakeProvider()
    return mgr, deliveries, logs


# ===================== C1 =====================

def test_c1_hard_trim_archive(td):
    section('C1 硬裁归档')
    check('C1 DEFAULT_CONFIG history_limit==60',
          DEFAULT_CONFIG.get('history_limit') == 60,
          f"got {DEFAULT_CONFIG.get('history_limit')!r}")
    check('C1 DEFAULT_CONFIG history_hard_char_limit==40000',
          DEFAULT_CONFIG.get('history_hard_char_limit') == 40000,
          f"got {DEFAULT_CONFIG.get('history_hard_char_limit')!r}")

    cfg = base_cfg(history_limit=12, history_soft_limit=999,
                   history_keep_recent=4, enable_rag=True)
    sess = AgentSession('+c1a', 'c1count', td, history_limit=12)
    hist = []
    for i in range(10):
        hist.append(Message(role='user', content=f'TRIMTOKEN-{i} 用户第{i}轮'))
        hist.append(Message(role='assistant', content=f'TRIMTOKEN-{i} 助手第{i}轮'))
    sess.history = hist
    prov = FakeProvider(reply='收到再来一条')
    sess.process('再来一条', [], prov, ToolRegistry(), cfg, {})
    check('C1 条数硬裁后 history≤12+2',
          len(sess.history) <= 12 + 2,
          f'len={len(sess.history)}')
    hits = get_rag_store(td).search('c1count', KIND_ARCHIVE, 'TRIMTOKEN-0', top_k=5)
    check('C1 archive 能搜到被裁的 TRIMTOKEN-0',
          any('TRIMTOKEN-0' in (h or '') for h in hits), hits)
    check('C1 硬裁未调用 complete',
          len(prov.complete_calls) == 0,
          f'complete_calls={prov.complete_calls}')

    cfg_c = base_cfg(history_limit=999, history_hard_char_limit=3000,
                     history_soft_limit=999, enable_rag=True)
    sess_c = AgentSession('+c1b', 'c1char', td, history_limit=999)
    hist_c = []
    for i in range(6):
        hist_c.append(Message(role='user', content=f'CHARTOKEN-{i} ' + ('字' * 800)))
        hist_c.append(Message(role='assistant', content=f'CHARREPLY-{i} ' + ('回' * 800)))
    sess_c.history = hist_c
    user_text = '字符上限再来一条'
    prov_c = FakeProvider(reply='短回复')
    sess_c.process(user_text, [], prov_c, ToolRegistry(), cfg_c, {})
    total = history_char_len(sess_c.history)
    check('C1 字符硬裁后总字符≤history_hard_char_limit',
          total <= 3000,
          f'total={total} n={len(sess_c.history)}')
    hits_c = get_rag_store(td).search('c1char', KIND_ARCHIVE, 'CHARTOKEN-0', top_k=5)
    check('C1 字符硬裁内容进 archive',
          any('CHARTOKEN-0' in (h or '') for h in hits_c), hits_c)
    check('C1 字符硬裁未调用 complete',
          len(prov_c.complete_calls) == 0,
          f'complete_calls={prov_c.complete_calls}')

    # 最后一轮自身就超限：只留本轮新增；tool 不进 archive，user 进 archive
    cfg_o = base_cfg(history_limit=999, history_hard_char_limit=3000,
                     history_soft_limit=999, enable_rag=True)
    sess_o = AgentSession('+c1c', 'c1over', td, history_limit=999)
    sess_o.history = [
        Message(role='user', content='OVERUSER-AAA 这一轮本身就超限'),
        Message(role='tool', content='OVERTOOL-ZZZ-1 ' + ('T' * 1500),
                tool_call_id='t1', name='web_search'),
        Message(role='tool', content='OVERTOOL-ZZZ-2 ' + ('T' * 1500),
                tool_call_id='t2', name='web_search'),
        Message(role='tool', content='OVERTOOL-ZZZ-3 ' + ('T' * 1500),
                tool_call_id='t3', name='web_search'),
        Message(role='tool', content='OVERTOOL-ZZZ-4 ' + ('T' * 1500),
                tool_call_id='t4', name='web_search'),
        Message(role='assistant', content='OVERASST-BBB 工具轮结束'),
    ]
    check('C1 超限轮预填字符>3000',
          history_char_len(sess_o.history) > 3000,
          history_char_len(sess_o.history))
    new_user = '超限轮之后的新消息'
    new_reply = '超限轮新回复'
    sess_o.process(new_user, [], FakeProvider(reply=new_reply), ToolRegistry(), cfg_o, {})
    roles = [m.role for m in sess_o.history]
    contents = [m.content for m in sess_o.history]
    check('C1 最后一轮自身超限后只剩本轮 user/assistant',
          roles == ['user', 'assistant']
          and new_user in contents and new_reply in contents
          and not any('OVERTOOL-ZZZ' in (c or '') for c in contents)
          and not any('OVERUSER-AAA' in (c or '') for c in contents),
          f'roles={roles} contents={contents}')
    check('C1 超限轮裁完仍≤history_hard_char_limit',
          history_char_len(sess_o.history) <= 3000,
          history_char_len(sess_o.history))
    store_o = get_rag_store(td)
    tool_hits = store_o.search('c1over', KIND_ARCHIVE, 'OVERTOOL-ZZZ', top_k=5)
    user_hits = store_o.search('c1over', KIND_ARCHIVE, 'OVERUSER-AAA', top_k=5)
    check('C1 被裁 tool 文本不进 archive',
          not any('OVERTOOL-ZZZ' in (h or '') for h in tool_hits), tool_hits)
    check('C1 被裁 user 文本进 archive',
          any('OVERUSER-AAA' in (h or '') for h in user_hits), user_hits)


# ===================== C2 =====================

def test_c2_compress_nonblocking(td):
    section('C2 压缩不阻塞热路径')
    cfg = base_cfg(history_keep_recent=4, history_soft_limit=8,
                   history_limit=80, compress_idle_seconds=60)
    mgr, _, _ = make_manager(td, cfg)
    slow = SlowCompressProvider(delay=2.0)
    mgr.get_provider = lambda: slow

    phone = '+c2block'
    sess = mgr.get_or_create(phone)
    with sess.lock:
        sess.history = fill_history(10, tag='C2BARGE')
        sess.needs_compress = True
        sess.last_active = (datetime.now() - timedelta(seconds=200)).isoformat()
        sess._save()
    n0 = len(sess.history)
    check('C2 预填 20 条', n0 == 20, n0)

    thr_err = []

    def _tick():
        try:
            scheduler_tick(mgr)
        except Exception as e:
            thr_err.append(e)

    t = threading.Thread(target=_tick, daemon=True)
    t.start()
    time.sleep(0.3)
    fast = FakeProvider(reply='插队回复')
    t0 = time.time()
    sess.process('插队消息', [], fast, ToolRegistry(), cfg, {})
    elapsed = time.time() - t0
    check('C2 压缩中 process 耗时<1s',
          elapsed < 1.0,
          f'elapsed={elapsed:.3f}s')
    t.join(timeout=8)
    check('C2 压缩线程无异常', not thr_err, str(thr_err))
    contents = [m.content for m in sess.history]
    check('C2 插队消息仍在 history',
          '插队消息' in contents, contents[:8])
    discarded = len(sess.history) >= 20 + 2 and bool(sess.needs_compress)
    applied_keep = '插队消息' in contents
    check('C2 CAS：丢弃压缩或应用后仍保留插队',
          discarded or applied_keep,
          f'len={len(sess.history)} needs_compress={sess.needs_compress} discarded={discarded}')

    # 正常路径：无插队
    phone_h = '+c2happy'
    sess_h = mgr.get_or_create(phone_h)
    with sess_h.lock:
        sess_h.history = fill_history(10, tag='C2HAPPY-SODA')
        sess_h.needs_compress = True
        sess_h.rolling_summary = ''
        sess_h.last_active = (datetime.now() - timedelta(seconds=200)).isoformat()
        sess_h._save()
    v_before = getattr(sess_h, 'version', 0)
    fast_sum = FakeProvider(summary='摘要：无插队正常压缩汽水。')
    mgr.get_provider = lambda: fast_sum
    scheduler_tick(mgr)
    check('C2 无插队 history 缩到 keep_recent 附近',
          len(sess_h.history) <= 4 + 2,
          f'len={len(sess_h.history)}')
    check('C2 无插队 rolling_summary 非空',
          bool((sess_h.rolling_summary or '').strip()),
          sess_h.rolling_summary)
    arch = get_rag_store(td).search(sess_h.user_id, KIND_ARCHIVE, 'C2HAPPY-SODA', top_k=5)
    check('C2 无插队 archive 有内容', len(arch) > 0, arch)
    check('C2 无插队 version 增大',
          hasattr(sess_h, 'version') and getattr(sess_h, 'version', 0) > v_before,
          f'version={getattr(sess_h, "version", None)} before={v_before}')

    v_reset = getattr(sess_h, 'version', 0)
    mgr.reset_session(phone_h)
    check('C2 reset_session 后 version+1',
          hasattr(sess_h, 'version') and getattr(sess_h, 'version', 0) > v_reset,
          f'version={getattr(sess_h, "version", None)} before={v_reset}')


# ===================== C3 =====================

def test_c3_bare_complete(td):
    section('C3 裸补全')
    check('C3 LLMProvider.complete 存在',
          hasattr(LLMProvider, 'complete'))
    if hasattr(LLMProvider, 'complete'):
        is_abs = bool(getattr(LLMProvider.complete, '__isabstractmethod__', False))
        check('C3 complete 非抽象', not is_abs)
        try:
            sig = inspect.signature(LLMProvider.complete)
            default = sig.parameters.get('max_tokens')
            check('C3 complete max_tokens 默认 1024',
                  default is not None and default.default == 1024,
                  str(sig))
        except (TypeError, ValueError) as e:
            check('C3 complete max_tokens 默认 1024', False, str(e))
    else:
        check('C3 complete 非抽象', False)
        check('C3 complete max_tokens 默认 1024', False)

    class OnlyChat(LLMProvider):
        name = 'onlychat'

        def chat(self, messages, tools=None):
            text = 'hello-complete'
            am = Message(role='assistant', content=text)
            return LLMResponse(assistant_message=am, tool_calls=[], text=text)

    try:
        only = OnlyChat()
        got = only.complete('hi')
        check('C3 仅实现 chat 时 complete 返回 chat.text',
              got == 'hello-complete', got)
    except Exception as e:
        check('C3 仅实现 chat 时 complete 返回 chat.text', False, f'{type(e).__name__}: {e}')

    class RecProv:
        def __init__(self):
            self.chat_n = 0
            self.complete_n = 0

        def chat(self, messages, tools=None):
            self.chat_n += 1
            text = '不应走 chat 的摘要'
            am = Message(role='assistant', content=text)
            return LLMResponse(assistant_message=am, tool_calls=[], text=text)

        def complete(self, prompt, max_tokens=1024) -> str:
            self.complete_n += 1
            return '摘要：complete 路径记录。'

    sess = AgentSession('+c3', 'c3rec', td, history_limit=40)
    sess.history = fill_history(10, tag='c3rec')
    sess.needs_compress = True
    rec = RecProv()
    ok = compress_session(sess, rec, base_cfg(history_keep_recent=4))
    check('C3 compress_session 成功', ok is True)
    check('C3 compress_session 调 complete 1 次',
          rec.complete_n == 1, f'complete_n={rec.complete_n}')
    check('C3 compress_session 不调 chat',
          rec.chat_n == 0, f'chat_n={rec.chat_n}')

    class ChatOnly:
        def chat(self, messages, tools=None):
            text = '回退摘要 FALLBACK-ONLY'
            am = Message(role='assistant', content=text)
            return LLMResponse(assistant_message=am, tool_calls=[], text=text)

    sess_f = AgentSession('+c3b', 'c3fb', td, history_limit=40)
    sess_f.history = fill_history(10, tag='c3fb')
    sess_f.needs_compress = True
    ok_f = compress_session(sess_f, ChatOnly(), base_cfg(history_keep_recent=4))
    check('C3 无 complete 属性时回退 chat',
          ok_f is True and 'FALLBACK-ONLY' in (sess_f.rolling_summary or ''),
          sess_f.rolling_summary)


# ===================== C4 =====================

def test_c4_chinese_bigrams(td):
    section('C4 中文二元组')
    toks = tokenize('老王爱喝汽水')
    for ch in ['老', '王', '爱', '喝', '汽', '水']:
        check(f'C4 单字 {ch}', ch in toks, toks)
    for bg in ['老王', '王爱', '爱喝', '喝汽', '汽水']:
        check(f'C4 二元组 {bg}', bg in toks, toks)

    en = tokenize('Hello WORLD DeepSeek123')
    check('C4 英文整词小写',
          'hello' in en and 'world' in en and 'deepseek123' in en, en)

    mixed = tokenize('汽水abc水果')
    check('C4 汽水abc水果 含二元组汽水', '汽水' in mixed, mixed)
    check('C4 汽水abc水果 含二元组水果', '水果' in mixed, mixed)
    check('C4 汽水abc水果 不含跨段水水', '水水' not in mixed, mixed)
    check('C4 汽水abc水果 含英文整词 abc', 'abc' in mixed, mixed)

    docs = ['老王爱喝汽水', '今天多喝水对身体好', '去水族馆看鱼', '汽车保养到期了']
    bm = _BM25([tokenize(d) for d in docs])
    scores = bm.scores(tokenize('汽水'))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    top1 = docs[ranked[0]] if ranked else None
    check('C4 BM25 top1 是老王爱喝汽水',
          top1 == '老王爱喝汽水',
          f'top1={top1} scores={list(zip(docs, scores))}')
    s1 = scores[ranked[0]] if ranked else 0
    s2 = scores[ranked[1]] if len(ranked) > 1 else 0
    check('C4 汽水分数 ≥ 第二名×1.5',
          s2 == 0 or s1 >= s2 * 1.5,
          f's1={s1} s2={s2}')

    uid = 'legacy_tok'
    d = os.path.join(td, 'rag', uid)
    os.makedirs(d, exist_ok=True)
    texts = ['归档里有独特词 UNIQUE-OLD-PKL-TOKEN', '无关的西瓜香蕉']
    jsonl = os.path.join(d, 'archive.jsonl')
    with open(jsonl, 'w', encoding='utf-8') as f:
        for i, text in enumerate(texts):
            f.write(json.dumps({'id': f'old{i}', 'text': text}, ensure_ascii=False) + '\n')
    old_bm = _BM25([tokenize(t) for t in texts])
    pkl_path = os.path.join(d, 'archive_bm25.pkl')
    with open(pkl_path, 'wb') as f:
        pickle.dump({'n': len(texts), 'bm25': old_bm}, f)  # 无 tok 字段
    store = get_rag_store(td)
    store.invalidate(uid, KIND_ARCHIVE)
    threw = None
    hits = []
    try:
        hits = store.search(uid, KIND_ARCHIVE, 'UNIQUE-OLD-PKL-TOKEN', top_k=3)
    except Exception as e:
        threw = e
    check('C4 旧 pkl 无 tok 时 search 不抛',
          threw is None, str(threw))
    check('C4 旧 pkl search 能返回结果',
          any('UNIQUE-OLD-PKL-TOKEN' in (h or '') for h in hits), hits)
    tok_flag = None
    try:
        with open(pkl_path, 'rb') as f:
            payload = pickle.load(f)
        tok_flag = payload.get('tok')
    except Exception as e:
        tok_flag = f'err:{e}'
    check("C4 旧 pkl 被重建且含 tok==2",
          tok_flag == 2, f'tok={tok_flag!r}')


# ===================== C5 =====================

def test_c5_archive_strips_tools(td):
    section('C5 归档剔除工具输出')
    tc = ToolCall(id='tc1', name='web_search', arguments={'query': 'x'})
    msgs = [
        Message(role='user', content='帮我搜资源 UNIQUE-USER-AAA'),
        Message(role='assistant', content='', tool_calls=[tc]),
        Message(role='tool', content='找到了 MAGNET-ZZZ-777 磁力', tool_call_id='tc1', name='web_search'),
        Message(role='assistant', content='这是搜索结果 UNIQUE-ASST-BBB'),
    ]
    chunks = _chunk_turns(msgs)
    blob = '\n'.join(chunks)
    check('C5 _chunk_turns 不含 MAGNET-ZZZ-777',
          'MAGNET-ZZZ-777' not in blob, blob)
    check('C5 _chunk_turns 含 user 文本',
          'UNIQUE-USER-AAA' in blob, blob)
    check('C5 _chunk_turns 含 assistant 文本',
          'UNIQUE-ASST-BBB' in blob, blob)
    check('C5 _chunk_turns 跳过纯 tool_calls assistant',
          '调用工具' not in blob and '[tool:' not in blob, blob)

    fmt = _format_dialog(msgs)
    check('C5 _format_dialog 仍含工具结果',
          'MAGNET-ZZZ-777' in fmt or '[tool:' in fmt, fmt[:400])

    sess = AgentSession('+c5', 'c5tool', td, history_limit=40)
    sess.history = msgs + fill_history(8, tag='c5keep')
    sess.needs_compress = True
    ok = compress_session(sess, FakeProvider(summary='摘要：搜过资源。'),
                           base_cfg(history_keep_recent=4))
    check('C5 压缩成功', ok is True)
    store = get_rag_store(td)
    magnet_hits = store.search('c5tool', KIND_ARCHIVE, 'MAGNET-ZZZ-777', top_k=5)
    check('C5 压缩后 archive 搜不到 MAGNET-ZZZ-777',
          not any('MAGNET-ZZZ-777' in (h or '') for h in magnet_hits), magnet_hits)
    user_hits = store.search('c5tool', KIND_ARCHIVE, 'UNIQUE-USER-AAA', top_k=5)
    asst_hits = store.search('c5tool', KIND_ARCHIVE, 'UNIQUE-ASST-BBB', top_k=5)
    check('C5 压缩后 archive 能搜到 user token',
          any('UNIQUE-USER-AAA' in (h or '') for h in user_hits), user_hits)
    check('C5 压缩后 archive 能搜到 assistant token',
          any('UNIQUE-ASST-BBB' in (h or '') for h in asst_hits), asst_hits)


# ===================== C6 =====================

def test_c6_short_query_no_rag(td):
    section('C6 短 query 不注入')
    check('C6 DEFAULT_CONFIG rag_min_query_chars==4',
          DEFAULT_CONFIG.get('rag_min_query_chars') == 4,
          f"got {DEFAULT_CONFIG.get('rag_min_query_chars')!r}")

    cfg = base_cfg(enable_rag=True, rag_top_k=4, rag_min_query_chars=4)
    store = get_rag_store(td)
    store.add_chunks('c6u', KIND_ARCHIVE, ['好 UNIQUE-SHORT-HIT 相关回忆块'])
    sess = AgentSession('+c6', 'c6u', td, history_limit=40)
    sess.rolling_summary = '用户喜欢短摘要测试 ROLLING-KEEP'
    sys_short = sess._system_message(cfg, query='好')
    check('C6 短 query 不出现「相关的回忆」',
          '相关的回忆' not in sys_short.content, sys_short.content[:400])
    check('C6 短 query 不注入 RAG 命中',
          'UNIQUE-SHORT-HIT' not in sys_short.content, sys_short.content[:400])
    check('C6 短 query 仍注入滚动摘要',
          'ROLLING-KEEP' in sys_short.content or '短摘要测试' in sys_short.content,
          sys_short.content[:400])

    sys_long = sess._system_message(cfg, query='UNIQUE-SHORT-HIT')
    check('C6 长 query 注入相关的回忆',
          '相关的回忆' in sys_long.content and 'UNIQUE-SHORT-HIT' in sys_long.content,
          sys_long.content[:400])


# ===================== C7 =====================

def test_c7_scheduler_no_full_load(td):
    section('C7 调度不全量加载')
    cfg = base_cfg(is_running=True, enable_auto_compress=True,
                    compress_scan_every_ticks=20, compress_idle_seconds=60)
    mgr, _, _ = make_manager(td, cfg)
    for i in range(30):
        phone = f'+scan{i:03d}'
        uid = f'scanuid{i:03d}'
        mgr.index[phone] = uid
        data = {
            'phone': phone,
            'user_id': uid,
            'created_at': '2020-01-01T00:00:00',
            'last_active': '2020-01-01T00:00:00',
            'rolling_summary': '',
            'needs_compress': False,
            'history': [
                {'role': 'user', 'content': 'hi'},
                {'role': 'assistant', 'content': 'yo'},
            ],
        }
        with open(os.path.join(td, f'{uid}.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    mgr._save_index()
    check('C7 预置 30 用户且 _sessions 为空',
          len(mgr.index) >= 30 and len(mgr._sessions) == 0,
          f'index={len(mgr.index)} cached={len(mgr._sessions)}')

    import agent.session as sess_mod
    orig_init = sess_mod.AgentSession.__init__
    counter = {'n': 0}

    def wrapped(self, *args, **kwargs):
        counter['n'] += 1
        return orig_init(self, *args, **kwargs)

    sess_mod.AgentSession.__init__ = wrapped
    try:
        for _ in range(3):
            scheduler_tick(mgr)
        check('C7 连续 3 次 tick 未构造未缓存 AgentSession',
              counter['n'] == 0,
              f'init_count={counter["n"]}')
    finally:
        sess_mod.AgentSession.__init__ = orig_init


# ===================== C8 =====================

def test_c8_summary_language(td):
    section('C8 摘要语言')
    check('C8 _SUMMARIZE_PROMPT 不含「中文摘要」',
          '中文摘要' not in (_SUMMARIZE_PROMPT or ''),
          _SUMMARIZE_PROMPT[:120])


# ===================== C9 =====================

def test_c9_dead_code(td):
    section('C9 死代码移除')
    import agent.session as sess_mod
    check('C9 无 read_memory_digest',
          not hasattr(sess_mod, 'read_memory_digest'))
    check('C9 无 MEMORY_DIGEST_LIMIT',
          not hasattr(sess_mod, 'MEMORY_DIGEST_LIMIT'))


# ===================== C10 =====================

def test_c10_admin_auth(td):
    section('C10 后台鉴权')
    check('C10 DEFAULT_CONFIG bind_host==127.0.0.1',
          DEFAULT_CONFIG.get('bind_host') == '127.0.0.1',
          f"got {DEFAULT_CONFIG.get('bind_host')!r}")
    check('C10 DEFAULT_CONFIG admin_token 默认空串',
          DEFAULT_CONFIG.get('admin_token') == '',
          f"got {DEFAULT_CONFIG.get('admin_token')!r}")

    flask_names = [
        'C10 无凭据返回 401',
        'C10 401 含 WWW-Authenticate',
        'C10 无凭据多路由均 401',
        'C10 Basic x:token 返回 200',
        'C10 Bearer token 返回 200',
        'C10 X-Admin-Token 返回 200',
        'C10 token 为空不校验',
        'C10 中文 token Basic 200',
        'C10 中文 token Bearer 200',
        'C10 中文 token X-Admin-Token 200',
        'C10 中文 token 错 token 401',
    ]
    try:
        import app as appmod
    except ImportError as e:
        for name in flask_names:
            skip(name, f'ImportError: {e}')
        return

    old_token = appmod.config.get('admin_token', '')
    client = appmod.app.test_client()
    token = 'test-admin-secret-c10'

    def _path(rule):
        return re.sub(r'<[^>]+>', 'x', rule.rule)

    def _open(path, method='GET', headers=None):
        return client.open(path, method=method, headers=headers or {})

    try:
        appmod.config['admin_token'] = token
        r = _open('/get_status', 'GET')
        check('C10 无凭据返回 401', r.status_code == 401, f'status={r.status_code}')
        wa = r.headers.get('WWW-Authenticate') or r.headers.get('Www-Authenticate')
        check('C10 401 含 WWW-Authenticate', bool(wa), dict(r.headers))

        bad = []
        for rule in appmod.app.url_map.iter_rules():
            if rule.endpoint == 'static':
                continue
            methods = [m for m in rule.methods if m not in ('HEAD', 'OPTIONS')]
            method = 'GET' if 'GET' in rule.methods else (methods[0] if methods else 'GET')
            path = _path(rule)
            resp = _open(path, method)
            if resp.status_code != 401:
                bad.append((path, method, resp.status_code))
        check('C10 无凭据多路由均 401', not bad, bad[:8])

        basic = 'Basic ' + base64.b64encode(f'x:{token}'.encode()).decode()
        r_b = _open('/get_status', 'GET', headers={'Authorization': basic})
        check('C10 Basic x:token 返回 200', r_b.status_code == 200, r_b.status_code)

        r_br = _open('/get_status', 'GET', headers={'Authorization': f'Bearer {token}'})
        check('C10 Bearer token 返回 200', r_br.status_code == 200, r_br.status_code)

        r_x = _open('/get_status', 'GET', headers={'X-Admin-Token': token})
        check('C10 X-Admin-Token 返回 200', r_x.status_code == 200, r_x.status_code)

        zh = '中文令牌'
        appmod.config['admin_token'] = zh
        zh_basic = 'Basic ' + base64.b64encode(f'x:{zh}'.encode('utf-8')).decode()
        r_zh_b = _open('/get_status', 'GET', headers={'Authorization': zh_basic})
        check('C10 中文 token Basic 200', r_zh_b.status_code == 200, r_zh_b.status_code)
        r_zh_br = _open('/get_status', 'GET', headers={'Authorization': f'Bearer {zh}'})
        check('C10 中文 token Bearer 200', r_zh_br.status_code == 200, r_zh_br.status_code)
        r_zh_x = _open('/get_status', 'GET', headers={'X-Admin-Token': zh})
        check('C10 中文 token X-Admin-Token 200', r_zh_x.status_code == 200, r_zh_x.status_code)
        r_zh_bad = _open('/get_status', 'GET', headers={'X-Admin-Token': '错误令牌'})
        check('C10 中文 token 错 token 401', r_zh_bad.status_code == 401, r_zh_bad.status_code)

        appmod.config['admin_token'] = ''
        r_off = _open('/get_status', 'GET')
        check('C10 token 为空不校验', r_off.status_code == 200, r_off.status_code)
    finally:
        appmod.config['admin_token'] = old_token


# ===================== C11 =====================

BLOCKED_URLS = [
    'http://127.0.0.1:1/',
    'http://169.254.169.254/latest/meta-data/',
    'http://localhost/',
    'http://[::1]/',
    'file:///etc/passwd',
    'http://10.0.0.1/',
    'http://192.168.1.1/',
    'http://0.0.0.0/',
]


class _FakeHTML:
    status_code = 200
    headers = {'content-type': 'text/html', 'Content-Type': 'text/html'}
    text = '<html><body>Hello RAG</body></html>'
    content = text.encode()

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_content(self, chunk_size=8192, decode_unicode=False):
        data = self.text if decode_unicode else self.text.encode()
        yield data


class _Fake302:
    status_code = 302
    headers = {'location': 'http://127.0.0.1/admin', 'Location': 'http://127.0.0.1/admin'}
    text = ''
    content = b''

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_content(self, chunk_size=8192, decode_unicode=False):
        return iter(())


def test_c11_ssrf(td):
    section('C11 web_fetch 防 SSRF')
    import tools.web as web
    import requests as reqmod

    ctx = ToolContext('c11u', '+c11', td, {})
    orig_web_get = web.requests.get
    orig_req_get = reqmod.get
    called: list = []

    def boom(url, *a, **k):
        called.append(url)
        raise AssertionError(f'requests.get should not be called: {url}')

    web.requests.get = boom
    reqmod.get = boom
    try:
        for url in BLOCKED_URLS:
            called.clear()
            try:
                out = WebFetchTool().run(ctx, url=url)
            except Exception as e:
                out = f'EXC:{e}'
            check(f'C11 拒绝 {url}',
                  isinstance(out, str) and '拒绝' in out, out[:200] if isinstance(out, str) else out)
            check(f'C11 未调用 get {url}',
                  not called, called)
    finally:
        web.requests.get = orig_web_get
        reqmod.get = orig_req_get

    orig_gai = socket.getaddrinfo

    def fake_gai(host, port, *a, **k):
        host_s = str(host).strip('[]')
        if host_s in ('127.0.0.1', 'localhost', '::1', '0.0.0.0', '::'):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port or 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 80))]

    def fake_ok(url, *a, **k):
        called.append(url)
        return _FakeHTML()

    called.clear()
    socket.getaddrinfo = fake_gai
    if hasattr(web, 'socket'):
        orig_web_gai = web.socket.getaddrinfo
        web.socket.getaddrinfo = fake_gai
    else:
        orig_web_gai = None
    web.requests.get = fake_ok
    reqmod.get = fake_ok
    try:
        out_ok = WebFetchTool().run(ctx, url='http://example.com/')
        check('C11 公网正向含 Hello RAG',
              isinstance(out_ok, str) and 'Hello RAG' in out_ok, out_ok[:300] if isinstance(out_ok, str) else out_ok)
    finally:
        socket.getaddrinfo = orig_gai
        if orig_web_gai is not None:
            web.socket.getaddrinfo = orig_web_gai
        web.requests.get = orig_web_get
        reqmod.get = orig_req_get

    def fake_redirect(url, *a, **k):
        called.append(url)
        if '127.0.0.1' in str(url) or 'localhost' in str(url):
            raise AssertionError(f'second hop leaked to {url}')
        return _Fake302()

    called.clear()
    socket.getaddrinfo = fake_gai
    if hasattr(web, 'socket'):
        web.socket.getaddrinfo = fake_gai
    web.requests.get = fake_redirect
    reqmod.get = fake_redirect
    try:
        try:
            out_r = WebFetchTool().run(ctx, url='http://example.com/redirect')
        except Exception as e:
            out_r = f'EXC:{e}'
        check('C11 重定向到内网含拒绝',
              isinstance(out_r, str) and '拒绝' in out_r, out_r[:300] if isinstance(out_r, str) else out_r)
        leaked = [u for u in called if '127.0.0.1' in str(u) or 'localhost' in str(u)]
        check('C11 重定向不请求 127.0.0.1',
              (len(called) <= 1 or not leaked) and not leaked,
              called)
    finally:
        socket.getaddrinfo = orig_gai
        if hasattr(web, 'socket') and orig_web_gai is not None:
            web.socket.getaddrinfo = orig_web_gai
        web.requests.get = orig_web_get
        reqmod.get = orig_req_get


def main():
    td = tempfile.mkdtemp(prefix='imsg_p0p1_')
    print(f'P0/P1/安全测试目录: {td}')
    suites = [
        test_c1_hard_trim_archive,
        test_c2_compress_nonblocking,
        test_c3_bare_complete,
        test_c4_chinese_bigrams,
        test_c5_archive_strips_tools,
        test_c6_short_query_no_rag,
        test_c7_scheduler_no_full_load,
        test_c8_summary_language,
        test_c9_dead_code,
        test_c10_admin_auth,
        test_c11_ssrf,
    ]
    try:
        for fn in suites:
            sub = tempfile.mkdtemp(prefix=fn.__name__ + '_', dir=td)
            try:
                fn(sub)
            except Exception:
                global FAIL
                FAIL += 1
                print(f'  EXCEPTION in {fn.__name__}')
                traceback.print_exc()
                ERRORS.append(f'  EXCEPTION  {fn.__name__}')
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f'\n==== P0/P1/安全测试: {PASS} passed, {FAIL} failed, {SKIP} skipped ====')
    if ERRORS:
        print('失败项:')
        for e in ERRORS:
            print(e)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
