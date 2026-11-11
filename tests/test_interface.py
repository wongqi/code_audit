"""Refactor acceptance: selection, strictly offline dispatch and observable I/O."""

import copy
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_audit

from audit import main, parser
from code_audit.collect import collect_github, collect_host, collect_hosts
from code_audit.config import host_options, resolve_single_host, select_hosts
from code_audit.progress import Progress
from code_audit.storage import load_snapshot, save_json, select_recent


class InterfaceTests(unittest.TestCase):
    setUp = test_audit.AuditTests.setUp
    git = test_audit.AuditTests.git
    capture = test_audit.AuditTests.capture
    persist = test_audit.AuditTests.persist
    write_config = test_audit.AuditTests.write_config

    def test_example_config_has_usable_host_selection_and_jump(self):
        from code_audit.config import load_config, resolve_host

        example = Path(__file__).parents[1] / "configs/config.example.yaml"
        config = load_config(example)
        self.assertEqual(
            select_hosts(config, ["X30-A", "CYVET-A"]),
            ["X30-1/X30-A", "CYVET-1/CYVET-A"],
        )
        ssh = config["_hosts"]["X30-1/X30-A"]["config"]["ssh"]
        self.assertEqual(resolve_host(config, ssh["jump"], "X30-1"), "X30-1/FHZN-A")
        self.assertEqual(
            Path(config["settings"]["snapshots_dir"]), example.parents[1] / "snapshots"
        )

    def test_minimal_config_and_removed_fields(self):
        raw = {
            "devices": {
                "D": {
                    "hosts": {
                        "A": {
                            "ssh": {"host": "localhost"},
                            "code_paths": [str(self.src)],
                        }
                    }
                }
            }
        }
        config = self.write_config(raw)
        self.assertEqual(
            host_options(config, "A")["host"], {"id": "A", "qualified_id": "D/A"}
        )
        self.assertNotIn("comparison", config)
        self.assertEqual(config["settings"]["host_timeout"], 180)
        self.assertNotIn("known_hosts", config["settings"])
        raw["comparison"] = {}
        with self.assertRaisesRegex(ValueError, "配置仅支持"):
            self.write_config(raw)
        raw.pop("comparison")
        raw["devices"]["D"]["model"] = "obsolete"
        with self.assertRaisesRegex(ValueError, "设备仅支持 hosts"):
            self.write_config(raw)

    def test_devices_hosts_conflicts_reserved_and_multiple_selection(self):
        host = {"ssh": {"host": "localhost"}, "code_paths": [str(self.src)]}
        config = self.write_config(
            {
                "devices": {
                    "D": {"hosts": {"A": host, "B": host}},
                    "A": {"hosts": {"C": host, "B": host}},
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "仅支持主机"):
            select_hosts(config, ["D"])
        self.assertEqual(select_hosts(config, ["D/A", "C", "D/A"]), ["D/A", "A/C"])
        self.assertEqual(len(select_hosts(config, all_hosts=True)), 4)
        for target in ("A", "B"):
            with self.assertRaisesRegex(ValueError, "仅支持主机|歧义"):
                select_hosts(config, [target])
        with self.assertRaisesRegex(ValueError, "仅支持主机"):
            resolve_single_host(config, "A")
        with self.assertRaises(ValueError):
            select_hosts(config, ["D"], all_hosts=True)
        with self.assertRaisesRegex(ValueError, "保留"):
            self.write_config({"devices": {"github": {"hosts": {"A": host}}}})
        with self.assertRaisesRegex(ValueError, "保留"):
            self.write_config({"devices": {"D": {"hosts": {"github": host}}}})

    def test_bad_cli_arguments_and_files_never_fall_back_to_names(self):
        for targets in (
            ["missing.json", "A"],
            ["./A", "B"],
            ["github"],
            ["A", "B", "A"],
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["--config", str(self.config_path), "compare", *targets]), 2
                )
        for targets in (["D"], ["github", "--all"], ["--all", "A"], []):
            with (
                patch(
                    "code_audit.collect.collect_hosts",
                    side_effect=AssertionError("unwanted SSH"),
                ),
                patch(
                    "code_audit.collect.collect_github",
                    side_effect=AssertionError("unwanted API"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(["--config", str(self.config_path), "collect", *targets]), 2
                )
        # Global flags work on either side of the subcommand.
        self.assertTrue(parser().parse_args(["collect", "github", "--quiet"]).quiet)
        self.assertTrue(parser().parse_args(["--quiet", "collect", "github"]).quiet)
        a = self.persist(
            {
                "kind": "github",
                "id": "g",
                "started_at": "t",
                "finished_at": "t",
                "complete": True,
                "errors": [],
                "repositories": [],
            }
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["--config", str(self.config_path), "compare", str(a), str(a)]), 2
            )

    def test_snapshot_validation_and_current_history_search(self):
        snapshot = self.capture()
        path = save_json(self.root / "snapshots/hosts/D/A", snapshot)
        original = path.read_bytes()
        self.assertEqual(select_recent(self.root / "snapshots", "D/A"), [path])
        self.assertEqual(path.read_bytes(), original)
        invalid = self.persist([])
        with self.assertRaisesRegex(ValueError, "无效快照类型"):
            load_snapshot(invalid)
        incomplete = self.capture()
        incomplete["repositories"][0]["errors"].append("unreadable file")
        self.assertFalse(load_snapshot(self.persist(incomplete))["complete"])
        missing_identity = self.capture()
        missing_identity["host"].pop("qualified_id")
        with self.assertRaisesRegex(ValueError, "缺少身份"):
            load_snapshot(self.persist(missing_identity))

    def test_beijing_filename_time_and_mixed_timezone_history_order(self):
        from code_audit.storage import display_time, now

        self.assertTrue(now().endswith("+08:00"))
        self.assertEqual(
            display_time("2026-11-11T01:31:03+00:00"), "2026-11-11 09:31:03"
        )
        old = self.capture()
        old["finished_at"] = "2026-11-11T09:30:00+08:00"
        newest = self.capture()
        newest["finished_at"] = "2026-11-11T01:31:03+00:00"
        first = save_json(self.root / "snapshots/hosts/D/A", old)
        second = save_json(self.root / "snapshots/hosts/D/A", newest)
        self.assertRegex(first.name, r"^\d{8}_\d{6}_[0-9a-f]{8}\.json$")
        self.assertEqual(select_recent(self.root / "snapshots", "D/A"), [second])
        self.assertTrue(parser().parse_args(["compare", "A", "B", "--export"]).export)

    def test_progress_thread_lines_redaction_heartbeat_and_quiet(self):
        stream = io.StringIO()
        with Progress(["sensitive-token"], stream=stream, interval=0.02) as progress:
            threads = [
                threading.Thread(
                    target=lambda n=n: progress(
                        "Host" + str(n), "waiting sensitive-token\nnext"
                    )
                )
                for n in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            time.sleep(0.06)
            for n in range(6):
                progress.done("Host" + str(n))
        value = stream.getvalue()
        self.assertNotIn("sensitive-token", value)
        self.assertIn("已等待", value)
        self.assertTrue(all(line.startswith("[Host") for line in value.splitlines()))
        quiet = io.StringIO()
        with Progress(quiet=True, stream=quiet, interval=0.01) as progress:
            progress("GitHub", "hidden")
            time.sleep(0.03)
            progress("GitHub", "failure", error=True)
        self.assertEqual(quiet.getvalue(), "[GitHub] failure\n")

    def test_concurrent_prompts_run_on_main_thread_and_pause_progress(self):
        from code_audit.collect import PROMPT_CONTEXT

        stream = io.StringIO()
        started = threading.Barrier(2)
        calls = []

        def fake_collect(config, identifier, progress=None):
            progress(identifier, "连接中")
            started.wait(timeout=2)
            answer = PROMPT_CONTEXT.broker.ask("确认同一跳板 [yes/no] ")
            return {"complete": answer == "yes"}

        def answer():
            calls.append(threading.current_thread())
            previous = stream.getvalue()
            time.sleep(0.08)
            self.assertEqual(stream.getvalue(), previous)
            return "yes"

        with (
            Progress(stream=stream, interval=0.01) as progress,
            patch("code_audit.collect.collect_host", side_effect=fake_collect),
            patch("builtins.input", side_effect=answer),
            patch("sys.stdin.isatty", return_value=True),
        ):
            results = dict(
                collect_hosts(self.config, ["D/A", "D/B"], progress=progress)
            )
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], threading.main_thread())
        self.assertTrue(all(value["complete"] for value in results.values()))
        self.assertEqual(stream.getvalue().count("确认同一跳板"), 1)

    def test_prompt_eof_and_shutdown_release_waiters(self):
        from code_audit.collect import HostKeyPrompts

        prompts = HostKeyPrompts()
        outcomes = []

        def waiter():
            try:
                prompts.ask("confirmation")
            except RuntimeError as exc:
                outcomes.append(str(exc))

        thread = threading.Thread(target=waiter)
        thread.start()
        while prompts.pending.empty():
            time.sleep(0.001)
        with (
            patch("builtins.input", side_effect=EOFError),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            prompts.service()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIn("无法读取", outcomes[0])
        prompts.close()
        with self.assertRaisesRegex(RuntimeError, "已中止"):
            prompts.ask("another")

    def test_paginated_github_emits_progress_and_continues_after_failure(self):
        stream = io.StringIO()
        response_data = [
            [{"full_name": "example/r" + str(i)} for i in range(100)],
            [{"full_name": "example/z-final"}],
            # First repository has 101 branches (two pages).
            [{"name": "b" + str(i), "commit": {"sha": "a" * 40}} for i in range(100)],
            [{"name": "last", "commit": {"sha": "b" * 40}}],
        ]

        def request(req, timeout):
            if response_data:
                data = response_data.pop(0)
            elif "/r1/branches" in req.full_url:
                raise ValueError("sensitive-token simulated failure")
            else:
                data = [{"name": "main", "commit": {"sha": "c" * 40}}]
            response = io.BytesIO(json.dumps(data).encode())
            response.url = req.full_url
            return response

        config = copy.deepcopy(self.config)
        config["github"]["token"] = "sensitive-token"
        with (
            Progress(["sensitive-token"], stream=stream) as progress,
            patch("urllib.request.OpenerDirector.open", side_effect=request),
            patch(
                "code_audit.collect.collect_host",
                side_effect=AssertionError("SSH forbidden"),
            ),
        ):
            snapshot = collect_github(config, progress=progress)
        self.assertFalse(snapshot["complete"])
        self.assertEqual(len(snapshot["repositories"]), 101)
        self.assertEqual(len(snapshot["repositories"][0]["branches"]), 101)
        self.assertEqual(sum(r["complete"] for r in snapshot["repositories"]), 100)
        self.assertIn("第 2 页", stream.getvalue())
        self.assertIn("101/101", stream.getvalue())
        self.assertIn("累计", stream.getvalue())
        self.assertNotIn("sensitive-token", stream.getvalue() + json.dumps(snapshot))

    def test_remote_progress_stderr_is_separate_from_snapshot_stdout(self):
        import paramiko

        events = []
        host_client = MagicMock()
        channel = host_client.get_transport.return_value.open_session.return_value
        snapshot = self.capture()
        channel.recv_ready.side_effect = [True, False, False]
        channel.recv.return_value = json.dumps(snapshot).encode()
        # Fragment one structured event across two network reads.
        channel.recv_stderr_ready.side_effect = [True, True, False, False]
        channel.recv_stderr.side_effect = [
            b'{"event":"progress","message":"scan',
            b'ning"}\n',
        ]
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        with patch.object(paramiko, "SSHClient", return_value=host_client):
            result = collect_host(
                self.config,
                "A",
                progress=lambda label, message, **kw: events.append((label, message)),
            )
        self.assertTrue(result["complete"])
        self.assertIn(("D/A", "scanning"), events)
        # The actual standalone transport contract is JSON stdout, NDJSON stderr.
        import base64

        options = base64.b64encode(json.dumps(self.options).encode()).decode()
        probe = Path(__file__).parents[1] / "code_audit/probe.py"
        process = subprocess.run(
            [sys.executable, str(probe), options], capture_output=True, check=True
        )
        self.assertTrue(json.loads(process.stdout)["complete"])
        self.assertTrue(
            all(
                json.loads(line)["event"] == "progress"
                for line in process.stderr.splitlines()
            )
        )


if __name__ == "__main__":
    unittest.main()
