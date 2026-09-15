"""联网搜索 / 网页阅读（客户端工具，provider 无关）。

为什么放在工具层而不是依赖模型自带搜索：只有部分后端有原生联网能力
（如 Anthropic 的 server tool），MiniMax / DeepSeek 等 OpenAI 兼容端点没有。
放进 harness 后，任何后端都能联网，换模型不会让这个能力消失。

后端优先级：配置了 search_api_key 就用对应服务商（serper / tavily / brave），
否则用免 key 的 DuckDuckGo。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Optional
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin

import requests

from tools.base import Tool, ToolContext

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
TIMEOUT = 20
MAX_RESULTS = 8
MAX_PAGE_CHARS = 6000
MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_FETCH_REDIRECTS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


def _ip_is_blocked(addr) -> bool:
    """私网 / 回环 / 链路本地 / 组播 / 保留 / 未指定 / 非全局 一律拒绝。"""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (not addr.is_global)
    )


def _is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF 护栏：只允许 http(s) 且解析后全部地址为公网。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'URL 无效'
    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        return False, '仅允许 http/https'
    host = (parsed.hostname or '').strip().rstrip('.').lower()
    if not host:
        return False, 'hostname 为空'
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        return False, '禁止本机/内网主机名'
    port = parsed.port or (443 if scheme == 'https' else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return False, 'DNS 解析失败'
    if not infos:
        return False, 'DNS 解析失败'
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if '%' in ip_str:
            ip_str = ip_str.split('%', 1)[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except Exception:
            return False, '地址无效'
        if _ip_is_blocked(addr):
            return False, '禁止内网/非公网地址'
    return True, ''


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = 'utf-8'
    m = re.search(r'charset=([^\s;]+)', content_type or '', re.I)
    if m:
        charset = m.group(1).strip().strip('"').strip("'")
    try:
        return raw.decode(charset, errors='replace')
    except Exception:
        return raw.decode('utf-8', errors='replace')


def _html_to_text(html: str) -> str:
    """HTML → 纯文本。优先 BeautifulSoup，没装就退回正则。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'noscript', 'header', 'footer', 'nav', 'svg']):
            tag.decompose()
        text = soup.get_text('\n')
    except Exception:
        text = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', html)
        text = re.sub(r'(?s)<[^>]+>', ' ', text)
        text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                    .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# ---------- 各搜索后端：统一返回 [{title, url, snippet}] ----------
def _search_serper(query: str, key: str, limit: int) -> list[dict]:
    r = requests.post('https://google.serper.dev/search',
                      headers={'X-API-KEY': key, 'Content-Type': 'application/json'},
                      json={'q': query, 'num': limit}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{'title': it.get('title', ''), 'url': it.get('link', ''), 'snippet': it.get('snippet', '')}
            for it in (r.json().get('organic') or [])[:limit]]


def _search_tavily(query: str, key: str, limit: int) -> list[dict]:
    r = requests.post('https://api.tavily.com/search',
                      json={'api_key': key, 'query': query, 'max_results': limit}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{'title': it.get('title', ''), 'url': it.get('url', ''), 'snippet': it.get('content', '')}
            for it in (r.json().get('results') or [])[:limit]]


def _search_brave(query: str, key: str, limit: int) -> list[dict]:
    r = requests.get('https://api.search.brave.com/res/v1/web/search',
                     headers={'X-Subscription-Token': key, 'Accept': 'application/json'},
                     params={'q': query, 'count': limit}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{'title': it.get('title', ''), 'url': it.get('url', ''), 'snippet': it.get('description', '')}
            for it in ((r.json().get('web') or {}).get('results') or [])[:limit]]


def _ddg_unwrap(href: str) -> str:
    """DuckDuckGo 的结果链接是 /l/?uddg=<编码后的真实地址>，解出来。"""
    if not href:
        return ''
    if href.startswith('//'):
        href = 'https:' + href
    try:
        q = parse_qs(urlparse(href).query)
        if 'uddg' in q:
            return unquote(q['uddg'][0])
    except Exception:
        pass
    return href


def _search_duckduckgo(query: str, limit: int) -> list[dict]:
    """免 key 的兜底搜索。"""
    r = requests.get(f'https://html.duckduckgo.com/html/?q={quote_plus(query)}',
                     headers={'User-Agent': UA}, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    out: list[dict] = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        for res in soup.select('div.result')[:limit * 2]:
            a = res.select_one('a.result__a')
            if not a:
                continue
            sn = res.select_one('.result__snippet')
            url = _ddg_unwrap(a.get('href', ''))
            if not url:
                continue
            out.append({'title': a.get_text(' ', strip=True),
                        'url': url,
                        'snippet': sn.get_text(' ', strip=True) if sn else ''})
            if len(out) >= limit:
                break
    except ImportError:
        # 没装 bs4 的正则兜底
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            url = _ddg_unwrap(m.group(1))
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if url:
                out.append({'title': title, 'url': url, 'snippet': ''})
            if len(out) >= limit:
                break
    return out


def _do_search(cfg: dict, query: str, limit: int) -> tuple[list[dict], str]:
    """返回 (结果, 使用的搜索源名称)。"""
    key = (cfg.get('search_api_key') or '').strip()
    backend = (cfg.get('search_backend') or '').strip().lower()
    if key and backend in ('serper', 'tavily', 'brave'):
        fn = {'serper': _search_serper, 'tavily': _search_tavily, 'brave': _search_brave}[backend]
        try:
            return fn(query, key, limit), backend
        except Exception as e:
            print(f"{backend} 搜索失败，回退 DuckDuckGo: {e}")
    return _search_duckduckgo(query, limit), 'duckduckgo'


class WebSearchTool(Tool):
    name = 'web_search'
    description = (
        "联网搜索，获取最新/实时信息（新闻、天气、价格、赛事、今天发生的事等）。"
        "任何你不确定或可能已过时的事实都应该先搜。返回若干条结果，含标题、摘要和来源网址。"
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '搜索关键词'},
            'limit': {'type': 'integer', 'description': '返回条数，默认 6'},
        },
        'required': ['query'],
    }

    def run(self, ctx: ToolContext, query: str = '', limit: int = 6) -> str:
        query = (query or '').strip()
        if not query:
            return '请提供搜索关键词。'
        try:
            limit = max(1, min(int(limit or 6), MAX_RESULTS))
        except Exception:
            limit = 6
        cfg = (ctx.services or {}).get('config') or {}
        try:
            results, source = _do_search(cfg, query, limit)
        except Exception as e:
            return f'搜索失败：{e}'
        if not results:
            return f'没有搜到“{query}”的结果。'
        lines = [f'搜索源：{source}｜关键词：{query}']
        for i, it in enumerate(results, 1):
            lines.append(f"{i}. {it['title']}\n   {it['snippet'][:200]}\n   来源：{it['url']}")
        lines.append('（回复用户时请说明信息来源网址）')
        return '\n'.join(lines)


class WebFetchTool(Tool):
    name = 'web_fetch'
    description = (
        "打开一个网址并读取正文内容。搜索结果的摘要不够用时，用这个读原文。"
        "只能用于对话里已出现的网址。"
    )
    parameters = {
        'type': 'object',
        'properties': {
            'url': {'type': 'string', 'description': '要读取的完整网址'},
        },
        'required': ['url'],
    }

    def run(self, ctx: ToolContext, url: str = '') -> str:
        url = (url or '').strip()
        if not url:
            return '请提供网址。'
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme.lower() not in ('http', 'https'):
            return '已拒绝：不允许访问内网 / 本机地址。'
        if not parsed.scheme:
            url = 'https://' + url
        ok, _reason = _is_safe_url(url)
        if not ok:
            return '已拒绝：不允许访问内网 / 本机地址。'

        current = url
        try:
            for hop in range(MAX_FETCH_REDIRECTS + 1):
                ok, _reason = _is_safe_url(current)
                if not ok:
                    return '已拒绝：不允许访问内网 / 本机地址。'
                r = requests.get(
                    current, headers={'User-Agent': UA}, timeout=TIMEOUT,
                    allow_redirects=False, stream=True,
                )
                try:
                    if r.status_code in _REDIRECT_STATUS:
                        loc = r.headers.get('Location') or r.headers.get('location') or ''
                        if hop >= MAX_FETCH_REDIRECTS:
                            return '读取网页失败：重定向过多。'
                        if not loc:
                            return '读取网页失败：重定向缺少 Location。'
                        current = urljoin(current, loc)
                        continue
                    r.raise_for_status()
                    ctype = r.headers.get('content-type', '') or r.headers.get('Content-Type', '')
                    if 'html' not in ctype and 'text' not in ctype:
                        return f'该地址不是网页内容（{ctype}）。'
                    chunks: list[bytes] = []
                    n = 0
                    for part in r.iter_content(chunk_size=8192):
                        if not part:
                            continue
                        if n + len(part) > MAX_FETCH_BYTES:
                            chunks.append(part[:MAX_FETCH_BYTES - n])
                            break
                        chunks.append(part)
                        n += len(part)
                    raw = b''.join(chunks)
                    text = _html_to_text(_decode_body(raw, ctype))
                    if not text:
                        return '这个网页没有可读正文。'
                    clipped = text[:MAX_PAGE_CHARS]
                    suffix = '\n\n（内容过长已截断）' if len(text) > MAX_PAGE_CHARS else ''
                    return f'来源：{current}\n\n{clipped}{suffix}'
                finally:
                    closer = getattr(r, 'close', None)
                    if closer:
                        try:
                            closer()
                        except Exception:
                            pass
            return '读取网页失败：重定向过多。'
        except Exception as e:
            return f'读取网页失败：{e}'


def make_web_tools() -> list:
    return [WebSearchTool(), WebFetchTool()]
