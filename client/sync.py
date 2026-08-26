"""客户端同步：push（写后即触发）、离线队列、每 1 分钟拉取、长轮询扇出即拉取、在线删除。"""

import json
import os
import uuid
from pathlib import Path

import requests

from . import config, credentials, journal
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

    def _headers(self):
        cred = credentials.require()
        return {
            "Authorization": f"Bearer {cred['token']}",
            "X-Device-Id": cred["device_id"],
        }

    def _request(self, method: str, path: str, *, json_body=None, timeout: float = 30.0):
        try:
            resp = requests.request(
                method,
                self.base_url + path,
                headers=self._headers(),
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as error:
            raise SyncError(f"无法连接服务端: {error}") from error
        if resp.status_code == 401:
            raise SyncError("鉴权失败：请检查客户端凭据。")
        if resp.status_code >= 400:
            raise SyncError(f"服务端返回 {resp.status_code}。")
        return resp.json()

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