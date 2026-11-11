"""Standalone remote probe. Keep this file Python-stdlib-only and read-only."""

import base64
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit


def timestamp():
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).isoformat(timespec="microseconds")


def canonical(value):
    # Kept local to mirror storage.normalize_repo without installing modules remotely.
    if not value:
        return None
    if "://" in value:
        parsed = urlsplit(value)
        host, path = parsed.hostname, parsed.path
    else:
        match = re.match(r"(?:[^/@]+@)?([^/:]+):(.+)$", value)
        if match:
            host, path = match.groups()
        elif value.count("/") >= 2:
            host, path = value.split("/", 1)
        else:
            return None
    if not host:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    result = host.lower() + "/" + path
    return result.lower() if host.lower() == "github.com" else result


class Probe:
    def __init__(self, options, progress=None):
        self.emit = progress or (lambda message: None)
        self.options = options
        self.deadline = time.monotonic() + options.get("timeout", 180)
        self.env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("host collection timeout")
        return seconds

    def command(self, root, *args, allow_failure=False):
        command = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            root,
        ] + list(args)
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            timeout=min(30, self.remaining()),
        )
        if result.returncode:
            if allow_failure:
                return None
            # Do not copy arbitrary git stderr (which may contain remote credentials).
            raise RuntimeError(
                "git " + args[0] + " failed, exit=" + str(result.returncode)
            )
        return result.stdout

    def text(self, root, *args, **kwargs):
        value = self.command(root, *args, **kwargs)
        return os.fsdecode(value).rstrip("\n") if value is not None else None

    @staticmethod
    def parse_status(raw):
        items = raw.split(b"\0")
        result = []
        i = 0
        while i < len(items):
            item = items[i]
            i += 1
            if not item:
                continue
            code, path = item[:2].decode("ascii"), os.fsdecode(item[3:])
            entry = {"code": code, "path": path}
            if "R" in code or "C" in code:
                entry["original_path"] = os.fsdecode(items[i])
                i += 1
            result.append(entry)
        return result

    def state(self, root):
        return (
            self.text(root, "rev-parse", "--verify", "HEAD", allow_failure=True),
            self.text(
                root, "symbolic-ref", "--short", "-q", "HEAD", allow_failure=True
            ),
            self.command(
                root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            ),
        )

    def repo(self, root, src):
        result = {
            "code_path": src,
            "path": os.path.relpath(root, src),
            "key": root,
            "complete": False,
            "errors": [],
            "status": [],
        }
        try:
            self.emit("正在采集仓库：" + root)
            start = self.state(root)
            result["head"], result["branch"] = start[:2]
            result["status"] = self.parse_status(start[2])
            result["dirty"] = bool(result["status"])
            if not result["head"]:
                result["errors"].append("HEAD 没有有效提交")
            result["commit"] = self.text(
                root, "log", "-1", "--format=%cI%n%s", allow_failure=True
            )
            remotes = {}
            for name in (self.text(root, "remote") or "").splitlines():
                value = self.text(root, "remote", "get-url", name, allow_failure=True)
                remotes[name] = canonical(value) if value else None
            result["remotes"] = remotes
            remote = branch = None
            if result["branch"]:
                prefix = "branch." + result["branch"]
                remote = self.text(
                    root, "config", "--get", prefix + ".remote", allow_failure=True
                )
                branch = self.text(
                    root, "config", "--get", prefix + ".merge", allow_failure=True
                )
                if branch and branch.startswith("refs/heads/"):
                    branch = branch[len("refs/heads/") :]
            result["upstream"] = {"remote": remote, "branch": branch}
            result["identity"] = remotes.get("origin") or remotes.get(remote)
            tracked = bool(remote and branch)
            result["target"] = {
                "repository": remotes.get(remote if tracked else "origin"),
                "branch": branch if tracked else result["branch"],
                "source": "upstream" if tracked else "origin_same_branch",
            }
            if self.state(root) != start:
                result["errors"].append("采集中仓库 HEAD、分支或 Git 状态发生变化")
            result["complete"] = not result["errors"]
        except Exception as exc:
            result["errors"].append(str(exc))
        return result

    def collect(self):
        result = {
            "kind": "host",
            "id": str(uuid.uuid4()),
            "started_at": timestamp(),
            "device": self.options["device"],
            "host": self.options["host"],
            "scope": {"branches": "current", "code_paths": self.options["code_paths"]},
            "architecture": platform.machine(),
            "system": platform.system(),
            "errors": [],
            "code_paths": [],
            "repositories": [],
        }
        prune = {".git", "build", "install", "log", "__pycache__", ".venv"}
        visited = []
        for configured in self.options["code_paths"]:
            src = os.path.abspath(os.path.expanduser(configured))
            self.emit("正在扫描 " + src)
            record = {
                "configured_path": configured,
                "path": src,
                "complete": False,
                "errors": [],
                "discovery_links": [],
            }
            result["code_paths"].append(record)
            try:
                self.remaining()
                resolved = os.path.realpath(src)
                if any(
                    resolved == other
                    or resolved.startswith(other.rstrip("/") + "/")
                    or other.startswith(resolved.rstrip("/") + "/")
                    for other in visited
                ):
                    raise ValueError("代码路径在展开后重叠：" + src)
                visited.append(resolved)
                if not os.path.isdir(src):
                    raise FileNotFoundError("代码路径不存在：" + src)
                roots = []
                for folder, directories, files in os.walk(
                    src,
                    followlinks=False,
                    onerror=lambda error: record["errors"].append(str(error)),
                ):
                    self.remaining()
                    if ".git" in directories or ".git" in files:
                        roots.append(folder)
                        self.emit("发现仓库：" + folder)
                    directories[:] = sorted(
                        name for name in directories if name not in prune
                    )
                    record["discovery_links"].extend(
                        os.path.join(folder, name)
                        for name in directories
                        if os.path.islink(os.path.join(folder, name))
                    )
                repos = []
                for number, root in enumerate(sorted(set(roots)), 1):
                    self.emit(
                        "仓库 " + str(number) + "/" + str(len(roots)) + "：" + root
                    )
                    repo = self.repo(root, src)
                    repos.append(repo)
                    if not repo["complete"]:
                        self.emit(
                            "失败："
                            + root
                            + "；"
                            + json.dumps(repo["errors"], ensure_ascii=True)
                        )
                result["repositories"].extend(repos)
                record["complete"] = not record["errors"] and all(
                    repo["complete"] for repo in repos
                )
            except Exception as exc:
                record["errors"].append(str(exc))
            if record["errors"]:
                self.emit(
                    "失败："
                    + src
                    + "；"
                    + json.dumps(record["errors"], ensure_ascii=True)
                )
        result["complete"] = bool(result["code_paths"]) and all(
            r["complete"] for r in result["code_paths"]
        )
        result["finished_at"] = timestamp()
        return result


if __name__ == "__main__":
    options = json.loads(base64.b64decode(sys.argv[1]))

    def event(message):
        sys.stderr.write(
            json.dumps({"event": "progress", "message": message}, ensure_ascii=True)
            + "\n"
        )
        sys.stderr.flush()

    print(json.dumps(Probe(options, progress=event).collect(), ensure_ascii=True))
