"""A / B / C 三层缓存。

- A 搜索页：search_cache/<sha1(归一化 query)>.html，按文件 mtime 过期
- B 摘要  ：cache/summaries.json，写穿落盘，重启不丢
- C 页面  ：沿用 main.py 的 wiki/*.html 文件缓存，本模块只提供单飞锁，
            避免同一个新词被并发请求重复生成（N 个并发 → 1 次模型调用）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("caches")

SEARCH_TTL = int(os.getenv("SEARCH_TTL_SECONDS", "86400"))
SUMMARY_TTL = int(os.getenv("SUMMARY_TTL_SECONDS", "86400"))

SEARCH_DIR = Path(os.getenv("SEARCH_CACHE_DIR", "search_cache"))
SUMMARY_FILE = Path(os.getenv("SUMMARY_CACHE_FILE", "cache/summaries.json"))

_summaries: dict[str, dict] = {}
_dirty = False
_locks: dict[str, asyncio.Lock] = {}

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """归一化缓存 key：去首尾空白、压缩内部空白、大小写折叠。"""
    return _WS.sub(" ", (text or "").strip()).casefold()


def _digest(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def init() -> None:
    SEARCH_DIR.mkdir(exist_ok=True)
    SUMMARY_FILE.parent.mkdir(exist_ok=True)
    _load_summaries()
    logger.info("缓存层就绪 | 搜索页 %d 个 | 摘要 %d 条", count_search(), len(_summaries))


# ---------------- A：搜索页 ----------------

def search_path(query: str) -> Path:
    return SEARCH_DIR / f"{_digest('search:' + norm(query))}.html"


def search_get(query: str) -> str | None:
    path = search_path(query)
    try:
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > SEARCH_TTL:
            path.unlink(missing_ok=True)
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def search_set(query: str, html: str) -> None:
    try:
        search_path(query).write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("搜索页缓存写入失败: %s", exc)


def count_search() -> int:
    try:
        return sum(1 for _ in SEARCH_DIR.glob("*.html"))
    except OSError:
        return 0


# ---------------- B：摘要 ----------------

def _load_summaries() -> None:
    global _summaries
    try:
        if SUMMARY_FILE.exists():
            raw = json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _summaries = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("摘要缓存读取失败，从空开始: %s", exc)
        _summaries = {}


def summary_get(title: str) -> str | None:
    entry = _summaries.get(norm(title))
    if not entry:
        return None
    if time.time() - float(entry.get("ts", 0)) > SUMMARY_TTL:
        _summaries.pop(norm(title), None)
        _mark_dirty()
        return None
    return entry.get("text") or None


def summary_set(title: str, summary: str) -> None:
    global _dirty
    text = (summary or "").strip()
    if not text:
        return
    key = norm(title)
    old = _summaries.get(key)
    if old and old.get("text") == text:
        return
    _summaries[key] = {"text": text, "ts": time.time()}
    _mark_dirty()


def store_summaries(mapping: dict[str, str]) -> None:
    for title, summary in mapping.items():
        summary_set(title, summary)


def _mark_dirty() -> None:
    global _dirty
    _dirty = True


def flush(force: bool = False) -> None:
    """把内存里的摘要写回磁盘（默认只在有改动时写）。"""
    global _dirty
    if not _dirty and not force:
        return
    try:
        SUMMARY_FILE.write_text(
            json.dumps(_summaries, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        _dirty = False
    except OSError as exc:
        logger.warning("摘要缓存落盘失败: %s", exc)


def count_summaries() -> int:
    return len(_summaries)


# ---------------- C：单飞锁 ----------------

def page_lock(name: str) -> asyncio.Lock:
    """同一个页面（或同一个带图变体）只允许一个协程生成，其余等待后读缓存。"""
    lock = _locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[name] = lock
    return lock


# ---------------- 维护 ----------------

def cleanup() -> int:
    """删除过期的搜索页缓存。"""
    deleted = 0
    now = time.time()
    try:
        for path in SEARCH_DIR.glob("*.html"):
            try:
                if now - path.stat().st_mtime > SEARCH_TTL:
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
    except OSError:
        return deleted
    return deleted


def stats() -> dict:
    return {
        "search_pages": count_search(),
        "summaries": count_summaries(),
        "locks": len(_locks),
        "search_ttl": SEARCH_TTL,
        "summary_ttl": SUMMARY_TTL,
    }
