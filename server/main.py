"""服务端 CLI：启动 hub、设备令牌管理、Records 导入、Records 渲染。"""

import argparse
import datetime
import os
import ssl
import subprocess
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

    last_render_version = store.data["version"]

    def run_ai_cycle() -> None:
        """一次自动任务循环：仅在权威数据版本变化时重渲染 Records。

        后台只是用它将 Records 与 state.json 保持同步；没有新条目/墓碑时跳过
        全量重渲染，避免每分钟对全部日期做无谓的磁盘写。
        """
        try:
            ai_analysis.run_due_automatic_tasks()
        except Exception:
            pass
        nonlocal last_render_version
        current = store.data["version"]
        if current != last_render_version:
            store.render_records(data_dir / "Records", data_dir / "Trash")
            last_render_version = current

    def admin_retry():
        return ai_analysis.retry_failed_automatic_tasks()

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
    if not (Path(certfile).is_file() and Path(keyfile).is_file()):
        print(
            f"[!] TLS 证书/密钥不存在：{certfile}, {keyfile}\n"
            "请先执行 `python -m server.main cert --ip 服务端IP` 生成自签证书。",
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


_CREDENTIAL_DEVICE_LABEL = "sync"  # 唯一链接凭证的内部设备标签（单一凭证模型，无多设备）


def _confirm_overwrite_credential() -> bool:
    """重签前的二次确认：已存在有效凭证时，覆盖会让旧凭证立即失效。"""
    try:
        answer = input("已存在有效链接凭证，重新签发将覆盖并作废旧凭证。输入 yes 确认：")
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _command_token(args: argparse.Namespace) -> int:
    """管理唯一链接凭证（仅 create / list 两个子命令）。

    - create：签发唯一 token；已有有效凭证时重签会覆盖并作废旧 token，需输入 yes 二次确认。
    - list：查看是否已配置有效凭证。
    不再提供 rotate/revoke：凭证是连接许可，重签即等价于 rotate；无多设备/多凭证
    分离的需求。凭证不绑定设备，设备由各端自报本机名区分。
    """
    store, _ = _store(Path(config.load()["server"]["data_dir"]))
    if args.action == "list":
        cred = store.active_credential()
        if cred:
            created = cred.get("created_at") or 0
            if created:
                stamp = datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
                print(f"有效链接凭证：已配置（生成于 {stamp}）")
            else:
                print("有效链接凭证：已配置（生成时间未知）")
        else:
            print("有效链接凭证：未配置（先执行 token create）。")
        return 0
    if args.action == "create":
        if store.device_ids() and not _confirm_overwrite_credential():
            print("已取消。", file=sys.stderr)
            return 1
        token = auth.new_token()
        store.register_device(_CREDENTIAL_DEVICE_LABEL, token)  # 覆盖并删除旧 token
        print("链接凭证已签发。令牌只显示一次，请妥善保存（服务端只存哈希）。")
        print(f"token: {token}")
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
        accepted = store.append_entries("import", entries)
        total += len(accepted)
    store.render_records(data_dir / "Records", data_dir / "Trash")
    print(f"导入完成：新增 {total} 条记录（来自 {source}）。")
    return 0


def _command_render(args: argparse.Namespace) -> int:
    store, data_dir = _store(Path(config.load()["server"]["data_dir"]))
    store.render_records(data_dir / "Records", data_dir / "Trash")
    print("Records 渲染完成。")
    return 0


def _command_report(args: argparse.Namespace) -> int:
    """手动生成周报 / 月报（与自动任务同一流程、同一报告路径，直接覆盖）。"""
    from .ai import settings as ai_settings
    from .ai.analysis import generate_analysis_report

    kind = args.kind
    try:
        anchor = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"无效日期: {args.date}（应为 YYYY-MM-DD）", file=sys.stderr)
        return 2
    try:
        model = ai_settings.ModelConfig.get_model()
    except Exception as error:
        print(f"模型配置无效: {error}", file=sys.stderr)
        return 2
    message, success, path = generate_analysis_report(kind, anchor, model)
    print(message)
    if success and path:
        print(f"报告已生成: {path}")
        return 0
    return 1


def _generate_cert(
    data_dir: Path,
    cn: str = "",
    ips: list[str] | None = None,
    dns: list[str] | None = None,
) -> tuple[Path, Path]:
    """生成自签证书（CA 能力，有效期 10 年）到 <data_dir>/tls/，返回 (certfile, keyfile)。

    固定输出 server.crt / server.key，与 server config 的缺省 TLS 位置一致。
    证书本身可作 CA，客户端 config.yaml 的 verify 指向 .crt 即可校验收信。
    """
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise RuntimeError("需要 cryptography：`pip install cryptography`（server/requirements.txt 已含）")

    import socket

    tls_dir = data_dir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    certfile = tls_dir / "server.crt"
    keyfile = tls_dir / "server.key"

    cn = cn or socket.gethostname()
    names = [x509.DNSName(cn)]
    for dns_name in dns or []:
        names.append(x509.DNSName(dns_name))
    for ip in ips or []:
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
    return certfile, keyfile


def _command_cert(args: argparse.Namespace) -> int:
    """生成自签证书（CA 能力）用于服务端直连 TLS。产物固定为 <data_dir>/tls/server.*。"""
    data_dir = Path(config.load()["server"]["data_dir"])
    try:
        certfile, keyfile = _generate_cert(data_dir, cn=args.cn, ips=args.ip, dns=args.dns)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("已生成自签证书（客户端 verify 指向 cert 即可校验收信）：")
    print(f"  {certfile}")
    print(f"  {keyfile}")
    return 0


_SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/myrecord-server.service")
_BACKUP_SERVICE_PATH = Path("/etc/systemd/system/myrecord-backup.service")
_BACKUP_TIMER_PATH = Path("/etc/systemd/system/myrecord-backup.timer")


def _deploy_dir() -> Path:
    """部署文件所在目录（server/deploy：backup.sh、systemd 单元与定时器模板）。"""
    return Path(__file__).resolve().parent / "deploy"


def _render_systemd(interpreter: str, project_root: Path) -> str:
    """渲染 systemd 单元：解释器路径 + 工程根（使 `python -m server.main run` 可解析）。

    systemd 单元是 Linux 格式，路径一律用正斜杠；Windows 上 Path 会渲染成反斜杠，
    这里用 as_posix() 归一化，避免在 Windows 上生成 `WorkingDirectory=\\srv\\...` 之类非法值。
    """
    return (
        "[Unit]\n"
        "Description=MyRecord server hub (sync + AI reports + fanout)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={interpreter} -m server.main run\n"
        f"WorkingDirectory={project_root.as_posix()}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "User=root\n"
    )


def _render_backup_unit(project_root: Path) -> str:
    """渲染备份 systemd 单元：backup.sh 绝对路径 + 与 server 单元一致的工程根。

    backup.sh 从自身位置推断工程根并读取 config.yaml 的 data_dir，因此 WorkingDirectory
    仅作归属参考；ExecStart 用绝对路径调用，运行时不依赖当前目录。
    """
    backup_script = (project_root / "server" / "deploy" / "backup.sh").as_posix()
    return (
        "[Unit]\n"
        "Description=MyRecord server data backup (weekly)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/bash {backup_script}\n"
        f"WorkingDirectory={project_root.as_posix()}\n"
        "User=root\n"
    )


def _command_deploy(args: argparse.Namespace) -> int:
    """一键安装并启动 systemd 服务（需 root）。

    自动带出当前解释器与工程根，无需手改单元文件；同时安装并启用每周自动备份定时器。
    若 TLS 证书缺失则先自动生成。
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print(
            "[!] 需要 root 权限：请用 sudo 运行，例如：sudo python -m server.main deploy",
            file=sys.stderr,
        )
        return 2
    cfg = config.load()
    data_dir = Path(cfg["server"]["data_dir"])
    certfile = Path(cfg["server"]["tls"]["certfile"])
    if not certfile.is_file():
        try:
            _generate_cert(data_dir)
        except RuntimeError as error:
            print(f"[!] 生成自签证书失败：{error}", file=sys.stderr)
            return 2
    project_root = Path(__file__).resolve().parent.parent
    deploy_dir = _deploy_dir()
    server_dest = _SYSTEMD_UNIT_PATH
    backup_dest = _BACKUP_SERVICE_PATH
    timer_dest = _BACKUP_TIMER_PATH
    for dest in (server_dest, backup_dest, timer_dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
    server_dest.write_text(
        _render_systemd(sys.executable, project_root), encoding="utf-8"
    )
    backup_dest.write_text(_render_backup_unit(project_root), encoding="utf-8")
    timer_dest.write_text(
        (deploy_dir / "myrecord-backup.timer").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    # 只部署并立即启动服务端，不执行 `enable`，避免开机自启动（如需开机自启请自行 `systemctl enable`）。
    subprocess.run(["systemctl", "start", "myrecord-server"], check=True)
    # 备份定时器启用并即刻生效（Persistent=true 会在错过触发时间后补跑）。
    subprocess.run(["systemctl", "enable", "--now", "myrecord-backup.timer"], check=True)
    print(f"已安装服务端单元：{server_dest}")
    print(f"已安装备份单元与定时器：{backup_dest}、{timer_dest}")
    print("已启动 myrecord-server，并启用 myrecord-backup.timer（每周自动备份）。")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动 hub 服务")
    token = sub.add_parser("token", help="链接凭证管理（create/list）")
    token.add_argument("action", choices=["create", "list"])
    imp = sub.add_parser("import", help="导入 Records 目录")
    imp.add_argument("--records", required=True)
    sub.add_parser("render", help="重新渲染当天 Records")
    rep = sub.add_parser("report", help="手动生成周报/月报（同一流程，直接覆盖）")
    rep.add_argument("--kind", required=True, choices=["weekly", "monthly"])
    rep.add_argument(
        "--date",
        required=True,
        help="周/月内任一天，按该日期所在自然周/月确定范围 (YYYY-MM-DD)",
    )
    sub.add_parser("deploy", help="一键安装并启动 systemd 服务（需 root）")
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
    if command == "report":
        return _command_report(args)
    if command == "deploy":
        return _command_deploy(args)
    if command == "cert":
        return _command_cert(args)
    print(f"未知命令: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())