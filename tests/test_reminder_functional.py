#!/usr/bin/env python3
"""系统性功能测试：定时提醒 / 任务（tools/reminder.py）。

不依赖 Flask / 真实 LLM / AppleScript。运行：
  python3 tests/test_reminder_functional.py
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

from providers.base import Message, LLMResponse
from agent.manager import AgentManager
from tools.base import ToolContext, ToolRegistry
from tools.reminder import (
    CreateReminderTool, ListRemindersTool, CancelReminderTool,
    make_reminder_tools, scheduler_tick, mark_fired, _parse_fire_at,
    _load, _save, _store_path, MISSED_GRACE_SECONDS, MAX_PENDING_PER_USER,
    MAX_FIRE_PER_TICK,
)
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
    def __init__(self, reply: str = '提醒：该做事了'):
        self.reply = reply
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages[-1].content if messages else '')
        am = Message(role='assistant', content=self.reply)
        return LLMResponse(assistant_message=am, tool_calls=[], text=self.reply)

    def complete(self, prompt, max_tokens=1024) -> str:
        return self.reply


def ctx(td, phone='+8610001', uid='user-a') -> ToolContext:
    return ToolContext(uid, phone, td, {})


def create(td, content, phone='+8610001', uid='user-a', **kwargs) -> str:
    return CreateReminderTool().run(ctx(td, phone, uid), content=content, **kwargs)


def list_r(td, phone='+8610001', uid='user-a') -> str:
    return ListRemindersTool().run(ctx(td, phone, uid))


def cancel(td, rid, phone='+8610001', uid='user-a') -> str:
    return CancelReminderTool().run(ctx(td, phone, uid), id=rid)


def load_items(td) -> list:
    return _load(td)


def make_mgr(td, deliveries=None, logs=None, reply='到点啦，记得喝水'):
    deliveries = deliveries if deliveries is not None else []
    logs = logs if logs is not None else []
    cfg = {**DEFAULT_CONFIG, 'is_running': True, 'enable_reminder': True, 'max_iters': 2}
    mgr = AgentManager(
        td, cfg,
        registry_factory=lambda: ToolRegistry(),
        deliver=lambda phone, text: deliveries.append((phone, text)),
        log=lambda m, level='info': logs.append((level, m)),
        max_workers=4,
    )
    mgr.get_provider = lambda: FakeProvider(reply=reply)
    return mgr, deliveries, logs


def wait_until(pred, timeout=3.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def extract_id(msg: str) -> str:
    # 「编号 abcd1234」
    if '编号' in msg:
        return msg.split('编号')[-1].strip().rstrip('）)').strip()
    return ''


# -------------------- suites --------------------

def test_parse_time():
    section('R1 时间解析')
    now = time.time()
    # delay
    t = _parse_fire_at('', delay_minutes=5)
    check('delay_minutes=5 ≈ 现在+300s', abs(t - (now + 300)) < 2, t - now)

    # absolute
    future = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=30, second=0, microsecond=0)
    t2 = _parse_fire_at(future.strftime('%Y-%m-%d %H:%M'))
    check('绝对时间 YYYY-MM-DD HH:MM', abs(t2 - future.timestamp()) < 1)

    # HH:MM — if already past today, tomorrow
    past_hm = (datetime.now() - timedelta(hours=1)).strftime('%H:%M')
    t3 = _parse_fire_at(past_hm)
    check('过去的 HH:MM 滚到明天', t3 > now + 60, datetime.fromtimestamp(t3))

    future_hm = (datetime.now() + timedelta(hours=2)).strftime('%H:%M')
    t4 = _parse_fire_at(future_hm)
    check('未来的 HH:MM 用今天', abs(t4 - now) < 3 * 3600 + 5)

    # fire_time 优先于 delay
    ft = (datetime.now() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
    t5 = _parse_fire_at(ft, delay_minutes=1)
    check('fire_time 优先于 delay_minutes', abs(t5 - _parse_fire_at(ft)) < 1)

    try:
        _parse_fire_at('不是时间')
        check('非法时间抛错', False)
    except ValueError:
        check('非法时间抛错', True)

    try:
        _parse_fire_at('')
        check('缺参数抛错', False)
    except ValueError:
        check('缺参数抛错', True)


def test_create_list_cancel(td):
    section('R2 创建 / 列表 / 取消')
    check('空内容拒绝', '不能为空' in create(td, '  '))

    past = (datetime.now() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M')
    past_msg = create(td, '迟到的', fire_time=past)
    check('过去绝对时间拒绝', '将来' in past_msg or '失败' in past_msg, past_msg)

    check('非法格式失败', '失败' in create(td, '坏时间', fire_time='明天八点'))

    r = create(td, '喝水', delay_minutes=30)
    rid = extract_id(r)
    check('相对分钟创建成功', '已设置' in r and rid, r)
    listed = list_r(td)
    check('列表可见', rid in listed and '喝水' in listed, listed)

    r2 = create(td, '开会', fire_time=(datetime.now() + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M'))
    rid2 = extract_id(r2)
    check('绝对时间创建成功', '已设置' in r2 and rid2, r2)
    listed2 = list_r(td)
    check('列表有两条', rid in listed2 and rid2 in listed2, listed2)

    c = cancel(td, rid)
    check('取消成功', '已取消' in c, c)
    listed3 = list_r(td)
    # list 过滤 done；cancelled 也 done=True
    check('取消后列表不含该条', rid not in listed3, listed3)
    check('另一条仍在', rid2 in listed3, listed3)

    check('取消不存在', '没找到' in cancel(td, 'deadbeef'))


def test_user_isolation(td):
    section('R3 每用户隔离（不能动别人的提醒）')
    r_a = create(td, 'Alice的提醒SECRET-A', phone='+A', uid='ua', delay_minutes=10)
    r_b = create(td, 'Bob的提醒SECRET-B', phone='+B', uid='ub', delay_minutes=10)
    id_a, id_b = extract_id(r_a), extract_id(r_b)

    la = list_r(td, phone='+A', uid='ua')
    lb = list_r(td, phone='+B', uid='ub')
    check('A 列表只有自己', 'SECRET-A' in la and 'SECRET-B' not in la, la)
    check('B 列表只有自己', 'SECRET-B' in lb and 'SECRET-A' not in lb, lb)

    # A 试图取消 B 的 id
    cross = cancel(td, id_b, phone='+A', uid='ua')
    check('A 不能取消 B 的提醒', '没找到' in cross, cross)
    lb2 = list_r(td, phone='+B', uid='ub')
    check('B 的提醒仍在', id_b in lb2 and 'SECRET-B' in lb2, lb2)

    items = load_items(td)
    check('存储里 phone 字段正确',
          any(i['id'] == id_a and i['phone'] == '+A' for i in items)
          and any(i['id'] == id_b and i['phone'] == '+B' for i in items))


def test_pending_cap(td):
    section('R4 每用户待触发上限')
    phone, uid = '+cap', 'ucap'
    # 直接写满上限，避免慢
    items = []
    for i in range(MAX_PENDING_PER_USER):
        items.append({
            'id': f'cap{i:04d}', 'phone': phone, 'user_id': uid,
            'content': f'x{i}', 'fire_at': time.time() + 3600 + i,
            'created_at': time.time(), 'done': False,
        })
    _save(td, items)
    extra = create(td, '超额一条', phone=phone, uid=uid, delay_minutes=5)
    check('达上限拒绝新建', '上限' in extra or '失败' in extra, extra)
    check('数量仍为上限',
          len([i for i in load_items(td) if i['phone'] == phone and not i.get('done')]) == MAX_PENDING_PER_USER)


def test_scheduler_fire_and_deliver(td):
    section('R5 到点触发 → run_event → 送达本人')
    mgr, deliveries, logs = make_mgr(td, reply='该开会了哦')

    # 写入一条已到期
    rid = 'due00001'
    _save(td, [{
        'id': rid, 'phone': '+FIRE1', 'user_id': 'ufire',
        'content': '开会', 'fire_at': time.time() - 5,
        'created_at': time.time() - 100, 'done': False,
    }])
    # 确保 index / session 存在
    mgr.index['+FIRE1'] = 'ufire'
    mgr._save_index()

    scheduler_tick(mgr)
    ok = wait_until(lambda: any(p == '+FIRE1' for p, _ in deliveries), timeout=3)
    check('到点后有投递', ok, deliveries)
    if ok:
        check('投递号码正确且仅本人',
              all(p == '+FIRE1' for p, _ in deliveries) and any('开会' in t or '该开会' in t for _, t in deliveries),
              deliveries)

    ok_done = wait_until(
        lambda: any(i['id'] == rid and i.get('done') for i in load_items(td)), timeout=3)
    check('成功后标记 done', ok_done, load_items(td))
    check('firing 已清除',
          any(i['id'] == rid and not i.get('firing') for i in load_items(td)))

    # 再 tick 不应重复发
    n = len(deliveries)
    scheduler_tick(mgr)
    time.sleep(0.3)
    check('done 后不重复触发', len(deliveries) == n, f'{n} -> {len(deliveries)}')


def test_scheduler_not_due_and_cancelled(td):
    section('R6 未到期 / 已取消 不触发')
    mgr, deliveries, _ = make_mgr(td)
    _save(td, [
        {'id': 'fut001', 'phone': '+ND1', 'user_id': 'und', 'content': '未来',
         'fire_at': time.time() + 9999, 'created_at': time.time(), 'done': False},
        {'id': 'can001', 'phone': '+ND2', 'user_id': 'und2', 'content': '已取消',
         'fire_at': time.time() - 10, 'created_at': time.time(), 'done': True, 'cancelled': True},
    ])
    mgr.index['+ND1'] = 'und'
    mgr.index['+ND2'] = 'und2'
    scheduler_tick(mgr)
    time.sleep(0.4)
    check('未到期不触发', not any(p == '+ND1' for p, _ in deliveries), deliveries)
    check('已取消不触发', not any(p == '+ND2' for p, _ in deliveries), deliveries)


def test_missed_grace(td):
    section('R7 过期过久标记 missed，不补发')
    mgr, deliveries, _ = make_mgr(td)
    rid = 'miss001'
    _save(td, [{
        'id': rid, 'phone': '+MISS', 'user_id': 'umiss',
        'content': '停机期间的提醒',
        'fire_at': time.time() - (MISSED_GRACE_SECONDS + 60),
        'created_at': time.time() - 99999, 'done': False,
    }])
    mgr.index['+MISS'] = 'umiss'
    scheduler_tick(mgr)
    time.sleep(0.2)
    items = load_items(td)
    hit = next(i for i in items if i['id'] == rid)
    check('标记 missed+done', hit.get('missed') and hit.get('done'), hit)
    check('不投递 missed', not any(p == '+MISS' for p, _ in deliveries), deliveries)


def test_max_fire_per_tick(td):
    section('R8 单轮触发上限')
    mgr, deliveries, _ = make_mgr(td, reply='批量提醒')
    items = []
    for i in range(MAX_FIRE_PER_TICK + 4):
        phone = f'+MF{i}'
        uid = f'umf{i}'
        items.append({
            'id': f'mf{i:03d}', 'phone': phone, 'user_id': uid,
            'content': f'批{i}', 'fire_at': time.time() - 2,
            'created_at': time.time(), 'done': False,
        })
        mgr.index[phone] = uid
    _save(td, items)
    mgr._save_index()

    scheduler_tick(mgr)
    wait_until(lambda: len(deliveries) >= MAX_FIRE_PER_TICK, timeout=3)
    # 稍等回调
    time.sleep(0.5)
    fired_ids = {i['id'] for i in load_items(td) if i.get('done') and not i.get('missed')}
    still_pending = [i for i in load_items(td) if not i.get('done') and not i.get('cancelled')]
    check('本轮最多触发 MAX_FIRE_PER_TICK',
          len(fired_ids) <= MAX_FIRE_PER_TICK + 1,  # 允许异步边界
          f'fired={len(fired_ids)} pending={len(still_pending)} deliveries={len(deliveries)}')
    check('仍有剩余留到下轮', len(still_pending) >= 3, still_pending)

    # 第二轮应继续消化
    scheduler_tick(mgr)
    wait_until(lambda: len([i for i in load_items(td) if i.get('done')]) >= MAX_FIRE_PER_TICK + 1, timeout=3)
    time.sleep(0.4)
    done2 = len([i for i in load_items(td) if i.get('done') and not i.get('missed')])
    check('下轮继续触发剩余', done2 > MAX_FIRE_PER_TICK, f'done={done2}')


def test_firing_retry_on_failure(td):
    section('R9 交付失败清 firing，可重试')
    deliveries = []
    cfg = {**DEFAULT_CONFIG, 'is_running': True, 'max_iters': 2}
    mgr = AgentManager(
        td, cfg,
        registry_factory=lambda: ToolRegistry(),
        deliver=lambda phone, text: deliveries.append((phone, text)),
        log=lambda m, l='info': None,
        max_workers=2,
    )
    # 第一次 provider 失败
    calls = {'n': 0}

    class Flaky:
        def chat(self, messages, tools=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('boom')
            am = Message(role='assistant', content='重试成功的提醒')
            return LLMResponse(assistant_message=am, tool_calls=[], text=am.content)

        def complete(self, prompt, max_tokens=1024) -> str:
            return self.chat([Message(role='user', content=prompt)], None).text

    mgr.get_provider = lambda: Flaky()
    rid = 'retry01'
    _save(td, [{
        'id': rid, 'phone': '+RETRY', 'user_id': 'uretry',
        'content': '重试我', 'fire_at': time.time() - 1,
        'created_at': time.time(), 'done': False,
    }])
    mgr.index['+RETRY'] = 'uretry'
    mgr._save_index()

    scheduler_tick(mgr)
    wait_until(lambda: calls['n'] >= 1, timeout=3)
    time.sleep(0.4)
    item = next(i for i in load_items(td) if i['id'] == rid)
    check('失败后未 done', not item.get('done'), item)
    check('失败后 firing 已清', not item.get('firing'), item)

    # 再次调度应能成功
    scheduler_tick(mgr)
    ok = wait_until(lambda: any(i['id'] == rid and i.get('done') for i in load_items(td)), timeout=3)
    check('重试后成功 done', ok, load_items(td))
    check('重试后有投递', any(p == '+RETRY' for p, _ in deliveries), deliveries)


def test_persist_and_tools_registry(td):
    section('R10 持久化与工具注册')
    r = create(td, '持久化提醒', delay_minutes=60)
    rid = extract_id(r)
    path = _store_path(td)
    check('reminders.json 落盘', os.path.exists(path), path)
    data = json.load(open(path))
    check('磁盘含该提醒', any(i['id'] == rid for i in data), data)

    tools = make_reminder_tools()
    names = {t.name for t in tools}
    check('三件套工具', names == {'create_reminder', 'list_reminders', 'cancel_reminder'}, names)

    # harness 路径：registry dispatch
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    c = ctx(td, '+P', 'up')
    out, err = reg.dispatch(c, 'create_reminder', {'content': '通过registry', 'delay_minutes': 15})
    check('registry 创建无异常', not err and '已设置' in out, out)


def test_only_own_phone_on_create(td):
    section('R11 创建强制绑定 ctx.phone（模型无法指定他人）')
    # 工具签名没有 phone 参数 —— 只能写到 ctx
    create(td, '只能给自己', phone='+SELF', uid='uself', delay_minutes=20)
    items = [i for i in load_items(td) if i.get('content') == '只能给自己']
    check('写入 phone=ctx.phone', len(items) == 1 and items[0]['phone'] == '+SELF', items)
    check('user_id=ctx.user_id', items[0]['user_id'] == 'uself', items[0])


def test_concurrent_creates(td):
    section('R12 并发创建不丢、不串用户')
    errors = []
    ids = []
    lock = threading.Lock()

    def worker(n):
        try:
            msg = create(td, f'并发{n}', phone=f'+C{n % 3}', uid=f'uc{n % 3}', delay_minutes=40 + n)
            with lock:
                if '已设置' in msg:
                    ids.append(extract_id(msg))
                else:
                    errors.append(msg)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    check('并发无异常失败', len(errors) == 0, errors)
    check('并发创建条数合理', len(ids) == 12, f'ids={len(ids)}')
    items = load_items(td)
    # 每个 id 唯一
    check('id 唯一', len({i['id'] for i in items}) == len(items))


def main():
    td = tempfile.mkdtemp(prefix='imsg_reminder_')
    print(f'定时任务功能测试目录: {td}')
    try:
        test_parse_time()
        # 各 suite 用独立子目录，避免 reminders.json 互相干扰
        suites = [
            test_create_list_cancel,
            test_user_isolation,
            test_pending_cap,
            test_scheduler_fire_and_deliver,
            test_scheduler_not_due_and_cancelled,
            test_missed_grace,
            test_max_fire_per_tick,
            test_firing_retry_on_failure,
            test_persist_and_tools_registry,
            test_only_own_phone_on_create,
            test_concurrent_creates,
        ]
        for fn in suites:
            sub = tempfile.mkdtemp(prefix=fn.__name__ + '_', dir=td)
            try:
                fn(sub)
            except Exception:
                global FAIL
                FAIL += 1
                print(f'  EXCEPTION in {fn.__name__}')
                traceback.print_exc()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f'\n==== 定时任务功能测试: {PASS} passed, {FAIL} failed ====')
    if ERRORS:
        print('失败项:')
        for e in ERRORS:
            print(e)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
