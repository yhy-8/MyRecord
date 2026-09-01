"""服务端 CLI：启动 hub、设备令牌管理、旧数据导入、Records 渲染。"""

import argparse
import datetime
import os
import ssl
import sys
from pathlib import Path

from . import config
from .hub import auth, server as hub_server
from .hub.render import parse_day_file
from .hub.store import Store


def _store(data_dir: Path) -> tuple[Store, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    store = Store(state_path)
    return store, data_dir


def _admin_retry_result(retry_callable) -> tuple[bool, str]:
    """归一化重试结果：retry_callable 返回 (message, ok)，统一为 (ok, message)。

    服务端 http.hub 的 _admin_retry 按 (ok, message) 解包，因此这里必须把
    automation.retry_failed_automatic_tasks() 的 (message, ok) 顺序交换过来。
    """
    try:
        message, ok = retry_callable()
        return ok, message
    except Exception as error:
        return False, f"重试失败: {error}"


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
        return _admin_retry_result(ai_analysis.retry_failed_automatic_tasks)

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
    certfile = server_cfg["tls"]["certfile"]
    keyfile = server_cfg["tls"]["keyfile"]
    if not (certfile and keyfile):
        print(
            "[!] 禁止明文传输：服务端只能在 TLS 下运行。\n"
            "请先执行 `python -m server.main cert` 生成自签证书，再在\n"
            "server/config.yaml 的 server.tls.certfile / keyfile 填入路径。",
            file=sys.stderr,
        )
        return 2
    if not (Path(certfile).is_file() and Path(keyfile).is_file()):
        print(
            f"[!] TLS 证书/密钥不存在：{certfile}, {keyfile}\n"
            "请先执行 `python -m server.main cert` 生成自签证书。",
            file=sys.stderr,
        )
        return 2
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile, keyfile)
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
        ssl_context=ssl_context,
    )

    import threading
    import time

    def automation_daemon():
        """后台：每分钟检查一次；run_due_automatic_tasks 内部按 15 分钟检测缺失并
        独立执行到期任务（日/周/月互不依赖、顺序无关）。"""
        while True:
            try:
                run_ai_cycle()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=automation_daemon, daemon=True).start()

    print(f"MyRecord 服务端已启动（{host}:{port}），数据目录：{data_dir}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


_CRED_LABEL = "sync"  # 唯一链接凭证的内部标签（单一凭证模型，无多设备）


def _command_token(args: argparse.Namespace) -> int:
    """管理唯一链接凭证。

    服务端只存在唯一一个 token；create 与 rotate 等价（签发并覆盖旧 token）。
    不再需要 --device：凭证不绑定设备，设备由各端自报本机名区分。
    """
    store, _ = _store(Path(config.load()["server"]["data_dir"]))
    if args.action == "list":
        active = store.device_ids()
        if active:
            print(f"有效链接凭证：{active[0]}")
        else:
            print("当前无有效链接凭证（先执行 token create）。")
        return 0
    if args.action in ("create", "rotate"):
        token = auth.new_token()
        store.register_device(_CRED_LABEL, token)  # 覆盖并删除旧 token
        print(f"device_id: {_CRED_LABEL}")
        print(f"token: {token}")
        print("令牌只显示一次，请妥善保存（服务端只存哈希）。签发会覆盖并作废旧凭证。")
        return 0
    if args.action == "revoke":
        if not store.revoke_device(_CRED_LABEL):
            print("当前无有效链接凭证可停用。", file=sys.stderr)
            return 1
        print(f"已停用链接凭证 {_CRED_LABEL}（所有客户端无法再同步）。")
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


def _command_cert(args: argparse.Namespace) -> int:
    """生成自签证书（CA 能力）用于服务端直连 TLS。

    输出到 <data_dir>/tls/server.crt 与 server.key；证书本身可作 CA，
    客户端 config.yaml 的 verify 指向 .crt 即可校验收信。可指定 --cn 与
    --ip/--dns 作为 SAN，确保客户端连接的地址被证书覆盖。
    """
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print(
            "需要 cryptography：`pip install cryptography`（server/requirements.txt 已含）",
            file=sys.stderr,
        )
        return 2

    cfg = config.load()
    data_dir = Path(cfg["server"]["data_dir"])
    tls_dir = data_dir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    certfile = tls_dir / "server.crt"
    keyfile = tls_dir / "server.key"

    import socket

    cn = args.cn or socket.gethostname()
    names = [x509.DNSName(cn)]
    for dns in args.dns or []:
        names.append(x509.DNSName(dns))
    for ip in args.ip or []:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(keyfile, 0o600)

    print("已生成自签证书（CA 能力，有效期 10 年）：")
    print(f"  cert: {certfile}")
    print(f"  key : {keyfile}")
    print("服务端已默认指向上述路径（server.tls 留空即自动使用缺省），直接 run 即可启用 HTTPS。")
    print("客户端 config.yaml 设 server_url=https://<地址>:8765、verify=该 .crt 证书路径（含本机名 SAN，建议 --ip 加服务器 IP）。")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动 hub 服务")
    token = sub.add_parser("token", help="链接凭证管理（单一共享 token）")
    token.add_argument("action", choices=["create", "rotate", "revoke", "list"])
    imp = sub.add_parser("import", help="导入旧版 Records 目录")
    imp.add_argument("--records", required=True)
    sub.add_parser("render", help="重新渲染当天 Records")
    cert = sub.add_parser("cert", help="生成自签证书（服务端直连 TLS）")
    cert.add_argument("--cn", default="", help="证书 CN（默认本机名）")
    cert.add_argument("--ip", action="append", default=[], help="SAN IP，可多次（如服务器公网/局域网 IP）")
    cert.add_argument("--dns", action="append", default=[], help="SAN 域名，可多次")
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
    if command == "cert":
        return _command_cert(args)
    print(f"未知命令: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())