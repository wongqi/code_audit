#!/usr/bin/env python3
"""Run from source: python audit.py collect|compare|serve."""

import argparse
import sys
from pathlib import Path

from code_audit.config import configured_secrets, load_config
from code_audit.presentation import export_report, terminal_text
from code_audit.progress import Progress
from code_audit.service import cli_input, run_collection, run_comparison
from code_audit.storage import redact


def parser():
    root = argparse.ArgumentParser(
        description="Code Audit · 按主机采集 Git 状态，离线比较快照",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python audit.py collect X30-A FHZN-A\n  python audit.py collect github\n  python audit.py compare CYVET-A CYVET-B --export\n  python audit.py serve\n\n--export 同时导出 HTML 和 JSON 到项目 exports/；设备名仅用于分层。",
    )

    def options(command, default=False):
        command.add_argument(
            "--config",
            default=str(Path(__file__).resolve().parent / "configs/config.yaml")
            if default
            else argparse.SUPPRESS,
            help="YAML 配置路径",
        )
        command.add_argument(
            "--quiet",
            action="store_true",
            default=False if default else argparse.SUPPRESS,
            help="隐藏进度，保留结果和错误",
        )

    options(root, True)
    commands = root.add_subparsers(dest="command", required=True)
    collect = commands.add_parser(
        "collect", help="采集指定主机或 GitHub；设备名只用于目录分层"
    )
    options(collect)
    collect.add_argument(
        "targets",
        nargs="*",
        metavar="HOST",
        help="主机简称、设备/主机，或 github；可同时指定多个对象",
    )
    collect.add_argument("--all", action="store_true", help="全部配置主机，不含 GitHub")
    compare = commands.add_parser("compare", help="只读快照，默认不写报告")
    options(compare)
    compare.add_argument(
        "targets", nargs="+", help="一个主机查历史，两个主机/GitHub/JSON 文件进行比较"
    )
    compare.add_argument(
        "--export",
        action="store_true",
        help="同时导出 HTML 和 JSON 到项目 exports/ 下的独立目录，无需文件名",
    )
    serve = commands.add_parser("serve", help="启动回环地址上的网页工作台")
    options(serve)
    serve.add_argument("--port", type=int, default=8765)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    config = None
    try:
        config = load_config(args.config)
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("端口必须在 1–65535 之间")
            from code_audit.webserver import create_app

            print(
                "Code Audit：http://127.0.0.1:"
                + str(args.port)
                + "；远程使用请在 VS Code 转发该端口",
                flush=True,
            )
            create_app(config).run(
                host="127.0.0.1",
                port=args.port,
                threaded=True,
                debug=False,
                use_reloader=False,
            )
            return 0
        with Progress(configured_secrets(config), quiet=args.quiet) as progress:
            if args.command == "collect":
                result = run_collection(config, args.targets, args.all, progress)
                for item in result["items"]:
                    print(
                        item["target"]
                        + (" 采集完整" if item["complete"] else " 采集不完整")
                        + "；仓库 "
                        + str(item["repositories"]),
                        flush=True,
                    )
                    print("快照：" + item["path"], flush=True)
                if result.get("cancelled"):
                    print("采集已停止；已保存的快照保留", file=sys.stderr, flush=True)
                return result["exit_code"]
            result = run_comparison(
                config, [cli_input(t) for t in args.targets], progress=progress
            )
            print(terminal_text(result), flush=True)
            if args.export:
                print("导出：" + str(export_report(result)), flush=True)
            return result["exit_code"]
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(
            "错误："
            + str(redact(str(exc), configured_secrets(config) if config else [])),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        print("已中止", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
