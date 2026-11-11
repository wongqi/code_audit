"""Shared use cases for the terminal and browser."""

import copy
import fcntl
from pathlib import Path

from .comparison import compare
from .config import configured_secrets, resolve_single_host, select_hosts
from .control import Cancellation, CollectionCancelled
from .storage import (
    indexed_path,
    load_snapshot,
    redact,
    save_json,
    select_recent,
    validate_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent


class CollectionLock:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / ".collect.lock")
        self.stream = None

    def acquire(self):
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            self.stream = None
            raise RuntimeError(
                "已有 CLI 或网页采集任务正在运行，请等待其完成"
            ) from None
        return self

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def collection_targets(config, targets, all_hosts=False):
    if not isinstance(targets, list) or any(
        not isinstance(value, str) for value in targets
    ):
        raise ValueError("采集对象必须是名称列表")
    if all_hosts:
        return select_hosts(config, targets, True)
    hosts = [value for value in targets if value != "github"]
    selected = select_hosts(config, hosts) if hosts else []
    if "github" in targets:
        selected.append("github")
    if not selected:
        raise ValueError("请选择主机或 GitHub，或使用 --all 选择全部主机")
    return selected


def run_collection(config, targets, all_hosts=False, progress=None, lease=None):
    from .collect import collect_github, collect_hosts

    identifiers = collection_targets(config, targets, all_hosts)
    lock = lease or CollectionLock(config.get("_lock_path")).acquire()
    results = []
    secrets = configured_secrets(config)
    base = Path(config["settings"]["snapshots_dir"])
    cancellation = getattr(progress, "cancellation", None) or Cancellation()
    cancelled = False
    snapshots = None
    try:

        def samples():
            hosts = [key for key in identifiers if key != "github"]
            if hosts:
                yield from collect_hosts(
                    config, hosts, progress, cancellation=cancellation
                )
            if "github" in identifiers:
                cancellation.check()
                yield "github", collect_github(config, progress)

        snapshots = samples()
        for identifier, snapshot in snapshots:
            # Keep failure snapshots as evidence, never overwrite a successful sample.
            path = save_json(
                base / ("github" if identifier == "github" else "hosts/" + identifier),
                snapshot,
                secrets,
            )
            results.append(
                {
                    "target": identifier,
                    "path": str(path),
                    "complete": snapshot["complete"],
                    "repositories": len(snapshot["repositories"]),
                    "branches": sum(
                        len(r["branches"]) for r in snapshot["repositories"]
                    )
                    if identifier == "github"
                    else None,
                }
            )
            if progress:
                progress(identifier, "快照已保存：" + str(path))
                if hasattr(progress, "done"):
                    progress.done(identifier)
    except (KeyboardInterrupt, CollectionCancelled):
        cancellation.cancel()
        cancelled = True
    finally:
        try:
            if snapshots is not None:
                snapshots.close()
        finally:
            lock.close()
    cancelled = cancelled or cancellation.event.is_set()
    return {
        "items": results,
        "cancelled": cancelled,
        "exit_code": 2 if cancelled or any(not r["complete"] for r in results) else 0,
    }


def cli_input(value):
    if value.lower().endswith(".json") or value.startswith(("./", "../", "/", "~/")):
        return {"path": str(Path(value).expanduser().resolve())}
    return {"name": value}


def resolve_input(config, spec, count=1, progress=None):
    if not isinstance(spec, dict):
        raise ValueError("请选择主机、GitHub 或快照文件")
    if set(spec) == {"name"}:
        name = spec["name"]
        if not isinstance(name, str):
            raise ValueError("无效主机名称")
        if name == "github":
            if count != 1:
                raise ValueError("历史比较只支持主机")
            host = None
        else:
            host = resolve_single_host(config, name)
        paths = select_recent(
            config["settings"]["snapshots_dir"], host, count, progress
        )
    elif count != 1:
        raise ValueError("自动历史比较需要主机名称")
    elif set(spec) == {"file"}:
        paths = [indexed_path(config["settings"]["snapshots_dir"], spec["file"])]
    elif set(spec) == {"path"}:
        paths = [Path(spec["path"])]
    elif set(spec) == {"data", "label"}:
        if not isinstance(spec["label"], str):
            raise ValueError("手动载入快照需要文件名")
        return [(validate_snapshot(copy.deepcopy(spec["data"])), spec["label"])]
    else:
        raise ValueError("无效快照选择")
    return [(load_snapshot(path), str(path.resolve())) for path in paths]


def run_comparison(config, inputs, progress=None):
    if not isinstance(inputs, list) or len(inputs) not in (1, 2):
        raise ValueError("比较需要一个主机名称或两个比较对象")
    if len(inputs) == 1:
        selected = resolve_input(config, inputs[0], count=2, progress=progress)
    else:
        selected = [
            resolve_input(config, spec, progress=progress)[0] for spec in inputs
        ]
    (left, llabel), (right, rlabel) = selected
    same_host = (
        left["kind"] == right["kind"] == "host"
        and left["host"]["qualified_id"] == right["host"]["qualified_id"]
    )
    result = compare(left, right, [llabel, rlabel], history=same_host)
    return redact(result, configured_secrets(config))
