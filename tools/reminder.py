"""定时提醒 / 任务工具（每用户独立）。

- 模型用 create_reminder 建提醒（绝对时间 fire_time 或相对 delay_minutes）。
- 持久化到 agent_state/reminders.json。
- app.py 的调度线程每 ~20s 调 scheduler_tick(manager)，到点的提醒交给该用户的 agent
  跑一轮生成话术并主动发 iMessage。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from tools.base import Tool, ToolContext

_STORE_LOCK = threading.Lock()
REMINDERS_FILE = 'reminders.json'


def _store_path(state_dir: str) -> str:
    return os.path.join(state_dir, REMINDERS_FILE)


def _load(state_dir: str) -> list:
    path = _store_path(state_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save(state_dir: str, items: list):
    os.makedirs(state_dir, exist_ok=True)
    tmp = _store_path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _store_path(state_dir))


def _parse_fire_at(fire_time: str = '', delay_minutes=None) -> float:
    """返回目标时间的 epoch 秒；无法解析时抛 ValueError。

    绝对时间 fire_time 优先于 delay_minutes —— 用户明确给了具体时间就不该被相对分钟数覆盖。
    """
    now = time.time()
    ft = (fire_time or '').strip()
    if ft:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%H:%M'):
            try:
                dt = datetime.strptime(ft, fmt)
                if fmt == '%H:%M':  # 只给了时分 → 今天，过了就明天
                    today = datetime.now()
                    dt = today.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
                    if dt.timestamp() <= now:
                        dt = dt + timedelta(days=1)  # 用日历加一天，跨夏令时不会差一小时
                return dt.timestamp()
            except ValueError:
                continue
        raise ValueError(f"无法解析时间：{ft}（请用 'YYYY-MM-DD HH:MM'）")
    if delay_minutes is not None:
        try:
            return now + float(delay_minutes) * 60.0
        except Exception:
            raise ValueError("delay_minutes 不是数字")
    raise ValueError("需要 fire_time 或 delay_minutes 之一")


class CreateReminderTool(Tool):
    name = 'create_reminder'
    description = (
        "为用户设置一个定时提醒/任务，到点会主动给用户发 iMessage。"
        "用 fire_time 给绝对时间（格式 'YYYY-MM-DD HH:MM'，按 system 里的当前时间换算），"
        "或用 delay_minutes 给相对分钟数。content 写要提醒的事。"
    )
    parameters = {
        'type': 'object',
        'properties': {
            'content': {'type': 'string', 'description': '要提醒的内容'},
            'fire_time': {'type': 'string', 'description': "绝对时间 'YYYY-MM-DD HH:MM'"},
            'delay_minutes': {'type': 'number', 'description': '相对现在多少分钟后'},
        },
        'required': ['content'],
    }

    def run(self, ctx: ToolContext, content: str = '', fire_time: str = '', delay_minutes=None) -> str:
        content = (content or '').strip()
        if not content:
            return '提醒内容不能为空。'
        try:
            fire_at = _parse_fire_at(fire_time, delay_minutes)
        except ValueError as e:
            return f'设置失败：{e}'
        if fire_at <= time.time():
            return '设置失败：提醒时间必须在将来。'
        item = {
            'id': uuid.uuid4().hex[:8],
            'phone': ctx.phone,
            'user_id': ctx.user_id,
            'content': content,
            'fire_at': fire_at,
            'created_at': time.time(),
            'done': False,
        }
        with _STORE_LOCK:
            items = _load(ctx.state_dir)
            items.append(item)
            _save(ctx.state_dir, items)
        when_str = datetime.fromtimestamp(fire_at).strftime('%Y-%m-%d %H:%M')
        return f'已设置提醒（{when_str}）：{content}（编号 {item["id"]}）'


class ListRemindersTool(Tool):
    name = 'list_reminders'
    description = "列出这位用户尚未触发的提醒。"
    parameters = {'type': 'object', 'properties': {}}

    def run(self, ctx: ToolContext) -> str:
        with _STORE_LOCK:
            items = _load(ctx.state_dir)
        mine = [i for i in items if i.get('phone') == ctx.phone and not i.get('done')]
        if not mine:
            return '当前没有待触发的提醒。'
        mine.sort(key=lambda i: i.get('fire_at', 0))
        lines = [
            f"[{i['id']}] {datetime.fromtimestamp(i['fire_at']).strftime('%m-%d %H:%M')} — {i['content']}"
            for i in mine
        ]
        return '\n'.join(lines)


class CancelReminderTool(Tool):
    name = 'cancel_reminder'
    description = "按编号取消一个尚未触发的提醒。"
    parameters = {
        'type': 'object',
        'properties': {'id': {'type': 'string', 'description': '提醒编号'}},
        'required': ['id'],
    }

    def run(self, ctx: ToolContext, id: str = '') -> str:
        rid = (id or '').strip()
        with _STORE_LOCK:
            items = _load(ctx.state_dir)
            found = False
            for i in items:
                if i.get('id') == rid and i.get('phone') == ctx.phone and not i.get('done'):
                    i['done'] = True
                    i['cancelled'] = True
                    found = True
                    break
            if found:
                _save(ctx.state_dir, items)
        return f'已取消提醒 {rid}。' if found else f'没找到编号 {rid} 的待触发提醒。'


def make_reminder_tools() -> list:
    return [CreateReminderTool(), ListRemindersTool(), CancelReminderTool()]


# 过期超过这个秒数的提醒不再补发（避免进程停机后重启时一次性轰炸用户），直接标记 missed
MISSED_GRACE_SECONDS = 30 * 60


def scheduler_tick(manager) -> None:
    """由 app 的调度线程周期调用：触发到点提醒。"""
    state_dir = getattr(manager, 'state_dir', 'agent_state')
    now = time.time()
    due = []
    with _STORE_LOCK:
        items = _load(state_dir)
        changed = False
        for i in items:
            if i.get('done') or i.get('cancelled') or i.get('firing'):
                continue
            fire_at = i.get('fire_at', 0)
            if fire_at > now:
                continue
            if now - fire_at > MISSED_GRACE_SECONDS:
                # 过期太久（多半是停机期间到点的）→ 直接标记错过，不补发
                i['done'] = True
                i['missed'] = True
                changed = True
                continue
            i['firing'] = True  # 中间态：已被本轮取走，交付成功后才置 done（见 mark_fired）
            due.append(i)
            changed = True
        if changed:
            _save(state_dir, items)
    for i in due:
        try:
            manager.run_event(
                i['phone'], f"到点提醒：{i['content']}",
                on_success=lambda rid=i['id']: mark_fired(state_dir, rid, True),
                on_failure=lambda rid=i['id']: mark_fired(state_dir, rid, False),
            )
        except Exception as e:
            print(f"触发提醒失败({i.get('phone')}): {e}")
            mark_fired(state_dir, i['id'], False)


def mark_fired(state_dir: str, reminder_id: str, success: bool) -> None:
    """交付回调：成功 → done；失败 → 清 firing 让下轮重试。"""
    with _STORE_LOCK:
        items = _load(state_dir)
        for i in items:
            if i.get('id') == reminder_id:
                if success:
                    i['done'] = True
                    i['fired_at'] = time.time()
                i['firing'] = False
                break
        _save(state_dir, items)
