"""空闲时异步压缩对话历史：摘要 + 归档进每用户 RAG。

热路径只调用 mark_if_needed（不调 LLM）。真正压缩由 scheduler_tick 在用户空闲时执行。
LLM 调用在 sess.lock 外进行，用 version CAS 避免覆盖用户新消息。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from providers.base import Message
from agent.rag import KIND_ARCHIVE, get_rag_store

if TYPE_CHECKING:
    from agent.manager import AgentManager
    from agent.session import AgentSession
    from providers.base import LLMProvider

_SUMMARIZE_PROMPT = (
    "你是对话压缩助手。请把下面这段 iMessage 对话压缩成 200～400 字（或等量英文词）的摘要，"
    "用对话本身使用的主要语言写摘要。"
    "保留：用户偏好、未完成事项、重要事实、约定与结论。省略寒暄与重复。"
    "只输出摘要正文，不要标题、不要 Markdown。\n\n"
    "已有滚动摘要（可为空，请与之合并，不要丢失旧要点）：\n{prev}\n\n"
    "待压缩的新对话：\n{dialog}"
)


def history_char_len(history: list[Message]) -> int:
    return sum(len(m.content or '') for m in history)


def exceeds_soft_limit(sess: 'AgentSession', cfg: dict) -> bool:
    soft = int(cfg.get('history_soft_limit', 20) or 20)
    budget = int(cfg.get('history_char_budget', 12000) or 12000)
    return len(sess.history) > soft or history_char_len(sess.history) > budget


def mark_if_needed(sess: 'AgentSession', cfg: dict) -> bool:
    """热路径：超软阈值则标记 needs_compress。调用方需已持有 sess.lock。"""
    if not cfg.get('enable_auto_compress', True):
        return False
    if exceeds_soft_limit(sess, cfg):
        if not sess.needs_compress:
            sess.needs_compress = True
            return True
    return False


def _cut_prefix(history: list[Message], keep_recent: int) -> tuple[list[Message], list[Message]]:
    """切出可压缩前缀，保留尾部 keep_recent，切点对齐到 user 边界。"""
    if len(history) <= keep_recent:
        return [], list(history)
    cut = len(history) - keep_recent
    start = cut
    while start > 0 and history[start].role != 'user':
        start -= 1
    # 若整段都压不成（切点为 0），不强压，等更长
    if start <= 0:
        return [], list(history)
    return history[:start], history[start:]


def _format_dialog(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        role = m.role
        if role == 'tool':
            # 工具结果缩略，避免摘要 prompt 爆炸
            body = (m.content or '')[:300]
            lines.append(f"[tool:{m.name or ''}] {body}")
        elif role == 'assistant':
            body = (m.content or '').strip()
            if not body and m.tool_calls:
                names = ','.join(tc.name for tc in m.tool_calls)
                body = f"(调用工具: {names})"
            lines.append(f"助手: {body}")
        elif role == 'user':
            lines.append(f"用户: {(m.content or '').strip()}")
        else:
            lines.append(f"{role}: {(m.content or '').strip()}")
    return '\n'.join(lines)


def _chunk_turns(messages: list[Message]) -> list[str]:
    """按 user 边界打成文本块，供 archive RAG。只保留 user 文本和 assistant 文本回复。"""
    chunks: list[str] = []
    cur: list[str] = []
    for m in messages:
        if m.role == 'user' and cur:
            text = '\n'.join(cur).strip()
            if text:
                chunks.append(text)
            cur = []
        if m.role == 'tool':
            continue
        if m.role == 'assistant':
            body = (m.content or '').strip()
            if body:
                cur.append(f"助手: {body}")
        elif m.role == 'user':
            cur.append(f"用户: {(m.content or '').strip()}")
    if cur:
        text = '\n'.join(cur).strip()
        if text:
            chunks.append(text)
    return chunks


def _snapshot_for_compress(sess: 'AgentSession', cfg: dict) -> Optional[dict]:
    """锁内快照：切出 prefix/recent，记下 version。prefix 为空则清标记并返回 None。

    调用方约定已持有 sess.lock，或无需持有锁（同步 compress_session）。
    """
    keep = int(cfg.get('history_keep_recent', 8) or 8)
    prefix, recent = _cut_prefix(sess.history, keep)
    if not prefix:
        sess.needs_compress = False
        sess._save()
        return None
    return {
        'prefix': list(prefix),
        'recent': list(recent),
        'version': sess.version,
        'prev_summary': (sess.rolling_summary or '').strip(),
    }


def _summarize(provider: 'LLMProvider', prev_summary: str, prefix: list[Message]) -> str:
    """锁外生成摘要。优先 provider.complete；鸭子类型假 provider 回退 chat。失败抛异常。"""
    dialog = _format_dialog(prefix)
    max_dialog_chars = 24000
    if len(dialog) > max_dialog_chars:
        dialog = dialog[-max_dialog_chars:]
    prompt = _SUMMARIZE_PROMPT.format(prev=prev_summary or '（无）', dialog=dialog)
    complete_fn = getattr(provider, 'complete', None)
    if complete_fn is not None:
        summary = (complete_fn(prompt, max_tokens=1024) or '').strip()
    else:
        resp = provider.chat([Message(role='user', content=prompt)], None)
        summary = (resp.text or '').strip()
    if not summary:
        raise RuntimeError('empty summary')
    return summary


def _apply_compress(sess: 'AgentSession', cfg: dict, snap: dict, summary: str,
                    log: Optional[Callable[[str], None]] = None) -> bool:
    """锁内 CAS 应用：version 变了则丢弃，不归档、不改 history。"""
    if sess.version != snap['version']:
        msg = f"压缩丢弃 {sess.user_id}: 会话已变化 (ver {snap['version']}->{sess.version})"
        if log:
            log(msg)
        else:
            print(msg)
        return False

    sess.rolling_summary = summary
    if len(sess.rolling_summary) > 2000:
        sess.rolling_summary = sess.rolling_summary[-2000:]

    chunks = _chunk_turns(snap['prefix'])
    if chunks:
        store = get_rag_store(sess.state_dir)
        store.add_chunks(sess.user_id, KIND_ARCHIVE, chunks, meta={'source': 'compress'})

    sess.history = snap['recent']
    sess.needs_compress = exceeds_soft_limit(sess, cfg)
    sess.version += 1
    sess._save()
    if log:
        log(f"已压缩 {sess.user_id}: 归档 {len(chunks)} 块，history→{len(sess.history)}")
    return True


def compress_session(sess: 'AgentSession', provider: 'LLMProvider', cfg: dict,
                     log: Optional[Callable[[str], None]] = None) -> bool:
    """同步压缩（调用方已持有或无需持有锁）。成功 True；无需压缩或失败 False。"""
    snap = _snapshot_for_compress(sess, cfg)
    if not snap:
        return False
    try:
        summary = _summarize(provider, snap['prev_summary'], snap['prefix'])
    except Exception as e:
        if log:
            log(f"压缩失败 {sess.user_id}: {e}")
        return False
    return _apply_compress(sess, cfg, snap, summary, log=log)


def _parse_last_active(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def _peek_session_flags(state_dir: str, user_id: str) -> tuple[bool, Optional[str]]:
    """只读会话文件的 needs_compress / last_active，不构造 AgentSession。"""
    path = os.path.join(state_dir, f'{user_id}.json')
    if not os.path.exists(path):
        return False, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return bool(data.get('needs_compress')), data.get('last_active')
    except Exception:
        return False, None


def _idle_enough(last_active: Optional[str], now: datetime, idle_sec: float) -> bool:
    la = _parse_last_active(last_active)
    if la is None:
        return False
    return (now - la).total_seconds() >= idle_sec


def scheduler_tick(manager: 'AgentManager') -> None:
    """空闲压缩调度：每 tick 最多压 1 个用户。LLM 在锁外调用。"""
    cfg = manager.config
    if not cfg.get('enable_auto_compress', True):
        return
    if not cfg.get('is_running'):
        return

    idle_sec = float(cfg.get('compress_idle_seconds', 90) or 90)
    now = datetime.now()
    n = getattr(manager, '_compress_ticks', 0) + 1
    manager._compress_ticks = n
    scan_every = max(1, int(cfg.get('compress_scan_every_ticks', 20) or 20))
    do_scan = ((n - 1) % scan_every == 0)  # 首个 tick 扫一次，之后每 scan_every 个 tick

    candidates: list[str] = []
    with manager._sessions_lock:
        cached = dict(manager._sessions)
        index_snapshot = list(manager.index.items())

    with manager._queue_lock:
        draining = set(manager._draining)

    for phone, sess in cached.items():
        if phone in draining:
            continue
        if not sess.needs_compress and not exceeds_soft_limit(sess, cfg):
            continue
        if not _idle_enough(sess.last_active, now, idle_sec):
            continue
        candidates.append(phone)

    if do_scan:
        for phone, uid in index_snapshot:
            if phone in cached or phone in draining:
                continue
            needs, last_active = _peek_session_flags(manager.state_dir, uid)
            if not needs:
                continue
            if not _idle_enough(last_active, now, idle_sec):
                continue
            candidates.append(phone)

    if not candidates:
        return

    phone = candidates[0]
    if phone in draining:
        return

    sess = manager.get_or_create(phone)
    got = sess.lock.acquire(blocking=False)
    if not got:
        return

    snap = None
    try:
        if not exceeds_soft_limit(sess, cfg) and not sess.needs_compress:
            sess.needs_compress = False
            sess._save()
            return
        la = _parse_last_active(sess.last_active)
        if la and (datetime.now() - la).total_seconds() < idle_sec:
            return
        snap = _snapshot_for_compress(sess, cfg)
    finally:
        sess.lock.release()

    if not snap:
        return

    try:
        provider = manager.get_provider()
    except Exception as e:
        manager.log(f"压缩跳过 {phone}：provider 未就绪 ({e})", 'warning')
        return

    try:
        summary = _summarize(provider, snap['prev_summary'], snap['prefix'])
    except Exception as e:
        manager.log(f"压缩失败 {sess.user_id}: {e}", 'warning')
        return

    got2 = sess.lock.acquire(timeout=5)
    if not got2:
        manager.log(f"压缩跳过 {phone}：应用时拿不到锁", 'warning')
        return
    try:
        _apply_compress(sess, cfg, snap, summary,
                        log=lambda m: manager.log(m, 'info'))
    finally:
        sess.lock.release()
