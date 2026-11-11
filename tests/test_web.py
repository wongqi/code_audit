"""Parity, persistence and concurrency acceptance tests; never contact real hosts."""

import copy
import io
import json
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_audit

from audit import main
from code_audit.comparison import compare
from code_audit.config import host_options, resolve_single_host
from code_audit.presentation import export_report, result_html
from code_audit.service import CollectionLock, run_collection, run_comparison
from code_audit.storage import load_snapshot, save_json, snapshot_index
from code_audit.webserver import Task, create_app


class WebTests(unittest.TestCase):
    setUp = test_audit.AuditTests.setUp
    git = test_audit.AuditTests.git
    capture = test_audit.AuditTests.capture
    persist = test_audit.AuditTests.persist

    def app(self):
        self.web = create_app(self.config)
        self.client = self.web.test_client()
        page = self.client.get("/").get_data(as_text=True)
        self.token = re.search(r'name="audit-token" content="([^"]+)"', page)[1]
        return self.client

    def post(self, route, data):
        return self.client.post(route, json=data, headers={"X-Audit-Token": self.token})

    def samples(self):
        base = Path(self.config["settings"]["snapshots_dir"])
        a = self.capture()
        a1 = save_json(base / "hosts/D/A", a)
        a2 = save_json(base / "hosts/D/A", self.capture())
        b = copy.deepcopy(a)
        b["host"] = {"id": "B", "qualified_id": "D/B"}
        bp = save_json(base / "hosts/D/B", b)
        r = a["repositories"][0]
        github = {
            "kind": "github",
            "id": "g",
            "started_at": "t",
            "finished_at": "t",
            "complete": True,
            "errors": [],
            "repositories": [
                {
                    "identity": r["identity"],
                    "complete": True,
                    "errors": [],
                    "branches": {"dev": r["head"]},
                }
            ],
        }
        gp = save_json(base / "github", github)
        return a1, a2, bp, gp

    def test_cli_web_same_result_offline_all_modes_and_no_reports(self):
        self.app()
        a1, a2, bp, gp = self.samples()
        cases = [
            (["A"], [{"name": "A"}]),
            (["A", "B"], [{"name": "A"}, {"name": "B"}]),
            (["A", "github"], [{"name": "A"}, {"name": "github"}]),
            (["github", "A"], [{"name": "github"}, {"name": "A"}]),
            (
                [str(a1), str(a2)],
                [
                    {"file": str(a1.relative_to(self.root / "snapshots"))},
                    {"file": str(a2.relative_to(self.root / "snapshots"))},
                ],
            ),
            (
                [str(a2), str(gp)],
                [
                    {"data": load_snapshot(a2), "label": a2.name},
                    {"data": load_snapshot(gp), "label": gp.name},
                ],
            ),
        ]
        previous = {
            p: p.read_bytes() for p in (self.root / "snapshots").rglob("*.json")
        }
        with (
            patch.object(
                socket, "socket", side_effect=AssertionError("network forbidden")
            ),
            patch("subprocess.Popen", side_effect=AssertionError("commands forbidden")),
        ):
            for cli, web in cases:
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    code = main(["--config", str(self.config_path), "compare", *cli])
                response = self.post("/api/compare", {"inputs": web})
                self.assertEqual(
                    response.status_code, 200, response.get_data(as_text=True)
                )
                result = response.json["result"]
                self.assertEqual(code, result["exit_code"])
                self.assertEqual(code, 0)
                self.assertIn("版本一致", output.getvalue())
                self.assertEqual(
                    result["summary"],
                    {"repositories": 1, "different": 0, "modified": 0, "unknown": 0},
                )
                self.assertIn("版本一致", response.json["html"])
        self.assertFalse((self.root / "reports").exists())
        self.assertEqual(
            previous,
            {p: p.read_bytes() for p in (self.root / "snapshots").rglob("*.json")},
        )

    def test_explicit_partial_and_invalid_snapshots(self):
        self.app()
        a1, a2, _, _ = self.samples()
        partial = load_snapshot(a1)
        partial["complete"] = False
        path = save_json(self.root / "snapshots/hosts/D/A", partial)
        data = {
            "inputs": [
                {"file": str(path.relative_to(self.root / "snapshots"))},
                {"data": load_snapshot(a2), "label": "manual.json"},
            ]
        }
        response = self.post("/api/compare", data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["result"]["exit_code"], 2)
        self.assertIn("不完整快照", response.json["html"])
        self.assertEqual(
            self.post(
                "/api/compare", {"inputs": [{"name": "D"}, {"name": "B"}]}
            ).status_code,
            400,
        )
        with self.assertRaisesRegex(ValueError, "D/A.*D/B"):
            resolve_single_host(self.config, "D")
        invalid = self.post(
            "/api/compare",
            {"inputs": [{"data": {}, "label": "invalid.json"}, {"name": "A"}]},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("重新采集", invalid.json["error"])

    def test_objects_expose_device_groups_for_host_labels(self):
        self.app()
        state = self.client.get("/api/state").json
        self.assertIn({"id": "D/A", "name": "A", "group": "D"}, state["objects"])
        self.assertIn({"id": "D/B", "name": "B", "group": "D"}, state["objects"])
        self.assertEqual(
            state["objects"][-1], {"id": "github", "name": "GitHub", "group": None}
        )
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("北京时间", page)

    def test_comparison_table_has_three_columns_and_three_color_outcomes(self):
        left = self.capture()
        right = copy.deepcopy(left)
        right["host"] = {"id": "B", "qualified_id": "D/B"}
        left["repositories"][0]["status"] = [{"code": "MM", "path": "<script>.py"}]
        cases = []
        cases.append(("equal", compare(left, right, ["A", "B"])))
        branch = copy.deepcopy(right)
        branch["repositories"][0]["branch"] = "other"
        cases.append(("different", compare(left, branch, ["A", "B"])))
        commit = copy.deepcopy(right)
        commit["repositories"][0]["head"] = "f" * 40
        cases.append(("different", compare(left, commit, ["A", "B"])))
        missing = copy.deepcopy(right)
        missing["repositories"] = []
        cases.append(("missing", compare(left, missing, ["A", "B"])))
        incomplete = copy.deepcopy(right)
        incomplete["complete"] = False
        incomplete["repositories"][0]["complete"] = False
        incomplete["repositories"][0]["errors"] = ["采集失败"]
        cases.append(("unknown", compare(left, incomplete, ["A", "B"])))
        ambiguous_left = copy.deepcopy(left)
        ambiguous_right = copy.deepcopy(right)
        for snapshot, prefix in ((ambiguous_left, "left"), (ambiguous_right, "right")):
            duplicate = copy.deepcopy(snapshot["repositories"][0])
            snapshot["repositories"][0]["key"] = "/" + prefix + "-one"
            duplicate["key"] = "/" + prefix + "-two"
            snapshot["repositories"].append(duplicate)
        cases.append(("unknown", compare(ambiguous_left, ambiguous_right, ["A", "B"])))
        for tone, result in cases:
            html = result_html(result)
            self.assertIn("status-" + tone, html)
            self.assertEqual(html.count("<th>"), 3)
            for row in result["rows"]:
                self.assertEqual(html.count('data-status="' + tone + '"'), 1)
                self.assertEqual(html.count("<td"), 6)
                self.assertIn('class="repo-details status-' + tone + '"', html)
                self.assertIn('aria-controls="repo-detail-1"', html)
            self.assertIn("版本结论：", html)
            self.assertIn("查看详情", html)
        equal = result_html(cases[0][1])
        self.assertIn("分支", equal)
        self.assertIn("摘要", equal)
        self.assertIn("Git 状态", equal)
        self.assertNotIn("<script>.py", equal)

    def test_partial_snapshot_keeps_successful_repository_conclusions(self):
        left = self.capture()
        right = copy.deepcopy(left)
        right["complete"] = False
        right["errors"] = ["其他代码路径采集失败"]
        result = compare(left, right, ["left", "right"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["rows"][0]["status"], "equal")
        self.assertEqual(result["summary"]["unknown"], 0)
        self.assertIn("不完整快照", result_html(result))
        missing = copy.deepcopy(right)
        missing["repositories"] = []
        result = compare(left, missing, ["left", "right"])
        self.assertEqual(result["rows"][0]["status"], "unknown")
        self.assertNotIn("status-missing", result_html(result))

    def test_code_paths_and_statuses_survive_github_direction_and_ambiguity(self):
        self.app()
        a, _, _, g = self.samples()
        for inputs in (
            [{"path": str(a)}, {"path": str(g)}],
            [{"path": str(g)}, {"path": str(a)}],
        ):
            result = run_comparison(self.config, inputs)
            self.assertEqual(result["rows"][0]["code_paths"], [str(self.src)])
            self.assertEqual(result["rows"][0]["status"], "equal")
        left = self.capture()
        right = copy.deepcopy(left)
        right["repositories"][0]["code_path"] = "/other/src"
        row = compare(left, right, ["left", "right"])["rows"][0]
        self.assertEqual(row["code_paths"], sorted([str(self.src), "/other/src"]))
        for snapshot, prefix in ((left, "left"), (right, "right")):
            duplicate = copy.deepcopy(snapshot["repositories"][0])
            snapshot["repositories"][0]["key"] = "/" + prefix + "1"
            duplicate["key"] = "/" + prefix + "2"
            snapshot["repositories"].append(duplicate)
        row = compare(left, right, ["left", "right"])["rows"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["code_paths"], sorted([str(self.src), "/other/src"]))
        right["repositories"][0]["key"] = left["repositories"][0]["key"]
        rows = compare(left, right, ["left", "right"])["rows"]
        self.assertEqual([row["status"] for row in rows], ["equal", "equal"])

    def test_git_file_codes_display_as_human_readable_labels(self):
        left = self.capture()
        right = copy.deepcopy(left)
        left["repositories"][0]["status"] = [
            {"code": "??", "path": "scripts/gimbal_snapshot.py"}
        ]
        result = compare(left, right, ["left", "right"])
        html = result_html(result)
        self.assertIn("未跟踪", html)
        self.assertIn("scripts/gimbal_snapshot.py", html)
        self.assertNotIn("??", html)
        self.assertEqual(
            result["rows"][0]["left"]["modification"]["files"][0]["code"], "??"
        )

    def test_change_groups_separate_index_worktree_untracked_and_conflicts(self):
        from code_audit.comparison import modification
        from code_audit.presentation import change_groups, modification_text

        files = [
            {"code": "AM", "path": "config/robot.yaml"},
            {"code": "MM", "path": "both.py"},
            {"code": "RM", "path": "new.py", "original_path": "old.py"},
            {"code": " D", "path": "deleted.py"},
            {"code": "??", "path": "untracked.py"},
            {"code": "UU", "path": "conflict.py"},
            {"code": "AA", "path": "both-added.py"},
            {"code": "T ", "path": "type-changed.py"},
        ]
        original = copy.deepcopy(files)
        groups = {g["label"]: g["files"] for g in change_groups(modification(files))}
        self.assertEqual(
            [f["operation"] for f in groups["暂存的更改"]],
            ["新增", "修改", "重命名", "类型变更"],
        )
        self.assertEqual(
            [f["operation"] for f in groups["未暂存的更改"]],
            ["修改", "修改", "修改", "删除"],
        )
        self.assertEqual(groups["暂存的更改"][2]["original_path"], "old.py")
        self.assertNotIn("original_path", groups["未暂存的更改"][2])
        self.assertEqual([f["path"] for f in groups["未跟踪的文件"]], ["untracked.py"])
        self.assertEqual(
            [f["path"] for f in groups["合并冲突"]], ["conflict.py", "both-added.py"]
        )
        self.assertEqual(files, original)
        self.assertEqual(modification_text(modification([])), "无本地更改")
        self.assertEqual(modification_text(None), "不适用")

    def test_details_have_one_repository_path_formatted_commit_and_separate_change_lists(
        self,
    ):
        from code_audit.presentation import export_content

        left = self.capture()
        right = copy.deepcopy(left)
        repo = left["repositories"][0]
        repo["commit"] = "2025-12-16T03:19:09+00:00\n更新配置 <script>"
        repo["status"] = [
            {"code": "AM", "path": "config/robot.yaml"},
            {"code": "??", "path": "<untracked>.py"},
        ]
        result = compare(left, right, ["left", "right"])
        for html in (result_html(result), export_content(result, "html")):
            self.assertIn("<b>仓库路径</b>", html)
            self.assertNotIn("<b>代码路径</b>", html)
            self.assertIn("2025-12-16 11:19:09", html)
            self.assertNotIn("2025-12-16T03:19:09+00:00", html)
            self.assertIn("更新配置 &lt;script&gt;", html)
            self.assertNotIn("分类可重叠", html)
            self.assertNotIn("工作区", html)
            sections = re.findall(
                r'<section class="change-group">(.*?)</section>', html, re.S
            )
            self.assertEqual(len(sections), 3)
            self.assertIn("暂存的更改（1 个文件）", sections[0])
            self.assertRegex(sections[0], r">新增</span>\s+config/robot\.yaml")
            self.assertIn("未暂存的更改（1 个文件）", sections[1])
            self.assertRegex(sections[1], r">修改</span>\s+config/robot\.yaml")
            self.assertIn("未跟踪的文件（1 个文件）", sections[2])
            self.assertIn("&lt;untracked&gt;.py", sections[2])

    def test_malformed_upload_is_reported_without_server_error(self):
        self.app()
        a1, _, _, _ = self.samples()
        for field, value in (
            ("target", None),
            ("target", []),
            ("commit", {}),
            ("identity", []),
        ):
            malformed = load_snapshot(a1)
            malformed["repositories"][0][field] = value
            response = self.post(
                "/api/compare",
                {"inputs": [{"data": malformed, "label": "bad.json"}, {"name": "B"}]},
            )
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))

    def test_export_explicit_download_and_no_server_files(self):
        self.app()
        a1, a2, _, _ = self.samples()
        data = {"inputs": [{"name": "A"}, {"name": "B"}]}
        before = set(self.root.rglob("*"))
        stylesheet = (Path(__file__).parents[1] / "code_audit/web/style.css").read_text()

        def assert_embedded_styles(html):
            style = re.search(r"<style>(.*?)</style>", html, re.S)
            self.assertIsNotNone(style)
            self.assertEqual(style[1].strip(), stylesheet.strip())

        compared = self.post("/api/compare", data).json["result"]
        response = self.post("/api/export", data)
        self.assertEqual(response.status_code, 200, response.data[:100])
        self.assertIn("attachment", response.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["result.html", "result.json"])
            self.assertIn("版本一致", archive.read("result.html").decode())
            assert_embedded_styles(archive.read("result.html").decode())
            self.assertEqual(
                json.loads(archive.read("result.json"))["rows"], compared["rows"]
            )
        self.assertEqual(before, set(self.root.rglob("*")))
        with (
            patch("code_audit.presentation.EXPORTS", self.root / "exports"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(self.config_path),
                        "compare",
                        str(a1),
                        str(a2),
                        "--export",
                    ]
                ),
                0,
            )
        folders = list((self.root / "exports").iterdir())
        self.assertEqual(len(folders), 1)
        assert_embedded_styles((folders[0] / "result.html").read_text())
        self.assertEqual(
            sorted(p.name for p in folders[0].iterdir()), ["result.html", "result.json"]
        )
        next_folder = export_report(compared, self.root / "exports")
        self.assertNotEqual(next_folder, folders[0])

    def test_api_boundaries_secrets_and_symlinks(self):
        self.config["_hosts"]["D/A"]["config"]["ssh"]["password"] = "do-not-expose"
        self.config["github"]["token"] = "private-token"
        self.app()
        a1, _, _, _ = self.samples()
        state = self.client.get("/api/state").get_data(as_text=True)
        self.assertNotIn("do-not-expose", state)
        self.assertNotIn("private-token", state)
        self.assertEqual(
            self.client.post("/api/collect", json={"targets": ["A"]}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/api/collect",
                json={"targets": ["A"]},
                headers={"X-Audit-Token": self.token, "Origin": "http://other.invalid"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/state", headers={"Host": "evil.invalid"}).status_code,
            403,
        )
        outside = self.root / "outside.json"
        outside.write_bytes(a1.read_bytes())
        (self.root / "snapshots/escape.json").symlink_to(outside)
        for spec in (
            {"file": "../outside.json"},
            {"file": "escape.json"},
            {"path": str(outside)},
        ):
            response = self.post("/api/compare", {"inputs": [spec, {"name": "A"}]})
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertNotIn(
            "escape.json", [r["file"] for r in snapshot_index(self.root / "snapshots")]
        )
        self.assertEqual(self.post("/api/collect", {"targets": "A"}).status_code, 400)

    def fake_collector(self, config, identifier, progress=None):
        data = self.capture()
        options = host_options(config, identifier)
        data["host"] = options["host"]
        data["device"] = options["device"]
        if progress:
            progress(identifier, "采集中")
        return data

    def wait_task(self):
        task = self.web.audit_task[0]
        task.thread.join(timeout=3)
        self.assertFalse(task.thread.is_alive())
        return task.view()

    def test_cli_web_collect_parity_device_multiple_all_github(self):
        self.app()
        selections = [
            (["A"], False),
            (["A", "B"], False),
            ([], True),
            (["github"], False),
            (["A", "github"], False),
        ]
        github = {
            "kind": "github",
            "id": "g",
            "started_at": "t",
            "finished_at": "t",
            "complete": True,
            "errors": [],
            "repositories": [],
        }
        with (
            patch("code_audit.collect.collect_host", side_effect=self.fake_collector),
            patch("code_audit.collect.collect_github", return_value=github),
        ):
            for targets, all_hosts in selections:
                cli = run_collection(self.config, targets, all_hosts)
                response = self.post(
                    "/api/collect", {"targets": targets, "all": all_hosts}
                )
                self.assertEqual(
                    response.status_code, 202, response.get_data(as_text=True)
                )
                web = self.wait_task()["result"]
                self.assertEqual(
                    sorted(
                        (r["target"], r["complete"], r["repositories"])
                        for r in cli["items"]
                    ),
                    sorted(
                        (r["target"], r["complete"], r["repositories"])
                        for r in web["items"]
                    ),
                )
                self.assertTrue(all(Path(r["path"]).is_file() for r in web["items"]))

    def test_task_reconnect_partial_failure_and_compare_during_collect(self):
        self.app()
        self.samples()
        ready = threading.Event()
        release = threading.Event()

        def collecting(config, identifier, progress=None):
            ready.set()
            release.wait(2)
            snapshot = self.fake_collector(config, identifier, progress)
            if identifier.endswith("/B"):
                snapshot["complete"] = False
                snapshot["errors"] = ["模拟失败"]
            return snapshot

        with patch("code_audit.collect.collect_host", side_effect=collecting):
            response = self.post("/api/collect", {"targets": ["A", "B"]})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(ready.wait(1))
            self.assertEqual(
                self.post("/api/collect", {"targets": ["A"]}).status_code, 409
            )
            self.assertEqual(
                self.post(
                    "/api/compare", {"inputs": [{"name": "A"}, {"name": "B"}]}
                ).status_code,
                200,
            )
            other = self.web.test_client()
            self.assertEqual(
                other.get("/api/task").json["task"]["id"], response.json["task"]["id"]
            )
            release.set()
            task = self.wait_task()
        self.assertEqual(task["state"], "partial")
        self.assertEqual(task["result"]["exit_code"], 2)
        self.assertEqual(len(task["result"]["items"]), 2)
        self.assertTrue(any("快照已保存" in e["message"] for e in task["events"]))

    def test_cross_process_lock_releases_on_exit_and_blocks_web(self):
        self.app()
        code = 'from code_audit.service import CollectionLock; import sys; lock=CollectionLock(sys.argv[1]).acquire();print("ready",flush=True);input()'
        process = subprocess.Popen(
            [sys.executable, "-c", code, self.config["_lock_path"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertEqual(
                self.post("/api/collect", {"targets": ["A"]}).status_code, 409
            )
            with self.assertRaisesRegex(RuntimeError, "CLI 或网页"):
                run_collection(self.config, ["A"])
        finally:
            process.communicate("\n", timeout=2)
        lock = CollectionLock(self.config["_lock_path"]).acquire()
        lock.close()

    def test_atomic_snapshot_publication(self):
        snapshot = self.capture()
        base = self.root / "atomic"
        from code_audit import storage

        original = storage.json.dump
        entered = threading.Event()
        release = threading.Event()

        def writing(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)

        with patch.object(storage.json, "dump", side_effect=writing):
            thread = threading.Thread(target=save_json, args=(base, snapshot))
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(snapshot_index(base), [])
            release.set()
            thread.join(timeout=2)
        self.assertEqual(len(snapshot_index(base)), 1)
        self.assertFalse(list(base.glob(".writing-*")))

    def test_stop_prompt_and_ssh_work_release_lock_and_keep_saved_results(self):
        from code_audit.collect import PROMPT_CONTEXT
        from code_audit.control import CollectionCancelled

        self.app()
        self.samples()
        for prompting in (True, False):
            started = threading.Event()
            closed = threading.Event()

            def collecting(config, identifier, progress=None):
                cancellation = PROMPT_CONTEXT.cancellation
                remove = cancellation.watch(closed.set)
                started.set()
                try:
                    if prompting:
                        PROMPT_CONTEXT.broker.ask("host SHA256:test yes/no")
                    else:
                        closed.wait(2)
                    cancellation.check()
                except CollectionCancelled:
                    data = self.fake_collector(config, identifier, progress)
                    data["complete"] = False
                    data["errors"] = ["采集已停止"]
                    return data
                finally:
                    remove()

            with patch("code_audit.collect.collect_host", side_effect=collecting):
                response = self.post("/api/collect", {"targets": ["A"]})
                self.assertEqual(response.status_code, 202)
                self.assertTrue(started.wait(1))
                task = self.web.audit_task[0]
                if prompting:
                    deadline = time.monotonic() + 1
                    while (
                        task.view()["question"] is None and time.monotonic() < deadline
                    ):
                        time.sleep(0.001)
                    self.assertIsNotNone(task.view()["question"])
                self.assertEqual(
                    self.post("/api/stop", {"task": "wrong"}).status_code, 400
                )
                self.assertEqual(
                    self.post("/api/stop", {"task": task.id}).status_code, 200
                )
                state = self.wait_task()
                self.assertTrue(closed.is_set())
                self.assertEqual(state["state"], "cancelled")
                self.assertEqual(state["result"]["exit_code"], 2)
                self.assertEqual(
                    self.post("/api/stop", {"task": task.id}).status_code, 400
                )
                lock = CollectionLock(self.config["_lock_path"]).acquire()
                lock.close()
                self.assertEqual(
                    self.post(
                        "/api/compare", {"inputs": [{"name": "A"}, {"name": "B"}]}
                    ).status_code,
                    200,
                )

    def test_stop_github_finishes_current_page_and_skips_further_requests(self):
        self.app()
        started = threading.Event()
        release = threading.Event()
        requests = []

        def request(req, timeout):
            requests.append(req.full_url)
            started.set()
            release.wait(2)
            response = io.BytesIO(
                json.dumps(
                    [{"full_name": "example/r" + str(i)} for i in range(100)]
                ).encode()
            )
            response.url = req.full_url
            return response

        with patch("urllib.request.OpenerDirector.open", side_effect=request):
            response = self.post("/api/collect", {"targets": ["github"]})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(started.wait(1))
            task = self.web.audit_task[0]
            self.assertEqual(self.post("/api/stop", {"task": task.id}).status_code, 200)
            self.assertEqual(
                self.post("/api/collect", {"targets": ["A"]}).status_code, 409
            )
            release.set()
            state = self.wait_task()
        self.assertEqual(state["state"], "cancelled")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(state["result"]["items"]), 1)
        self.assertFalse(state["result"]["items"][0]["complete"])
        self.assertTrue(Path(state["result"]["items"][0]["path"]).is_file())

    def test_cli_interrupt_closes_active_collectors(self):
        from code_audit.collect import PROMPT_CONTEXT

        closed = threading.Event()
        started = threading.Event()

        def collecting(config, identifier, progress=None):
            cancellation = PROMPT_CONTEXT.cancellation
            remove = cancellation.watch(closed.set)
            started.set()
            try:
                closed.wait(2)
                cancellation.check()
            finally:
                remove()

        from code_audit.collect import HostKeyPrompts

        def service(prompts):
            if started.wait(1):
                raise KeyboardInterrupt

        with (
            patch("code_audit.collect.collect_host", side_effect=collecting),
            patch.object(HostKeyPrompts, "service", service),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(["--config", str(self.config_path), "collect", "A"]), 2
            )
        self.assertTrue(closed.is_set())

    def test_web_host_key_accept_reject_timeout_and_redaction(self):
        for decision in (True, False):
            task = Task(["private-token"], prompt_timeout=1)
            outcomes = []
            worker = threading.Thread(
                target=lambda: outcomes.append(
                    task.prompt("host SHA256:abc private-token")
                )
            )
            worker.start()
            deadline = time.monotonic() + 1
            while not task.view()["question"] and time.monotonic() < deadline:
                time.sleep(0.001)
            question = task.view()["question"]
            self.assertNotIn("private-token", question["message"])
            task.confirm(question["id"], decision)
            worker.join(1)
            self.assertEqual(outcomes, ["yes" if decision else "no"])
            with self.assertRaises(ValueError):
                task.confirm(question["id"], True)
        task = Task(prompt_timeout=0.02)
        self.assertEqual(task.prompt("timeout"), "no")
        self.assertTrue(task.events[-1]["error"])
        self.assertIsNone(task.view()["question"])


if __name__ == "__main__":
    unittest.main()
