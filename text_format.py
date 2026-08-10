"""iMessage 回复文本清洗。

iMessage 不渲染 Markdown，`**加粗**`、`# 标题`、``代码`` 这些原样显示很丑。
所有出站文本在 send_imessage 里统一过 clean_reply()，把 Markdown 降级成纯文本。

设计要点：
- 幂等：clean_reply(clean_reply(x)) == clean_reply(x)（重试队列会重复清洗）
- 保守：宁可漏删也不能误伤正常文本 —— user_id、3 * 4、磁力链接、URL 必须原样保留
"""
from __future__ import annotations

import re

# ---- 思维链 ----
_THINK_PAIR = re.compile(r'<\s*think\s*>.*?<\s*/\s*think\s*>', re.DOTALL | re.IGNORECASE)
_THINK_ENTITY = re.compile(r'&lt;\s*think\s*&gt;.*?&lt;\s*/\s*think\s*&gt;', re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r'<\s*think\s*>.*\Z', re.DOTALL | re.IGNORECASE)  # 未闭合（流被截断）

# ---- Markdown ----
# 整行删除的规则要连行尾换行一起吃掉，否则会留下空行
_FENCE = re.compile(r'^[ \t]*(?:```|~~~)[^\n]*\n?', re.MULTILINE)
_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)')
_LINK = re.compile(r'\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)')
_HEADING = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+', re.MULTILINE)
_QUOTE = re.compile(r'^[ \t]{0,3}>[ \t]?', re.MULTILINE)
# $ 锚点不能省：否则 ***加粗斜体*** 的开头三个星号会被当成分隔线吃掉
_HR = re.compile(r'^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$\n?', re.MULTILINE)
_BULLET = re.compile(r'^([ \t]*)[-*+][ \t]+', re.MULTILINE)
_TABLE_SEP = re.compile(r'^[ \t]{0,3}\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$\n?', re.MULTILINE)
_TABLE_ROW = re.compile(r'^[ \t]{0,3}\|(.+)\|[ \t]*$', re.MULTILINE)

# 强调：定界符必须紧贴非空白内容。
# 下划线版额外用 ASCII 词字符做边界（注意不能用 \w —— 中文也是 \w，会挡住「__加粗__文字」），
# 这样 user_id / a_b_c / ubuntu_24.04.iso 里的下划线不会被当成强调。
_BOLD_ITALIC_STAR = re.compile(r'\*\*\*(\S(?:.*?\S)?)\*\*\*', re.DOTALL)
_BOLD_STAR = re.compile(r'\*\*(\S(?:.*?\S)?)\*\*', re.DOTALL)
_ITALIC_STAR = re.compile(r'(?<!\*)\*(\S(?:[^*\n]*?\S)?)\*(?!\*)')
_BOLD_ITALIC_US = re.compile(r'(?<![A-Za-z0-9_])___(\S(?:.*?\S)?)___(?![A-Za-z0-9_])', re.DOTALL)
_BOLD_US = re.compile(r'(?<![A-Za-z0-9_])__(\S(?:.*?\S)?)__(?![A-Za-z0-9_])', re.DOTALL)
# 内容首尾不能是下划线，否则 __init__ 会被当成 _斜体_ 而被啃掉一层下划线
_ITALIC_US = re.compile(r'(?<![A-Za-z0-9_])_([^_\s](?:[^_\n]*?[^_\s])?)_(?![A-Za-z0-9_])')
_STRIKE = re.compile(r'~~(\S(?:.*?\S)?)~~', re.DOTALL)

# 形如 __init__ / __main__ 的标识符跟 Markdown 加粗同形：内容纯属标识符字符时按标识符保留
_IDENTIFIER_LIKE = re.compile(r'^[A-Za-z0-9_]+$')


def _us_emphasis_repl(m: re.Match) -> str:
    content = m.group(1)
    if _IDENTIFIER_LIKE.match(content):
        return m.group(0)  # __init__ / __a_b__ 这类，原样保留
    return content

_MULTI_BLANK = re.compile(r'\n{3,}')
_TRAILING_WS = re.compile(r'[ \t]+$', re.MULTILINE)


def strip_think(text: str) -> str:
    """剥离推理模型的 <think> 块，含未闭合的情况。"""
    if not text:
        return text
    text = _THINK_PAIR.sub('', text)
    text = _THINK_ENTITY.sub('', text)
    text = _THINK_OPEN.sub('', text)
    return text


def _link_repl(m: re.Match) -> str:
    label, url = (m.group(1) or '').strip(), m.group(2).strip()
    if not label or label == url:
        return url
    return f"{label}: {url}"


def _image_repl(m: re.Match) -> str:
    alt, url = (m.group(1) or '').strip(), m.group(2).strip()
    return f"{alt}: {url}" if alt else url


def _table_row_repl(m: re.Match) -> str:
    cells = [c.strip() for c in m.group(1).split('|')]
    return ' | '.join(c for c in cells if c)


def strip_markdown(text: str) -> str:
    """把 Markdown 降级成 iMessage 里能直接读的纯文本。"""
    if not text:
        return text
    original = text

    text = _FENCE.sub('', text)          # 代码围栏：去掉栅栏行，保留代码内容
    text = _INLINE_CODE.sub(r'\1', text)
    text = _IMAGE.sub(_image_repl, text)  # 图片要在链接之前处理
    text = _LINK.sub(_link_repl, text)
    text = _HEADING.sub('', text)
    text = _QUOTE.sub('', text)
    text = _HR.sub('', text)
    text = _TABLE_SEP.sub('', text)
    text = _TABLE_ROW.sub(_table_row_repl, text)
    text = _BULLET.sub(r'\1• ', text)     # 列表项换成 • （幂等：• 不再被匹配）

    text = _BOLD_ITALIC_STAR.sub(r'\1', text)
    text = _BOLD_STAR.sub(r'\1', text)
    text = _ITALIC_STAR.sub(r'\1', text)
    text = _BOLD_ITALIC_US.sub(_us_emphasis_repl, text)
    text = _BOLD_US.sub(_us_emphasis_repl, text)
    text = _ITALIC_US.sub(_us_emphasis_repl, text)
    text = _STRIKE.sub(r'\1', text)

    text = _TRAILING_WS.sub('', text)
    text = _MULTI_BLANK.sub('\n\n', text)
    text = text.strip()

    # 兜底：整段被清空说明规则误伤（如全文只有分隔线），退回原文
    return text if text else original.strip()


def clean_reply(text: str) -> str:
    """出站文本的统一清洗入口：思维链 → Markdown → 空行整理。"""
    if not text:
        return text
    return strip_markdown(strip_think(text))
