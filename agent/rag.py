"""每用户隔离的轻量 BM25 RAG（对话归档 + 长期记忆）。

路径一律带 user_id，禁止跨用户混合语料。索引对象按 (state_dir, user_id, kind) 缓存。
"""
from __future__ import annotations

import json
import math
import os
import pickle
import threading
import uuid
from datetime import datetime
from typing import Iterable, Optional

KIND_ARCHIVE = 'archive'
KIND_MEMORY = 'memory'
_VALID_KINDS = (KIND_ARCHIVE, KIND_MEMORY)
TOK_VERSION = 2  # 中文单字 + 连续中文二元组


def tokenize(text: str) -> list[str]:
    """中文单字 + 连续中文片段内的二元组 + 英文/数字词（小写）。

    英文、标点、空白会打断中文片段，二元组不跨片段。
    """
    if not text:
        return []
    out: list[str] = []
    i = 0
    n = len(text)
    cjk_run: list[str] = []

    def flush_cjk():
        if not cjk_run:
            return
        out.extend(cjk_run)
        for a, b in zip(cjk_run, cjk_run[1:]):
            out.append(a + b)
        cjk_run.clear()

    while i < n:
        ch = text[i]
        if '\u4e00' <= ch <= '\u9fff':
            cjk_run.append(ch)
            i += 1
            continue
        flush_cjk()
        if ('A' <= ch <= 'Z') or ('a' <= ch <= 'z') or ('0' <= ch <= '9'):
            j = i + 1
            while j < n:
                c = text[j]
                if ('A' <= c <= 'Z') or ('a' <= c <= 'z') or ('0' <= c <= '9'):
                    j += 1
                else:
                    break
            out.append(text[i:j].lower())
            i = j
            continue
        i += 1
    flush_cjk()
    return out


def rag_dir_for(state_dir: str, user_id: str) -> str:
    return os.path.join(state_dir, 'rag', user_id)


def _corpus_path(state_dir: str, user_id: str, kind: str) -> str:
    return os.path.join(rag_dir_for(state_dir, user_id), f'{kind}.jsonl')


def _index_path(state_dir: str, user_id: str, kind: str) -> str:
    return os.path.join(rag_dir_for(state_dir, user_id), f'{kind}_bm25.pkl')


class _BM25:
    """极简 BM25Okapi，无外部依赖。"""

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_tokens = corpus_tokens
        self.N = len(corpus_tokens)
        self.doc_len = [len(doc) or 1 for doc in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 1.0
        self.df: dict[str, int] = {}
        for doc in corpus_tokens:
            for t in set(doc):
                self.df[t] = self.df.get(t, 0) + 1
        self.idf = {
            t: math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for t, df in self.df.items()
        }

    def scores(self, query_tokens: list[str]) -> list[float]:
        if not self.N or not query_tokens:
            return [0.0] * self.N
        out = [0.0] * self.N
        for i, doc in enumerate(self.corpus_tokens):
            if not doc:
                continue
            tf: dict[str, int] = {}
            for t in doc:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            dl = self.doc_len[i]
            for t in query_tokens:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                f = tf[t]
                score += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            out[i] = score
        return out


class UserRAGStore:
    """单个 state_dir 下、按 user_id 隔离的 RAG 存储。"""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self._lock = threading.Lock()
        # cache key: (user_id, kind) -> {'docs': list[dict], 'bm25': _BM25|None}
        self._cache: dict[tuple[str, str], dict] = {}

    def _ensure_kind(self, kind: str):
        if kind not in _VALID_KINDS:
            raise ValueError(f'invalid rag kind: {kind}')

    def _load(self, user_id: str, kind: str) -> dict:
        key = (user_id, kind)
        if key in self._cache:
            return self._cache[key]
        docs: list[dict] = []
        path = _corpus_path(self.state_dir, user_id, kind)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        docs.append(json.loads(line))
            except Exception as e:
                print(f"加载 RAG 语料失败 ({user_id}/{kind}): {e}")

        bm25 = None
        ip = _index_path(self.state_dir, user_id, kind)
        if os.path.exists(ip) and docs:
            try:
                with open(ip, 'rb') as f:
                    payload = pickle.load(f)
                # 缺 tok 版本标记视为过期（旧单字分词），用新分词重建
                if payload.get('n') == len(docs) and payload.get('tok') == TOK_VERSION:
                    bm25 = payload.get('bm25')
            except Exception:
                bm25 = None
        if bm25 is None and docs:
            bm25 = _BM25([tokenize(d.get('text') or '') for d in docs])
            self._persist_index(user_id, kind, bm25, len(docs))

        entry = {'docs': docs, 'bm25': bm25}
        self._cache[key] = entry
        return entry

    def _persist_corpus_line(self, user_id: str, kind: str, doc: dict):
        d = rag_dir_for(self.state_dir, user_id)
        os.makedirs(d, exist_ok=True)
        with open(_corpus_path(self.state_dir, user_id, kind), 'a', encoding='utf-8') as f:
            f.write(json.dumps(doc, ensure_ascii=False) + '\n')

    def _persist_index(self, user_id: str, kind: str, bm25: _BM25, n: int):
        d = rag_dir_for(self.state_dir, user_id)
        os.makedirs(d, exist_ok=True)
        tmp = _index_path(self.state_dir, user_id, kind) + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump({'n': n, 'bm25': bm25, 'tok': TOK_VERSION}, f)
        os.replace(tmp, _index_path(self.state_dir, user_id, kind))

    def has_docs(self, user_id: str, kind: str) -> bool:
        """该用户该 kind 是否已有文档（加锁）。"""
        self._ensure_kind(kind)
        with self._lock:
            entry = self._load(user_id, kind)
            return bool(entry['docs'])

    def add_chunks(self, user_id: str, kind: str, texts: Iterable[str],
                   meta: Optional[dict] = None) -> int:
        """追加文本块并重建该用户该 kind 的 BM25。返回新增条数。"""
        self._ensure_kind(kind)
        added = 0
        with self._lock:
            entry = self._load(user_id, kind)
            for text in texts:
                text = (text or '').strip()
                if not text:
                    continue
                doc = {
                    'id': uuid.uuid4().hex[:12],
                    'ts': datetime.now().isoformat(),
                    'text': text,
                }
                if meta:
                    doc.update(meta)
                entry['docs'].append(doc)
                self._persist_corpus_line(user_id, kind, doc)
                added += 1
            if added:
                entry['bm25'] = _BM25([tokenize(d.get('text') or '') for d in entry['docs']])
                self._persist_index(user_id, kind, entry['bm25'], len(entry['docs']))
        return added

    def rebuild(self, user_id: str, kind: str, texts: Iterable[str]) -> int:
        """用给定文本全量重建该用户该 kind（覆盖语料与索引）。"""
        self._ensure_kind(kind)
        docs = []
        for text in texts:
            text = (text or '').strip()
            if not text:
                continue
            docs.append({
                'id': uuid.uuid4().hex[:12],
                'ts': datetime.now().isoformat(),
                'text': text,
            })
        with self._lock:
            d = rag_dir_for(self.state_dir, user_id)
            os.makedirs(d, exist_ok=True)
            cp = _corpus_path(self.state_dir, user_id, kind)
            tmp = cp + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                for doc in docs:
                    f.write(json.dumps(doc, ensure_ascii=False) + '\n')
            os.replace(tmp, cp)
            bm25 = _BM25([tokenize(d['text']) for d in docs]) if docs else None
            if bm25:
                self._persist_index(user_id, kind, bm25, len(docs))
            elif os.path.exists(_index_path(self.state_dir, user_id, kind)):
                try:
                    os.remove(_index_path(self.state_dir, user_id, kind))
                except Exception:
                    pass
            self._cache[(user_id, kind)] = {'docs': docs, 'bm25': bm25}
        return len(docs)

    def search(self, user_id: str, kind: str, query: str, top_k: int = 4) -> list[str]:
        """返回该用户该 kind 下最相关的文本（已隔离）。"""
        self._ensure_kind(kind)
        q = (query or '').strip()
        if not q or top_k <= 0:
            return []
        with self._lock:
            entry = self._load(user_id, kind)
            docs = entry['docs']
            bm25 = entry['bm25']
            if not docs or bm25 is None:
                return []
            scores = bm25.scores(tokenize(q))
            ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            out = []
            for i in ranked:
                if scores[i] <= 0:
                    break
                text = (docs[i].get('text') or '').strip()
                if text:
                    out.append(text)
                if len(out) >= top_k:
                    break
            return out

    def invalidate(self, user_id: str, kind: Optional[str] = None):
        with self._lock:
            if kind:
                self._cache.pop((user_id, kind), None)
            else:
                for k in list(self._cache):
                    if k[0] == user_id:
                        del self._cache[k]

    def purge_user(self, user_id: str):
        """删除该用户全部 RAG 文件并清缓存。"""
        import shutil
        with self._lock:
            for k in list(self._cache):
                if k[0] == user_id:
                    del self._cache[k]
        path = rag_dir_for(self.state_dir, user_id)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


# 按 state_dir 单例，供 session / memory / compress 共用
_stores: dict[str, UserRAGStore] = {}
_stores_lock = threading.Lock()


def get_rag_store(state_dir: str) -> UserRAGStore:
    abspath = os.path.abspath(state_dir)
    with _stores_lock:
        store = _stores.get(abspath)
        if store is None:
            store = UserRAGStore(abspath)
            _stores[abspath] = store
        return store


def ensure_memory_index(state_dir: str, user_id: str) -> None:
    """若 memory.md 有内容但 memory RAG 为空，则从文件重建（兼容旧数据）。"""
    from agent.session import memory_dir_for, MEMORY_FILENAME
    mem_path = os.path.join(memory_dir_for(state_dir, user_id), MEMORY_FILENAME)
    if not os.path.exists(mem_path):
        return
    try:
        with open(mem_path, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    except Exception:
        return
    if not text:
        return
    store = get_rag_store(state_dir)
    if store.has_docs(user_id, KIND_MEMORY):
        return
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        store.rebuild(user_id, KIND_MEMORY, lines)


def search_user_context(state_dir: str, user_id: str, query: str, top_k: int = 4) -> str:
    """对本用户的 archive + memory 各搜一半，拼成可注入 system 的文本。"""
    if not query or top_k <= 0:
        return ''
    try:
        ensure_memory_index(state_dir, user_id)
    except Exception:
        pass
    store = get_rag_store(state_dir)
    # 记忆优先一点：长期事实更稳
    mem_k = max(1, top_k // 2)
    arch_k = max(1, top_k - mem_k)
    parts = []
    for label, kind, k in (
        ('长期记忆', KIND_MEMORY, mem_k),
        ('历史对话', KIND_ARCHIVE, arch_k),
    ):
        hits = store.search(user_id, kind, query, top_k=k)
        if hits:
            parts.append(f"[{label}]\n" + '\n---\n'.join(hits))
    return '\n\n'.join(parts)
