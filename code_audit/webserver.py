"""Loopback-only Flask UI, with one in-memory collection task."""

import copy
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, render_template, request

from .config import configured_secrets
from .control import Cancellation
from .presentation import export_archive, result_html
from .service import CollectionLock, collection_targets, run_collection, run_comparison
from .storage import file_stamp, now, redact, snapshot_index


class Task:
    def __init__(self, secrets_to_hide=(), prompt_timeout=120):
        self.cancellation = Cancellation()
        self.id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.events = []
        self.active = {}
        self.state = "running"
        self.result = None
        self.error = None
        self.question = None
        self.answer = threading.Event()
        self.secrets = secrets_to_hide
        self.prompt_timeout = prompt_timeout
        self.heartbeat_stop = threading.Event()
        self.thread = None

    def __call__(self, label, message, error=False):
        with self.lock:
            self.active[label] = (str(message), time.monotonic(), time.monotonic())
            self.events.append(
                redact(
                    {
                        "time": now(),
                        "label": label,
                        "message": str(message),
                        "error": error,
                    },
                    self.secrets,
                )
            )
            self.events[:] = self.events[-2000:]

    def done(self, label):
        with self.lock:
            self.active.pop(label, None)

    def prompt(self, message):
        with self.lock:
            self.cancellation.check()
            self.question = {
                "id": uuid.uuid4().hex,
                "message": redact(message, self.secrets),
                "expires_at": time.time() + self.prompt_timeout,
                "answer": None,
            }
            self.answer.clear()
            started = time.monotonic()
        accepted = self.answer.wait(self.prompt_timeout)
        with self.lock:
            decision = self.question["answer"] if accepted else False
            self.question = None
            elapsed = time.monotonic() - started
            for label, (text, began, last) in list(self.active.items()):
                self.active[label] = (text, began + elapsed, last + elapsed)
            self.cancellation.check()
            if not accepted:
                self("SSH", "主机密钥确认超时，未接受该密钥", error=True)
            return "yes" if decision else "no"

    def confirm(self, question_id, accepted):
        with self.lock:
            if (
                not self.question
                or self.question["id"] != question_id
                or self.question["answer"] is not None
                or time.time() >= self.question["expires_at"]
            ):
                raise ValueError("确认已过期或已处理，请刷新任务状态")
            if not isinstance(accepted, bool):
                raise ValueError("请明确接受或拒绝密钥")
            self.question["answer"] = accepted
            self.answer.set()

    def cancel(self):
        with self.lock:
            if self.state not in ("running", "stopping"):
                raise ValueError("采集任务已结束")
            self.state = "stopping"
            self.cancellation.event.set()
            self("采集", "正在停止，关闭 SSH 连接；GitHub 请求最多等待当前 API 超时")
            if self.question:
                self.question["answer"] = False
                self.answer.set()
        self.cancellation.cancel()

    def heartbeat(self):
        while not self.heartbeat_stop.wait(1):
            with self.lock:
                if self.question:
                    continue
                for label, (message, began, last) in list(self.active.items()):
                    current = time.monotonic()
                    if current - last >= 5:
                        self.events.append(
                            {
                                "time": now(),
                                "label": label,
                                "message": redact(
                                    message
                                    + "；已等待 "
                                    + str(int(current - began))
                                    + " 秒……",
                                    self.secrets,
                                ),
                                "error": False,
                            }
                        )
                        self.events[:] = self.events[-2000:]
                        self.active[label] = (message, began, current)

    def view(self):
        with self.lock:
            return copy.deepcopy(
                {
                    "id": self.id,
                    "state": self.state,
                    "events": self.events,
                    "result": self.result,
                    "error": self.error,
                    "question": self.question,
                }
            )


def create_app(config):
    app = Flask(
        __name__, template_folder="web", static_folder="web", static_url_path="/assets"
    )
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    token = secrets.token_urlsafe(32)
    task_lock = threading.Lock()
    current = [None]

    @app.before_request
    def check_request():
        host = urlsplit("http://" + request.host).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            abort(403, description="只允许通过回环地址或 VS Code 本地端口转发访问")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                abort(403, description="拒绝跨源操作")
            if not secrets.compare_digest(
                request.headers.get("X-Audit-Token", ""), token
            ):
                abort(403, description="会话已过期，请刷新页面")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"
        )
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(redact(str(exc), configured_secrets(config)))), 400

    @app.errorhandler(RuntimeError)
    def busy(exc):
        return jsonify(error=str(redact(str(exc), configured_secrets(config)))), 409

    @app.errorhandler(OSError)
    def file_error(exc):
        return jsonify(error=str(redact(str(exc), configured_secrets(config)))), 400

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.get("/")
    def index():
        return render_template("index.html", token=token)

    @app.get("/api/state")
    def state():
        objects = [
            {"id": key, "name": e["host_id"], "group": e["device_id"]}
            for key, e in config["_hosts"].items()
        ]
        objects.append({"id": "github", "name": "GitHub", "group": None})
        return jsonify(
            redact(
                {
                    "objects": objects,
                    "snapshots": snapshot_index(config["settings"]["snapshots_dir"]),
                    "task": current[0].view() if current[0] else None,
                },
                configured_secrets(config),
            )
        )

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def comparison(data):
        inputs = data.get("inputs")
        if not isinstance(inputs, list) or any(
            not isinstance(item, dict)
            or set(item) not in ({"name"}, {"file"}, {"data", "label"})
            for item in inputs
        ):
            raise ValueError("无效快照选择，服务端只读取快照目录内文件")
        return run_comparison(config, inputs)

    @app.post("/api/compare")
    def compare_route():
        result = comparison(body())
        return jsonify(result=result, html=result_html(result))

    @app.post("/api/export")
    def export_route():
        data = body()
        result = comparison(data)
        response = Response(export_archive(result), mimetype="application/zip")
        response.headers["Content-Disposition"] = (
            'attachment; filename="code-audit_' + file_stamp() + '.zip"'
        )
        return response

    @app.post("/api/collect")
    def collect_route():
        data = body()
        all_hosts = data.get("all", False)
        if not isinstance(all_hosts, bool):
            raise ValueError("all 必须是布尔值")
        targets = data.get("targets", [])
        collection_targets(config, targets, all_hosts)
        with task_lock:
            if current[0] and current[0].state in ("running", "stopping"):
                raise RuntimeError("已有网页采集任务正在运行，请等待完成")
            lease = CollectionLock(config.get("_lock_path")).acquire()
            task = Task(configured_secrets(config))
            current[0] = task

            def worker():
                heartbeat = threading.Thread(target=task.heartbeat, daemon=True)
                heartbeat.start()
                try:
                    result = run_collection(
                        config, targets, all_hosts, task, lease=lease
                    )
                    with task.lock:
                        task.result = result
                        task.state = (
                            "cancelled"
                            if task.cancellation.event.is_set()
                            or result.get("cancelled")
                            else "complete"
                            if result["exit_code"] == 0
                            else "partial"
                        )
                except Exception as exc:
                    with task.lock:
                        task.error = str(redact(str(exc), task.secrets))
                        task.state = (
                            "cancelled"
                            if task.cancellation.event.is_set()
                            else "failed"
                        )
                finally:
                    lease.close()
                    task.heartbeat_stop.set()

            task.thread = threading.Thread(target=worker, daemon=True)
            task.thread.start()
        return jsonify(task=task.view()), 202

    @app.get("/api/task")
    def task_route():
        return jsonify(task=current[0].view() if current[0] else None)

    @app.post("/api/stop")
    def stop_route():
        data = body()
        task = current[0]
        if not task or data.get("task") != task.id:
            raise ValueError("采集任务已过期")
        task.cancel()
        return jsonify(task=task.view())

    @app.post("/api/confirm")
    def confirm_route():
        data = body()
        task = current[0]
        if not task or data.get("task") != task.id:
            raise ValueError("采集任务已过期")
        task.confirm(data.get("question"), data.get("accepted"))
        return jsonify(ok=True)

    app.audit_task = current
    return app
