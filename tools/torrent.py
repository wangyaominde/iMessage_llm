"""影视 / 资源磁力搜索（多源并查 + 质量分级）。

方法来自实战总结：
- 不要死磕单一源。apibay 对日剧、国产片经常 0 结果；日剧几乎必须 nyaa，
  欧美老片 solidtorrents / YTS 更稳。所以四个源并行查，合并去重。
- 中文片名多数源索引不到，要用「英文名 + 年份」搜（这一步由模型在调用前完成，
  见工具 description）。
- 质量排序：4K REMUX > 4K HDR/DV > 1080p BluRay > WEB-DL；720p 默认沉底。
- 中文用户优先：带 国语/中字/双音轨 的 1080p 往往优于纯英音小包。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode

import requests

from tools.base import Tool, ToolContext

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
TIMEOUT = 20
_NULL_HASH = '0000000000000000000000000000000000000000'
_HASH_RE = re.compile(r'\b([a-fA-F0-9]{40})\b')

TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.tracker.cl:1337/announce',
    'udp://tracker.openbittorrent.com:6969/announce',
    'udp://exodus.desync.com:6969/announce',
]


# ---------- 质量分级 ----------
def _quality(name: str) -> tuple[int, str]:
    """返回 (分级, 标签)。分级越大越好，720p 及以下沉底。"""
    n = (name or '').lower()
    is4k = ('2160p' in n or '4k' in n or 'uhd' in n)
    remux = 'remux' in n
    hdr = ('hdr' in n or 'dolby vision' in n or 'dovi' in n or re.search(r'\bdv\b', n))
    bluray = ('bluray' in n or 'blu-ray' in n or 'bdrip' in n or 'bdremux' in n)
    web = ('web-dl' in n or 'webdl' in n or 'webrip' in n or 'web ' in n)
    if is4k and remux:
        return 5, '4K REMUX'
    if is4k and hdr:
        return 4, '4K HDR/DV'
    if is4k:
        return 4, '4K'
    if '1080p' in n and bluray:
        return 3, '1080p BluRay'
    if '1080p' in n:
        return 2, '1080p WEB' if web else '1080p'
    if '720p' in n or '480p' in n or 'hdtv' in n:
        return 0, '720p 及以下'
    return 1, '未标注'


# 中文资源的各种写法：国语/国配/国英/国粤、中字/中英/简繁、多音轨、字幕 等
_CN_PAT = re.compile(
    r'国语|国配|国英|国粤|粤语|中字|中文|中英|简中|繁中|简繁|双语|字幕|音轨|'
    r'\bchs\b|\bcht\b|\bbig5\b|zh-?cn|mandarin|cantonese',
    re.IGNORECASE)


def _has_chinese_audio_or_sub(name: str) -> bool:
    return bool(_CN_PAT.search(name or ''))


def _human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return str(n) if n else '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _magnet(info_hash: str, name: str) -> str:
    trs = ''.join(f"&tr={quote(t)}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash.lower()}&dn={quote(name or '')}{trs}"


# ---------- 各源：统一返回 [{src, name, hash, size, seeders}] ----------
def _src_apibay(q: str) -> list[dict]:
    r = requests.get('https://apibay.org/q.php', params={'q': q, 'cat': '0'},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for t in (r.json() or []):
        h = (t.get('info_hash') or '').lower()
        if not h or h == _NULL_HASH or t.get('name') == 'No results returned':
            continue
        out.append({'src': 'apibay', 'name': t.get('name') or '', 'hash': h,
                    'size': int(t.get('size') or 0), 'seeders': int(t.get('seeders') or 0)})
    return out


def _src_solid(q: str) -> list[dict]:
    r = requests.get('https://solidtorrents.to/api/v1/search',
                     params={'q': q, 'sort': 'seeders'}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for t in (r.json().get('results') or []):
        h = (t.get('infohash') or t.get('info_hash') or '').lower()
        if not h or h == _NULL_HASH:
            continue
        out.append({'src': 'solid', 'name': t.get('title') or t.get('name') or '', 'hash': h,
                    'size': int(t.get('size') or 0),
                    'seeders': int(t.get('seeders') or t.get('seeds') or 0)})
    return out


def _src_yts(q: str) -> list[dict]:
    r = requests.get('https://yts.lt/api/v2/list_movies.json',
                     params={'query_term': q}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for m in ((r.json().get('data') or {}).get('movies') or []):
        for t in (m.get('torrents') or []):
            h = (t.get('hash') or '').lower()
            if not h:
                continue
            name = f"{m.get('title')} ({m.get('year')}) {t.get('quality')} {t.get('type')}"
            out.append({'src': 'yts', 'name': name, 'hash': h,
                        'size': t.get('size_bytes') or 0,
                        'seeders': int(t.get('seeds') or 0)})
    return out


def _src_nyaa(q: str) -> list[dict]:
    """日剧 / 动漫 / 生肉：apibay 常年 0 结果，这里是主力源。"""
    url = 'https://nyaa.si/?' + urlencode({'f': 0, 'c': '0_0', 'q': q, 's': 'seeders', 'o': 'desc'})
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        for row in soup.select('tr.default, tr.success, tr.danger'):
            a = row.select_one('a[href^="/view/"][title]')
            magnet = row.select_one('a[href^="magnet:"]')
            if not a or not magnet:
                continue
            m = _HASH_RE.search(magnet.get('href', ''))
            if not m:
                continue
            tds = row.find_all('td')
            size = tds[3].get_text(strip=True) if len(tds) > 3 else ''
            seeders = 0
            if len(tds) > 5:
                try:
                    seeders = int(tds[5].get_text(strip=True))
                except Exception:
                    pass
            out.append({'src': 'nyaa', 'name': a.get('title') or a.get_text(strip=True),
                        'hash': m.group(1).lower(), 'size': size, 'seeders': seeders})
    except ImportError:
        for m in _HASH_RE.finditer(r.text):
            out.append({'src': 'nyaa', 'name': '(nyaa)', 'hash': m.group(1).lower(),
                        'size': '', 'seeders': 0})
    return out


_SOURCES = (_src_apibay, _src_solid, _src_yts, _src_nyaa)


def _search_all(query: str) -> tuple[list[dict], list[str]]:
    """四源并行查，返回 (合并去重后的结果, 失败的源名)。"""
    rows: list[dict] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
        futs = {pool.submit(fn, query): fn.__name__ for fn in _SOURCES}
        for fut, name in futs.items():
            try:
                rows.extend(fut.result())
            except Exception as e:
                failed.append(f"{name.replace('_src_', '')}({type(e).__name__})")
    # 按 hash 去重，保留 seeders 更高的那条
    best: dict[str, dict] = {}
    for r in rows:
        h = r['hash']
        if h not in best or (r.get('seeders') or 0) > (best[h].get('seeders') or 0):
            best[h] = r
    return list(best.values()), failed


class TorrentSearchTool(Tool):
    name = 'torrent_search'
    description = (
        "搜索影视 / 剧集 / 资源的磁力链接（同时查 apibay、solidtorrents、YTS、nyaa 四个源）。\n"
        "重要：多数源只索引英文标题。用户给中文片名时，先自行换算成「英文片名 + 年份」再搜"
        "（例：《从21世纪安全撤离》→ Evacuate from the 21st Century 2024；"
        "日剧《外道之歌》→ Gedou no Uta）。片名有歧义时先问清用户，不要默认最热的那部。\n"
        "结果按画质排序：4K REMUX > 4K HDR/DV > 1080p BluRay > 1080p WEB > 其它，"
        "720p 及以下沉底；带国语/中字/双音轨的会标出来。"
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '搜索词，优先用「英文片名 + 年份」'},
            'limit': {'type': 'integer', 'description': '返回条数，默认 8'},
            'allow_720p': {'type': 'boolean', 'description': '是否接受 720p 及以下，默认否'},
        },
        'required': ['query'],
    }

    def run(self, ctx: ToolContext, query: str = '', limit: int = 8, allow_720p: bool = False) -> str:
        query = (query or '').strip()
        if not query:
            return '请提供搜索关键词（建议用英文片名 + 年份）。'
        try:
            limit = max(1, min(int(limit or 8), 15))
        except Exception:
            limit = 8

        try:
            rows, failed = _search_all(query)
        except Exception as e:
            return f'搜索失败：{e}'

        if not rows:
            tip = ('。若片名是中文，请换成英文片名 + 年份再搜一次；'
                   '日剧 / 动漫请用罗马字（如 Gedou no Uta）')
            return f'没有找到“{query}”的资源{tip}。'

        # 打分：画质分级 → 中文音轨/字幕 → 做种数
        for r in rows:
            q, label = _quality(r['name'])
            r['q'], r['label'] = q, label
            r['cn'] = _has_chinese_audio_or_sub(r['name'])
        if not allow_720p:
            filtered = [r for r in rows if r['q'] > 0]
            rows = filtered or rows  # 全是 720p 时不至于空手而归
        rows.sort(key=lambda r: (r['q'], r['cn'], r.get('seeders') or 0), reverse=True)
        rows = rows[:limit]

        lines = [f'“{query}”的资源（按画质+做种排序）：']
        for r in rows:
            marks = f"[{r['label']}]"
            if r['cn']:
                marks += '[中文音轨/字幕]'
            size = r['size'] if isinstance(r['size'], str) and r['size'] else _human_size(r['size'])
            lines.append(f"• {r['name']}\n  {marks} 大小 {size} | 做种 {r.get('seeders', 0)} | 源 {r['src']}"
                         f"\n  {_magnet(r['hash'], r['name'])}")
        if failed:
            lines.append(f"（部分源未响应：{', '.join(failed)}）")
        return '\n'.join(lines)


def make_torrent_tools() -> list:
    return [TorrentSearchTool()]
