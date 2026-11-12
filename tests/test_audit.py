import copy
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from audit import main, parser
from code_audit.collect import (
    GitHubClient,
    SameOriginRedirect,
    collect_github,
    collect_host,
    collect_hosts,
)
from code_audit.comparison import compare, modification
from code_audit.config import (
    configured_secrets,
    host_options,
    load_config,
    resolve_host,
    select_hosts,
)
from code_audit.presentation import export_content
from code_audit.probe import Probe
from code_audit.storage import (
    load_snapshot,
    normalize_repo,
    redact,
    save_json,
    select_recent,
)


def compare_snapshots(left, right, history=False):
    return compare(
        load_snapshot(left), load_snapshot(right), [str(left), str(right)], history
    )


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.repo = self.src / "sample"
        self.repo.mkdir()
        self.git("init", "-b", "dev")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/example/sample.git")
        (self.repo / "code.txt").write_text("initial\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        (self.repo / "package.xml").write_text(
            "<package><name>sample</name><version>1.0</version></package>"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.options = {
            "device": {"id": "D"},
            "host": {"id": "A", "qualified_id": "D/A"},
            "code_paths": [str(self.src)],
            "timeout": 30,
        }
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            "settings:\n  snapshots_dir: snapshots\n"
            "devices:\n  D:\n    hosts:\n"
            "      A:\n        ssh: {host: localhost}\n        code_paths: ["
            + str(self.src)
            + "]\n"
            "      B:\n        ssh: {host: localhost}\n        code_paths: ["
            + str(self.src)
            + "]\n"
            "github:\n  organization: example\n"
        )
        self.config = load_config(self.config_path)
        self.config["_lock_path"] = str(self.root / ".collect.lock")

    def git(self, *args):
        return (
            subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )

    def capture(self):
        result = Probe(self.options).collect()
        self.assertTrue(result["complete"], json.dumps(result))
        return result

    def persist(self, value):
        return save_json(self.root / "snapshots", value)

    def test_probe_ignores_ros_metadata_and_scans_non_ros(self):
        (self.repo / "package.xml").write_text("not even XML")
        orphan = self.src / "orphan"
        orphan.mkdir()
        (orphan / "package.xml").write_text("<package/>")
        snapshot = self.capture()
        self.assertEqual(len(snapshot["repositories"]), 1)
        self.assertNotIn("packages", snapshot)
        self.assertNotIn("packages", snapshot["repositories"][0])

    def test_status_without_file_content_hashes_and_index_is_unchanged(self):
        (self.repo / "code.txt").write_text("staged")
        self.git("add", "code.txt")
        (self.repo / "code.txt").write_text("unstaged")
        unusual = "new\n\t<script>.txt"
        (self.repo / unusual).write_text("new")
        (self.repo / "ignored").write_text("ignored")
        original = (self.repo / ".git/index").read_bytes()
        with patch.object(
            Probe, "command", autospec=True, side_effect=Probe.command
        ) as commands:
            result = self.capture()["repositories"][0]
        self.assertEqual(original, (self.repo / ".git/index").read_bytes())
        self.assertIn({"code": "MM", "path": "code.txt"}, result["status"])
        self.assertIn({"code": "??", "path": unusual}, result["status"])
        self.assertNotIn("ignored", [entry["path"] for entry in result["status"]])
        self.assertNotIn("files", result)
        self.assertNotIn("index", result)
        self.assertFalse(
            any(
                call.args[2] in ("ls-files", "cat-file")
                for call in commands.call_args_list
            )
        )
        counts = modification(result["status"])["counts"]
        self.assertEqual(
            (counts["staged"], counts["unstaged"], counts["untracked"]), (1, 1, 1)
        )

    def test_same_head_different_modifications_are_only_a_hint(self):
        (self.repo / "code.txt").write_text("left")
        left = self.capture()
        (self.repo / "code.txt").write_text("right")
        right = self.capture()
        result = compare_snapshots(self.persist(left), self.persist(right))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["rows"][0]["conclusion"], "版本一致")
        self.assertEqual(result["summary"]["modified"], 1)
        self.assertNotIn("file_changes", result["rows"][0])

    def test_different_branch_same_content_and_different_head_same_tree(self):
        left = self.capture()
        self.git("checkout", "-qb", "other")
        self.git("commit", "--allow-empty", "-qm", "new history, same tree")
        right = self.capture()
        row = compare_snapshots(self.persist(left), self.persist(right))["rows"][0]
        self.assertFalse(row["branch_equal"])
        self.assertFalse(row["head_equal"])

    def test_staged_deletion_rename_and_conflict(self):
        self.git("mv", "code.txt", "renamed.txt")
        snapshot = self.capture()
        status = snapshot["repositories"][0]["status"]
        self.assertEqual(
            status, [{"code": "R ", "path": "renamed.txt", "original_path": "code.txt"}]
        )
        self.git("reset", "--hard", "-q", "HEAD")
        self.git("checkout", "-qb", "other")
        (self.repo / "code.txt").write_text("other")
        self.git("commit", "-qam", "other")
        self.git("checkout", "-q", "dev")
        (self.repo / "code.txt").write_text("dev")
        self.git("commit", "-qam", "dev")
        subprocess.run(
            ["git", "-C", str(self.repo), "merge", "other"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result = self.capture()["repositories"][0]
        self.assertIn({"code": "UU", "path": "code.txt"}, result["status"])
        self.assertEqual(modification(result["status"])["counts"]["conflict"], 1)

    def test_worktree_git_file_and_nested_repository(self):
        worktree = self.src / "worktree"
        self.git("worktree", "add", "-b", "worktree-branch", str(worktree))
        nested = self.repo / "vendor"
        nested.mkdir()
        subprocess.run(["git", "-C", str(nested), "init", "-q"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(nested),
                "-c",
                "user.name=x",
                "-c",
                "user.email=x@x",
                "commit",
                "--allow-empty",
                "-qm",
                "empty",
            ],
            check=True,
        )
        snapshot = self.capture()
        self.assertEqual(len(snapshot["repositories"]), 3)
        self.assertTrue((worktree / ".git").is_file())

    def test_target_saved_and_credentials_removed(self):
        self.git(
            "remote",
            "set-url",
            "origin",
            "https://user:password@github.com/example/sample.git?access_token=secret",
        )
        self.git("config", "branch.dev.remote", "origin")
        self.git("config", "branch.dev.merge", "refs/heads/other")
        snapshot = self.capture()
        target = snapshot["repositories"][0]["target"]
        self.assertEqual(target["branch"], "other")
        self.assertEqual(target["repository"], "github.com/example/sample")
        serialized = json.dumps(snapshot)
        self.assertNotIn("password", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertEqual(
            normalize_repo("git@github.com:EXAMPLE/sample.git"),
            "github.com/example/sample",
        )
        self.assertNotIn("ghp_abcdef", redact("token=ghp_abcdef"))

    def test_state_change_missing_path_and_detached_head(self):
        probe = Probe(self.options)
        original = probe.state

        def changing(root):
            state = original(root)
            (self.repo / "code.txt").write_text("changed during collection")
            return state

        with patch.object(probe, "state", side_effect=changing):
            self.assertFalse(probe.collect()["complete"])
        self.git("checkout", "--", "code.txt")
        before = self.capture()
        self.git("checkout", "--detach", "-q")
        detached = self.capture()
        result = compare_snapshots(self.persist(before), self.persist(detached))
        self.assertIsNone(result["rows"][0]["branch_equal"])
        self.assertEqual(result["exit_code"], 0)
        self.options["code_paths"].append(str(self.root / "missing"))
        self.assertFalse(Probe(self.options).collect()["complete"])

    def test_history_skips_incomplete_and_rejects_wrong_host(self):
        first = self.capture()
        directory = self.root / "history/hosts/D/A"
        a = save_json(directory, first)
        incomplete = copy.deepcopy(first)
        incomplete["complete"] = False
        save_json(directory, incomplete)
        with self.assertRaisesRegex(ValueError, "缺少历史基线"):
            select_recent(self.root / "history", "D/A", count=2)
        second = self.capture()
        b = save_json(directory, second)
        self.assertEqual(select_recent(self.root / "history", "D/A", count=2), [a, b])
        second["host"]["id"] = "B"
        with self.assertRaisesRegex(ValueError, "同一主机"):
            compare_snapshots(a, self.persist(second), history=True)

    def test_duplicate_ambiguity_and_absolute_path_pairing(self):
        left, right = self.capture(), self.capture()
        right["repositories"][0]["identity"] = "github.com/example/renamed"
        report = compare_snapshots(self.persist(left), self.persist(right))
        self.assertEqual(report["exit_code"], 1)
        for snap, suffix in ((left, "left"), (right, "right")):
            first = snap["repositories"][0]
            first["identity"] = "github.com/example/sample"
            first["key"] = "/projects/" + suffix + "1"
            second = copy.deepcopy(first)
            second["key"] = "/projects/" + suffix + "2"
            snap["repositories"].append(second)
        self.assertEqual(
            compare_snapshots(self.persist(left), self.persist(right))["exit_code"], 2
        )
        for lrepo, rrepo in zip(left["repositories"], right["repositories"]):
            rrepo["key"] = lrepo["key"]
        self.assertEqual(
            compare_snapshots(self.persist(left), self.persist(right))["exit_code"], 0
        )

    def test_github_comparison_states_are_offline(self):
        host = self.capture()
        repo = host["repositories"][0]
        github = {
            "kind": "github",
            "id": "g",
            "started_at": "t",
            "finished_at": "t",
            "complete": True,
            "errors": [],
            "repositories": [
                {
                    "identity": repo["identity"],
                    "complete": True,
                    "branches": {"dev": repo["head"]},
                    "errors": [],
                }
            ],
        }
        a, b = self.persist(host), self.persist(github)
        with (
            patch.object(
                socket, "socket", side_effect=AssertionError("network prohibited")
            ),
            patch(
                "subprocess.Popen",
                side_effect=AssertionError("external command prohibited"),
            ),
        ):
            self.assertEqual(compare_snapshots(a, b)["exit_code"], 0)
            self.assertEqual(compare_snapshots(a, a)["exit_code"], 0)
            with patch("sys.stdout"):
                self.assertEqual(
                    main(
                        ["--config", str(self.config_path), "compare", str(a), str(b)]
                    ),
                    0,
                )
        repo["status"] = [{"code": "??", "path": "untracked"}]
        self.assertEqual(compare_snapshots(self.persist(host), b)["exit_code"], 0)
        github["repositories"][0]["branches"]["dev"] = "a" * 40
        self.assertEqual(compare_snapshots(a, self.persist(github))["exit_code"], 1)
        github["repositories"][0]["branches"] = {}
        self.assertEqual(compare_snapshots(a, self.persist(github))["exit_code"], 2)

    def test_github_collection_all_repositories_all_branches_and_no_host_access(self):
        # No host snapshots exist: GitHub collection must still cover every repo,
        # including repos and branches which no configured host checks out.
        def pages(endpoint):
            if endpoint.startswith("/orgs/"):
                return [
                    {"full_name": "example/sample"},
                    {"full_name": "example/not-on-boards"},
                ]
            return [
                {"name": "dev", "commit": {"sha": "a" * 40}},
                {"name": "release/next", "commit": {"sha": "b" * 40}},
            ]

        with (
            patch(
                "code_audit.collect.collect_host",
                side_effect=AssertionError("SSH prohibited"),
            ),
            patch(
                "code_audit.collect.load_snapshot",
                create=True,
                side_effect=AssertionError("snapshots prohibited"),
            ),
            patch.object(GitHubClient, "pages", side_effect=pages) as api,
        ):
            result = collect_github(self.config)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope"]["branches"], "all")
        self.assertTrue(result["scope"]["all"])
        self.assertEqual(len(result["repositories"]), 2)
        self.assertEqual(api.call_count, 3)
        for repo in result["repositories"]:
            self.assertEqual(set(repo["branches"]), {"dev", "release/next"})

        def partially_failing(endpoint):
            if endpoint.endswith("/sample/branches"):
                raise RuntimeError("GitHub HTTP 403 权限不足或限流")
            return pages(endpoint)

        with patch.object(GitHubClient, "pages", side_effect=partially_failing):
            failed = collect_github(self.config)
        self.assertFalse(failed["complete"])
        self.assertEqual(sum(r["complete"] for r in failed["repositories"]), 1)
        self.assertIn(
            "403",
            next(r for r in failed["repositories"] if not r["complete"])["errors"][0],
        )
        args = parser().parse_args(["collect", "github"])
        self.assertEqual(args.targets, ["github"])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parser().parse_args(["collect", "github", "--host-snapshots", "old.json"])

    def test_github_pagination_and_redirect_guard(self):
        import io
        from urllib.error import HTTPError
        from urllib.request import Request

        api = GitHubClient(self.config)
        responses = []
        for size in (100, 1):
            stream = io.BytesIO(
                json.dumps([{"name": str(x)} for x in range(size)]).encode()
            )
            stream.url = "https://api.github.com/test"
            responses.append(stream)
        with patch.object(api.opener, "open", side_effect=responses) as request:
            self.assertEqual(len(api.pages("/test")), 101)
            self.assertIn("page=2", request.call_args[0][0].full_url)
        for status in (401, 403, 404, 429):
            with patch.object(
                api.opener,
                "open",
                side_effect=HTTPError(
                    "https://api.github.com/test", status, "", {}, None
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    api.pages("/test")
        with self.assertRaises(ValueError):
            SameOriginRedirect().redirect_request(
                Request("https://api.github.com/test"),
                None,
                302,
                "",
                {},
                "https://evil.invalid/",
            )

    def test_selective_hosts_and_partial_failure(self):
        def fake(config, identifier, progress=None):
            return {"host": {"id": identifier}, "complete": identifier == "A"}

        with patch("code_audit.collect.collect_host", side_effect=fake) as probe:
            self.assertEqual(len(list(collect_hosts(self.config, ["A"]))), 1)
            self.assertEqual(probe.call_args.args[1], "A")
            self.assertEqual(probe.call_count, 1)
            results = dict(collect_hosts(self.config, ["A", "B"]))
            self.assertTrue(results["A"]["complete"])
            self.assertFalse(results["B"]["complete"])

    def test_html_export_escapes_special_characters_and_redacts(self):
        snapshot = self.capture()
        result = compare_snapshots(self.persist(snapshot), self.persist(snapshot))
        payload = '</script><script>alert("injected")</script>'
        result["rows"][0]["repository"] = payload
        result["errors"].append(
            "https://user:password@github.com/org/repo private-secret"
        )
        result = redact(result, ["private-secret"])
        page = export_content(result, "html")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("user:password", page)
        self.assertNotIn("private-secret", page)

    def test_config_nested_devices_paths_and_credentials(self):
        entry = self.config["_hosts"]["D/A"]
        self.assertEqual(entry["device_id"], "D")
        self.assertEqual(host_options(self.config, "A")["code_paths"], [str(self.src)])
        self.assertEqual(host_options(self.config, "D/A")["device"]["id"], "D")
        self.assertEqual(
            self.config["settings"]["snapshots_dir"], str(self.root / "snapshots")
        )
        entry["config"]["ssh"]["password"] = "local-password"
        self.config["github"]["token"] = {"env": "AUDIT_TEST_TOKEN"}
        with patch.dict(os.environ, {"AUDIT_TEST_TOKEN": "local-token"}):
            self.assertEqual(
                set(configured_secrets(self.config)), {"local-password", "local-token"}
            )

    def write_config(self, data):
        import yaml

        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True))
        return load_config(self.config_path)

    def test_same_host_name_on_two_devices_selection_and_validation(self):
        host = {"ssh": {"host": "localhost"}, "code_paths": [str(self.src)]}
        raw = {
            "devices": {
                "设备甲": {
                    "hosts": {
                        "计算板": copy.deepcopy(host),
                        "桥接板": copy.deepcopy(host),
                    }
                },
                "设备乙": {"hosts": {"计算板": copy.deepcopy(host)}},
            }
        }
        config = self.write_config(raw)
        with self.assertRaisesRegex(ValueError, "仅支持主机"):
            select_hosts(config, ["设备甲"])
        self.assertEqual(select_hosts(config, ["设备乙/计算板"]), ["设备乙/计算板"])
        self.assertEqual(resolve_host(config, "计算板", "设备甲"), "设备甲/计算板")
        with self.assertRaisesRegex(ValueError, "歧义"):
            resolve_host(config, "计算板")
        with self.assertRaisesRegex(ValueError, "未知主机"):
            select_hosts(config, ["设备丙"])
        self.assertEqual(len(select_hosts(config, all_hosts=True)), 3)
        raw["devices"]["设备甲"]["hosts"]["计算板"]["ssh"]["jump"] = "桥接板"
        config = self.write_config(raw)
        self.assertEqual(
            host_options(config, "设备甲/计算板")["device"]["id"], "设备甲"
        )
        raw["devices"]["设备甲"]["hosts"]["桥接板"]["ssh"]["jump"] = "计算板"
        with self.assertRaisesRegex(ValueError, "循环"):
            self.write_config(raw)
        self.config_path.write_text("devices:\n  设备甲: {}\n  设备甲: {}\n")
        with self.assertRaisesRegex(ValueError, "重复"):
            load_config(self.config_path)
        self.config_path.write_text("boards: {}")
        with self.assertRaisesRegex(ValueError, "配置仅支持"):
            load_config(self.config_path)

    def test_code_paths_validation_and_config_location(self):
        host = {"ssh": {"host": "localhost"}, "code_paths": [str(self.src)]}
        raw = {"devices": {"D": {"hosts": {"A": host}}}}
        for paths in (
            [],
            "not-a-list",
            ["relative/path"],
            [str(self.src), str(self.repo)],
            [str(self.src), str(self.src / "../src")],
        ):
            host["code_paths"] = paths
            with self.assertRaisesRegex(ValueError, "code_paths|代码路径"):
                self.write_config(raw)
        host["code_paths"] = [str(self.src)]
        folder = self.root / "configs"
        folder.mkdir()
        self.config_path = folder / "config.yaml"
        config = self.write_config(raw)
        self.assertEqual(
            config["settings"]["snapshots_dir"], str(self.root / "snapshots")
        )
        self.assertEqual(config["settings"].get("reports_dir"), None)
        default = parser().parse_args(["collect", "github"]).config
        self.assertEqual(Path(default).parent.name, "configs")

    def test_arbitrary_paths_direct_repo_and_only_current_branch(self):
        self.git("branch", "not-collected")
        self.options["code_paths"] = [str(self.repo)]
        with patch.object(
            Probe, "command", autospec=True, side_effect=Probe.command
        ) as commands:
            snapshot = self.capture()
        self.assertEqual(snapshot["scope"]["branches"], "current")
        repo = snapshot["repositories"][0]
        self.assertEqual(repo["branch"], "dev")
        self.assertNotIn("branches", repo)
        self.assertEqual(repo["path"], ".")
        self.assertEqual(repo["key"], str(self.repo))
        self.assertEqual(repo["code_path"], str(self.repo))
        # Reading branch.dev.remote/merge is allowed; enumerating branch heads is not.
        self.assertFalse(
            any(
                call.args[2] in ("branch", "for-each-ref", "show-ref")
                for call in commands.call_args_list
            )
        )
        self.assertNotIn("workspace", json.dumps(snapshot))
        before = self.capture()
        alternate = self.root / "different-project-root"
        alternate.mkdir()
        self.options["code_paths"] = [str(self.repo), str(alternate)]
        multiple = self.capture()
        self.assertEqual(len(multiple["code_paths"]), 2)
        self.assertEqual(
            compare_snapshots(self.persist(before), self.persist(multiple))[
                "exit_code"
            ],
            0,
        )
        (self.root / "alias").symlink_to(self.repo, target_is_directory=True)
        self.options["code_paths"] = [str(self.repo), str(self.root / "alias")]
        self.assertFalse(Probe(self.options).collect()["complete"])

    def test_changed_code_root_preserves_repository_matching(self):
        before = self.capture()
        # Scan the same repository directly instead of through its parent.
        self.options["code_paths"] = [str(self.repo)]
        after = self.capture()
        report = compare_snapshots(self.persist(before), self.persist(after))
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["status"], "equal")

    def test_snapshot_load_and_compare_without_modification(self):
        snapshot = self.capture()
        path = self.persist(snapshot)
        original = path.read_bytes()
        report = compare_snapshots(path, self.persist(snapshot), history=True)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(load_snapshot(path)["host"]["qualified_id"], "D/A")
        self.assertNotIn("workspace", json.dumps(report))

    def test_history_does_not_mix_devices_with_same_host_name(self):
        a = self.capture()
        b = copy.deepcopy(a)
        b["device"]["id"] = "Other"
        b["host"]["qualified_id"] = "Other/A"
        with self.assertRaisesRegex(ValueError, "同一主机"):
            compare_snapshots(self.persist(a), self.persist(b), history=True)
        folder = self.root / "history-nested/hosts"
        save_json(folder / "D/A", a)
        save_json(folder / "Other/A", b)
        with self.assertRaisesRegex(ValueError, "缺少历史基线"):
            select_recent(folder.parent, "D/A", count=2)
        latest = self.capture()
        newest = save_json(folder / "D/A", latest)
        older, selected = select_recent(folder.parent, "D/A", count=2)
        self.assertEqual(selected, newest)
        self.assertEqual(load_snapshot(older)["host"]["qualified_id"], "D/A")

    def test_cli_host_selection_and_snapshot_storage(self):
        snapshot = self.capture()

        def fake(config, identifiers, progress=None, cancellation=None):
            for host_id in identifiers:
                data = copy.deepcopy(snapshot)
                data["host"] = host_options(config, host_id)["host"]
                yield host_id, data

        with (
            patch("code_audit.collect.collect_hosts", side_effect=fake) as collect,
            patch("sys.stdout"),
        ):
            self.assertEqual(
                main(["--config", str(self.config_path), "collect", "A", "B"]), 0
            )
        self.assertEqual(collect.call_args.args[1], ["D/A", "D/B"])
        self.assertEqual(
            len(list((self.root / "snapshots/hosts/D/A").glob("*.json"))), 1
        )
        self.assertEqual(
            len(list((self.root / "snapshots/hosts/D/B").glob("*.json"))), 1
        )
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parser().parse_args(["collect", "A", "--all", "--devices", "D"])

    def test_ssh_jump_resolves_within_device_and_does_not_scan_jump(self):
        import paramiko

        raw = {
            "devices": {
                "D": {
                    "hosts": {
                        "A": {
                            "ssh": {"host": "target.invalid", "jump": "B"},
                            "code_paths": [str(self.src)],
                        },
                        "B": {
                            "ssh": {"host": "jump.invalid"},
                            "code_paths": [str(self.src)],
                        },
                    }
                },
                "Other": {
                    "hosts": {
                        "B": {
                            "ssh": {"host": "wrong.invalid"},
                            "code_paths": [str(self.src)],
                        }
                    }
                },
            }
        }
        config = self.write_config(raw)
        snapshot = self.capture()
        jump_client, target_client = MagicMock(), MagicMock()
        channel = target_client.get_transport.return_value.open_session.return_value
        channel.recv_ready.side_effect = [True, False, False]
        channel.recv.return_value = json.dumps(snapshot).encode()
        channel.recv_stderr_ready.return_value = False
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        with patch.object(
            paramiko, "SSHClient", side_effect=[jump_client, target_client]
        ):
            result = collect_host(config, "D/A")
        self.assertTrue(result["complete"], result)
        self.assertEqual(
            jump_client.connect.call_args.kwargs["hostname"], "jump.invalid"
        )
        self.assertEqual(
            target_client.connect.call_args.kwargs["hostname"], "code-audit-D%2FA"
        )
        self.assertEqual(
            jump_client.get_transport.return_value.open_channel.call_args.args[1],
            ("target.invalid", 22),
        )
        self.assertTrue(channel.sendall.called)
        jump_client.get_transport.return_value.open_session.assert_not_called()
        jump_client.close.assert_called_once()
        target_client.close.assert_called_once()

    def test_github_only_config_and_cli_default_full_collection(self):
        config = self.write_config(
            {
                "github": {"organization": "example"},
                "settings": {"snapshots_dir": "snapshots"},
            }
        )

        def pages(endpoint):
            if endpoint.startswith("/orgs/"):
                return [{"full_name": "example/fresh", "private": True}]
            return [{"name": "future/branch", "commit": {"sha": "f" * 40}}]

        with (
            patch.object(GitHubClient, "pages", side_effect=pages),
            patch("sys.stdout"),
        ):
            self.assertEqual(
                main(["--config", str(self.config_path), "collect", "github"]), 0
            )
        output = list(
            Path(config["settings"]["snapshots_dir"]).joinpath("github").glob("*.json")
        )
        self.assertEqual(len(output), 1)
        snapshot = load_snapshot(output[0])
        self.assertTrue(snapshot["complete"])
        self.assertEqual(
            snapshot["repositories"][0]["branches"]["future/branch"], "f" * 40
        )
        # A GitHub snapshot collected first can be compared to a host collected later.
        host = self.capture()
        host["repositories"][0]["target"] = {
            "repository": "github.com/example/fresh",
            "branch": "future/branch",
        }
        report = compare_snapshots(self.persist(host), output[0])
        self.assertTrue(report["complete"])
        self.assertEqual(report["rows"][0]["right"]["branch"], "future/branch")


if __name__ == "__main__":
    unittest.main()
