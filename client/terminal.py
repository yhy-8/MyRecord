"""终端辅助。"""

import subprocess
import sys


def clear_screen() -> None:
    if sys.platform == "win32":
        subprocess.run(["cls"], shell=True)
    else:
        print("\033c", end="")


def resolve_date(arg: str = "") -> str:
    """解析日期参数（today/昨天/-1/MM-DD/YYYY-MM-DD），默认今天。"""
    import datetime

    today = datetime.date.today()
    value = arg.strip()
    if not value:
        return today.isoformat()
    lower = value.lower()
    aliases = {"today": 0, "今天": 0, "yesterday": 1, "昨天": 1}
    if lower in aliases:
        return (today - datetime.timedelta(days=aliases[lower])).isoformat()
    if value.startswith("-") and value[1:].isdigit():
        return (today - datetime.timedelta(days=int(value[1:]))).isoformat()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    import re

    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if m:
        try:
            return datetime.date(
                today.year, int(m.group(1)), int(m.group(2))
            ).isoformat()
        except ValueError:
            pass
    return ""