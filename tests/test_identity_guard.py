#!/usr/bin/env python3
"""身份泄漏拦截：自称 MiniMax / Claude / GPT 等不得出站，也不得写入历史。"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers.base import Message, LLMResponse
from tools.base import ToolRegistry
from agent.session import AgentSession, _guard_turn
from text_format import (
    IDENTITY_COVER, clean_reply, guard_identity, has_identity_leak,
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


SCREENSHOT = (
    '我由 MiniMax 开发 🚀 一家 2022 年初成立的全球 AI 基础模型公司，'
    '专门搞大模型研究的。我的版本是 MiniMax-M3，知识截止到 2026 年 1 月 ~'
)


class FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, tools=None):
        am = Message(
            role='assistant', content=self.reply,
            raw={'text': self.reply}, raw_provider='openai',
        )
        return LLMResponse(assistant_message=am, tool_calls=[], text=self.reply)

    def complete(self, prompt, max_tokens=1024):
        return '摘要'


def test_guard_rules():
    section('自称厂商 → 拦截')
    out = guard_identity(SCREENSHOT)
    check('截图口径被替换', out == IDENTITY_COVER, out[:80])
    check('替换后不含 MiniMax', 'minimax' not in out.lower() and 'MiniMax' not in out)
    check('幂等', guard_identity(out) == out)
    check('统一口径自身不触发', not has_identity_leak(IDENTITY_COVER), IDENTITY_COVER)

    check('我是 Claude', guard_identity('我是 Claude。') == IDENTITY_COVER)
    check('我是海螺AI', '海螺' not in guard_identity('你好，我是海螺AI。'))
    check('I am ChatGPT', 'chatgpt' not in guard_identity("Hi, I am ChatGPT.").lower())
    check('created by Anthropic', 'anthropic' not in guard_identity(
        'I was created by Anthropic.').lower())
    old_lie = (
        '我是一个随负载自动切换的智能模型（基于 DeepseekV3、GPT-4o、Grok2、Gemini 2 Flash Exo）。'
    )
    check('旧假名单被拦', guard_identity(old_lie) == IDENTITY_COVER, guard_identity(old_lie)[:80])
    check('统一口径不提具体模型', not any(
        x in IDENTITY_COVER.lower() for x in ('minimax', 'deepseek', 'gpt-4', 'claude', 'gemini', 'grok')
    ), IDENTITY_COVER)

    section('新闻提及厂商 → 放行')
    news = 'MiniMax 今日发布 MiniMax-M3，股价上涨。来源：https://example.com/news'
    check('新闻不拦截', guard_identity(news) == news, guard_identity(news)[:80])
    check('Google 搜索不误伤', guard_identity('我用 Google 搜了一下天气，明天有雨。')
          == '我用 Google 搜了一下天气，明天有雨。')

    section('夹杂有用内容 → 只删泄漏句')
    mixed = '我由 MiniMax 开发。明天记得带伞。'
    mixed_out = guard_identity(mixed)
    check('保留带伞', '带伞' in mixed_out, mixed_out)
    check('去掉 MiniMax', 'minimax' not in mixed_out.lower(), mixed_out)
    check('不是整段替换', mixed_out != IDENTITY_COVER, mixed_out)

    section('出站 clean_reply 也拦')
    check('clean_reply 截图', 'minimax' not in clean_reply(SCREENSHOT).lower())
    check('clean_reply 新闻', 'MiniMax-M3' in clean_reply(news))


def test_history_rewrite():
    section('写入历史时改写 + 丢掉 raw')
    td = tempfile.mkdtemp()
    try:
        sess = AgentSession('+1', 'idg', td, history_limit=40)
        reply = sess.process(
            '你的创建者是谁', [], FakeProvider(SCREENSHOT),
            ToolRegistry(), dict(DEFAULT_CONFIG), {},
        )
        check('process 返回不含 MiniMax', 'minimax' not in reply.lower(), reply[:80])
        check('process 返回统一口径', reply == IDENTITY_COVER, reply[:80])
        asst = [m for m in sess.history if m.role == 'assistant']
        check('历史有 assistant', bool(asst))
        check('历史不含 MiniMax', all('minimax' not in (m.content or '').lower() for m in asst),
              asst[-1].content[:80] if asst else '')
        check('raw 已丢弃', asst[-1].raw is None and asst[-1].raw_provider is None)

        appended = [Message(role='assistant', content=SCREENSHOT,
                            raw={'x': 1}, raw_provider='openai')]
        got = _guard_turn(SCREENSHOT, appended)
        check('_guard_turn 文本', got == IDENTITY_COVER)
        check('_guard_turn 消息', appended[0].content == IDENTITY_COVER)
        check('_guard_turn 清 raw', appended[0].raw is None)
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == '__main__':
    test_guard_rules()
    test_history_rewrite()
    print(f'\n==== 身份拦截测试: {PASS} passed, {FAIL} failed ====')
    if ERRORS:
        print('\n失败项:')
        for e in ERRORS:
            print(e)
    raise SystemExit(1 if FAIL else 0)
