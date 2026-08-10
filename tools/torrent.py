"""BT 资源搜索工具。

用公开的 apibay(The Pirate Bay 镜像)JSON 接口检索，返回标题/大小/做种数 + 磁力链接。
无需 key；网络不通或无结果时优雅返回提示。仅使用公开可访问信息。
"""
from __future__ import annotations

import requests

from tools.base import Tool, ToolContext

_API = 'https://apibay.org/q.php'
_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.tracker.cl:1337/announce',
    'udp://tracker.openbittorrent.com:6969/announce',
    'udp://exodus.desync.com:6969/announce',
]
_NULL_HASH = '0000000000000000000000000000000000000000'


def _human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _magnet(info_hash: str, name: str) -> str:
    from urllib.parse import quote
    trs = ''.join(f"&tr={quote(t)}" for t in _TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}{trs}"


class TorrentSearchTool(Tool):
    name = 'torrent_search'
    description = (
        "搜索 BT/磁力资源（影视、软件、资料等），返回标题、大小、做种数和磁力链接。"
        "用户想找可下载的种子/磁力时使用。"
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '搜索关键词'},
            'limit': {'type': 'integer', 'description': '返回条数，默认 8'},
        },
        'required': ['query'],
    }

    def run(self, ctx: ToolContext, query: str = '', limit: int = 8) -> str:
        query = (query or '').strip()
        if not query:
            return '请提供搜索关键词。'
        try:
            limit = max(1, min(int(limit or 8), 15))
        except Exception:
            limit = 8
        try:
            resp = requests.get(_API, params={'q': query, 'cat': '0'},
                                headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return f'搜索失败（网络或接口问题）：{e}'

        if not isinstance(data, list) or not data:
            return f'没有找到与“{query}”相关的资源。'
        if len(data) == 1 and data[0].get('info_hash') == _NULL_HASH:
            return f'没有找到与“{query}”相关的资源。'

        rows = [d for d in data if d.get('info_hash') and d['info_hash'] != _NULL_HASH]
        rows.sort(key=lambda d: int(d.get('seeders', 0) or 0), reverse=True)
        rows = rows[:limit]
        if not rows:
            return f'没有找到与“{query}”相关的资源。'

        lines = [f"“{query}”的资源（按做种数排序）："]
        for d in rows:
            name = d.get('name', '(无名)')
            size = _human_size(d.get('size'))
            seeders = d.get('seeders', 0)
            lines.append(f"• {name}\n  大小 {size} | 做种 {seeders}\n  {_magnet(d['info_hash'], name)}")
        return '\n'.join(lines)


def make_torrent_tools() -> list:
    return [TorrentSearchTool()]
