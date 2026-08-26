"""服务端 CLI：启动 hub、设备令牌管理、旧数据导入、Records 渲染。"""

import argparse
import datetime
import sys
from pathlib import Path

from . import config
from .hub import auth, server as hub_server
from .hub.parser import parse_day_file
from .hub.store import Store


def _store(data_dir: Path) -> tuple[Store, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    store = Store(state_path)
    return store, data_dir


def _command_run(args: argparse.Namespace) -> int:
    cfg = config.load()
    server_cfg = cfg["server"]
    store, data_dir = _store(Path(server_cfg["data_dir"]))

    from .ai import analysis as ai_analysis
    from .ai import logging_config as ai_logging

    ai_logging.configure_logging()

    def render_records() -> None:
        store.render_records(data_dir / "Records", data_dir / "Trash")

    def list_reports(kind: str) -> list[str]:
        base = data_dir / "AnalysisReports"
        if not base.exists():
            return []
        prefix = base / (kind or "").strip("/")
        if not prefix.exists() or not prefix.is_dir():
            return []
        return sorted(str(path.relative_to(base)) for path in prefix.rglob("*.md"))

    def read_report(rel: str) -> str | None:
        base = data_dir / "AnalysisReports"
        target = (base / rel).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def automation_status() -> dict:
        status_path = data_dir / "AnalysisReports" / ".automation-state.json"
        if not status_path.exists():
            return {}
        try:
            import json
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def run_ai_cycle() -> None:
        """一次自动任务循环：与原来相同的定时调度语义。"""
        try:
            ai_analysis.run_due_automatic_tasks()
        except Exception:
            pass
        store.render_records(data_dir / "Records", data_dir / "Trash")

    def admin_retry():
        try:
            ok, message = ai_analysis.retry_failed_automatic_tasks()
            return ok, message
        except Exception as error:
            return False, f"重试失败: {error}"

    def admin_set_model(name):
        from .ai import settings as ai_settings
        try:
            ai_settings.ModelConfig.select(name)
            return True, f"已永久切换为 {name}"
        except Exception as error:
            return False, f"切换模型失败: {error}"

    def status_ai():
        from .ai import settings as ai_settings
        return {
            "current_model": ai_settings.CONFIG.get("current_model", ""),
            "models": [m.get("name") for m in ai_settings.ModelConfig.models()],
        }

    render_records()
    host = server_cfg["host"]
    port = int(server_cfg["port"])
    httpd = hub_server.serve(
        store,
        host,
        port,
        list_reports=list_reports,
        read_report=read_report,
        automation_status=automation_status,
        admin_retry=admin_retry,
        admin_set_model=admin_set_model,
        status_ai=status_ai,
    )

    import threading
    import time

    def automation_daemon():
        """后台：每分钟执行一次自动任务（日总结→周报→月报），与原来定时语义一致。"""
        while True:
            try:
                run_ai_cycle()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=automation_daemon, daemon=True).start()

    print(f"AgentRecord 服务端已启动（{host}:{port}），数据目录：{data_dir}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _command_token(args: argparse.Namespace) -> int:
    store, _ = _store(Path(config.load()["server"]["data_dir"]))
    if args.action == "list":
        for device_id in store.device_ids():
            print(device_id)
        return 0
    if args.action in ("create", "rotate"):
        if not args.device:
            print("需要 --device 参数。", file=sys.stderr)
            return 2
        token = auth.new_token()
        if args.action == "create":
            device_id = store.register_device(args.device, token)
            print(f"device_id: {device_id}")
            print(f"token: {token}")
        else:
            if not store.rotate_token(args.device, token):
                print("设备不存在或已停用。", file=sys.stderr)
                return 1
            print(f"device_id: {args.device}")
            print(f"token: {token}")
        print("令牌只显示一次，请妥善保存（服务端只存哈希）。")
        return 0
    if args.action == "revoke":
        if not args.device:
            print("需要 --device 参数。", file=sys.stderr)
            return 2
        if not store.revoke_device(args.device):
            print("设备不存在。", file=sys.stderr)
            return 1
        print(f"已停用设备 {args.device}")
        return 0
    print(f"未知操作: {args.action}", file=sys.stderr)
    return 2


def _command_import(args: argparse.Namespace) -> int:
    source = Path(args.records).resolve()
    if not source.is_dir():
        print(f"目录不存在: {source}", file=sys.stderr)
        return 2
    store, data_dir = _store(Path(config.load()["server"]["data_dir"]))
    total = 0
    for path in sorted(source.glob("*.md")):
        date = path.stem
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            continue
        parsed = parse_day_file(date, path.read_text(encoding="utf-8"))
        entries = []
        for entry in parsed["entries"]:
            entries.append(
                {
                    "entry_id": entry["entry_id"],
                    "date": date,
                    "ts": entry["ts"],
                    "tag": entry["tag"],
                    "text": entry["text"],
                }
            )
        accepted = store.append_entries("legacy", entries)
        total += len(accepted)
    store.render_records(data_dir / "Records", data_dir / "Trash")
    print(f"导入完成：新增 {total} 条记录（来自 {source}）。")
    return 0


def _command_render(args: argparse.Namespace) -> int:
    store, data_dir = _store(Path(config.load()["server"]["data_dir"]))
    store.render_records(data_dir / "Records", data_dir / "Trash")
    print("Records 渲染完成。")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动 hub 服务")
    token = sub.add_parser("token", help="设备令牌管理")
    token.add_argument("action", choices=["create", "rotate", "revoke", "list"])
    token.add_argument("--device", default="")
    imp = sub.add_parser("import", help="导入旧版 Records 目录")
    imp.add_argument("--records", required=True)
    sub.add_parser("render", help="重新渲染当天 Records")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    command = args.command or "run"
    if command == "run":
        return _command_run(args)
    if command == "token":
        return _command_token(args)
    if command == "import":
        return _command_import(args)
    if command == "render":
        return _command_render(args)
    print(f"未知命令: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())