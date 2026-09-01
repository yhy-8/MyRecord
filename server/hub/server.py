"""HTTP 同步服务（stdlib ThreadingHTTPServer，无第三方框架）。

路由：
- POST /api/sync/push        推送新条目 {entries, version}
- GET  /api/sync/pull?version=N  拉取增量
- GET  /api/sync/longpoll?version=N 长轮询增量（扇出）
- POST /api/entries/delete   在线删除当天最新一条 {date}
- GET  /api/status           中心状态
- GET  /api/reports[?kind=...] 报告列表
- GET  /api/reports/<kind>/<name> 报告内容
- GET  /api/health

鉴权：Authorization: Bearer <token> + X-Device-Id 头。
"""

import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


logger = logging.getLogger(__name__)


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


class SyncHandler(BaseHTTPRequestHandler):
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
        if parsed.path == "/api/reports":
            return self._reports_list(parse_qs(parsed.query))
        if parsed.path.startswith("/api/reports/"):
            return self._report_file(parsed.path)
        self._send_json(404, {"error": "not found"})

    def _route_post(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sync/push":
            return self._push()
        if parsed.path == "/api/entries/delete":
            return self._delete()
        if parsed.path == "/api/admin/retry":
            return self._admin_retry()
        if parsed.path == "/api/admin/model":
            return self._admin_model()
        self._send_json(404, {"error": "not found"})

    # ---------- 同步 ----------

    def _push(self):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        body = _read_json(self)
        if body is None or not isinstance(body.get("entries"), list):
            return self._send_json(400, {"error": "bad request"})
        store = self.server.store
        device = _device_id(self)
        entries = body["entries"] or []
        accepted = store.append_entries(device, entries)
        after_version = int(body.get("version", 0) or 0)
        delta = store.pull(after_version)
        logger.info(
            "sync_push device=%s sent=%d accepted=%d version=%d",
            device,
            len(entries),
            len(accepted),
            store.data["version"],
        )
        self._send_json(
            200,
            {
                "ok": True,
                "accepted": accepted,
                "version": store.data["version"],
                **delta,
            },
        )

    def _pull(self, query):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        try:
            after_version = int((query.get("version") or ["0"])[0])
        except ValueError:
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

    def _longpoll(self, query):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        try:
            after_version = int((query.get("version") or ["0"])[0])
        except ValueError:
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

    # ---------- 删除 ----------

    def _delete(self):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        body = _read_json(self)
        if body is None:
            return self._send_json(400, {"error": "bad request"})
        date = str(body.get("date") or "")
        store = self.server.store
        entry = store.latest_entry_for_date(date)
        if entry is None:
            logger.info("sync_delete device=%s date=%s deleted=none", _device_id(self), date)
            return self._send_json(
                200,
                {"ok": True, "deleted": None, "version": store.data["version"]},
            )
        store.tombstone(entry["entry_id"], _device_id(self))
        logger.info(
            "sync_delete device=%s date=%s deleted=%s version=%d",
            _device_id(self),
            date,
            entry["entry_id"],
            store.data["version"],
        )
        self._send_json(
            200,
            {
                "ok": True,
                "deleted": entry["entry_id"],
                "version": store.data["version"],
                **store.pull(entry["v"] - 1),
            },
        )

    # ---------- 状态 ----------

    def _status(self):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        snapshot = self.server.store.snapshot()
        logger.info(
            "status_request device=%s entries=%d tombstones=%d",
            _device_id(self),
            len(snapshot["entries"]),
            len(snapshot["tombstones"]),
        )
        self._send_json(
            200,
            {
                "entry_count": len(snapshot["entries"]),
                "tombstone_count": len(snapshot["tombstones"]),
                "devices": {
                    name: {} for name in self.server.store.device_names()
                },
                "automation": self.server.automation_status() or {},
                "ai": self.server.status_ai() or {},
            },
        )

    # ---------- 服务端 AI 管理 ----------

    def _admin_retry(self):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        if self.server.admin_retry is None:
            return self._send_json(501, {"error": "AI 未接入"})
        ok, message = self.server.admin_retry()
        self._send_json(200, {"ok": ok, "message": message})

    def _admin_model(self):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        if self.server.admin_set_model is None:
            return self._send_json(501, {"error": "AI 未接入"})
        body = _read_json(self)
        if body is None or not body.get("name"):
            return self._send_json(400, {"error": "bad request"})
        ok, message = self.server.admin_set_model(str(body["name"]))
        self._send_json(200, {"ok": ok, "message": message})

    # ---------- 报告 ----------

    def _reports_list(self, query):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
        kind = query.get("kind") or [""]
        kind_value = kind[0] if kind else ""
        files = self.server.list_reports(kind_value)
        logger.info("reports_list device=%s kind=%s count=%d", _device_id(self), kind_value, len(files))
        self._send_json(200, {"reports": files})

    def _report_file(self, path):
        if not _auth_ok(self):
            return self._send_json(401, {"error": "unauthorized"})
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
    server = ThreadingHTTPServer((host, port), SyncHandler)
    if ssl_context is not None:
        # 用自签证书直连 TLS：把监听 socket 包成 TLS，客户端用 verify 校验证书
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    server.store = store
    server.list_reports = list_reports or (lambda kind: [])
    server.read_report = read_report or (lambda rel: None)
    server.automation_status = automation_status or (lambda: {})
    server.admin_retry = admin_retry
    server.admin_set_model = admin_set_model
    server.status_ai = status_ai or (lambda: {})
    server.failures = {}
    return server