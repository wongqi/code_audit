"""Read-only SSH and GitHub collection; service.py persists the returned snapshots."""

import base64
import concurrent.futures
import hashlib
import json
import queue
import select
import shlex
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import configured_secrets, host_options, resolve_host, secret
from .control import Cancellation, CollectionCancelled
from .storage import new_snapshot, normalize_repo, now, redact

HOST_LOCK = threading.Lock()
PROMPT_CONTEXT = threading.local()


def key_fingerprint(key):
    if key is None:
        return "未记录"
    digest = (
        base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    )
    return key.get_name() + " SHA256:" + digest


def host_key_alias(identifier, entry, ssh_config):
    """Use OpenSSH aliases when set; isolate private addresses behind jump hosts."""
    for name in dict.fromkeys(
        (identifier, entry["host_id"], entry["config"]["ssh"]["host"])
    ):
        alias = ssh_config.lookup(name).get("hostkeyalias")
        if alias:
            return alias
    if entry["config"]["ssh"].get("jump"):
        return "code-audit-" + quote(identifier, safe="")
    return None


class HostKeyPrompts:
    """Let worker threads request trust; only the main thread reads stdin."""

    def __init__(self, progress=None):
        self.progress = progress
        self.pending = queue.Queue()
        self.decisions = {}
        self.waited = 0.0
        self.paused_at = None
        self.lock = threading.Lock()
        self.closed = False

    @property
    def elapsed(self):
        return self.waited + (
            time.monotonic() - self.paused_at if self.paused_at is not None else 0.0
        )

    def ask(self, message):
        with self.lock:
            if self.closed:
                raise RuntimeError("采集已中止")
            request = concurrent.futures.Future()
            self.pending.put((message, request))
        return request.result()

    def service(self):
        try:
            message, request = self.pending.get_nowait()
        except queue.Empty:
            return
        started = time.monotonic()
        self.paused_at = started
        failure = None
        answer = None
        try:
            if message not in self.decisions:
                if self.progress is not None and hasattr(self.progress, "prompt"):
                    answer = self.progress.prompt(message)
                else:
                    sys.stderr.write(message)
                    sys.stderr.flush()
                    answer = input()
                self.decisions[message] = answer
            answer = self.decisions[message]
        except EOFError:
            failure = RuntimeError(
                "无法读取主机密钥确认，请在交互终端输入 yes 并按回车"
            )
        except Exception as exc:
            failure = exc
        except BaseException:
            request.set_exception(RuntimeError("主机密钥确认已中止"))
            raise
        finally:
            self.waited += time.monotonic() - started
            self.paused_at = None
        if failure:
            request.set_exception(failure)
        else:
            request.set_result(answer)

    def close(self):
        with self.lock:
            self.closed = True
            while True:
                try:
                    _, request = self.pending.get_nowait()
                except queue.Empty:
                    break
                request.set_exception(RuntimeError("采集已中止"))


def collect_task(config, identifier, progress, prompts, cancellation):
    PROMPT_CONTEXT.cancellation = cancellation
    PROMPT_CONTEXT.broker = prompts
    try:
        return collect_host(config, identifier, progress=progress)
    finally:
        del PROMPT_CONTEXT.broker
        del PROMPT_CONTEXT.cancellation


def collect_host(config, host_id, progress=None):
    # Lazy import: offline compare never imports Paramiko.
    import paramiko

    host_id = resolve_host(config, host_id)
    emit = lambda message, error=False: (
        progress(host_id, message, error=error) if progress else None
    )
    snapshot = new_snapshot("host")
    snapshot.update(
        host={"id": config["_hosts"][host_id]["host_id"], "qualified_id": host_id},
        repositories=[],
        code_paths=[],
    )
    clients = []
    callbacks = []
    cancellation = (
        getattr(PROMPT_CONTEXT, "cancellation", None)
        or getattr(progress, "cancellation", None)
        or Cancellation()
    )
    prompts = getattr(PROMPT_CONTEXT, "broker", None)
    initial_waited = prompts.elapsed if prompts else 0.0
    local_waited = 0.0
    deadline = time.monotonic() + config["settings"]["host_timeout"]
    settings = config["settings"]
    known_hosts = Path("~/.ssh/known_hosts").expanduser()

    def remaining():
        cancellation.check()
        value = (
            deadline
            - time.monotonic()
            + local_waited
            + (prompts.elapsed - initial_waited if prompts else 0.0)
        )
        if value <= 0:
            raise TimeoutError("主机采集超时")
        return value

    class ConfirmPolicy(paramiko.MissingHostKeyPolicy):
        def __init__(self, identifier, endpoint):
            self.identifier = identifier
            self.endpoint = endpoint

        def missing_host_key(self, client, hostname, key):
            nonlocal local_waited
            with HOST_LOCK:
                if known_hosts.exists():
                    client.load_host_keys(str(known_hosts))
                existing = client.get_host_keys().lookup(hostname)
                if existing:
                    if existing.get(key.get_name()) != key:
                        raise paramiko.BadHostKeyException(
                            hostname, key, existing.get(key.get_name())
                        )
                    return
                description = (
                    self.identifier
                    + "（"
                    + self.endpoint
                    + "；密钥记录名 "
                    + hostname
                    + "）"
                )
                if not sys.stdin.isatty() and not (
                    progress is not None and hasattr(progress, "prompt")
                ):
                    raise paramiko.SSHException(
                        "未知主机密钥："
                        + description
                        + "；"
                        + key_fingerprint(key)
                        + "；请先交互连接或配置 known_hosts"
                    )
                message = (
                    "接受主机 "
                    + description
                    + " "
                    + key_fingerprint(key)
                    + "？请输入 yes/no 并按回车： "
                )
                if prompts:
                    answer = prompts.ask(message)
                else:
                    started = time.monotonic()
                    try:
                        if progress is not None and hasattr(progress, "prompt"):
                            answer = progress.prompt(message)
                        else:
                            sys.stderr.write(message)
                            sys.stderr.flush()
                            answer = input()
                    finally:
                        local_waited += time.monotonic() - started
                cancellation.check()
                if answer.strip().lower() != "yes":
                    raise paramiko.SSHException("未接受主机密钥")
                known_hosts.parent.mkdir(parents=True, exist_ok=True)
                client.get_host_keys().add(hostname, key.get_name(), key)
                client.save_host_keys(str(known_hosts))

    def connect(identifier, visited):
        cancellation.check()
        if identifier in visited:
            raise ValueError("SSH 跳板配置循环")
        entry = config["_hosts"][identifier]
        host = entry["config"]
        ssh = host["ssh"]
        port = int(ssh.get("port", 22))
        endpoint = ssh["host"] + ":" + str(port)
        alias = host_key_alias(identifier, entry, ssh_config)
        emit(
            "正在连接 "
            + identifier
            + " ("
            + ssh["host"]
            + ":"
            + str(ssh.get("port", 22))
            + ")……"
        )
        sock = None
        if alias:
            emit(identifier + " 使用主机密钥别名：" + alias)
        if ssh.get("jump"):
            jump_id = resolve_host(config, ssh["jump"], entry["device_id"])
            emit("通过跳板 " + jump_id + " 连接……")
            jump = connect(jump_id, visited | {identifier})
            sock = jump.get_transport().open_channel(
                "direct-tcpip",
                (ssh["host"], ssh.get("port", 22)),
                ("127.0.0.1", 0),
                timeout=min(settings["connect_timeout"], remaining()),
            )
        client = paramiko.SSHClient()
        clients.append(client)
        callbacks.append(cancellation.watch(client.close))
        client.load_system_host_keys()
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(ConfirmPolicy(identifier, endpoint))
        key = ssh.get("key_filename")
        if key:
            key_path = Path(key).expanduser()
            key = str(
                key_path if key_path.is_absolute() else Path(config["_root"]) / key_path
            )
        timeout = min(settings["connect_timeout"], remaining())
        if alias and sock is None:
            sock = socket.create_connection((ssh["host"], port), timeout=timeout)
            callbacks.append(cancellation.watch(sock.close))
        try:
            # The socket has the real destination. Paramiko uses hostname/port for
            # host-key lookup; OpenSSH HostKeyAlias is stored without a port suffix.
            client.connect(
                hostname=alias or ssh["host"],
                port=22 if alias else port,
                username=ssh.get("username"),
                password=secret(ssh.get("password")),
                key_filename=key,
                passphrase=secret(ssh.get("key_password")),
                sock=sock,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
                allow_agent=ssh.get("allow_agent", True),
                look_for_keys=ssh.get("look_for_keys", True),
            )
        except paramiko.BadHostKeyException as exc:
            remove_command = (
                "ssh-keygen -f "
                + shlex.quote(str(known_hosts))
                + " -R "
                + shlex.quote(exc.hostname)
            )
            raise RuntimeError(
                "SSH 主机密钥不匹配："
                + identifier
                + "（"
                + endpoint
                + "）；"
                + "密钥记录名："
                + exc.hostname
                + "；"
                + "本次指纹："
                + key_fingerprint(exc.key)
                + "；"
                + "已保存指纹："
                + key_fingerprint(exc.expected_key)
                + "；"
                + "记录文件："
                + str(known_hosts)
                + "；"
                + "已拒绝连接。请先核对目标主机与指纹；若为不同跳板后的相同内网地址，请配置不同 HostKeyAlias；"
                + "仅在确认旧记录失效后执行："
                + remove_command
                + "，然后重新采集并确认新密钥。程序不会自动删除或覆盖记录。"
            ) from None
        return client

    try:
        ssh_config = paramiko.SSHConfig()
        ssh_config_path = Path("~/.ssh/config").expanduser()
        if ssh_config_path.exists():
            with ssh_config_path.open(encoding="utf-8") as stream:
                ssh_config.parse(stream)
        options = host_options(config, host_id)
        snapshot["host"] = options["host"]
        snapshot["device"] = options["device"]
        client = connect(host_id, set())
        options["timeout"] = max(0.1, remaining() - 1)
        encoded = base64.b64encode(json.dumps(options).encode()).decode()
        command = "PYTHONDONTWRITEBYTECODE=1 python3 - " + shlex.quote(encoded)
        channel = client.get_transport().open_session(timeout=remaining())
        channel.settimeout(remaining())
        channel.exec_command(command)
        channel.sendall(Path(__file__).with_name("probe.py").read_bytes())
        channel.shutdown_write()
        output, errors = bytearray(), bytearray()
        pending = bytearray()

        def read_event(line):
            try:
                event = json.loads(line)
                if event.get("event") == "progress":
                    message = event.get("message", "")
                    emit(message, error=message.startswith("失败："))
                    return
            except (ValueError, AttributeError):
                pass
            errors.extend(line[:2000] + b"\n")

        while True:
            remaining()
            while channel.recv_ready():
                output.extend(channel.recv(65536))
            while channel.recv_stderr_ready():
                pending.extend(channel.recv_stderr(65536))
                while b"\n" in pending:
                    line, _, tail = pending.partition(b"\n")
                    pending[:] = tail
                    read_event(line)
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            select.select([channel], [], [], min(0.1, remaining()))
        if pending:
            read_event(bytes(pending))
        status = channel.recv_exit_status()
        if status:
            raise RuntimeError(
                "远端采集失败，退出码 "
                + str(status)
                + "："
                + errors.decode(errors="replace")[:1000]
            )
        cancellation.check()
        snapshot = json.loads(output)
    except Exception as exc:
        message = "采集已停止" if cancellation.event.is_set() else str(exc)
        emit("失败：" + message, error=True)
        snapshot["errors"].append(message)
        snapshot["complete"] = False
        snapshot["finished_at"] = now()
    finally:
        for remove in callbacks:
            remove()
        for client in reversed(clients):
            client.close()
    return redact(snapshot, configured_secrets(config))


def collect_hosts(config, identifiers, progress=None, cancellation=None):
    cancellation = (
        cancellation or getattr(progress, "cancellation", None) or Cancellation()
    )
    prompts = HostKeyPrompts(progress)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(3, len(identifiers), config["settings"]["concurrency"])
    )
    jobs = {
        pool.submit(
            collect_task, config, identifier, progress, prompts, cancellation
        ): identifier
        for identifier in identifiers
    }
    try:
        while jobs:
            if cancellation.event.is_set():
                prompts.close()
            else:
                prompts.service()
            completed, _ = concurrent.futures.wait(
                jobs, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in completed:
                identifier = jobs.pop(future)
                yield identifier, future.result()
    except BaseException:
        cancellation.cancel()
        raise
    finally:
        prompts.close()
        for future in jobs:
            future.cancel()
        pool.shutdown(wait=True)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original, target = urlsplit(req.full_url), urlsplit(newurl)
        if (original.scheme, original.netloc) != (target.scheme, target.netloc):
            raise ValueError("拒绝 GitHub API 跨主机重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubClient:
    def __init__(self, config, progress=None):
        self.progress = progress
        self.cancellation = getattr(progress, "cancellation", None) or Cancellation()
        self.options = config["github"]
        self.api = self.options.get("api_url", "https://api.github.com").rstrip("/")
        parsed = urlsplit(self.api)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
        ):
            raise ValueError("GitHub API 地址必须是不含凭据的 HTTPS 地址")
        self.token = secret(self.options.get("token"))
        self.timeout = config["settings"]["github_timeout"]
        self.host = (
            "github.com"
            if parsed.hostname == "api.github.com"
            else self.options.get("web_host", parsed.hostname)
        )
        self.opener = build_opener(SameOriginRedirect())

    def pages(self, endpoint):
        items = []
        for page in range(1, 10001):
            self.cancellation.check()
            if self.progress:
                label = (
                    "仓库列表"
                    if endpoint.startswith("/orgs/")
                    else "分支列表 "
                    + endpoint.split("/branches")[0].removeprefix("/repos/")
                )
                self.progress(
                    "GitHub", "正在读取 " + label + "，第 " + str(page) + " 页……"
                )
            req = Request(
                self.api
                + endpoint
                + ("&" if "?" in endpoint else "?")
                + "per_page=100&page="
                + str(page),
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "code-audit",
                    **({"Authorization": "Bearer " + self.token} if self.token else {}),
                },
            )
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    # urllib only forwards credentials to the configured API; do not follow
                    # a redirect to a different origin with an Authorization header.
                    if urlsplit(response.url).netloc != urlsplit(self.api).netloc:
                        raise ValueError("GitHub API 重定向到了其他主机")
                    values = json.load(response)
            except HTTPError as exc:
                message = {
                    401: "认证失败",
                    403: "权限不足或限流",
                    404: "不存在或无权访问",
                    429: "限流",
                }.get(exc.code, "API 错误")
                raise RuntimeError(
                    "GitHub HTTP " + str(exc.code) + " " + message
                ) from None
            except URLError:
                raise RuntimeError("GitHub 网络连接失败") from None
            self.cancellation.check()
            if not isinstance(values, list):
                raise ValueError("GitHub API 返回格式错误")
            items.extend(values)
            if len(values) < 100:
                return items
        raise ValueError("GitHub 分页超出限制")


def collect_github(config, progress=None):
    emit = lambda message, error=False: (
        progress("GitHub", message, error=error) if progress else None
    )
    snapshot = new_snapshot("github")
    snapshot.update(
        repositories=[],
        scope={
            "all": True,
            "branches": "all",
            "organization": config["github"].get("organization"),
        },
        api_url=config["github"].get("api_url", "https://api.github.com"),
    )
    try:
        emit("正在连接 GitHub API……")
        api = GitHubClient(config, progress=progress)
        organization = config["github"].get("organization")
        if not isinstance(organization, str) or not organization.strip():
            raise ValueError("必须配置 github.organization")
        values = api.pages("/orgs/" + quote(organization, safe="") + "/repos?type=all")
        targets = {
            normalize_repo(api.host + "/" + item["full_name"]) for item in values
        }
        snapshot["scope"]["repositories"] = sorted(targets)
        emit("仓库总数：" + str(len(targets)))
        branches = 0
        for number, target in enumerate(sorted(targets), 1):
            emit(
                str(number)
                + "/"
                + str(len(targets))
                + " "
                + target
                + "：正在读取分支……"
            )
            record = {
                "identity": target,
                "branches": {},
                "complete": False,
                "errors": [],
                "started_at": now(),
            }
            snapshot["repositories"].append(record)
            try:
                parts = target.split("/")
                if len(parts) != 3 or parts[0] != api.host:
                    raise ValueError("目标不属于配置的 GitHub 主机，或仓库地址无效")
                endpoint = (
                    "/repos/"
                    + quote(parts[1], safe="")
                    + "/"
                    + quote(parts[2], safe="")
                    + "/branches"
                )
                record["branches"] = {
                    item["name"]: item["commit"]["sha"] for item in api.pages(endpoint)
                }
                record["complete"] = True
            except CollectionCancelled:
                raise
            except Exception as exc:
                record["errors"].append(str(exc))
                emit("失败：" + target + "；" + str(exc), error=True)
            record["finished_at"] = now()
            branches += len(record["branches"])
            emit(
                str(number)
                + "/"
                + str(len(targets))
                + " "
                + target
                + "："
                + ("完成" if record["complete"] else "失败")
                + "，"
                + str(len(record["branches"]))
                + " 个分支；累计 "
                + str(branches)
                + " 个分支"
            )
        api.cancellation.check()
        snapshot["complete"] = not snapshot["errors"] and all(
            r["complete"] for r in snapshot["repositories"]
        )
    except Exception as exc:
        snapshot["errors"].append(str(exc))
        emit("失败：" + str(exc), error=True)
    snapshot["finished_at"] = now()
    return redact(snapshot, configured_secrets(config))
