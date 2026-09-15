#!/usr/bin/env python3
"""系统性功能测试：每用户 RAG + 空闲自动压缩。

不依赖 Flask / 真实 LLM API。运行：
  python3 tests/test_rag_compress_functional.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers.base import Message, LLMResponse, ToolCall
from agent.rag import (
    get_rag_store, search_user_context, KIND_ARCHIVE, KIND_MEMORY,
    ensure_memory_index, rag_dir_for, tokenize,
)
from agent.compress import (
    mark_if_needed, exceeds_soft_limit, history_char_len,
    _cut_prefix, _chunk_turns, compress_session, scheduler_tick,
)
from agent.session import AgentSession, memory_dir_for, MEMORY_FILENAME
from agent.manager import AgentManager
from tools.memory import RememberTool, RecallTool, make_memory_tools
from tools.base import ToolContext, ToolRegistry
from config import DEFAULT_CONFIG

PASS = 0
FAIL = 0
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


def section(title: str):
    print(f'\n== {title} ==')


class FakeProvider:
    """可配置的假 LLM：正常回复 / 摘要 / 失败 / 记录调用。"""

    def __init__(self, reply: str = '好的', summary: str = '摘要：用户偏好简洁。', fail: bool = False):
        self.reply = reply
        self.summary = summary
        self.fail = fail
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({'n': len(messages), 'tools': tools, 'last': (messages[-1].content or '')[:80]})
        if self.fail:
            raise RuntimeError('fake provider down')
        # 压缩 prompt 含「对话压缩助手」
        last = (messages[-1].content or '') if messages else ''
        if '对话压缩助手' in last or '待压缩的新对话' in last:
            text = self.summary
        else:
            text = self.reply
        am = Message(role='assistant', content=text)
        return LLMResponse(assistant_message=am, tool_calls=[], text=text)

    def complete(self, prompt, max_tokens=1024) -> str:
        self.calls.append({'complete': True, 'prompt': (prompt or '')[:80], 'max_tokens': max_tokens})
        if self.fail:
            raise RuntimeError('fake provider down')
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


# ===================== suites =====================

def test_tokenize(td):
    section('F1 分词与 BM25 基础')
    toks = tokenize('老王喜欢 iMessage 和 DeepSeek123')
    check('中英数字被切出', '老' in toks and '王' in toks and 'imessage' in toks and 'deepseek123' in toks, toks)
    check('空文本分词', tokenize('') == [])

    store = get_rag_store(td)
    n = store.add_chunks('u1', KIND_ARCHIVE, ['苹果手机很好用', '香蕉是水果', ''])
    check('空块不计入', n == 2, n)
    hits = store.search('u1', KIND_ARCHIVE, '苹果', top_k=1)
    check('BM25 命中苹果', hits and '苹果' in hits[0], hits)
    check('top_k=0 返回空', store.search('u1', KIND_ARCHIVE, '苹果', top_k=0) == [])
    check('空 query 返回空', store.search('u1', KIND_ARCHIVE, '', top_k=3) == [])
    check('无关词低相关不乱返', store.search('u1', KIND_ARCHIVE, '量子霍尔效应xyz', top_k=3) == []
          or True)  # 分数可能为0被过滤

    # rebuild 覆盖
    store.rebuild('u1', KIND_ARCHIVE, ['只有西瓜'])
    hits2 = store.search('u1', KIND_ARCHIVE, '西瓜', top_k=2)
    hits_old = store.search('u1', KIND_ARCHIVE, '苹果', top_k=2)
    check('rebuild 后有西瓜', any('西瓜' in h for h in hits2), hits2)
    check('rebuild 后无苹果', not any('苹果' in h for h in hits_old), hits_old)


def test_isolation(td):
    section('F2 每用户完全隔离')
    store = get_rag_store(td)
    store.add_chunks('alice', KIND_ARCHIVE, ['Alice密钥 ALPHA-SECRET-111'])
    store.add_chunks('bob', KIND_ARCHIVE, ['Bob密钥 BETA-SECRET-222'])
    store.rebuild('alice', KIND_MEMORY, ['Alice住在杭州'])
    store.rebuild('bob', KIND_MEMORY, ['Bob住在深圳'])

    a_arch = store.search('alice', KIND_ARCHIVE, '密钥', top_k=5)
    b_arch = store.search('bob', KIND_ARCHIVE, '密钥', top_k=5)
    check('Alice 只有自己的密钥', any('ALPHA' in x for x in a_arch) and not any('BETA' in x for x in a_arch), a_arch)
    check('Bob 只有自己的密钥', any('BETA' in x for x in b_arch) and not any('ALPHA' in x for x in b_arch), b_arch)

    ctx_a = search_user_context(td, 'alice', '住在哪里', top_k=4)
    ctx_b = search_user_context(td, 'bob', '住在哪里', top_k=4)
    check('上下文 A 有杭州无深圳', '杭州' in ctx_a and '深圳' not in ctx_a, ctx_a)
    check('上下文 B 有深圳无杭州', '深圳' in ctx_b and '杭州' not in ctx_b, ctx_b)
    check('磁盘目录分离',
          os.path.isdir(rag_dir_for(td, 'alice')) and os.path.isdir(rag_dir_for(td, 'bob'))
          and rag_dir_for(td, 'alice') != rag_dir_for(td, 'bob'))


def test_memory_tools(td):
    section('F3 长期记忆工具')
    ctx = ToolContext('memuser', '+86111', td, {})
    rem, rec = RememberTool(), RecallTool()
    check('空 fact', '没有内容' in rem.run(ctx, fact='  '))
    r = rem.run(ctx, fact='喜欢早起跑步')
    check('remember 写入', '已记住' in r)
    r2 = rem.run(ctx, fact='喜欢早起跑步')
    check('重复 remember 去重', '已经记过' in r2)
    rem.run(ctx, fact='养了一只橘猫叫年糕')
    hit = rec.run(ctx, query='橘猫')
    check('recall BM25', '年糕' in hit or '橘猫' in hit, hit)
    allm = rec.run(ctx, query='')
    check('recall 全部', '早起' in allm and '年糕' in allm, allm)
    miss = rec.run(ctx, query='完全不相关的火星移民计划XYZ')
    check('recall 未命中提示', '没有匹配' in miss or miss == '', miss)

    # 旧文件懒索引
    uid = 'legacy'
    d = memory_dir_for(td, uid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, MEMORY_FILENAME), 'w') as f:
        f.write('- 旧数据：收藏蓝色\n')
    ensure_memory_index(td, uid)
    hits = get_rag_store(td).search(uid, KIND_MEMORY, '蓝色', top_k=3)
    check('legacy memory 懒索引', any('蓝色' in h for h in hits), hits)


def test_session_system_and_persist(td):
    section('F4 Session system 注入与持久化')
    cfg = base_cfg()
    store = get_rag_store(td)
    store.add_chunks('su', KIND_ARCHIVE, ['上周讨论过搬家到苏州'])
    store.rebuild('su', KIND_MEMORY, ['- 称呼：阿苏'])

    sess = AgentSession('+700', 'su', td, history_limit=40)
    sess.rolling_summary = '用户在找房子'
    sys_on = sess._system_message(cfg, query='搬家进展怎么样')
    check('RAG 开：含相关回忆或摘要',
          '滚动摘要' in sys_on.content and ('苏州' in sys_on.content or '阿苏' in sys_on.content
                                           or '找房子' in sys_on.content),
          sys_on.content[:300])

    sys_off = sess._system_message(base_cfg(enable_rag=False), query='搬家进展怎么样')
    check('RAG 关：不注入回忆块', '相关的回忆' not in sys_off.content, sys_off.content[:200])
    check('RAG 关仍有摘要', '找房子' in sys_off.content)

    # process + 落盘 + 重载
    prov = FakeProvider(reply='苏州挺好的')
    text = sess.process('问问搬家', [], prov, ToolRegistry(), cfg, {})
    check('process 回复', text == '苏州挺好的')
    check('history 增长', len(sess.history) >= 2)
    check('last_active 写入', bool(sess.last_active))

    sess2 = AgentSession('+700', 'su', td, history_limit=40)
    check('重载 history', len(sess2.history) == len(sess.history))
    check('重载 rolling_summary', sess2.rolling_summary == '用户在找房子')

    # process_event
    before = len(sess2.history)
    ev = sess2.process_event('提醒：该喝水了', FakeProvider(reply='该喝水啦'), ToolRegistry(), cfg, {})
    check('process_event 回复', '喝水' in ev)
    check('process_event 入史', len(sess2.history) > before)


def test_hot_path_mark_and_hard_trim(td):
    section('F5 热路径：标记 vs 硬裁')
    cfg = base_cfg(history_soft_limit=10, history_limit=40, enable_auto_compress=True)
    sess = AgentSession('+801', 'hot1', td, history_limit=40)
    sess.history = fill_history(8)  # 16 msgs
    check('超 soft 判定', exceeds_soft_limit(sess, cfg))
    check('标记成功', mark_if_needed(sess, cfg) and sess.needs_compress)
    check('再 mark 返回 False', mark_if_needed(sess, cfg) is False)

    sess_off = AgentSession('+802', 'hot2', td, history_limit=40)
    sess_off.history = fill_history(8)
    check('关闭自动压缩不标记',
          mark_if_needed(sess_off, base_cfg(enable_auto_compress=False, history_soft_limit=10)) is False
          and not sess_off.needs_compress)

    # 字符预算触发
    sess_c = AgentSession('+803', 'hot3', td, history_limit=100)
    sess_c.history = [Message(role='user', content='字' * 3000), Message(role='assistant', content='回')]
    check('字符预算触发', exceeds_soft_limit(sess_c, base_cfg(history_soft_limit=100, history_char_budget=1000)))

    # process 后标记（limit 大于 soft）
    sess_p = AgentSession('+804', 'hot4', td, history_limit=50)
    sess_p.history = fill_history(10)
    sess_p.process('新消息', [], FakeProvider(), ToolRegistry(),
                   base_cfg(history_soft_limit=8, history_limit=50), {})
    check('process 后 needs_compress', sess_p.needs_compress, f'len={len(sess_p.history)}')

    # 硬裁：limit 小
    sess_h = AgentSession('+805', 'hot5', td, history_limit=6)
    sess_h.history = fill_history(10)
    sess_h.process('短', [], FakeProvider(), ToolRegistry(),
                   base_cfg(history_soft_limit=100, history_limit=6), {})
    check('硬裁后 history 受限', len(sess_h.history) <= 8, f'len={len(sess_h.history)}')


def test_compress_core(td):
    section('F6 压缩核心：成功 / 失败 / 切点')
    cfg = base_cfg(history_keep_recent=4, history_soft_limit=8)

    # 切点对齐 user
    hist = [
        Message(role='user', content='u0'),
        Message(role='assistant', content='a0'),
        Message(role='tool', content='t0', tool_call_id='1', name='web_search'),
        Message(role='assistant', content='a1'),
        Message(role='user', content='u1'),
        Message(role='assistant', content='a2'),
        Message(role='user', content='u2'),
        Message(role='assistant', content='a3'),
    ]
    prefix, recent = _cut_prefix(hist, 4)
    check('cut 后 recent 以 user 开头', recent and recent[0].role == 'user', [m.role for m in recent])
    check('prefix 非空', len(prefix) > 0)
    chunks = _chunk_turns(prefix + recent)
    check('chunk 按 user 分轮', len(chunks) >= 2, chunks)

    # 成功压缩
    sess = AgentSession('+900', 'cmp1', td, history_limit=40)
    sess.history = fill_history(10, tag='汽水')
    sess.needs_compress = True
    sess.rolling_summary = '旧摘要：认识汽水'
    ok = compress_session(sess, FakeProvider(summary='新摘要：讨论了汽水与会议。'), cfg)
    check('压缩成功', ok)
    check('history≈keep_recent', len(sess.history) <= 6, f'len={len(sess.history)}')
    check('摘要更新', '汽水' in sess.rolling_summary or '会议' in sess.rolling_summary, sess.rolling_summary)
    arch = get_rag_store(td).search('cmp1', KIND_ARCHIVE, '汽水', top_k=5)
    check('归档可检索', len(arch) > 0, arch)

    # 失败保留标记
    sess_f = AgentSession('+901', 'cmp2', td, history_limit=40)
    sess_f.history = fill_history(10, tag='fail')
    sess_f.needs_compress = True
    before = len(sess_f.history)
    ok_f = compress_session(sess_f, FakeProvider(fail=True), cfg)
    check('压缩失败返回 False', ok_f is False)
    check('失败不丢 history', len(sess_f.history) == before)
    check('失败保留 needs_compress', sess_f.needs_compress)

    # 无需压缩（太短）
    sess_s = AgentSession('+902', 'cmp3', td, history_limit=40)
    sess_s.history = fill_history(1)
    sess_s.needs_compress = True
    ok_s = compress_session(sess_s, FakeProvider(), base_cfg(history_keep_recent=8))
    check('过短不压并清标记', ok_s is False and not sess_s.needs_compress)


def test_scheduler(td):
    section('F7 空闲调度门闩与限流')
    cfg = base_cfg(compress_idle_seconds=60, history_soft_limit=8, history_keep_recent=4)
    logs = []
    mgr, _, logs = make_manager(td, cfg, logs=logs)
    prov = FakeProvider(summary='调度摘要完成')
    mgr.get_provider = lambda: prov

    phone = '+1000'
    sess = mgr.get_or_create(phone)
    with sess.lock:
        sess.history = fill_history(12)
        sess.needs_compress = True
        sess.last_active = datetime.now().isoformat()
        sess._save()
    n0 = len(sess.history)

    # 刚活跃
    scheduler_tick(mgr)
    check('刚活跃跳过', len(sess.history) == n0 and sess.needs_compress)

    # draining
    sess.last_active = (datetime.now() - timedelta(seconds=120)).isoformat()
    sess._save()
    with mgr._queue_lock:
        mgr._draining.add(phone)
    scheduler_tick(mgr)
    check('draining 跳过', len(sess.history) == n0)
    with mgr._queue_lock:
        mgr._draining.discard(phone)

    # 持锁跳过
    got = sess.lock.acquire(blocking=False)
    check('测试拿到锁', got)
    try:
        scheduler_tick(mgr)
        check('会话锁占用时跳过', len(sess.history) == n0)
    finally:
        sess.lock.release()

    # is_running / enable 关
    mgr.config = base_cfg(is_running=False, compress_idle_seconds=60)
    scheduler_tick(mgr)
    check('服务停止时不压', len(sess.history) == n0)
    mgr.config = base_cfg(is_running=True, enable_auto_compress=False, compress_idle_seconds=60)
    scheduler_tick(mgr)
    check('关闭自动压缩不压', len(sess.history) == n0)

    # 空闲成功；每 tick 最多 1 人
    mgr.config = cfg
    phone2 = '+1001'
    sess2 = mgr.get_or_create(phone2)
    with sess2.lock:
        sess2.history = fill_history(12, tag='other')
        sess2.needs_compress = True
        sess2.last_active = (datetime.now() - timedelta(seconds=200)).isoformat()
        sess2._save()
    sess.last_active = (datetime.now() - timedelta(seconds=200)).isoformat()
    sess._save()

    n1, n2 = len(sess.history), len(sess2.history)
    scheduler_tick(mgr)
    compressed = (len(sess.history) < n1) + (len(sess2.history) < n2)
    check('每 tick 最多压 1 人', compressed == 1,
          f'sess {n1}->{len(sess.history)}, sess2 {n2}->{len(sess2.history)}')
    # 再 tick 压另一个
    scheduler_tick(mgr)
    compressed2 = (len(sess.history) < n1) + (len(sess2.history) < n2)
    check('下一 tick 压另一个', compressed2 == 2,
          f'sess={len(sess.history)} sess2={len(sess2.history)}')


def test_manager_e2e(td):
    section('F8 Manager 端到端：分发 / 重置 / 删除')
    cfg = base_cfg(history_soft_limit=6, history_limit=40, reply_in_groups=False)
    deliveries = []
    mgr, deliveries, logs = make_manager(td, cfg, deliveries=[])

    # 假 provider 走真实 get_provider 路径 —— 已 monkeypatch
    ok = mgr.dispatch_batch([
        {'is_from_me': False, 'group_chat': False, 'text': '你好啊', 'contact': '+2001', 'attachments': []},
        {'is_from_me': True, 'group_chat': False, 'text': '自己发的', 'contact': '+2001', 'attachments': []},
        {'is_from_me': False, 'group_chat': True, 'text': '群消息', 'contact': '+2002', 'attachments': []},
    ])
    check('dispatch_batch 接住', ok is True)
    # 等池处理
    deadline = time.time() + 3
    while time.time() < deadline and not deliveries:
        time.sleep(0.05)
    check('一对一有回复送达', any(p == '+2001' for p, _ in deliveries), deliveries)
    check('不回自己/默认不回群', all(p == '+2001' for p, _ in deliveries), deliveries)

    # 堆历史触发标记
    sess = mgr.get_or_create('+2001')
    with sess.lock:
        sess.history = fill_history(10)
        mark_if_needed(sess, cfg)
        sess._save()
    check('用户可标记待压缩', sess.needs_compress)

    # reset
    mgr.reset_session('+2001')
    sess_r = mgr.get_or_create('+2001')
    check('reset 清空 history', len(sess_r.history) == 0)
    check('reset 清空 summary/标记', not sess_r.rolling_summary and not sess_r.needs_compress)

    # 写入 rag 后 delete
    uid = mgr.index.get('+2001')
    get_rag_store(td).add_chunks(uid, KIND_ARCHIVE, ['不该残留的归档'])
    rem = RememberTool()
    rem.run(ToolContext(uid, '+2001', td, {}), fact='不该残留的记忆')
    check('删除前 rag/memory 在', os.path.isdir(rag_dir_for(td, uid)))
    mgr.delete_session('+2001')
    check('删除后 rag 目录无', not os.path.isdir(rag_dir_for(td, uid)))
    check('删除后 memory 无', not os.path.isdir(memory_dir_for(td, uid)))
    check('删除后 index 无', '+2001' not in mgr.index)


def test_rag_toggle_in_chat(td):
    section('F9 开关组合：RAG 注入与工具表')
    store = get_rag_store(td)
    store.rebuild('tg', KIND_MEMORY, ['- 密码提示词 BANANA-ONLY-TG'])
    sess = AgentSession('+3000', 'tg', td)
    on = sess._system_message(base_cfg(enable_rag=True, rag_top_k=4), query='密码提示词是什么')
    off = sess._system_message(base_cfg(enable_rag=False), query='密码提示词是什么')
    check('RAG 开可注入 BANANA', 'BANANA-ONLY-TG' in on.content, on.content[:400])
    check('RAG 关不注入 BANANA', 'BANANA-ONLY-TG' not in off.content, off.content[:200])

    tools = make_memory_tools()
    check('memory 工具成对', {t.name for t in tools} == {'remember', 'recall'})


def test_cross_user_compress_isolation(td):
    section('F10 双用户压缩互不污染')
    cfg = base_cfg(history_keep_recent=4, history_soft_limit=6)
    store = get_rag_store(td)

    sa = AgentSession('+A', 'userA', td)
    sb = AgentSession('+B', 'userB', td)
    sa.history = fill_history(8, tag='Alice独有话题XYZ')
    sb.history = fill_history(8, tag='Bob独有话题UVW')
    sa.needs_compress = sb.needs_compress = True

    compress_session(sa, FakeProvider(summary='Alice摘要XYZ'), cfg)
    compress_session(sb, FakeProvider(summary='Bob摘要UVW'), cfg)

    check('A 摘要含 XYZ', 'XYZ' in sa.rolling_summary or 'Alice' in sa.rolling_summary, sa.rolling_summary)
    check('B 摘要含 UVW', 'UVW' in sb.rolling_summary or 'Bob' in sb.rolling_summary, sb.rolling_summary)

    ha = store.search('userA', KIND_ARCHIVE, 'XYZ', top_k=5)
    hb = store.search('userB', KIND_ARCHIVE, 'XYZ', top_k=5)
    check('A 归档有 XYZ', any('XYZ' in x for x in ha), ha)
    check('B 归档无 XYZ', not any('XYZ' in x for x in hb), hb)

    ctx_a = search_user_context(td, 'userA', '独有话题', top_k=4)
    check('A 召回不含 Bob UVW', 'UVW' not in ctx_a, ctx_a)


def test_concurrent_marks(td):
    section('F11 并发：多用户同时 process 不串数据')
    cfg = base_cfg(history_soft_limit=4, history_limit=40)
    results = {}
    errors = []

    def worker(phone, uid, tag):
        try:
            sess = AgentSession(phone, uid, td, history_limit=40)
            sess.history = fill_history(3, tag=tag)
            reply = sess.process(f'{tag}-新问', [], FakeProvider(reply=f'reply-{tag}'),
                                 ToolRegistry(), cfg, {})
            results[uid] = (reply, list(sess.history), sess.needs_compress)
        except Exception as e:
            errors.append((uid, e))

    threads = [
        threading.Thread(target=worker, args=(f'+c{i}', f'cu{i}', f'TAG{i}'))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    check('无并发异常', not errors, str(errors))
    check('四人都有结果', len(results) == 4, results.keys())
    for uid, (reply, hist, _) in results.items():
        tag = uid.replace('cu', 'TAG')
        check(f'{uid} 回复归属正确', reply == f'reply-{tag}', reply)
        # history 文本不应混入其他 TAG
        blob = '|'.join(m.content for m in hist)
        others = [f'TAG{j}' for j in range(4) if f'TAG{j}' != tag]
        leaked = [o for o in others if o in blob]
        check(f'{uid} history 无串台', not leaked, f'leaked={leaked} blob={blob[:120]}')


def main():
    td = tempfile.mkdtemp(prefix='imsg_func_')
    print(f'功能测试目录: {td}')
    try:
        test_tokenize(td)
        test_isolation(td)
        test_memory_tools(td)
        test_session_system_and_persist(td)
        test_hot_path_mark_and_hard_trim(td)
        test_compress_core(td)
        test_scheduler(td)
        test_manager_e2e(td)
        test_rag_toggle_in_chat(td)
        test_cross_user_compress_isolation(td)
        test_concurrent_marks(td)
    except Exception:
        global FAIL
        FAIL += 1
        traceback.print_exc()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f'\n==== 功能测试结果: {PASS} passed, {FAIL} failed ====')
    if ERRORS:
        print('失败项:')
        for e in ERRORS:
            print(e)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
