"""Offline Git version comparison. No network or external commands."""

from collections import defaultdict

from .storage import normalize_repo, now


def modification(status):
    counts = dict.fromkeys(
        ("staged", "unstaged", "untracked", "deleted", "renamed", "conflict"), 0
    )
    for entry in status:
        code = entry["code"]
        if code == "??":
            counts["untracked"] += 1
            continue
        if code in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
            counts["conflict"] += 1
            continue
        if code[0] not in " ?!":
            counts["staged"] += 1
        if code[1] not in " ?!":
            counts["unstaged"] += 1
        if "D" in code:
            counts["deleted"] += 1
        if "R" in code:
            counts["renamed"] += 1
    return {"dirty": bool(status), "counts": counts, "files": status}


def side(repo):
    if repo is None:
        return None
    return {
        "path": repo["key"],
        "code_path": repo.get("code_path"),
        "branch": repo.get("branch"),
        "head": repo.get("head"),
        "commit": repo.get("commit"),
        "modification": modification(repo["status"]),
        "errors": repo["errors"],
    }


def source(data, label):
    return {
        "label": label,
        "id": data["id"],
        "kind": data["kind"],
        "host": data.get("host"),
        "device": data.get("device"),
        "started_at": data["started_at"],
        "finished_at": data["finished_at"],
        "complete": data["complete"],
        "errors": data["errors"],
        "code_paths": data.get("code_paths", []),
    }


def pair_repositories(left, right):
    groups = [defaultdict(list), defaultdict(list)]
    for group, snapshot in zip(groups, (left, right)):
        for repo in snapshot["repositories"]:
            identity = normalize_repo(repo.get("identity")) or "path:" + repo["key"]
            group[identity].append(repo)
    for identity in sorted(set(groups[0]) | set(groups[1])):
        ls, rs = list(groups[0][identity]), list(groups[1][identity])
        if len(ls) == len(rs) == 1:
            yield identity, ls[0], rs[0], False
            continue
        for repo in list(ls):
            matches = [r for r in rs if r["key"] == repo["key"]]
            if len(matches) == 1:
                other = matches[0]
                ls.remove(repo)
                rs.remove(other)
                yield identity, repo, other, False
        if len(ls) == len(rs) == 1:
            yield identity, ls[0], rs[0], False
        elif ls and rs:
            yield identity, ls, rs, True
        else:
            for repo in ls:
                yield identity, repo, None, False
            for repo in rs:
                yield identity, None, repo, False


def compare(left, right, labels, history=False):
    if left["kind"] == right["kind"] == "github":
        raise ValueError("不支持 GitHub 与 GitHub 比较")
    reversed_sources = left["kind"] == "github"
    if reversed_sources:
        left, right = right, left
        labels = list(reversed(labels))
    if history and (
        right["kind"] != "host"
        or (
            left["host"]["qualified_id"] != right["host"]["qualified_id"]
            or left["host"]["id"] != right["host"]["id"]
        )
    ):
        raise ValueError("历史比较需要同一主机的两份快照")
    github = right["kind"] == "github"
    result = {
        "kind": "comparison",
        "created_at": now(),
        "mode": "github" if github else "history" if history else "hosts",
        "sources": [source(left, labels[0]), source(right, labels[1])],
        "rows": [],
        "errors": [],
    }
    if not left["complete"] or not right["complete"]:
        result["errors"].append(
            "输入包含不完整快照；完整采集并配对的仓库单独判定，信息不足的仓库显示无法判断"
        )
    if github:
        remotes = {normalize_repo(r["identity"]): r for r in right["repositories"]}
        pairs = [
            (
                r.get("identity") or r["key"],
                r,
                remotes.get(normalize_repo(r.get("target", {}).get("repository"))),
                False,
            )
            for r in left["repositories"]
        ]
    else:
        pairs = pair_repositories(left, right)
    for identity, lrepo, rrepo, ambiguous in pairs:
        row = {
            "repository": identity,
            "left": None,
            "right": None,
            "code_paths": [],
            "branch_equal": None,
            "head_equal": None,
            "complete": True,
            "different": False,
            "conclusion": "",
            "errors": [],
        }
        result["rows"].append(row)
        repositories = (lrepo if isinstance(lrepo, list) else [lrepo]) + (
            rrepo if isinstance(rrepo, list) else [rrepo]
        )
        row["code_paths"] = sorted(
            {r["code_path"] for r in repositories if r and r.get("code_path")}
        )
        if ambiguous:
            row.update(
                complete=False,
                conclusion="无法判断：仓库副本匹配歧义",
                errors=[
                    "左侧：" + "、".join(r["key"] for r in lrepo),
                    "右侧：" + "、".join(r["key"] for r in rrepo),
                ],
            )
            continue
        row["left"] = side(lrepo)
        if github:
            target = lrepo.get("target", {})
            row["target"] = target
            if not target.get("repository") or not target.get("branch"):
                row["errors"].append("未确定 GitHub 比较目标")
            elif rrepo is None:
                row["errors"].append("GitHub 快照未覆盖目标仓库")
            elif not rrepo["complete"]:
                row["errors"].extend(rrepo["errors"] or ["GitHub 仓库采集不完整"])
            elif target["branch"] not in rrepo["branches"]:
                row["errors"].append("GitHub 快照不存在目标分支：" + target["branch"])
            else:
                row["right"] = {
                    "branch": target["branch"],
                    "head": rrepo["branches"][target["branch"]],
                    "commit": None,
                    "path": target["repository"],
                    "modification": None,
                    "errors": [],
                }
            if not lrepo["complete"]:
                row["errors"].extend(lrepo["errors"] or ["主机仓库采集不完整"])
        else:
            row["right"] = side(rrepo)
            if lrepo is None or rrepo is None:
                selected = lrepo or rrepo
                row["complete"] = (
                    left["complete"] and right["complete"] and selected["complete"]
                )
                row["different"] = bool(row["complete"])
                row["conclusion"] = (
                    ("仅右侧存在" if lrepo is None else "仅左侧存在")
                    if row["complete"]
                    else "无法判断：采集不完整"
                )
                if not row["complete"]:
                    row["errors"].append("不能确认另一侧确实不存在该仓库")
                continue
            if not lrepo["complete"] or not rrepo["complete"]:
                row["errors"].extend(
                    lrepo["errors"] + rrepo["errors"] or ["仓库采集不完整"]
                )
        if row["errors"]:
            row.update(complete=False, conclusion="无法判断")
            continue
        ls, rs = row["left"], row["right"]
        row["branch_equal"] = (
            ls["branch"] == rs["branch"] if ls["branch"] and rs["branch"] else None
        )
        row["head_equal"] = ls["head"] == rs["head"]
        # GitHub compares HEAD to the recorded tracking target, not local branch spelling.
        row["different"] = not row["head_equal"] or (
            not github and row["branch_equal"] is False
        )
        differences = []
        if not github and row["branch_equal"] is False:
            differences.append("分支不同")
        if not row["head_equal"]:
            differences.append("提交不同")
        row["conclusion"] = "；".join(differences) if differences else "版本一致"
    rows = result["rows"]
    for row in rows:
        row["status"] = (
            "unknown"
            if not row["complete"]
            else "missing"
            if row["left"] is None or row["right"] is None
            else "different"
            if row["different"]
            else "equal"
        )
    result["complete"] = not result["errors"] and all(r["complete"] for r in rows)
    result["summary"] = {
        "repositories": len(rows),
        "different": sum(r["different"] for r in rows),
        "unknown": sum(not r["complete"] for r in rows),
        "modified": sum(
            any(
                s and s.get("modification") and s["modification"]["dirty"]
                for s in (r["left"], r["right"])
            )
            for r in rows
        ),
    }
    result["exit_code"] = (
        2 if not result["complete"] else 1 if result["summary"]["different"] else 0
    )
    if reversed_sources:
        result["sources"].reverse()
        for row in rows:
            row["left"], row["right"] = row["right"], row["left"]
    return result
