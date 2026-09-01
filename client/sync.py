"""客户端同步：启动/手动完整同步、写后即 push、离线队列、长连接（长轮询）扇出即拉取、在线删除。

同步模型：不密集轮询云端。启动或 /sync 时做一次完整对账；运行期间保持一条长连接
（长轮询挂起，服务端有更新即返回并立即应用），每条记录写入即触发 push。
"""

import json
import os
import uuid
from pathlib import Path

import requests

from . import config, identity, journal
from .file_lock import file_lock


class SyncError(RuntimeError):
    """同步失败（网络/鉴权/协议）。"""


def _state_path():
    return Path(__file__).resolve().parent / "state.json"


def _outbox_path():
    return Path(__file__).resolve().parent / "outbox.json"


def _read_state():
    path = _state_path()
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0) or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _save_state(version: int) -> None:
    path = _state_path()
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
    os.replace(tmp, path)


def _save_outbox(entries: list[dict]) -> None:
    path = _outbox_path()
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_outbox() -> list[dict]:
    path = _outbox_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return list(value.get("entries", [])) if isinstance(value, dict) else []
    except (OSError, ValueError, TypeError):
        return []


class SyncClient:
    def __init__(self, server_url: str | None = None):
        self.base_url = (server_url or config.load()["client"]["server_url"]).rstrip("/")

    # ---------- 底层请求 ----------

    def _verify(self):
        """TLS 服务器证书校验：返回路径或 False。

        - 空/未配置 → False（跳过校验，适用于 http 明文或仅内网信任）
        - 某路径 → 交给 requests 校验该 CA/自签证书
        """
        return config.load()["client"].get("verify") or False

    def _headers(self):
        cred = identity.load()
        if not cred:
            raise SyncError("未配置凭据，仅本地记录；上线前请先写入 credentials.json。")
        return {
            "Authorization": f"Bearer {cred['token']}",
            "X-Device-Id": identity.device_name(),
        }

    def _request(self, method: str, path: str, *, json_body=None, timeout: float = 30.0):
        try:
            resp = requests.request(
                method,
                self.base_url + path,
                headers=self._headers(),
                json=json_body,
                timeout=timeout,
                verify=self._verify(),
            )
        except requests.RequestException as error:
            raise SyncError(f"无法连接服务端: {error}") from error
        if resp.status_code == 401:
            raise SyncError("鉴权失败：请检查客户端凭据。")
        if resp.status_code >= 400:
            raise SyncError(f"服务端返回 {resp.status_code}。")
        return resp.json()

    # ---------- 连接探测 ----------

    def probe(self) -> dict:
        """区分「能否连到服务端」与「是否持有凭据」两个独立维度。

        - connected: 网络/TLS 是否可建立（用无需鉴权的 /api/health 探测）。
        - has_credentials: 本地是否已写入凭据 token（能否修改数据的前提）。
        - error: connected=False 时的失败原因。

        过去启动时无条件打印「已连接服务端」，把「仅配置了服务器地址」误当成
        「已连接」；服务端未启动也显示已连接。这里先真实探测，避免误报。
        能连上服务端不代表有改数据的凭据；有凭据也不代表当前在线——两者独立。
        """
        connected = False
        error = ""
        try:
            resp = requests.get(
                self.base_url + "/api/health",
                timeout=5.0,
                verify=self._verify(),
            )
            connected = resp.status_code == 200
            if not connected:
                error = f"服务端返回 {resp.status_code}"
        except requests.RequestException as exc:
            # 用简明原因，避免完整异常串（含 host/port、可能换行）撑破界面。
            error = {
                requests.exceptions.ConnectTimeout: "连接超时",
                requests.exceptions.ReadTimeout: "读取超时（无响应）",
                requests.exceptions.ConnectionError: "连接被拒绝或网络不可达",
            }.get(type(exc), type(exc).__name__)
        return {
            "connected": connected,
            "has_credentials": bool(identity.load()),
            "error": error,
        }

    # ---------- 对账应用 ----------

    def _apply_delta(self, delta: dict) -> None:
        entries = delta.get("entries", []) or []
        tombstones = delta.get("tombstones", []) or []
        with file_lock(_state_path()):
            journal.apply_delta(entries, tombstones)
            _save_state(int(delta.get("version", _read_state())))

    # ---------- push / 离线队列 ----------

    def push_new(self, entry: dict) -> None:
        """先把条目写入 outbox（写后即触发），再尝试立即 push。离线则留在队列。"""
        with file_lock(_outbox_path()):
            outbox = _load_outbox()
            if not any(e.get("entry_id") == entry["entry_id"] for e in outbox):
                outbox.append(entry)
            _save_outbox(outbox)
        try:
            self.send_pending()
        except SyncError:
            pass  # 离线：保留队列，等待下次拉取/推送补齐

    def send_pending(self) -> dict:
        with file_lock(_outbox_path()):
            outbox = _load_outbox()
            if not outbox:
                return {"accepted": [], "version": _read_state(), "entries": [], "tombstones": []}
            delta = self._request(
                "POST",
                "/api/sync/push",
                json_body={"entries": outbox, "version": _read_state()},
            )
            accepted = set(delta.get("accepted", []) or [])
            remaining = [e for e in outbox if e["entry_id"] not in accepted]
            _save_outbox(remaining)
        self._apply_delta(delta)
        return delta

    # ---------- 完整同步（启动 / 手动 /sync） ----------

    def full_sync(self) -> None:
        """完整同步一次：先冲刷离线队列，再拉取对账，再同步报告。

        用于客户端启动时链接云端、以及手动 /sync 命令。
        """
        self.send_pending()
        self.pull()
        self.sync_reports()

    # ---------- 拉取 / 长轮询 ----------

    def pull(self) -> None:
        delta = self._request("GET", f"/api/sync/pull?version={_read_state()}")
        self._apply_delta(delta)

    def longpoll(self) -> bool:
        """阻塞长轮询；有新数据返回 True，超时返回 False。"""
        delta = self._request(
            "GET",
            f"/api/sync/longpoll?version={_read_state()}",
            timeout=config.load()["client"]["longpoll_timeout_seconds"] + 5.0,
        )
        changed = bool(delta.get("entries") or delta.get("tombstones"))
        self._apply_delta(delta)
        return changed

    # ---------- 删除 ----------

    def delete_latest(self, date: str) -> str | None:
        delta = self._request(
            "POST", "/api/entries/delete", json_body={"date": date}
        )
        self._apply_delta(delta)
        return delta.get("deleted")

    # ---------- 状态 ----------

    def status(self) -> dict:
        return self._request("GET", "/api/status")

    # ---------- 服务端 AI 管理 ----------

    def admin_retry(self) -> dict:
        return self._request("POST", "/api/admin/retry")

    def admin_set_model(self, name: str) -> dict:
        return self._request("POST", "/api/admin/model", json_body={"name": name})

    # ---------- 报告同步（客户端本地保存完整副本） ----------

    def _report_content(self, rel: str) -> str | None:
        """拉取单个报告正文（服务端返回 Markdown 文本）。"""
        try:
            resp = requests.get(
                self.base_url + "/api/reports/" + rel,
                headers=self._headers(),
                timeout=30.0,
                verify=self._verify(),
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        return resp.text

    def sync_reports(self) -> None:
        """拉取缺失报告文件到本地 AnalysisReports（不做 /v 查看）。"""
        remote = (self._request("GET", "/api/reports") or {}).get("reports", []) or []
        if not remote:
            return
        base = config.load()["client"]["analysis_dir"]
        base.mkdir(parents=True, exist_ok=True)
        for rel in remote:
            target = base / rel
            if target.exists() and target.stat().st_size > 0:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self._report_content(rel)
            if content is None:
                continue
            tmp = target.with_name(target.name + ".syncing.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)