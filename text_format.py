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


# ---- 身份泄漏拦截 ----
# 模型预训练人设会自称 MiniMax / Claude / GPT 等；这是本仓库作者的 iMessage 助手，
# 厂商只是后端。只拦「我是/我由某某厂商」这类自称，新闻里提到公司名不误伤。
_VENDOR = (
    r'(?:mini\s*max|minimax|海螺(?:\s*ai)?|hailuo(?:\s*ai)?|'
    r'anthropic|claude|'
    r'openai|chatgpt|gpt-?\d|'
    r'deepseek|deep\s*seek|'
    r'gemini|'
    r'(?<![a-z])xai(?![a-z])|x\.ai|\bgrok\b|'
    r'qwen|通义(?:千问)?|'
    r'豆包|doubao|'
    r'\bkimi\b|moonshot|'
    r'智谱|chatglm|\bglm-?\d|'
    r'mistral|'
    r'\bllama\b|'
    r'字节跳动)'
)
_LEAK_RES = [
    re.compile(r'我[们]?由\s*' + _VENDOR, re.I),
    re.compile(
        r'我[们]?(?:是|叫|名为)\s*(?:一[个名只]|an?\s+|the\s+)?'
        r'(?:AI\s*|ai\s*|人工智能|大模型|语言模型|助手|assistant\s*){0,3}' + _VENDOR,
        re.I,
    ),
    re.compile(r'我[们]?来自\s*' + _VENDOR, re.I),
    re.compile(r'我[们]?所属(?:于)?\s*' + _VENDOR, re.I),
    re.compile(r'本(?:模型|助手|产品)由\s*' + _VENDOR, re.I),
    re.compile(r'我的(?:版本|型号|名字|名称)\s*(?:是|:|：)?\s*' + _VENDOR, re.I),
    re.compile(r'我[们]?(?:用的|跑的|调用的)(?:模型)?(?:是|了)?\s*' + _VENDOR, re.I),
    re.compile(r'我[们]?.{0,12}就是\s*' + _VENDOR, re.I),
    re.compile(r'(?:底层|当前|这次)(?:用的|跑的)?(?:模型)?(?:是|:|：)?\s*' + _VENDOR, re.I),
    re.compile(r'随负载'),
    re.compile(r'智能模型（基于'),
    re.compile(r'deepseekv3.{0,48}gpt-?4o.{0,48}grok', re.I),
    re.compile(r'我的?知识截止', re.I),
    re.compile(
        r"\bi(?:['’]m|\s+am|\s+was)\s+(?:an?\s+|the\s+)?"
        r'(?:ai\s+|large\s+language\s+model\s+|language\s+model\s+|assistant\s+|chatbot\s+)*'
        + _VENDOR,
        re.I,
    ),
    re.compile(
        r"\bi(?:['’]m|\s+am|\s+was)\s+(?:an?\s+|the\s+)?"
        r'(?:ai\s+|assistant\s+|language\s+model\s+)*'
        r'(?:created|developed|trained|built|made)\s+by\s+' + _VENDOR,
        re.I,
    ),
    re.compile(r"\bi(?:['’]m|\s+am)\b.{0,48}based on\s+" + _VENDOR, re.I),
    re.compile(r'my knowledge\s+(?:cut[- ]?off|cutoff|is current (?:up )?through)', re.I),
]
_SENT_SPLIT = re.compile(r'(?<=[。！？!?])\s*|(?<=\n)')

IDENTITY_COVER = '我是汽水西瓜，一个跑在 iMessage 上的助手。底层模型不讨论。'


def sentence_has_identity_leak(sentence: str) -> bool:
    s = (sentence or '').strip()
    if not s:
        return False
    return any(p.search(s) for p in _LEAK_RES)


def has_identity_leak(text: str) -> bool:
    return any(sentence_has_identity_leak(p) for p in _SENT_SPLIT.split(text or '') if p.strip())


def guard_identity(text: str, *, allow_cover: bool = True) -> str:
    """去掉自称厂商/创建者的句子。整段都是泄漏则换成统一口径。"""
    if not text:
        return text
    parts = _SENT_SPLIT.split(text)
    kept: list[str] = []
    dropped = False
    for part in parts:
        if not part.strip():
            if kept:
                kept.append(part)
            continue
        if sentence_has_identity_leak(part):
            dropped = True
            continue
        kept.append(part)
    leftover = ''.join(kept).strip()
    leftover = _MULTI_BLANK.sub('\n\n', leftover).strip()
    if leftover:
        return leftover
    if dropped and allow_cover:
        return IDENTITY_COVER
    return leftover if dropped else text


def clean_reply(text: str) -> str:
    """出站文本的统一清洗入口：思维链 → Markdown → 身份拦截。"""
    if not text:
        return text
    return guard_identity(strip_markdown(strip_think(text)))

