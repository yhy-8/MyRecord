"""交互入口（统一单模式，共 /v /c /h /d /status /retry /model /sync 八个命令）。

同步规范：客户端不密集轮询云端。启动时一次性完整同步（full_sync）；运行期间保持
一条长连接（长轮询挂起，扇出即拉取），每条记录写入即触发 push；手动同步用 /sync。
"""

import datetime
import re
import subprocess
import sys
import threading
import time

from rich.console import Console
from rich.panel import Panel

from . import identity, journal
from .sync import SyncClient, SyncError


def _banner() -> None:
    from rich.console import Console

    console = Console()
    console.print(Panel.fit("[bold]MyRecord 客户端[/bold]", border_style="cyan"))


def _help_text() -> str:
    return (
        "/v [日期]     查看本地日记（不提供报告查看，建议用专用 md 阅读软件）\n"
        "/c            清屏\n"
        "/h            帮助\n"
        "/sync         立即手动同步一次（推送离线队列、拉取对账、同步报告）\n"
        "/d            在线删除当天最新一条（需联网，服务端确认，入垃圾桶）\n"
        "/status       查看服务端 AI 自动任务状态\n"
        "/retry        直接重试全部失败的服务端自动任务（顺序无关）\n"
        "/model        永久切换服务端 AI 模型\n"
        "普通输入       立即写入当天记录并即时同步到云端（长连接保持中）\n"
    )


def _show_help() -> None:
    """把帮助文本包装成与开头一致的 Panel 展示。"""
    from rich.console import Console
    from rich.panel import Panel

    Console().print(Panel.fit(_help_text(), title="帮助", border_style="cyan"))


# ---------- 终端辅助 ----------


def clear_screen() -> None:
    if sys.platform == "win32":
        subprocess.run(["cls"], shell=True)
    else:
        print("\033c", end="")


def resolve_date(arg: str = "") -> str:
    """解析日期参数（today/昨天/-1/MM-DD/YYYY-MM-DD），默认今天。"""
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

    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if m:
        try:
            return datetime.date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    return ""


def show_day(arg: str) -> None:
    """查看本地日记（渲染 Markdown；不提供报告查看）。"""
    from rich.markdown import Markdown

    date = resolve_date(arg)
    if not date:
        Console().print("[yellow][!][/yellow] 无法解析日期。")
        return
    path = journal.day_path(date)
    if not path.exists():
        print(f"（{date} 还没有记录。）")
        return
    Console().print(Markdown(_render_day_markdown(path.read_text(encoding="utf-8"))))


_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def _render_day_markdown(text: str) -> str:
    """把 `<summary>` 区域转成 blockquote，其余原样交给 Markdown 渲染。

    原因：`<summary>` 是自定义标记，rich Markdown 会丢弃未知 HTML 标签内的正文；
    转成 blockquote 后总结正文（含其中 Markdown）可被正常渲染。
    """

    def _to_quote(match: re.Match) -> str:
        summary = match.group(1).strip()
        if not summary:
            return ""
        return "\n".join("> " + line for line in summary.splitlines()) + "\n"

    return _SUMMARY_RE.sub(_to_quote, text)


# ---------- 命令处理 ----------


def _handle_command(client: SyncClient, text: str) -> None:
    """统一命令解析：以 / 开头的输入只按命令处理，未知命令提示而不写入。"""
    from rich.console import Console

    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    handlers = {
        "/c": lambda: clear_screen(),
        "/h": lambda: _show_help(),
        "/v": lambda: show_day(arg),
        "/d": lambda: _handle_delete(client),
        "/sync": lambda: _handle_sync(client),
        "/status": lambda: _handle_status(client),
        "/retry": lambda: _handle_retry(client),
        "/model": lambda: _handle_model(client),
    }
    handler = handlers.get(cmd)
    if handler:
        handler()
    else:
        Console().print(f"[yellow][!][/yellow] 未知命令：{cmd}（输入 /h 查看帮助）。")


def _print_automation_status(automation: dict) -> None:
    """展示自动任务逐任务状态（ok/failed/pending/blocked）。"""
    from rich.console import Console

    tasks = automation.get("tasks") or {}
    if not tasks:
        return
    console = Console()
    labels = {
        "daily_summary": "日总结",
        "weekly_report": "周报",
        "monthly_report": "月报",
    }
    status_text = {
        "ok": "完成",
        "failed": "失败（待重试）",
        "blocked": "已达重试上限",
        "pending": "待生成",
    }
    console.print("自动任务:")
    for task, record in tasks.items():
        st = record.get("status", "") if isinstance(record, dict) else ""
        label = labels.get(task, task)
        console.print(f"  {label}: {status_text.get(st, st)}")


def _handle_status(client: SyncClient) -> None:
    from rich.console import Console

    console = Console()
    try:
        status = client.status()
    except SyncError as error:
        console.print(f"[red][!][/red] {error}")
        return
    console.print(f"服务端版本: {status.get('version')}")
    console.print(f"条目数: {status.get('entry_count')}   已删数: {status.get('tombstone_count')}")
    devices = status.get("devices") or {}
    console.print("设备: " + (", ".join(devices.keys()) if devices else "（无）"))
    ai = status.get("ai") or {}
    if ai.get("current_model"):
        console.print(f"AI 模型: {ai['current_model']}")
    _print_automation_status(status.get("automation") or {})


def _handle_sync(client: SyncClient) -> None:
    from rich.console import Console

    console = Console()
    try:
        client.full_sync()
    except SyncError as error:
        console.print(f"[red][!][/red] 同步失败：{error}")
        return
    console.print("[green][*][/green] 已与云端完成同步（推送离线队列、拉取对账、同步报告）。")


def _handle_delete(client: SyncClient) -> None:
    from rich.console import Console

    console = Console()
    today = datetime.date.today().isoformat()
    try:
        deleted = client.delete_latest(today)
    except SyncError as error:
        console.print(f"[red][!][/red] 删除失败（需在线）：{error}")
        return
    if deleted:
        console.print(f"[cyan][*][/cyan] 已删除当天最新一条（{deleted}），正文移入垃圾桶。")
    else:
        console.print("[yellow][!][/yellow] 当天暂无记录可删除。")


def _handle_retry(client: SyncClient) -> None:
    from rich.console import Console

    console = Console()
    try:
        result = client.admin_retry()
    except SyncError as error:
        console.print(f"[red][!][/red] 重试失败：{error}")
        return
    if result.get("ok"):
        console.print(f"[green][*][/green] {result.get('message')}")
    else:
        console.print(f"[yellow][!][/yellow] {result.get('message')}")


def _handle_model(client: SyncClient) -> None:
    """永久切换服务端 AI 模型；不带参数时按模型列表循环。"""
    from rich.console import Console

    console = Console()
    name = ""

    try:
        status = client.status()
    except SyncError as error:
        console.print(f"[red][!][/red] 无法读取服务端：{error}")
        return
    models = list((status.get("ai") or {}).get("models") or [])
    if not models:
        console.print("[yellow][!][/yellow] 服务端未配置模型。")
        return
    current = (status.get("ai") or {}).get("current_model", "")
    if not name:
        try:
            index = models.index(current)
        except ValueError:
            index = -1
        name = models[(index + 1) % len(models)]
    try:
        result = client.admin_set_model(name)
    except SyncError as error:
        console.print(f"[red][!][/red] 切换失败：{error}")
        return
    if result.get("ok"):
        console.print(f"[cyan][*][/cyan] {result.get('message')}")
    else:
        console.print(f"[yellow][!][/yellow] {result.get('message')}")


def _write_record(client: SyncClient, text: str) -> None:
    """本地优先写入：不要求凭据/在线，始终先落盘；随后尽力即时同步。

    entry_id 由内容+时间确定性生成（设备无关），所以哪怕在没有凭据、离线
    的情况下记录，上线拿到凭据后也能被服务端按 entry_id 正确合并去重。
    """
    now = datetime.datetime.now()
    entry = {
        "entry_id": identity.make_entry_id(
            now.strftime("%Y-%m-%d"), int(now.timestamp()), text
        ),
        "device_id": identity.device_name(),  # 设备名：本机名，用于区分设备
        "date": now.strftime("%Y-%m-%d"),
        "ts": int(now.timestamp()),
        "tag": "",
        "text": text,
    }
    # 本地写入永不回滚，与同步成败无关
    journal.append_record(entry)
    # 尽力即时同步：无凭据或离线时 push_new 会静默失败并留在离线队列
    client.push_new(entry)


def _longpoll_loop(client: SyncClient) -> None:
    """维持同一条长连接（长轮询挂起）：服务端有更新（扇出）即立即应用并同步报告。

    长轮询超时会立即重新挂起，等效于一直保持一条长连接；不密集访问云端。
    每次成功返回都冲刷一次离线队列（空队列时零网络请求），处理断网后回连补齐。
    """
    while True:
        try:
            changed = client.longpoll()
            if changed:
                # 扇出增量已应用；顺带把新报告同步到本地
                client.sync_reports()
            # 冲刷离线队列（空队列时 send_pending 直接返回，不产生请求）
            client.send_pending()
        except SyncError:
            # 离线/服务端不可达：静默等待，保留离线队列
            time.sleep(5)
        time.sleep(0.5)


def _report_startup_status(client: SyncClient, status: dict) -> None:
    """按「能否连到服务端」与「是否持有凭据」两个独立维度分别播报启动状态。

    过去无条件打印「已连接服务端」，把「仅配置了地址」误当成「已连接」，
    服务端未启动也显示已连接；这里先真实探测（/api/health），再区分：
      - 连接：网络/TLS 能否建立（连得上不代表有改数据权限）
      - 凭据：是否持有 token（有凭据才能修改中心数据）
    """
    console = Console()
    if status["connected"]:
        console.print(f"[green][*][/green] 已连接服务端：{client.base_url}（网络可达）")
    else:
        reason = f"（{status['error']}）" if status.get("error") else ""
        console.print(f"[red][!][/red] 无法连接服务端：{client.base_url}{reason}")
    if status["has_credentials"]:
        console.print("[green][*][/green] 凭据：已配置，可同步与修改数据")
    else:
        console.print("[yellow][!][/yellow] 凭据：未配置，仅本地记录；上线前请先写入 credentials.json")


def run_interactive() -> None:
    # 不要求凭据：无凭据时仅本地记录，上线拿到凭据后再同步（本地优先）
    client = SyncClient()

    # 启动先真实探测，再按两维度分别播报：能否连到服务端、是否持有凭据。
    # 过去无条件打印「已连接服务端」，服务端未启动也显示已连接，属误报。
    status = client.probe()
    _report_startup_status(client, status)

    # 启动时一次性链接云端并完整同步（拉取对账 + 冲刷离线队列 + 同步报告）
    # 仅在「服务端可达 + 持凭据」时才可能成功；其余情况本地优先，暂不复现同一失败。
    if status["connected"] and status["has_credentials"]:
        try:
            client.full_sync()
            print("启动同步完成：本地已与云端对账一致。")
        except SyncError as error:
            Console().print(f"[yellow][!][/yellow] {error}（本地照常记录，上线后自动同步）")
    else:
        print("本地照常记录；联网且凭据就绪后，可用 /sync 同步到云端。")

    _banner()
    _show_help()

    # 运行期间保持一条长连接（长轮询），实现扇出即拉取；不密集轮询云端。
    thread = threading.Thread(
        target=_longpoll_loop, args=(client,), daemon=True
    )
    thread.start()

    while True:
        try:
            raw = input(">> ")
        except (KeyboardInterrupt, EOFError):
            print("系统退出。")
            break
        text = raw.strip()
        if not text:
            continue
        if text.startswith("/"):
            _handle_command(client, text)
            continue
        _write_record(client, text)