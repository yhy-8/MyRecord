"""客户端同步：持续/完整同步、写后即 push、离线队列、长连接（长轮询）扇出即拉取、在线删除。

同步模型：不密集轮询云端。后台线程连接成功即完整对账，之后保持一条长连接
（长轮询挂起，服务端有更新即返回并立即应用），每条记录写入即触发 push；
断线自动重连重新完整对账。
"""

import json
import logging
import warnings

import requests
import urllib3

from .atomic_write import atomic_write

from . import config, identity, journal
from .file_lock import file_lock


logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    """同步失败（网络/鉴权/协议）。"""


# verify 为空（跳过 TLS 校验）时只提示一次，避免长连接循环里反复刷屏。
_VERIFY_WARNED = False


def _state_path():
    # 以 config.yaml 所在目录为持久化基准：打包 exe 时该目录为 exe 同级（非临时 _MEIPASS），
    # 避免状态文件随进程退出被丢弃。
    return config.config_path().parent / "state.json"


def _outbox_path():
    return config.config_path().parent / "outbox.json"


def _read_state():
    path = _state_path()
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0) or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _save_state(version: int) -> None:
    atomic_write(_state_path(), json.dumps({"version": version}))


def _save_outbox(entries: list[dict]) -> None:
    atomic_write(_outbox_path(), json.dumps({"entries": entries}, ensure_ascii=False))


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

        - 空/未配置 → False（跳过校验，适用于自签证书的直连信任）；此时同时
          抑制 urllib3 的 InsecureRequestWarning，否则每次 HTTPS 请求都会把
          该警告打进 stderr，污染交互终端并在长连接循环里反复刷屏。
        - 某路径 → 交给 requests 校验该 CA/自签证书

        注意：verify 为空即关闭证书校验（默认），存在中间人风险。这里给出一次性
        显式警示，避免“不安全且静默”。
        """
        verify = config.load()["client"].get("verify")
        if not verify:
            # 只提示一次，避免长连接循环里每次请求都刷一条告警。
            global _VERIFY_WARNED
            if not _VERIFY_WARNED:
                warnings.warn(
                    "当前未配置 client.verify，TLS 证书校验已关闭（存在中间人风险）。"
                    "建议把服务端自签的 server.crt 拷到客户端并把它设为 verify 路径。",
                    UserWarning,
                    stacklevel=2,
                )
                _VERIFY_WARNED = True
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            return False
        return verify

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

    # ---------- 完整同步（启动 / 重连自动对账） ----------

    def full_sync(self) -> None:
        """完整同步一次：先冲刷离线队列，再完整对账（重建本地镜像），再同步报告。

        用于客户端启动链接云端、以及后台重连自动对账。完整对账从 version=0
        重新拉取全部条目与墓碑并重建/补齐本地 Records，而非依赖增量游标——
        避免「本地文件丢失但游标未回退」时增量 pull 拉不到内容，导致云端有
        数据却同步不下来。
        """
        self.send_pending()
        self.reconcile()
        self.sync_reports()

    def reconcile(self) -> None:
        """从服务端权威状态完整对账：拉取全部条目与墓碑，重建/补齐本地镜像。"""
        delta = self._request("GET", "/api/sync/pull?version=0")
        self._apply_delta(delta)

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
        """把云端报告同步到本地 AnalysisReports（不做 /v 查看）。

        同一时间段只保留最新生成：已有本地副本时仍校验与云端最新内容，
        不一致则覆盖（服务端重新生成后客户端同步到最新版本）。
        """
        remote = (self._request("GET", "/api/reports") or {}).get("reports", []) or []
        if not remote:
            return
        base = config.load()["client"]["analysis_dir"]
        base.mkdir(parents=True, exist_ok=True)
        base_resolved = base.resolve()
        for rel in remote:
            target = (base / rel).resolve()
            # 兜底：即使服务端返回带 ../ 的恶意相对路径，也绝不写到 analysis_dir 之外。
            if not target.is_relative_to(base_resolved):
                logger.warning("report_path_escapes_analysis_dir rel=%s", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self._report_content(rel)
            if content is None:
                continue
            # 同一时间段报告只保留最新生成：已存在本地副本时也校验与云端最新内容是否一致，
            # 不一致（服务端重新生成、内容更新、或本地副本损坏/不可读）则覆盖，不跳过旧副本。
            if target.exists():
                try:
                    local = target.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    local = None
                if local == content:
                    continue
            atomic_write(target, content)
