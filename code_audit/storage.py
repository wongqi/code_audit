"""Validate, atomically save and select local snapshots."""

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

BEIJING = dt.timezone(dt.timedelta(hours=8))


def now():
    return dt.datetime.now(BEIJING).isoformat(timespec="microseconds")


def file_stamp():
    return dt.datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")


def display_time(value):
    try:
        return (
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(BEIJING)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except (TypeError, ValueError, AttributeError):
        return value or "未知时间"


def new_snapshot(kind):
    return {
        "kind": kind,
        "id": str(uuid.uuid4()),
        "started_at": now(),
        "complete": False,
        "errors": [],
    }


def normalize_repo(value):
    if not value:
        return None
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path
    else:
        scp = re.match(r"(?:[^/@]+@)?([^/:]+):(.+)$", value)
        if scp:
            host, path = scp.groups()
        elif value.count("/") >= 2:
            host, path = value.split("/", 1)
        else:
            return None
    path = path.strip("/").removesuffix(".git")
    result = host.lower() + "/" + path
    return result.lower() if host.lower() == "github.com" else result


def redact(value, secrets=()):
    """Scrub both structured output and exception messages before persistence."""
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(https?://)[^/\s@]+@", r"\1", value)
        value = re.sub(
            r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
            "[REDACTED]",
            value,
        )
        value = re.sub(
            r"([?&](?:token|access_token|password|auth)=)[^&\s]+",
            r"\1[REDACTED]",
            value,
            flags=re.I,
        )
        for secret in sorted((str(x) for x in secrets if x), key=len, reverse=True):
            # Short SSH passwords may also occur in filenames. Only scrub standalone
            # occurrences; no secret-bearing config object is ever serialized.
            value = re.sub(
                r"(?<![\w/])" + re.escape(secret) + r"(?![\w/])", "[REDACTED]", value
            )
        return value
    return value


def save_json(directory, value, secrets=()):
    import tempfile

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = file_stamp()
    path = directory / (stamp + "_" + uuid.uuid4().hex[:8] + ".json")
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(redact(value, secrets), stream, ensure_ascii=True, indent=2)
            stream.write(chr(10))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return path


def validate_snapshot(data):
    if not isinstance(data, dict) or data.get("kind") not in ("host", "github"):
        raise ValueError("无效快照类型，请重新采集")
    if any(
        not isinstance(data.get(k), str) or not data[k]
        for k in ("id", "started_at", "finished_at")
    ):
        raise ValueError("快照缺少采集身份或时间，请重新采集")
    if (
        not isinstance(data.get("complete"), bool)
        or not isinstance(data.get("errors"), list)
        or not isinstance(data.get("repositories"), list)
    ):
        raise ValueError("快照缺少完整性或仓库字段，请重新采集")
    if data["kind"] == "host":
        host = data.get("host")
        if not isinstance(host, dict) or not all(
            isinstance(host.get(k), str) and host[k] for k in ("id", "qualified_id")
        ):
            raise ValueError("主机快照缺少身份，请重新采集")
        roots = data.get("code_paths", [])
        if not isinstance(roots, list) or any(
            not isinstance(r, dict)
            or not isinstance(r.get("complete"), bool)
            or not isinstance(r.get("errors"), list)
            for r in roots
        ):
            raise ValueError("代码路径记录无效，请重新采集")
        if any(not r["complete"] or r["errors"] for r in roots):
            data["complete"] = False
    seen = set()
    for repo in data["repositories"]:
        if (
            not isinstance(repo, dict)
            or not isinstance(repo.get("complete"), bool)
            or not isinstance(repo.get("errors"), list)
        ):
            raise ValueError("仓库完整性字段错误，请重新采集")
        identity = repo.get("key") if data["kind"] == "host" else repo.get("identity")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("仓库身份重复或无效，请重新采集")
        seen.add(identity)
        if data["kind"] == "host":
            if not isinstance(repo.get("code_path"), str) or not isinstance(
                repo.get("status"), list
            ):
                raise ValueError("仓库 Git 状态缺失，请重新采集")
            for field in ("identity", "commit"):
                if repo.get(field) is not None and not isinstance(repo[field], str):
                    raise ValueError("仓库字段无效：" + field)
            target = repo.get("target", {})
            if not isinstance(target, dict) or any(
                target.get(k) is not None and not isinstance(target[k], str)
                for k in ("repository", "branch")
            ):
                raise ValueError("仓库跟踪目标无效，请重新采集")
            if repo.get("branch") is not None and not isinstance(repo["branch"], str):
                raise ValueError("无效分支字段")
            if repo.get("head") is not None and (
                not isinstance(repo["head"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", repo["head"])
            ):
                raise ValueError("无效提交 SHA，请重新采集")
            for item in repo["status"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("path"), str)
                    or not isinstance(item.get("code"), str)
                    or len(item["code"]) != 2
                ):
                    raise ValueError("Git 更改记录无效，请重新采集")
            if repo["complete"] and not repo.get("head"):
                repo["complete"] = False
                repo["errors"].append("HEAD 没有有效提交")
        elif not isinstance(repo.get("branches"), dict) or any(
            not isinstance(name, str)
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha)
            for name, sha in repo["branches"].items()
        ):
            raise ValueError("GitHub 分支记录无效，请重新采集")
        if repo["errors"]:
            repo["complete"] = False
    if data["errors"] or any(not r["complete"] for r in data["repositories"]):
        data["complete"] = False
    return data


def load_snapshot(path):
    with Path(path).open(encoding="utf-8") as stream:
        return validate_snapshot(json.load(stream))


def snapshot_index(base):
    base = Path(base).resolve()
    records = []
    for path in sorted(base.rglob("*.json")):
        if not path.resolve().is_relative_to(base):
            continue
        record = {"file": path.relative_to(base).as_posix()}
        try:
            data = load_snapshot(path)
            record.update(
                kind=data["kind"],
                host=data.get("host", {}).get("qualified_id"),
                started_at=data["started_at"],
                finished_at=data["finished_at"],
                display_time=display_time(data["finished_at"]),
                complete=data["complete"],
                error=None,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            record.update(
                kind=None, host=None, complete=False, error=str(exc), finished_at=""
            )
        records.append(record)

    def sort_key(record):
        try:
            stamp = dt.datetime.fromisoformat(
                record["finished_at"].replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError, AttributeError, OSError):
            stamp = float("-inf")
        return stamp, record["file"]

    return sorted(records, key=sort_key, reverse=True)


def select_recent(base, host=None, count=1, progress=None):
    matching = [
        r
        for r in snapshot_index(base)
        if (r["host"] == host if host else r["kind"] == "github")
    ]
    for record in matching:
        if not record["complete"] and progress:
            progress("快照", "跳过不完整采集：" + record["file"])
    found = [Path(base) / r["file"] for r in matching if r["complete"]]
    if len(found) < count:
        raise ValueError(
            "缺少历史基线：需要该主机至少两份完整快照"
            if count == 2
            else "找不到完整本地快照：" + (host or "GitHub")
        )
    return list(reversed(found[:count]))


def indexed_path(base, file):
    base = Path(base).resolve()
    if not isinstance(file, str):
        raise ValueError("请选择快照文件")
    path = (base / file).resolve()
    if (
        not path.is_relative_to(base)
        or path.suffix.lower() != ".json"
        or not path.is_file()
    ):
        raise ValueError("快照文件不存在或位于允许目录之外")
    return path
