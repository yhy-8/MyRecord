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
from .hub.atomic_write import atomic_write
from .hub.store import Store


def _store(data_dir: Path) -> tuple[Store, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    store = Store(state_path, data_dir / "Records", data_dir / "Trash")
    return store, data_dir


def _command_run(args: argparse.Namespace) -> int:
    cfg = config.load()
    server_cfg = cfg["server"]
    store, data_dir = _store(Path(server_cfg["data_dir"]))

    from .ai import analysis as ai_analysis
    from .ai import logging_config as ai_logging
    from .hub import backup as hub_backup

    ai_logging.configure_logging()
    # 启动即清一次存量空占位日记文件（无记录日不生成文件，见 automation._purge_empty_placeholder_days），
    # 使自动任务以「文件存在性」判定有无内容（§3.2 / §9.2）。
    ai_analysis._purge_empty_placeholder_days()

    def render_records() -> None:
        store.render_records(data_dir / "Records", data_dir / "Trash")

    def list_reports(kind: str) -> list[str]:
        base = data_dir / "AnalysisReports"
        if not base.exists():
            return []
        base_resolved = base.resolve()
        prefix = (base / (kind or "").strip("/")).resolve()
        # kind 来自客户端查询参数：只枚举 AnalysisReports 目录内的文件，
        # 剔除 ../ 等越界组合，避免列出目录之外的路径。
        if not prefix.is_relative_to(base_resolved) or not prefix.is_dir():
            return []
        return sorted(str(path.relative_to(base_resolved)) for path in prefix.rglob("*.md"))

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
        """一次自动任务循环：仅在权威数据版本变化时重渲染 Records。

        后台只是用它将 Records 与 state.json 保持同步；没有新条目/墓碑时跳过
        全量重渲染，避免每分钟对全部日期做无谓的磁盘写。新条目/墓碑已被
        Store 写后即时渲染（见 append_entries/tombstone），这里只作为兜底。
        """
        try:
            ai_analysis.run_due_automatic_tasks()
        except Exception:
            pass
        current = store.data["version"]
        if current != store.rendered_version():
            store.render_records(data_dir / "Records", data_dir / "Trash")

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
            f"请先执行 `python -m {_package_name()}.main cert --ip 服务端IP` 生成自签证书。",
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
        独立执行到期任务（日/周/月互不依赖、顺序无关）。数据备份由调度线程一并触发：
        距上次成功备份满 7 天则作为子进程执行 backup.sh（见 hub/backup.py）。"""
        while True:
            try:
                run_ai_cycle()
            except Exception:
                pass
            try:
                hub_backup.run_backup_if_due(data_dir)
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
    凭证是连接许可，重签即等价于 rotate；凭证不绑定设备，设备由各端自报本机名区分。
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
    """导入既有 Records：**整文件拷贝**，不做解析/重排。

    历史日以 `Records/*.md` 整文件为权威（可承载旧格式）。用户把旧日记
    放上云端 = 目录整体拷贝，旧格式原样保留，无需解析成条目。日期合法才拷贝到
    `data_dir/Records/<date>.md`（存在则覆盖——云端权威）。
    只做整文件拷贝、不回调 `render_records`：导入不写入 state，尤其不能重渲染
    “今天”（会用空 state 覆盖刚导入的今日文件）。
    """
    source = Path(args.records).resolve()
    if not source.is_dir():
        print(f"目录不存在: {source}", file=sys.stderr)
        return 2
    _, data_dir = _store(Path(config.load()["server"]["data_dir"]))
    records_dir = data_dir / "Records"
    records_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    skipped = 0
    for path in sorted(source.glob("*.md")):
        date = path.stem
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            skipped += 1
            continue
        content = path.read_text(encoding="utf-8")
        atomic_write(records_dir / f"{date}.md", content)  # 原样拷贝（含旧格式）
        total += 1
    print(
        f"导入完成：整文件拷贝 {total} 个 Records（来自 {source}）；"
        f"跳过 {skipped} 个非法日期文件。"
    )
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


def _deploy_dir() -> Path:
    """部署文件所在目录（server/deploy：backup.sh、systemd 单元与定时器模板）。"""
    return Path(__file__).resolve().parent / "deploy"


def _venv_dir() -> Path:
    """服务端虚拟环境目录：server/.venv（随包位置推导，支持服务端工程改名）。"""
    return Path(__file__).resolve().parent / ".venv"


def _venv_python() -> Path:
    """服务端虚拟环境中的 python 解释器（Linux 下位于 .venv/bin/python）。"""
    return _venv_dir() / "bin" / "python"


def _running_in_venv(venv_dir: Path) -> bool:
    """当前解释器是否运行在目标 venv 内。

    venv 的 bin/python 通常是到基础解释器的符号链接，不能靠 Path().resolve() 判断；
    Python 在 venv 内运行时 sys.prefix 指向该 venv 目录（且与 sys.base_prefix 不相等）。
    """
    return Path(sys.prefix).resolve() == venv_dir.resolve()


def _package_name() -> str:
    """当前 server 包的实际文件夹名，即 `python -m <名称>.main` 所用的包名。

    由部署文件所在目录反推，避免硬编码 server/：服务端工程改名后，只要以
    `python -m <新名>.main` 启动，一键部署仍能正确渲染单元与备份脚本路径。
    """
    return _deploy_dir().parent.name


def _render_systemd(interpreter: str, project_root: Path) -> str:
    """渲染 systemd 单元：解释器路径 + 工程根（使 `python -m <包名>.main run` 可解析）。

    systemd 单元是 Linux 格式，路径一律用正斜杠；Windows 上 Path 会渲染成反斜杠，
    这里用 as_posix() 归一化，避免在 Windows 上生成 `WorkingDirectory=\\srv\\...` 之类非法值。
    解释器同样归一化：_command_deploy 传入 str(venv_py)，Windows 上会带反斜杠。
    """
    return (
        "[Unit]\n"
        "Description=MyRecord server hub (sync + AI reports + fanout)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={Path(interpreter).as_posix()} -m {_package_name()}.main run\n"
        f"WorkingDirectory={project_root.as_posix()}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "User=root\n"
    )


def _api_config_status(raw: dict) -> str:
    """按 config.raw 给出活动模型 api_key 是否就绪的简短描述（不泄露密钥）。

    config.load() 返回的 raw 是 config.yaml 顶层对象（models/current_model 等）。
    deploy 用它向用户说明 API 配置状态，避免用户误以为有了 config.yaml 就有 AI 能力。
    """
    models = raw.get("models", []) if isinstance(raw, dict) else []
    if not models:
        return "未配置模型（复制 server/config.example.yaml 为 config.yaml 并填入 api_key）"
    current = raw.get("current_model")
    active = next(
        (m for m in models if isinstance(m, dict) and m.get("name") == current),
        models[0] if models else None,
    )
    if not isinstance(active, dict):
        return "模型配置无效（检查 server/config.yaml 的 models）"
    name = active.get("name", "未命名")
    if not str(active.get("api_key") or "").strip():
        return f"活动模型 {name} 的 api_key 为空，AI 暂不可用"
    return f"活动模型 {name} 已配置 api_key，AI 可用"


def _command_deploy(args: argparse.Namespace) -> int:
    """一键安装并启动 systemd 服务（需 root，仅 Linux）。

    自动完成：创建 server/.venv 虚拟环境并安装 requirements、生成自签证书、签发链接凭证
    （唯一共享 token，仅当尚无有效凭证时，避免覆盖作废旧凭证），并按当前包实际路径渲染
    systemd 单元（仅 `myrecord-server.service`；每周自动备份由服务端内置调度触发，见
    hub/backup.py），然后 `systemctl daemon-reload` 并 `start` 主服务（不 `enable` 开机自启，
    防止部署出错后重启自动拉起损坏服务）。服务端 `ExecStart` 一律用 venv 的 python 运行，
    部署完成会打印摘要：虚拟环境、自签证书、链接凭证、api_key、服务部署、数据备份六方面状态一目了然。

    模型 api_key 仍需人工填入 server/config.yaml（当前为空则无 AI 能力）；填好后只需
    `systemctl restart myrecord-server` 即可生效，无需重新部署。
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print(
            f"[!] 需要 root 权限：请用 sudo 运行，例如：sudo python -m {_package_name()}.main deploy",
            file=sys.stderr,
        )
        return 2
    # 0) 自举：当前解释器不在目标 venv 内运行时，先建 venv，再用 venv 的 python 重新执行 deploy。
    #    依赖一律装进 server/.venv（不污染默认/系统 Python），因此无需预先 pip install。
    venv_dir = _venv_dir()
    venv_py = _venv_python()
    if not _running_in_venv(venv_dir):
        if not venv_py.is_file():
            print(f"正在创建虚拟环境 {venv_dir} ...")
            subprocess.run([sys.executable, "-m", "venv", venv_dir.as_posix()], check=True)
        print(f"依赖将装进 {venv_dir}（不污染默认 Python）；改用 {venv_py} 重新执行 deploy ...")
        return subprocess.run(
            [venv_py.as_posix(), "-m", f"{_package_name()}.main", "deploy"],
            check=False,
        ).returncode
    # 1) 虚拟环境：已运行在 venv 内（本进程即 venv 的 python），装依赖并刷新。
    venv_created = not venv_py.is_file()
    if venv_created:
        print(f"正在创建虚拟环境 {venv_dir} ...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir.as_posix()], check=True)
    reqs = Path(__file__).resolve().parent / "requirements.txt"
    print("正在把服务端依赖安装到虚拟环境（venv 的 pip）...")
    subprocess.run(
        [venv_py.as_posix(), "-m", "pip", "install", "-r", reqs.as_posix()], check=True
    )
    # 2) 配置提示：config.yaml 缺失时仍可部署（默认配置、无 AI），api_key 必须人工填。
    config_missing = not config.config_path().is_file()
    if config_missing:
        print("[!] 未找到 server/config.yaml：服务将以默认配置运行（无 AI 能力）。")
        print("    请复制 config.example.yaml 为 config.yaml，并填入模型 api_key。")
    cfg = config.load()
    data_dir = Path(cfg["server"]["data_dir"])
    # 3) 自签证书（缺失才生成）。
    certfile = Path(cfg["server"]["tls"]["certfile"])
    cert_created = False
    if not certfile.is_file():
        try:
            _generate_cert(data_dir)
            cert_created = True
        except RuntimeError as error:
            print(f"[!] 生成自签证书失败：{error}", file=sys.stderr)
            return 2
    # 4) 链接凭证（唯一共享 token）：仅当尚无有效凭证时签发，避免覆盖作废旧凭证。
    store, _ = _store(data_dir)
    token = None
    if not store.active_credential():
        token = auth.new_token()
        store.register_device(_CREDENTIAL_DEVICE_LABEL, token)
        print("已签发链接凭证（唯一共享 token，服务端只存哈希，请妥善保存）：")
        print(f"token: {token}")
    else:
        print("已存在有效链接凭证，跳过签发（如需轮换请运行 token create）。")
    # 5) 渲染并安装 systemd 单元（用 venv 的 python 作为 ExecStart 解释器）。
    project_root = Path(__file__).resolve().parent.parent
    server_dest = _SYSTEMD_UNIT_PATH
    server_dest.parent.mkdir(parents=True, exist_ok=True)
    # 重新部署：同名服务单元已存在（前一进程仍在运行）时，先正确关停再覆盖最新单元。
    # 首次部署时单元尚不存在，跳过 stop（`systemctl stop` 未注册单元会报错）。
    redeployed = server_dest.exists()
    if redeployed:
        print("检测到已部署的同名服务：先关停旧服务，再覆盖新单元并重新启动。")
        subprocess.run(["systemctl", "stop", "myrecord-server"], check=False)
    server_dest.write_text(
        _render_systemd(str(venv_py), project_root), encoding="utf-8"
    )
    # 6) daemon-reload && start（主服务不 enable 开机自启；备份由服务端内置调度触发）。
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "start", "myrecord-server"], check=True)
    # 7) 部署摘要：虚拟环境/自签证书/链接凭证/api_key/服务状态一次说明到位。
    api_status = _api_config_status(cfg.get("raw"))
    print("\n================ 部署完成 ================")
    print(f"· 虚拟环境 ：{'已新建' if venv_created else '沿用已有'} {venv_dir}")
    print(f"  （服务端用 {venv_py} 运行，已切到虚拟环境）")
    print(f"· 自签证书 ：{'已生成' if cert_created else '已存在'} {certfile}")
    if token:
        print("· 链接凭证 ：已新签发（token 见上方输出；服务端只存哈希，请勿丢失）")
    else:
        print("· 链接凭证 ：已存在有效凭证，跳过签发（如需轮换请运行 token create）")
    print(f"· API 配置 ：{api_status}")
    print(
        f"· 服务部署 ：已{'覆盖并重新启动' if redeployed else '写入并启动'} "
        "myrecord-server（不启用开机自启动）"
    )
    print("· 数据备份 ：每周自动备份已内置（距上次满 7 天即作为子进程执行 backup.sh；")
    print("  备份到 server/backups/，保留最近 7 份；也可手动/定时直接运行 server/deploy/backup.sh）")
    if config_missing:
        print("· 配置提醒 ：未找到 server/config.yaml，服务以默认配置（无 AI）运行；")
        print("  复制 config.example.yaml 为 config.yaml 并填入 api_key 后 restart 即可。")
    if token:
        print("· 下一步   ：把 token 写入 client/credentials.json，并填好 server/config.yaml 的 api_key。")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动 hub 服务")
    token = sub.add_parser("token", help="链接凭证管理（create/list）")
    token.add_argument("action", choices=["create", "list"])
    imp = sub.add_parser("import", help="导入 Records 目录")
    imp.add_argument("--records", required=True)
    sub.add_parser("render", help="重新渲染全部 Records")
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