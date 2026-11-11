"""One readable result template for browser, HTML export and terminal text."""

import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .storage import display_time, file_stamp

TEMPLATES = Path(__file__).with_name("web")
ENV = Environment(
    loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"])
)
STATUS_LABELS = {
    "equal": "版本一致",
    "different": "版本不同",
    "missing": "单侧缺失",
    "unknown": "无法判断",
}


def change_groups(value):
    """Display index and working-tree changes independently, as Git's XY columns do."""
    if value is None:
        return []
    groups = {
        label: []
        for label in (
            "暂存的更改",
            "未暂存的更改",
            "未跟踪的文件",
            "合并冲突",
            "其他状态",
        )
    }
    operations = {
        "M": "修改",
        "A": "新增",
        "D": "删除",
        "R": "重命名",
        "C": "复制",
        "T": "类型变更",
    }
    for entry in value["files"]:
        code = entry["code"]
        if code == "??":
            groups["未跟踪的文件"].append(dict(entry, operation="未跟踪"))
        elif code in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
            groups["合并冲突"].append(dict(entry, operation="冲突"))
        else:
            recognized = False
            for label, status in zip(("暂存的更改", "未暂存的更改"), code):
                if status in operations:
                    item = dict(entry, operation=operations[status])
                    if status not in ("R", "C"):
                        item.pop("original_path", None)
                    groups[label].append(item)
                    recognized = True
            if not recognized:
                groups["其他状态"].append(dict(entry, operation="状态异常"))
    return [
        {"label": label, "files": files} for label, files in groups.items() if files
    ]


def modification_text(value):
    if value is None:
        return "不适用"
    if not value["dirty"]:
        return "无本地更改"
    return "；".join(
        group["label"] + " " + str(len(group["files"]))
        for group in change_groups(value)
    )


ENV.filters["modification"] = modification_text
ENV.filters["change_groups"] = change_groups
ENV.filters["time"] = display_time
ENV.globals["status_labels"] = STATUS_LABELS
EXPORTS = Path(__file__).resolve().parent.parent / "exports"


def result_html(result):
    return ENV.get_template("result.html").render(result=result)


def export_content(result, output_format):
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output_format == "html":
        return ENV.get_template("export.html").render(result=result)
    raise ValueError("只支持 .html 或 .json 导出")


def export_files(result):
    return {
        "result." + output_format: export_content(result, output_format)
        for output_format in ("html", "json")
    }


def export_report(result, directory=None):
    files = export_files(result)
    folder = Path(directory or EXPORTS) / (file_stamp() + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    try:
        for filename, content in files.items():
            (folder / filename).write_text(content, encoding="utf-8")
    except BaseException:
        shutil.rmtree(folder)
        raise
    return folder.resolve()


def export_archive(result):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in export_files(result).items():
            archive.writestr(filename, content)
    return buffer.getvalue()


def terminal_text(result):
    count = result["summary"]
    lines = [
        "仓库 {repositories}；版本差异 {different}；本地更改 {modified}；无法判断 {unknown}".format(
            **count
        )
    ]
    for src in result["sources"]:
        host = src.get("host")
        lines.append(
            ("GitHub" if src["kind"] == "github" else host["qualified_id"])
            + "："
            + src["label"]
            + "；"
            + display_time(src["started_at"])
            + " → "
            + display_time(src["finished_at"])
            + ("；完整" if src["complete"] else "；不完整")
        )
    for row in result["rows"]:
        lines.append(row["repository"] + "：" + row["conclusion"])
        for label, side in zip(("左", "右"), (row["left"], row["right"])):
            if side:
                lines.append(
                    "  "
                    + label
                    + "："
                    + (side["branch"] or "游离 HEAD")
                    + "；"
                    + (side["head"] or "无提交")
                    + "；"
                    + modification_text(side["modification"])
                )
        lines.extend("  注意：" + error for error in row["errors"])
    lines.extend("注意：" + error for error in result["errors"])
    return "\n".join(lines)
