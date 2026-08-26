"""交互入口（统一单模式，共 /v /c /h /d /status /retry /model 七个命令）。"""

import datetime
import threading
import time

from rich.panel import Panel

from .. import config, idseq, journal, sync
from ..sync import SyncClient, SyncError


def _banner() -> None:
    from rich.console import Console

    console = Console()
    console.print(Panel.fit("[bold]AgentRecord 客户端[/bold]", border_style="cyan"))


def _help_text() -> str:
    return (
        "/v [日期]     查看本地日记（不提供报告查看，建议用专用 md 阅读软件）\n"
        "/c            清屏\n"
        "/h            帮助\n"
        "/d            在线删除当天最新一条（需联网，服务端确认，入垃圾桶）\n"
        "/status       查看服务端 AI 自动任务状态\n"
        "/retry        按队列重试服务端 AI 自动任务\n"
        "/model        永久切换服务端 AI 模型\n"
        "普通输入       立即写入当天记录并即时同步到云端\n"
    )


def _handle_view(arg: str) -> None:
    from .view import show_day

    date = arg.split(maxsplit=1)[1].strip() if " " in arg else ""
    show_day(date)


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
    automation = status.get("automation") or {}
    if automation.get("errors"):
        console.print(f"[yellow][!][/yellow] 自动任务异常: {automation['errors']}")


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
    cred = _require_creds()
    now = datetime.datetime.now()
    entry = {
        "entry_id": idseq.make_entry_id(cred["device_id"]),
        "device_id": cred["device_id"],
        "date": now.strftime("%Y-%m-%d"),
        "ts": int(now.timestamp()),
        "tag": "",
        "text": text,
    }
    journal.append_record(entry)
    client.push_new(entry)


def _require_creds() -> dict:
    from .. import credentials

    return credentials.require()


def _background_loop(client: SyncClient) -> None:
    """后台：长轮询扇出即拉取 + 每 1 分钟拉取对账。服务端不可达时静默重试。"""
    cfg = config.load()["client"]
    interval = int(cfg["poll_interval_seconds"])
    last_pull = 0.0
    while True:
        try:
            changed = client.longpoll()
            if changed:
                # 有增量已应用；顺带把离线队列推上去并同步报告
                client.send_pending()
                client.sync_reports()
            else:
                now = time.monotonic()
                if now - last_pull >= interval:
                    client.pull()
                    client.send_pending()
                    client.sync_reports()
                    last_pull = now
        except (SyncError, Exception):
            # 离线/服务端不可达：静默等待，保留离线队列
            time.sleep(5)
        time.sleep(0.5)


def run_interactive() -> None:
    # 仅确认本地凭据存在（返回值不用）
    _require_creds()
    client = SyncClient()
    print("已连接服务端：" + client.base_url)
    _banner()
    print(_help_text())

    thread = threading.Thread(
        target=_background_loop, args=(client,), daemon=True
    )
    thread.start()

    from ..terminal import clear_screen

    while True:
        try:
            raw = input(">> ")
        except (KeyboardInterrupt, EOFError):
            print("系统退出。")
            break
        text = raw.strip()
        if not text:
            continue
        if text == "/c":
            clear_screen()
            continue
        if text == "/h":
            print(_help_text())
            continue
        if text.startswith("/v"):
            _handle_view(text)
            continue
        if text == "/d":
            _handle_delete(client)
            continue
        if text == "/status":
            _handle_status(client)
            continue
        if text == "/retry":
            _handle_retry(client)
            continue
        if text == "/model":
            _handle_model(client)
            continue
        _write_record(client, text)