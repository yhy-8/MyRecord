"""客户端同步：持续/完整同步、非阻塞入队 + 后台推送、离线队列、长连接（长轮询）
扇出即拉取、在线删除。

同步模型：不密集轮询云端。后台**接收线程**连接成功即完整对账，之后保持一条长连接
（长轮询挂起，服务端有更新即返回并立即应用）；记录写入只做本地入队（`enqueue`，不
联网），由后台**推送线程**（`flush_pending_loop`）入队即推、失败定期重试。连接复用
（HTTP/1.1 keep-alive）避免每次请求重做 TLS；断线自动重连重新完整对账。
"""

import hashlib
import json
import logging
import threading

import requests
import urllib3

from .atomic_write import atomic_write

from . import config, identity, journal, trust
from .file_lock import file_lock


logger = logging.getLogger(__name__)
# 长轮询单次挂起由服务端固定 25 秒（见 hub/store.wait_for_change）；客户端请求超时
# 固定取一个略大的值，保证服务端先响应，客户端不会先超时判为离线。
_LONGPOLL_REQUEST_TIMEOUT = 30.0


class SyncError(RuntimeError):
    """同步失败（网络/鉴权/协议）。"""


def _state_path():
    # 以 config.yaml 所在目录为持久化基准：打包 exe 时该目录为 exe 同级（非临时 _MEIPASS），
    # 避免状态文件随进程退出被丢弃。
    return config.config_path().parent / "state.json"


def _outbox_path():
    return config.config_path().parent / "outbox.json"


def _read_state():
    """读取本地“已见到的变更令牌”（今天文件的 sha256）；缺失/损坏回退空串。"""
    path = _state_path()
    if not path.exists():
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    token = value.get("token", "") if isinstance(value, dict) else ""
    return token if isinstance(token, str) else ""


def _save_state(token: str) -> None:
    atomic_write(_state_path(), json.dumps({"token": token}))


def _save_outbox(entries: list[dict], deletes: list[dict] | None = None) -> None:
    atomic_write(
        _outbox_path(),
        json.dumps({"entries": entries, "deletes": deletes or []}, ensure_ascii=False),
    )


def _load_outbox() -> list[dict]:
    path = _outbox_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return list(value.get("entries", [])) if isinstance(value, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def _load_deletes() -> list[dict]:
    """离线待推送的删除（tombstone + 被删正文）。"""
    path = _outbox_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return list(value.get("deletes", [])) if isinstance(value, dict) else []
    except (OSError, ValueError, TypeError):
        return []


class SyncClient:
    def __init__(self, server_url: str | None = None):
        self.base_url = (server_url or config.load()["client"]["server_url"]).rstrip("/")
        self._session: requests.Session | None = None
        self._session_key = None
        # 入队信号：输入线程 enqueue 后唤醒后台推送线程，输入线程绝不做网络 I/O。
        self._outbox_event = threading.Event()
        # 串行化 full_sync 与后台推送（两者都会 send_pending），避免并发重复推送/写库。
        self._sync_lock = threading.Lock()
        # 接收/发送两个后台线程会并发调用 _client()，加锁避免重复建 Session/互相覆盖。
        self._session_lock = threading.Lock()

    # ---------- 底层请求 ----------

    def _client(self) -> requests.Session:
        """返回按当前固定证书构造的 Session（固定证书变化时重建）。

        已固定证书时挂 PinnedHTTPAdapter：只认这张证书（链）、不校验 IP/主机名，
        因此自签证书 SAN 不含连接 IP 时也能正常严格校验（TOFU 首次确认后即固定）。
        尚未固定证书时不校验（仅发生在 TOFU 确认前的探测；正常启动会先 ensure_trust）。
        """
        cafile = trust.pinned_cert()
        key = str(cafile) if cafile else ""
        with self._session_lock:
            if self._session is not None and self._session_key == key:
                return self._session
            session = requests.Session()
            if cafile:
                session.mount("https://", trust.PinnedHTTPAdapter(str(cafile)))
            else:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                session.verify = False
            self._session, self._session_key = session, key
            return session

    def ensure_trust(self) -> bool:
        """建立/确认服务端证书信任（TOFU）；返回 False 表示用户拒绝，调用方应退出。"""
        return trust.ensure_trusted(self.base_url)

    def _headers(self):
        cred = identity.load()
        if not cred:
            raise SyncError("未配置凭据，仅本地记录；上线前请先写入 credentials.json。")
        token = str(cred["token"])
        # HTTP 头必须是 latin-1 可编码的：中文占位/误粘的串会在 requests 构造头时抛
        # UnicodeEncodeError（不属于 RequestException，会穿透成崩溃），这里提前给出可读错误。
        if not token.isascii():
            raise SyncError(
                "凭据 token 含非 ASCII 字符，请检查 credentials.json："
                "token 应为服务端签发的随机字符串（见 credentials.example.json）。"
            )
        return {
            "Authorization": f"Bearer {token}",
            "X-Device-Id": identity.device_name(),
        }

    def _request(self, method: str, path: str, *, json_body=None, timeout: float = 30.0):
        try:
            resp = self._client().request(
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

    def _request_payload(self, method: str, path: str, *, json_body=None, timeout: float = 30.0):
        """返回 (status_code, json_payload)；401 抛鉴权错误，其余 HTTP 状态不抛（供 422 探测）。"""
        try:
            resp = self._client().request(
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
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        return resp.status_code, payload

    # ---------- 连接探测 ----------

    def probe(self) -> dict:
        """区分「能否连到服务端」与「是否持有凭据」两个独立维度。

        - connected: 网络/TLS 是否可建立（用无需鉴权的 /api/health 探测）。
        - has_credentials: 本地是否已写入凭据 token（能否修改数据的前提）。
        - error: connected=False 时的失败原因。

        能连上服务端不代表有改数据的凭据；有凭据也不代表当前在线——两者独立。
        """
        connected = False
        error = ""
        try:
            resp = self._client().get(
                self.base_url + "/api/health",
                timeout=5.0,
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
            # 本地“今天”文件也是权威：只补齐缺失条目、按墓碑移除，绝不整文件重写
            # （与服务端“文件是权威”一致；手工编辑不会被远端覆盖）。
            journal.apply_delta(entries, tombstones)
            _save_state(str(delta.get("token", _read_state())))

    # ---------- push / 离线队列 ----------

    def _store_in_outbox(self, entry: dict) -> None:
        """把条目写入离线队列（本地、快速，不做任何网络 I/O）。

        仅“今天”（UTC+8）的条目才进队列；`date < 今天` 的过期条目不推（由 send_pending 作废）。
        """
        with file_lock(_outbox_path()):
            outbox = _load_outbox()
            deletes = _load_deletes()
            if entry.get("date") == journal.today_utc8():
                if not any(e.get("entry_id") == entry["entry_id"] for e in outbox):
                    outbox.append(entry)
            else:
                # 过期/异常：不进队列（send_pending 也会过滤作废）。
                logger.warning(
                    "push_new_expired skip entry_id=%s date=%s",
                    entry.get("entry_id"),
                    entry.get("date"),
                )
            _save_outbox(outbox, deletes)

    def _store_delete_in_outbox(self, delete: dict) -> None:
        """把一条删除（墓碑 + 被删正文）写入离线队列（本地、快速，不联网）。"""
        with file_lock(_outbox_path()):
            outbox = _load_outbox()
            deletes = _load_deletes()
            if not any(d.get("entry_id") == delete["entry_id"] for d in deletes):
                deletes.append(delete)
            _save_outbox(outbox, deletes)

    def enqueue_delete(self, entry: dict) -> None:
        """本地入队一条删除并唤醒后台推送线程（**不联网、不阻塞**）。

        ``entry`` 是被删条目（正文一并带上，即使云端从未见过它，也能进服务端垃圾桶）。
        """
        self._store_delete_in_outbox(
            {
                "entry_id": entry["entry_id"],
                "date": entry.get("date", ""),
                "entry": entry,
            }
        )
        self._outbox_event.set()

    def enqueue(self, entry: dict) -> None:
        """本地入队并唤醒后台推送线程（**不联网、不阻塞**）。

        客户端主输入线程专用：记录先落盘、再入队即返回；能否立即推送、何时重试，
        全部交给后台推送线程（flush_pending_loop）。即使启动时连不上服务端，
        后续每条记录也不会因网络而卡顿。
        """
        self._store_in_outbox(entry)
        self._outbox_event.set()

    def flush_pending_loop(self, interval: float = 5.0) -> None:
        """后台推送循环：入队即推；失败则每 interval 秒重试（直到成功或过期作废）。

        运行在 daemon 线程，永不向外抛异常（网络/磁盘错误只记日志并保持队列）。
        send_pending 在 outbox 为空时直接返回、不发请求，因此空转的成本极低。
        """
        while True:
            self._outbox_event.wait(timeout=interval)
            self._outbox_event.clear()
            try:
                with self._sync_lock:
                    self.send_pending()
            except Exception as error:  # noqa: BLE001 - 后台线程必须自愈，绝不退出
                # debug 而非 warning：离线时每 interval 秒会失败一次，warning 会刷屏。
                logger.debug("flush_pending_failed error=%s", error)

    def push_new(self, entry: dict) -> None:
        """先把条目写入 outbox，再尝试立即 push。离线则留在队列。

        ``enqueue`` 的非阻塞版是 cli 的默认路径；本方法保留给需要“写后即确认”的调用方。
        """
        self._store_in_outbox(entry)
        try:
            self.send_pending()
        except SyncError:
            pass  # 离线：保留队列，等待下次拉取/推送补齐

    def send_pending(self) -> dict:
        """冲刷离线队列：仅发“今天”的条目与删除；过期（date < 今天）直接作废清除。

        网络请求在 outbox 锁外进行：即使服务端慢或断连，也不一直占用 outbox 锁，
        其余线程（写入 / 后台同步）仍可继续维护离线队列。
        """
        with file_lock(_outbox_path()):
            outbox = _load_outbox()
            deletes = _load_deletes()
            today = journal.today_utc8()
            # 过期作废：date != 今天的条目/删除从 outbox 永久移除（过期或异常）。
            pending = [e for e in outbox if e.get("date") == today]
            pending_deletes = [d for d in deletes if d.get("date") == today]
            if len(pending) != len(outbox) or len(pending_deletes) != len(deletes):
                _save_outbox(pending, pending_deletes)
            if not pending and not pending_deletes:
                return {"accepted": [], "token": _read_state(), "entries": [], "tombstones": []}
        status, delta = self._request_payload(
            "POST",
            "/api/sync/push",
            json_body={
                "entries": pending,
                "deletes": pending_deletes,
                "token": _read_state(),
            },
        )
        if status >= 400 and status != 422:
            # 404/500 等非预期，交由上层当作网络问题重试（outbox 保留）。
            raise SyncError(f"服务端返回 {status}。")
        rejected = set((delta or {}).get("rejected", []) or [])
        accepted = set((delta or {}).get("accepted", []) or [])
        # 422：服务端已处理但未入库的 id（含整批拒绝时仍已应用的删除）都要清出队列。
        removed = rejected | accepted if status == 422 else accepted
        with file_lock(_outbox_path()):
            # 只移除本次已确认处理（被接受/被拒作废）的条目与删除；
            # 保留网络请求期间其它线程新增到 outbox 的条目。
            current = _load_outbox()
            current_deletes = _load_deletes()
            _save_outbox(
                [e for e in current if e["entry_id"] not in removed],
                [d for d in current_deletes if d["entry_id"] not in removed],
            )
        if status == 422:
            return {
                "accepted": sorted(accepted),
                "token": _read_state(),
                "entries": [],
                "tombstones": [],
                "rejected": sorted(rejected),
            }
        self._apply_delta(delta)
        return delta

    # ---------- 完整同步（启动 / 重连自动对账） ----------

    def full_sync(self) -> None:
        """完整同步一次：先冲刷离线队列（只今天、丢弃过期），再拉取今天全量合并进本地，
        再按哈希清单对历史日做整文件校验（云端权威覆盖本地），最后同步报告。

        用于客户端启动链接云端、以及后台重连自动对账。拉取今天全量（不受令牌影响）
        并只补齐本地缺失、按墓碑移除，而非依赖令牌/游标——避免「本地文件丢失」
        时拉不到内容，也避免覆盖本地已有（可能手工编辑过的）内容。
        """
        self.send_pending()
        self.reconcile()
        self.sync_history_files()
        self.sync_reports()

    def run_full_sync(self) -> None:
        """与后台推送循环互斥地做一次完整同步（供后台接收线程/启动调用）。"""
        with self._sync_lock:
            self.full_sync()

    def reconcile(self) -> None:
        """从服务端权威状态完整对账：拉取“今天”的全部条目与墓碑，合并进本地“今天”文件。

        只补齐本地缺失的条目、按墓碑移除已删条目（`apply_delta`），**不整文件重建**——
        本地“今天”文件同样是权威，对账不做覆盖，避免抹掉本地已有内容。
        """
        delta = self._request("GET", "/api/sync/pull")
        self._apply_delta(delta)

    # ---------- 拉取 / 长轮询 ----------

    def pull(self) -> None:
        delta = self._request("GET", f"/api/sync/pull?token={_read_state()}")
        self._apply_delta(delta)

    def longpoll(self) -> bool:
        """阻塞长轮询；有新数据返回 True，超时返回 False。"""
        delta = self._request(
            "GET",
            f"/api/sync/longpoll?token={_read_state()}",
            timeout=_LONGPOLL_REQUEST_TIMEOUT,
        )
        changed = bool(delta.get("entries") or delta.get("tombstones"))
        self._apply_delta(delta)
        return changed

    # ---------- 删除 ----------

    # ---------- 历史日整文件校验 ----------

    def _fetch_record_file(self, name: str) -> str | None:
        """拉取 /api/records/<name> 整文件原文（name 为日期或特殊名 ``trash``）；异常返回 None。"""
        try:
            resp = self._client().get(
                self.base_url + "/api/records/" + name,
                headers=self._headers(),
                timeout=30.0,
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        return resp.text

    def _file_sha256(self, path) -> str:
        """返回本地文件的 SHA-256；不可读/不存在返回空串（视为需要下载）。

        对规范化后的文本取哈希（read_text 统一 \r\n -> \n），与服务端哈希及
        下发字节（content.encode("utf-8")）一致，避免 Windows 客户端因写盘时
        换行被转为 \r\n 而Hash永不收敛、启动即全量重下载历史文件。
        """
        try:
            return hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
        except (OSError, UnicodeError):
            return ""

    def sync_history_files(self) -> None:
        """历史日（date < 今天）哈希清单对账：仅对哈希不同的条目整文件覆盖。

        云端清单即权威全集（date + sha256）。仅当本地文件缺失或哈希与云端不同时才
        拉取覆盖；哈希相同即跳过，避免每次启动对全部历史日做整文件下载与逐字节比对。
        本地存在但云端不存在的历史文件（含“过期未同步的今天”被作废后残留的日子）删除。
        """
        records_dir = journal.records_dir()
        records_dir.mkdir(parents=True, exist_ok=True)
        today = journal.today_utc8()
        try:
            data = self._request("GET", "/api/records") or {}
        except SyncError:
            return  # 服务端不可达：保留本地，由后台重连时再校验
        cloud = {f["date"]: f["sha256"] for f in (data.get("files") or [])}
        # 1) 覆盖：云端存在的历史日，与本地哈希比对，仅不同才拉取整文件覆盖（cloud 为准）。
        for date in sorted(cloud):
            if date == today:
                continue
            path = records_dir / f"{date}.md"
            if path.exists() and self._file_sha256(path) == cloud[date]:
                continue
            content = self._fetch_record_file(date)
            if content is None:
                continue
            # 直接整文件覆盖（云端文件已含 <summary>，不保留本地 summary）。
            atomic_write(path, content)
            logger.info("history_file_overwrite date=%s", date)
        # 1b) 单文件垃圾桶 Records/trash.md：清单里的 `trash` 字段是与云端对账的哈希，整文件覆盖。
        #     离线删除的正文已由 send_pending 先推给服务端，云端为准不会丢；
        #     服务端未提供 trash 哈希（如未渲染）时保留本地，不删。
        trash_sha = data.get("trash")
        if trash_sha is not None:
            trash = records_dir / "trash.md"
            if not (trash.exists() and self._file_sha256(trash) == trash_sha):
                content = self._fetch_record_file("trash")
                if content is not None:
                    atomic_write(trash, content)
                    logger.info("trash_file_overwrite")
        # 2) 删除：本地存在但云端没有的文件（不保护“今天”，今天由对账/推送维护）。
        #    date < 今天且不在云端 => 作废；date > 今天（未来日期）也不在云端 => 异常残留，一并清理。
        #    trash.md 不是日期文件，由上面的 trash 哈希单独对账，这里跳过。
        local_files = {p.stem for p in records_dir.glob("*.md")}
        for date in sorted(local_files):
            if date == today or date == "trash":
                continue
            if date in cloud:
                continue
            try:
                (records_dir / f"{date}.md").unlink()
                logger.info("history_file_removed date=%s", date)
            except OSError:
                pass

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
            resp = self._client().get(
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
        """按云端报告哈希清单把报告同步到本地 AnalysisReports（不做 /v 查看）。

        清单即权威全集（rel + sha256）：仅对本地副本缺失或哈希与云端不同的报告
        拉取覆盖，未变即跳过。同一时间段只保留最新生成。云端不存在的本地
        副本一并清理（与 records 哈希对账一致：云端为准），避免残留旧报告。
        """
        data = self._request("GET", "/api/reports") or {}
        cloud = {f["rel"]: f["sha256"] for f in (data.get("files") or [])}
        base = config.load()["client"]["analysis_dir"]
        base.mkdir(parents=True, exist_ok=True)
        base_resolved = base.resolve()
        for rel in sorted(cloud):
            target = (base / rel).resolve()
            # 兜底：即使服务端返回带 ../ 的恶意相对路径，也绝不写到 analysis_dir 之外。
            if not target.is_relative_to(base_resolved):
                logger.warning("report_path_escapes_analysis_dir rel=%s", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # 同一时间段报告只保留最新生成：本地副本哈希与云端不同（服务端重新生成、
            # 内容更新、或本地副本损坏/不可读）才拉取覆盖，未变即跳过。
            if target.exists() and self._file_sha256(target) == cloud[rel]:
                continue
            content = self._report_content(rel)
            if content is None:
                continue
            atomic_write(target, content)
        # 清理：云端已不存在的本地报告副本（云端为准）。rel 由 rglob 得到，天然在
        # analysis_dir 内，不会删到目录之外；仅处理 .md（报告文件）。
        # 必须用 base_resolved 做 rglob：relative_to 是逐段文本比较，而非解析后的路径比较；
        # 未解析的 base 在 Windows 上可能是 8.3 短名（如 RUNNER~1），与 resolve() 后的
        # 长名（runneradmin）即使指向同一目录，文本也不同，会让 relative_to 抛 ValueError。
        local = {
            p.relative_to(base_resolved).as_posix()
            for p in base_resolved.rglob("*.md")
            if p.is_file()
        }
        for rel in sorted(local - set(cloud)):
            try:
                (base / rel).unlink()
                logger.info("report_removed rel=%s", rel)
            except OSError:
                pass
