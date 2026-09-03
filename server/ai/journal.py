"""原始日记读取与总结区域更新。

每天一个 Markdown 文件及其内容格式（标记常量、day_header、extract_summary、
记录正则）由 server/hub/render.py 统一维护。本模块只保留 AI 分析链路的本地写
入动作（更新 <summary> 区域），并一律复用该单一格式来源，避免两套格式定义漂移。
分析代码只能通过这里提供的接口写入日记，避免未来 Agent 直接改动原始记录流。
"""

import hashlib
import re

from common.atomic_write import atomic_write

from ..hub import render  # 日记格式权威：标记/day_header/extract_summary/DEFAULT_SUMMARY
from . import settings
from .file_lock import FileLock


# 与渲染/解析共用同一定义（单一来源），供 AI 链路与测试引用。
RECORD_MARKER = render.RECORD_MARKER
ESCAPED_RECORD_MARKER = render.ESCAPED_RECORD_MARKER
DEFAULT_SUMMARY = render.DEFAULT_SUMMARY


def _acquire_journal_lock() -> FileLock:
    lock = FileLock.acquire(settings.DIARY_DIR / ".journal.lock", blocking=True)
    if lock is None:
        raise RuntimeError("日记文件锁获取失败")
    return lock


def extract_summary(text: str) -> str:
    """从日记文本提取 <summary> 正文；缺失时返回占位符。"""
    return render.extract_summary(text)


def update_summary_for_date(
    date: str,
    summary_text: str,
    *,
    expected_content_hash: str | None = None,
) -> str:
    """安全地把总结写入某日文档顶部的 <summary> 区域。"""
    file_path = settings.DIARY_DIR / f"{date}.md"
    lock = _acquire_journal_lock()
    try:
        if not file_path.exists():
            return f"找不到 {date} 的记录。"
        content = file_path.read_text(encoding="utf-8")
        if expected_content_hash is not None:
            actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual_hash != expected_content_hash:
                return f"{date} 的记录在总结生成期间发生变化，未写入过时总结。"
        if not re.search(r"<summary>.*?</summary>", content, re.DOTALL):
            return f"{date} 的记录缺少 <summary> 区域。"
        new_content = re.sub(
            r"<summary>.*?</summary>",
            lambda _match: f"<summary>\n{summary_text}\n</summary>",
            content,
            count=1,
            flags=re.DOTALL,
        )
        atomic_write(file_path, new_content)
        return f"{date} 的总结已写入文档顶部。"
    finally:
        lock.release()