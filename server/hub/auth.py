"""设备令牌哈希与校验。

使用标准库 hashlib.scrypt（加盐、内存困难、常量时间比较）。服务端只存哈希，
令牌明文不落盘。相比计划中的 argon2id 减少第三方依赖，功能等价。
"""

import hashlib
import hmac
import re
import secrets


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16


def slugify(value: str) -> str:
    """把设备名转成安全的 device_id 标签（去空格/特殊字符）。"""
    cleaned = re.sub(r"[^0-9A-Za-z_\-]+", "-", value.strip()).strip("-")
    return cleaned[:32]


def new_token() -> str:
    """生成一次性的设备长令牌。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """返回可存储的哈希串：scrypt$N$r$p$salt$hash（十六进制）。"""
    salt = secrets.token_bytes(_SALT_LEN)
    digest = hashlib.scrypt(
        token.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_token(token: str, stored: str) -> bool:
    """常量时间校验令牌；stored 格式不符时返回 False。"""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except (ValueError, OverflowError):
        return False
    try:
        digest = hashlib.scrypt(
            token.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(digest, expected)