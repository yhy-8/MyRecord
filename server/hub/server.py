"""HTTP 同步服务（stdlib ThreadingHTTPServer，无第三方框架）。

路由：
- POST /api/sync/push        推送新条目/删除 {entries, deletes}（**仅今天**；历史日被拒绝 422）
- GET  /api/sync/pull?version=N  拉取今天全量（游标仅作变更令牌，不筛选数据）
- GET  /api/sync/longpoll?version=N 长轮询扇出（令牌落后且有变化即返回全量）
- GET  /api/status           中心状态（计数从今天的 Records 文件解析）
- GET  /api/records          哈希清单（历史日 date+sha256 + 单文件垃圾桶 trash）
- GET  /api/records/<name>   Records/<name>.md 整文件原文（日期或 trash）
- GET  /api/reports[?kind=...] 报告哈希清单（rel + sha256）
- GET  /api/reports/<kind>/<name> 报告内容
- GET  /api/health

鉴权：Authorization: Bearer <token> + X-Device-Id 头。
"""

import datetime
import hashlib
import json
import logging
import re
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


logger = logging.getLogger(__name__)

# TLS 握手超时：只建了 TCP 却不握手的半开连接（客户端睡眠/掉线、端口扫描）必须在有限
# 时间内放弃，绝不能阻塞 accept 主循环（见 SyncHandler.setup 的延迟握手）。
_TLS_HANDSHAKE_TIMEOUT = 10.0
# 单条连接的 socket 超时：空闲 keep-alive 连接与慢请求读取超过该值即关闭、释放工作线程。
# 长轮询的服务端 25 秒挂起期间不进行 socket 读，因此不受此超时影响。
_REQUEST_TIMEOUT = 35.0


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_iso_date(value: object) -> bool:
    """判断是否为合法 YYYY-MM-DD（供 `/api/records/<date>` 防路径穿越）。"""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _client_addr(handler) -> str:
    return (handler.client_address[0] if handler.client_address else "") or ""


_DEVICE_HEADER = "X-Device-Id"


def _auth_ok(handler) -> bool:
    store = handler.server.store
    auth_header = handler.headers.get("Authorization", "")
    m = re.fullmatch(r"Bearer\s+(\S+)", auth_header.strip())
    if not m:
        return False
    device_id = handler.headers.get(_DEVICE_HEADER, "")
    if not device_id:
        logger.warning("auth_missing_device addr=%s", _client_addr(handler))
        return False
    failures = handler.server.failures
    now = time.time()
    if device_id in failures and failures[device_id][1] > now:
        logger.warning("auth_locked_out device=%s addr=%s", device_id, _client_addr(handler))
        return False  # 失败锁定中
    ok = store.verify_device(device_id, m.group(1))
    if ok:
        failures.pop(device_id, None)
    else:
        count, _ = failures.get(device_id, (0, 0))
        failures[device_id] = (count + 1, now + 60 if count + 1 >= _LOCKOUT_THRESHOLD else 0)
        logger.warning("auth_failed device=%s addr=%s", device_id, _client_addr(handler))
    return ok


def _query_version(query) -> int | None:
    """解析 longpoll/pull 的 version 参数；无法解析时返回 None。"""
    try:
        return int((query.get("version") or ["0"])[0])
    except ValueError:
        return None


def _authed(method):
    """装饰器：被装饰的 handler 先做 Bearer 鉴权，默认统一返回 401。"""
    def wrapper(self, *args, **kwargs):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        return method(self, *args, **kwargs)
    return wrapper


_LOCKOUT_THRESHOLD = 5


def _read_json(handler) -> dict | None:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length) if length else b""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _device_id(handler) -> str:
    return handler.headers.get("X-Device-Id", "")


class _SyncHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 延迟 TLS 握手开关 + 精简错误日志。

    daemon_threads=True（ThreadingHTTPServer 默认），工作线程不阻塞进程退出。
    """

    tls_deferred = False

    def handle_error(self, request, client_address):
        # 默认实现把完整 traceback 打到 stderr；半开连接/端口扫描会不断刷屏。
        # 这里只记一行简明原因（异常仍在 sys.exc_info() 中）。
        logger.warning(
            "request_error addr=%s error=%s", client_address, sys.exc_info()[1]
        )


class SyncHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 允许 keep-alive：客户端一次 TLS 握手后可复用同一条连接发多个请求
    # （长连接），不必每条记录、每次拉取都重做一次 TLS 握手。
    protocol_version = "HTTP/1.1"
    timeout = _REQUEST_TIMEOUT

    def setup(self):
        # 延迟 TLS 握手到工作线程执行：serve() 用 do_handshake_on_connect=False 包裹
        # 监听 socket，accept 返回后不再做握手。否则任一客户端只建 TCP 不握手（睡眠、
        # 掉线、NAT 超时、端口扫描）都会把 accept 主循环卡死，整个服务端假死到重启。
        if getattr(self.server, "tls_deferred", False):
            self.request.settimeout(_TLS_HANDSHAKE_TIMEOUT)
            try:
                self.request.do_handshake()
            except (OSError, ssl.SSLError):
                # 由 _SyncHTTPServer.handle_error 统一记录一行简明原因后关闭连接。
                raise
        super().setup()

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send_json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # pragma: no cover - 防御性兜底
            try:
                self._send_json(500, {"error": str(error)})
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            try:
                self._send_json(500, {"error": str(error)})
            except Exception:
                pass

    def _route_get(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._send_json(200, {"ok": True})
        if parsed.path == "/api/sync/pull":
            return self._pull(parse_qs(parsed.query))
        if parsed.path == "/api/sync/longpoll":
            return self._longpoll(parse_qs(parsed.query))
        if parsed.path == "/api/status":
            return self._status()
        if parsed.path == "/api/records":
            return self._records_list()
        if parsed.path.startswith("/api/records/"):
            return self._records_file(parsed.path)
        if parsed.path == "/api/reports":
            return self._reports_list(parse_qs(parsed.query))
        if parsed.path.startswith("/api/reports/"):
            return self._report_file(parsed.path)
        self._send_json(404, {"error": "not found"})

    def _route_post(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sync/push":
            return self._push()
        if parsed.path == "/api/admin/retry":
            return self._admin_retry()
        if parsed.path == "/api/admin/model":
            return self._admin_model()
        self._send_json(404, {"error": "not found"})

    # ---------- 同步 ----------

    @_authed
    def _push(self):
        body = _read_json(self)
        if body is None or not isinstance(body.get("entries", []), list):
            return self._send_json(400, {"error": "bad request"})
        store = self.server.store
        device = _device_id(self)
        entries = body.get("entries") or []
        deletes = body.get("deletes") or []
        accepted, rejected = store.append_entries(device, entries)
        delete_accepted, delete_rejected = store.apply_deletes(device, deletes)
        accepted = accepted + delete_accepted
        rejected = rejected + delete_rejected
        logger.info(
            "sync_push device=%s entries=%d deletes=%d accepted=%d rejected=%d version=%d",
            device,
            len(entries),
            len(deletes),
            len(accepted),
            len(rejected),
            store.data["version"],
        )
        if rejected:
            # 批次内含历史日/异常条目或删除：服务器「已处理但未入库」，
            # 客户端应作废这些 id，不要重试。
            return self._send_json(
                422,
                {"ok": False, "error": "expired", "rejected": rejected, "accepted": accepted},
            )
        # 游标已降级：写端点不再下发“增量”，直接返回今天全量（客户端按 entry_id 幂等合并）。
        state = store.today_state()
        self._send_json(
            200,
            {
                "ok": True,
                "accepted": accepted,
                **state,
            },
        )

    @_authed
    def _pull(self, query):
        after_version = _query_version(query)
        if after_version is None:
            return self._send_json(400, {"error": "bad version"})
        delta = self.server.store.pull(after_version)
        logger.info(
            "sync_pull device=%s after=%d version=%d entries=%d tombstones=%d",
            _device_id(self),
            after_version,
            delta["version"],
            len(delta["entries"]),
            len(delta["tombstones"]),
        )
        self._send_json(200, delta)

    @_authed
    def _longpoll(self, query):
        after_version = _query_version(query)
        if after_version is None:
            return self._send_json(400, {"error": "bad version"})
        delta = self.server.store.wait_for_change(after_version, timeout=25.0)
        changed = bool(delta["entries"] or delta["tombstones"])
        logger.info(
            "sync_longpoll device=%s after=%d version=%d entries=%d tombstones=%d changed=%s",
            _device_id(self),
            after_version,
            delta["version"],
            len(delta["entries"]),
            len(delta["tombstones"]),
            changed,
        )
        self._send_json(200, delta)

    # ---------- 状态 ----------

    @_authed
    def _status(self):
        # 计数以「今天」的 Records 文件为准（而非 state.json 内存副本）：用户直接编辑
        # 日记文件后，/status 立即反映文件真实内容，不会因 state 与文件不一致而失真。
        entry_count, tombstone_count = self.server.store.today_file_counts()
        logger.info(
            "status_request device=%s entries=%d tombstones=%d",
            _device_id(self),
            entry_count,
            tombstone_count,
        )
        self._send_json(
            200,
            {
                "entry_count": entry_count,
                "tombstone_count": tombstone_count,
                "devices": {
                    name: {} for name in self.server.store.device_names()
                },
                "automation": self.server.automation_status() or {},
                "ai": self.server.status_ai() or {},
            },
        )

    # ---------- Records 整文件 ----------

    @_authed
    def _records_list(self):
        """GET /api/records：返回 Records 下历史日哈希清单（date+sha256）与单文件垃圾桶哈希。

        清单即权威全集：客户端据此对每个历史日算本地哈希比对，仅哈希不同才下载整文件。
        ``Records/trash.md``（单文件垃圾桶）作为清单的一个成员单独返回哈希（``trash`` 字段），
        客户端整文件对账；它不按日期命名，不进入 ``files`` 的日期列表。
        """
        store = self.server.store
        records_dir = store.records_dir
        files = []
        trash_digest = None
        if records_dir and records_dir.is_dir():
            for path in sorted(records_dir.glob("*.md")):
                # 对规范化后的文本取哈希（read_text 统一 \r\n -> \n），与
                # _records_file 下发的字节表示（content.encode("utf-8")）一致，
                # 避免 Linux 服务端 + Windows 客户端因换行差异Hash永不收敛。
                # 单个文件不可读/非 UTF-8 时仍列出该日期、sha256 置空（未知），不让整个清单
                # 接口失败，也保证清单仍是「权威全集」（§4）：否则客户端会把“清单缺失”
                # 误当成“云端已删除”，删掉本地历史副本。
                try:
                    digest = hashlib.sha256(
                        path.read_text(encoding="utf-8").encode("utf-8")
                    ).hexdigest()
                except (OSError, UnicodeError):
                    logger.warning("records_list_unreadable name=%s", path.stem)
                    digest = ""
                if path.stem == "trash":
                    trash_digest = digest
                    continue
                files.append({"date": path.stem, "sha256": digest})
        logger.info(
            "records_list device=%s count=%d trash=%s",
            _device_id(self),
            len(files),
            trash_digest is not None,
        )
        payload = {"files": files}
        if trash_digest is not None:
            payload["trash"] = trash_digest
        self._send_json(200, payload)

    @_authed
    def _records_file(self, path):
        """GET /api/records/<name>：返回 Records/<name>.md 整文件原文。

        ``name`` 为合法 YYYY-MM-DD（历史日整文件同步）或特殊名 ``trash``（单文件垃圾桶）。
        校验防止路径穿越，存在则原样回传；不存在返回 404。文件是权威内容（可承载旧格式），
        原样回传，不解析、不重排。
        """
        name = path[len("/api/records/"):].strip("/")
        store = self.server.store
        records_dir = store.records_dir
        if records_dir is None:
            return self._send_json(404, {"error": "not found"})
        if name == "trash":
            target = (records_dir / "trash.md").resolve()
        else:
            if not _valid_iso_date(name):
                return self._send_json(400, {"error": "bad date"})
            target = (records_dir / f"{name}.md").resolve()
        if not target.is_relative_to(records_dir.resolve()) or not target.is_file():
            logger.info(
                "records_file device=%s name=%s status=404", _device_id(self), name
            )
            return self._send_json(404, {"error": "not found"})
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._send_json(500, {"error": "read failed"})
        logger.info(
            "records_file device=%s name=%s bytes=%d", _device_id(self), name, len(content)
        )
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- 服务端 AI 管理 ----------

    @_authed
    def _admin_retry(self):
        if self.server.admin_retry is None:
            return self._send_json(501, {"error": "AI 未接入"})
        ok, message = self.server.admin_retry()
        self._send_json(200, {"ok": ok, "message": message})

    @_authed
    def _admin_model(self):
        if self.server.admin_set_model is None:
            return self._send_json(501, {"error": "AI 未接入"})
        body = _read_json(self)
        if body is None or not body.get("name"):
            return self._send_json(400, {"error": "bad request"})
        ok, message = self.server.admin_set_model(str(body["name"]))
        self._send_json(200, {"ok": ok, "message": message})

    # ---------- 报告 ----------

    @_authed
    def _reports_list(self, query):
        """GET /api/reports：返回云端报告哈希清单（rel + sha256）。

        清单即权威全集：客户端只对哈希不同的报告拉取覆盖，未变即跳过。
        """
        kind = query.get("kind") or [""]
        kind_value = kind[0] if kind else ""
        files = self.server.list_reports(kind_value)
        entry = []
        for rel in files:
            content = self.server.read_report(rel)
            if content is None:
                continue
            entry.append(
                {"rel": rel, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            )
        logger.info("reports_list device=%s kind=%s count=%d", _device_id(self), kind_value, len(files))
        self._send_json(200, {"files": entry})

    @_authed
    def _report_file(self, path):
        rel = path[len("/api/reports/"):].strip("/")
        content = self.server.read_report(rel)
        if content is None:
            logger.info("report_read device=%s rel=%s status=404", _device_id(self), rel)
            return self._send_json(404, {"error": "not found"})
        logger.info("report_read device=%s rel=%s bytes=%d", _device_id(self), rel, len(content))
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(
    store,
    host,
    port,
    *,
    list_reports=None,
    read_report=None,
    automation_status=None,
    admin_retry=None,
    admin_set_model=None,
    status_ai=None,
    ssl_context=None,
):
    server = _SyncHTTPServer((host, port), SyncHandler)
    if ssl_context is not None:
        # 用自签证书直连 TLS：把监听 socket 包成 TLS，客户端用 verify 校验证书。
        # do_handshake_on_connect=False 让 accept 不在主循环里做握手，改由工作线程
        # 在 SyncHandler.setup 中限时握手——避免半开连接卡死 accept 主循环。
        server.socket = ssl_context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )
        server.tls_deferred = True
    server.store = store
    server.list_reports = list_reports or (lambda kind: [])
    server.read_report = read_report or (lambda rel: None)
    server.automation_status = automation_status or (lambda: {})
    server.admin_retry = admin_retry
    server.admin_set_model = admin_set_model
    server.status_ai = status_ai or (lambda: {})
    server.failures = {}
    return server