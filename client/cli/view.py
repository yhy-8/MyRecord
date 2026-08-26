"""查看本地日记（不提供报告查看）。"""

from .. import journal
from ..terminal import resolve_date


def show_day(arg: str) -> None:
    date = resolve_date(arg)
    if not date:
        print("[黄色][!] 无法解析日期。[/黄色]")
        return
    path = journal.day_path(date)
    if not path.exists():
        print(f"（{date} 还没有记录。）")
        return
    print(f"===== {date} =====")
    print(path.read_text(encoding="utf-8"))