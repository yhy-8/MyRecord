"""客户端 TLS 信任（TOFU：首次使用即信任）与证书固定。

首次连接服务端时**不盲目信任**：把服务端出示的证书详情打印出来让用户确认，确认后把证书
落盘到 `client/server.crt` 并自动把 `config.yaml` 的 `verify` 指向它。之后每次连接都用该
证书做**证书固定（certificate pinning）**——只认这一张证书（链），不校验 IP/主机名，因此
自签直连、按 IP 连接时也能防止中间人。

若之后服务端证书变化（重签或被替换），把新旧两张证书详情都展示出来，由用户决定是否覆盖；
拒绝则结束进程（离线记录意义不大）。
"""

import hashlib
import os
import socket
import ssl
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import ParseResult, urlparse

from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from . import config
from .atomic_write import atomic_write


class TrustError(RuntimeError):
    """无法完成服务器证书信任建立（地址无效 / 网络不可达 / 证书无法解析等）。"""


class PinnedHTTPAdapter(HTTPAdapter):
    """只固定（pin）指定证书、跳过 IP/主机名校验的 HTTPS 适配器。

    客户端按 IP 直连自签服务端时，证书 SAN 通常不含该 IP，标准校验会因主机名不匹配
    失败。这里只校验「服务端出示的证书确实由本地固定证书（链）签发」，不校验地址，
    等价于证书固定——自签场景下证书本身就是身份，比主机名更直接。
    """

    def __init__(self, cafile: str, **kwargs) -> None:
        self._cafile = cafile
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        context = create_urllib3_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=self._cafile)
        pool_kwargs["ssl_context"] = context
        # urllib3 v2 会独立于 SSLContext 再做一次主机名匹配，必须显式关闭。
        pool_kwargs["assert_hostname"] = False
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


def cert_path() -> Path:
    """固定证书落盘位置：`client/server.crt`（打包后为 exe 同级目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "server.crt"
    return Path(__file__).resolve().parent / "server.crt"


def _parse_server_url(server_url: str) -> ParseResult:
    """解析并校验服务端地址：必须为 https 且含主机名（客户端强制加密，拒绝明文）。

    ``urlparse`` 对畸形地址（如未闭合的 IPv6 ``https://[::1``）会抛 ``ValueError``，
    这里统一转成可被调用方处理的 ``TrustError``，避免穿透成未捕获异常。
    """
    try:
        parsed = urlparse(server_url)
    except ValueError as error:
        raise TrustError(f"无效的服务端地址：{server_url}") from error
    if parsed.scheme != "https":
        raise TrustError(
            f"服务端地址必须为 https（客户端强制加密，拒绝明文传输；当前 {server_url}）"
        )
    if not parsed.hostname:
        raise TrustError(f"无效的服务端地址：{server_url}")
    return parsed


def _fetch_der(server_url: str, timeout: float) -> bytes:
    parsed = _parse_server_url(server_url)
    host = parsed.hostname
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise TrustError(f"无效的服务端端口：{server_url}") from error
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as error:
        raise TrustError(f"无法连接服务端 {host}:{port}：{error}") from error
    if not der:
        raise TrustError("服务端未提供证书。")
    return der


def fetch_peer_pem(server_url: str, timeout: float = 6.0) -> str:
    """抓取服务端当前出示的证书（PEM）。"""
    return ssl.DER_cert_to_PEM_cert(_fetch_der(server_url, timeout))


def fingerprint(pem: str) -> str:
    """证书 SHA-256 指纹（大写、冒号分隔），用于比对与展示。"""
    der = ssl.PEM_cert_to_DER_cert(pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


_NAME_SHORT = {
    "commonName": "CN",
    "countryName": "C",
    "organizationName": "O",
    "organizationalUnitName": "OU",
    "localityName": "L",
    "stateOrProvinceName": "ST",
}


def _format_name(name) -> str:
    parts = [
        f"{_NAME_SHORT.get(key, key)}={value}" for rdn in (name or ()) for key, value in rdn
    ]
    return ", ".join(parts) or "(无)"


def _format_date(text: str) -> str:
    try:
        return datetime.strptime(text, "%b %d %H:%M:%S %Y %Z").strftime("%Y-%m-%d")
    except ValueError:
        return text or "(未知)"


def _decode(pem: str) -> dict:
    """用标准库解析 PEM 证书字段；失败则降级为空字典（仅影响展示，不影响指纹）。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".crt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(pem)
            tmp = handle.name
        # ssl 未提供公开的「解析 PEM 为字段」接口，_test_decode_cert 是 CPython 长期稳定
        # 的私有实现；即便失效也只降级为不展示字段，指纹仍可用。
        return ssl._ssl._test_decode_cert(tmp)
    except Exception:
        return {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def describe(pem: str) -> str:
    """把证书关键字段格式化成多行文本，供用户在确认前核对。"""
    info = _decode(pem)
    san = info.get("subjectAltName") or ()
    san_text = ", ".join(f"{kind}:{value}" for kind, value in san) or "(无)"
    return "\n".join(
        [
            f"  主题 (Subject)  ：{_format_name(info.get('subject'))}",
            f"  颁发者 (Issuer) ：{_format_name(info.get('issuer'))}",
            f"  有效期          ：{_format_date(info.get('notBefore', ''))} → "
            f"{_format_date(info.get('notAfter', ''))}",
            f"  备用名 (SAN)    ：{san_text}",
            f"  SHA-256 指纹    ：{fingerprint(pem)}",
        ]
    )


def _confirm(question: str, input_func) -> bool:
    while True:
        try:
            answer = input_func(f"{question} [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("", "n", "no", "否"):
            return False
        print("请输入 y（信任）或 n（拒绝）。")


def pinned_cert() -> Path | None:
    """返回客户端应固定的服务端证书路径。

    优先取 config.yaml 的 `verify`（指向的证书），其次回退到 `client/server.crt`；
    都没有则返回 None。回退保证即使 `verify` 被清空，只要已固定过证书仍会严格校验，
    不会静默降级为不校验。
    """
    verify = config.load()["client"].get("verify")
    if verify:
        path = Path(str(verify))
        if path.is_file():
            return path
    path = cert_path()
    return path if path.is_file() else None


def _save(pem: str) -> None:
    atomic_write(cert_path(), pem)
    config.set_verify("./server.crt")


def ensure_trusted(server_url: str, *, input_func=input) -> bool:
    """确保客户端信任服务端证书；返回 False 表示用户拒绝（调用方应结束进程）。

    三种情形：
    - 本地无固定证书：展示服务端证书详情，用户确认后落盘并写 config；拒绝则退出。
    - 本地有固定证书且与当前一致：直接通过。
    - 本地有固定证书但不一致：展示新旧两证书，用户确认后覆盖；拒绝则退出。
    网络不可达时：已有固定证书则沿用（后台自动重连）；没有则无法建立信任，返回 False。

    地址非 https（或非法）一律返回 False：客户端强制加密，绝不因已有固定证书而降级明文。
    """
    try:
        _parse_server_url(server_url)
    except TrustError as error:
        print(f"[!] {error}")
        return False
    pinned = pinned_cert()
    pinned_pem = ""
    if pinned is not None:
        try:
            pinned_pem = pinned.read_text(encoding="utf-8")
            ssl.PEM_cert_to_DER_cert(pinned_pem)  # 确认本地证书可解析
        except Exception:
            pinned, pinned_pem = None, ""

    try:
        peer = fetch_peer_pem(server_url)
    except TrustError as error:
        if pinned is not None:
            print(f"[!] {error}；沿用已固定的本地证书，后台将自动重连。")
            return True
        print(f"[!] {error}；尚未建立服务器信任，无法开始记录。")
        return False

    if pinned is None:
        print("首次连接服务端，请核对以下证书；确认后将固定到本地，用于以后每次连接校验：")
        print(describe(peer))
        if not _confirm("是否信任并固定该服务端证书？", input_func):
            print("[!] 已拒绝，退出。")
            return False
        _save(peer)
        print(f"[*] 已固定证书到 {cert_path()}，并已把 config.yaml 的 verify 指向它。")
        return True

    if fingerprint(pinned_pem) == fingerprint(peer):
        return True

    print("[!] 服务端证书与本地已固定的证书不一致！可能是服务端重签了证书，也可能是中间人攻击。")
    print(f"本地已固定（{pinned}）：")
    print(describe(pinned_pem))
    print("服务端当前：")
    print(describe(peer))
    if not _confirm("是否用服务端当前证书覆盖本地已固定证书？", input_func):
        print("[!] 拒绝覆盖，退出。")
        return False
    _save(peer)
    print(f"[*] 已用服务端当前证书覆盖本地固定证书（{cert_path()}）。")
    return True
