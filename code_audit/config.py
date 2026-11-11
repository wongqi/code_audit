"""Load YAML configuration and resolve host names within device groups."""

import os
import posixpath
import re
from pathlib import Path

import yaml

DEFAULTS = {
    "snapshots_dir": "../snapshots",
    "concurrency": 3,
    "connect_timeout": 10,
    "host_timeout": 180,
    "github_timeout": 20,
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate names instead of silently dropping a device."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)) or key in result:
            raise ValueError("YAML 存在重复或无效的字段名，请检查设备、主机和其他配置")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " 必须是 YAML 映射")
    return value


def identifier(value, label):
    # Names may be Chinese, but also name local snapshot directories.
    if (
        not isinstance(value, str)
        or value in (".", "..")
        or not value.strip()
        or value != value.strip()
        or re.search(r"[/\\\x00-\x1f\x7f]", value)
    ):
        raise ValueError(label + "不能为空或包含斜杠、反斜杠、控制字符")


def load_config(path):
    path = Path(path).resolve()
    try:
        with path.open(encoding="utf-8") as stream:
            config = yaml.load(stream, Loader=UniqueKeyLoader) or {}
    except yaml.YAMLError:
        # YAML diagnostics contain source lines, which may contain credentials.
        raise ValueError("YAML 配置格式错误，请检查缩进和字符串引号") from None
    mapping(config, "配置")
    if set(config) - {"devices", "github", "settings"}:
        raise ValueError("配置仅支持 devices、github、settings，请检查字段名称")
    config["settings"] = {**DEFAULTS, **mapping(config.get("settings", {}), "settings")}
    config["_root"] = str(path.parent)
    snapshots_dir = Path(config["settings"]["snapshots_dir"]).expanduser()
    config["settings"]["snapshots_dir"] = str(
        (
            snapshots_dir
            if snapshots_dir.is_absolute()
            else path.parent / snapshots_dir
        ).resolve()
    )
    for key in ("concurrency", "connect_timeout", "host_timeout", "github_timeout"):
        value = config["settings"][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(key + " 必须大于零")
    if not isinstance(config["settings"]["concurrency"], int):
        raise ValueError("concurrency 必须为整数")
    if set(config["settings"]) - set(DEFAULTS):
        raise ValueError("settings 包含未知的选项")
    for key in ("devices", "github"):
        config[key] = mapping(config.get(key, {}), key)
    config["_hosts"] = {}
    for device_id, device in config["devices"].items():
        identifier(device_id, "设备名称")
        mapping(device, "设备 " + device_id)
        if device_id.lower() == "github":
            raise ValueError("github 是保留选择名称")
        if set(device) - {"hosts"}:
            raise ValueError("设备仅支持 hosts，请检查配置字段：" + device_id)
        hosts = mapping(device.get("hosts", {}), "设备的 hosts")
        if not hosts:
            raise ValueError("设备必须至少包含一台主机：" + device_id)
        for host_id, host in hosts.items():
            identifier(host_id, "主机名称")
            if host_id.lower() == "github":
                raise ValueError("github 是保留选择名称")
            mapping(host, "主机 " + host_id)
            qualified = device_id + "/" + host_id
            if set(host) - {"ssh", "code_paths"}:
                raise ValueError(
                    "主机仅支持 ssh 和 code_paths，请检查配置字段：" + qualified
                )
            code_paths = host.get("code_paths")
            if not isinstance(code_paths, list) or not code_paths:
                raise ValueError(qualified + " 的 code_paths 必须是非空路径列表")
            normalized = []
            for code_path in code_paths:
                if (
                    not isinstance(code_path, str)
                    or not code_path.startswith(("/", "~/"))
                    or "\x00" in code_path
                ):
                    raise ValueError(qualified + " 的代码路径必须是绝对路径或 ~/ 路径")
                value = posixpath.normpath(code_path)
                if any(
                    value == other
                    or value.startswith(other.rstrip("/") + "/")
                    or other.startswith(value.rstrip("/") + "/")
                    for other in normalized
                ):
                    raise ValueError(qualified + " 的代码路径不能重复或相互嵌套")
                normalized.append(value)
            ssh = mapping(host.get("ssh"), qualified + " 的 ssh")
            if not isinstance(ssh.get("host"), str) or not ssh["host"]:
                raise ValueError(qualified + " 必须配置 ssh.host")
            port = ssh.get("port", 22)
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                raise ValueError(qualified + " 的 SSH 端口无效")
            config["_hosts"][qualified] = {
                "device_id": device_id,
                "host_id": host_id,
                "config": host,
                "code_paths": normalized,
            }
    for qualified in config["_hosts"]:
        visited, current = set(), qualified
        while current:
            if current in visited:
                raise ValueError("SSH 跳板配置循环：" + qualified)
            visited.add(current)
            entry = config["_hosts"][current]
            jump = entry["config"]["ssh"].get("jump")
            current = resolve_host(config, jump, entry["device_id"]) if jump else None
    return config


def resolve_host(config, selector, preferred_device=None):
    if not isinstance(selector, str):
        raise ValueError("主机选择必须是名称或 设备名/主机名")
    hosts = config["_hosts"]
    if selector in hosts:
        return selector
    if preferred_device and preferred_device + "/" + selector in hosts:
        return preferred_device + "/" + selector
    matches = [key for key, entry in hosts.items() if entry["host_id"] == selector]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError("主机名称有歧义，请使用完整名称：" + "、".join(matches))
    raise ValueError("未知主机：" + selector)


def select_hosts(config, selectors=None, all_hosts=False):
    if all_hosts and selectors:
        raise ValueError("--all 不能与名称同时使用")
    if all_hosts:
        result = list(config["_hosts"])
    else:
        result = [resolve_single_host(config, name) for name in selectors or []]
    if not result:
        raise ValueError("请选择主机，或显式使用 --all")
    return list(dict.fromkeys(result))


def resolve_single_host(config, name):
    if name in config["devices"]:
        choices = [
            key for key, entry in config["_hosts"].items() if entry["device_id"] == name
        ]
        raise ValueError("仅支持主机，设备只用于分层；请选择：" + "、".join(choices))
    return resolve_host(config, name)


def secret(value):
    if isinstance(value, dict):
        if set(value) != {"env"}:
            raise ValueError("凭据引用必须使用 {env: 环境变量名}")
        name = value["env"]
        if name not in os.environ:
            raise ValueError("缺少环境变量：" + str(name))
        return os.environ[name]
    return value


def configured_secrets(config):
    result = []
    for entry in config.get("_hosts", {}).values():
        for key in ("password", "key_password"):
            value = entry["config"].get("ssh", {}).get(key)
            if value is not None:
                try:
                    result.append(secret(value))
                except ValueError:
                    pass
    value = config.get("github", {}).get("token")
    if value is not None:
        try:
            result.append(secret(value))
        except ValueError:
            pass
    return result


def host_options(config, host_id):
    qualified = resolve_host(config, host_id)
    entry = config["_hosts"][qualified]
    device_id = entry["device_id"]
    identity = {"id": entry["host_id"], "qualified_id": qualified}
    return {
        "device": {"id": device_id},
        "host": identity,
        "code_paths": entry["code_paths"],
        "timeout": config["settings"]["host_timeout"],
    }
