#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_snapshot.py —— 采集 GitHub 仓库的「固定 commit 快照」

用法（照抄即可）：
    # 采集某个仓库默认分支的最新一版
    python collect_snapshot.py octocat/Hello-World

    # 精确锁定到某一个 commit（强烈推荐，保证可复现）
    python collect_snapshot.py octocat/Hello-World --ref 7fd1a60b01f91b314f59955a4e4d4e80d8edf11d

    # 顺便打成一个 zip 便于上传
    python collect_snapshot.py octocat/Hello-World --zip

产出目录结构：
    snapshots/
      octocat__Hello-World__7fd1a60/
        repo/           <- 展平后的仓库源码（顶层 xxx-yyy-sha/ 已自动去掉）
        metadata.json   <- 采集元数据（提交信息、统计、结构探测）
        snapshot.zip    <- 仅 --zip 时生成

技术要点：
  1) 不用 git clone。走 codeload tarball —— 本机 github.com 不可达也照样能采，
     且天然锁定 commit，不会出现「采完隔天内容变了」的问题。
  2) 解压时会自动剥掉 tar 包内层的 {owner}-{repo}-{sha}/ 顶层目录，
     让你直接拿到干净的仓库根。
  3) 使用 filter='data' 防御 tar 路径穿越（恶意仓库写入上层目录）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from gh_client import GHError, GitHub, parse_repo_arg

# --------------------------- 配置区 ---------------------------

# 统计时跳过的目录（这些是依赖/构建产物，不代表仓库自身质量）
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", ".gradle", ".idea",
    ".vscode", "vendor", ".tox", ".mypy_cache", ".pytest_cache",
    ".svn", ".hg", "bin", "obj", "coverage", ".terraform", ".dart_tool",
}

# 这些文件计入「行数」统计（其它扩展名只统计文件大小）
CODE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".java": "Java", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL",
    ".lua": "Lua", ".r": "R", ".m": "Objective-C", ".pl": "Perl",
}

# 判定「这是个正经工程」的关键配套文件
BUILD_FILES = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
    "poetry.lock", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
    "CMakeLists.txt", "Makefile", "mix.exs", "stack.yaml", "Package.swift",
    "pubspec.yaml", "deno.json", "flake.nix",
]

CI_PATHS = [
    ".github/workflows", ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml",
    "Jenkinsfile", ".circleci/config.yml", "bitbucket-pipelines.yml", ".drone.yml",
]

DOC_EXT = {".md", ".rst", ".txt", ".adoc", ".org"}


# --------------------------- 工具函数 ---------------------------


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def short_sha(sha: str, n: int = 8) -> str:
    return sha[:n]


def scan_directory(root: Path) -> dict:
    """遍历解压后的仓库，统计规模与结构特征。"""
    total_files = 0
    total_bytes = 0
    total_lines = 0
    lang_lines: dict = {}
    lang_files: dict = {}
    max_depth = 0

    has_readme = False
    has_license = False
    has_contributing = False
    has_tests = False
    has_submodules = False
    has_ci = False
    build_files_found: list = []
    top_level_entries: list = []

    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        rel_depth = len(dp.parts) - root_depth
        max_depth = max(max_depth, rel_depth)

        # 原地剪枝跳过依赖/构建目录
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        if rel_depth == 0:
            top_level_entries = sorted(dirnames + filenames)[:40]

        for fn in filenames:
            fp = dp / fn
            try:
                size = fp.stat().st_size
            except OSError:
                continue

            total_files += 1
            total_bytes += size

            lower = fn.lower()
            ext = fp.suffix.lower()

            # README 家族
            if lower.startswith("readme"):
                has_readme = True
            if lower.startswith("license") or lower.startswith("copying"):
                has_license = True
            if lower.startswith("contributing"):
                has_contributing = True
            if lower == ".gitmodules":
                has_submodules = True

            # 测试目录/文件探测
            parts_lower = {p.lower() for p in dp.relative_to(root).parts}
            if (
                "test" in lower or lower.startswith("test_") or lower.endswith("_test.py")
                or lower.endswith(".test.js") or lower.endswith(".spec.js")
                or lower.endswith("_test.go") or lower.endswith("test.ts")
            ):
                has_tests = True
            if any(p in {"test", "tests", "spec", "specs", "__tests__"} for p in parts_lower):
                has_tests = True

            # CI 探测（只要路径里出现即算）
            if ext == ".yml" or ext == ".yaml" or lower == "jenkinsfile":
                rel_unix = str(fp.relative_to(root)).replace("\\", "/")
                if any(rel_unix.startswith(c.rstrip("/")) or f"/{c}" in f"/{rel_unix}" for c in CI_PATHS):
                    has_ci = True

            # 构建文件（只认顶层，子包里的 package.json 不算工程根）
            if rel_depth == 0 and fn in BUILD_FILES:
                build_files_found.append(fn)

            # 代码行数统计
            if ext in CODE_EXT:
                lang = CODE_EXT[ext]
                try:
                    if size > 3 * 1024 * 1024:  # 跳过超大生成文件
                        lines = 0
                    else:
                        with fp.open("r", encoding="utf-8", errors="ignore") as f:
                            lines = sum(1 for _ in f)
                except OSError:
                    lines = 0
                total_lines += lines
                lang_lines[lang] = lang_lines.get(lang, 0) + lines
                lang_files[lang] = lang_files.get(lang, 0) + 1

    # CI 目录单独补一次（os.walk 的 yml 判断可能漏掉空 workflows 目录）
    if not has_ci:
        for c in CI_PATHS:
            p = root / c
            if p.exists():
                has_ci = True
                break

    return {
        "file_count": total_files,
        "total_size_bytes": total_bytes,
        "total_size_human": human_size(total_bytes),
        "code_lines": total_lines,
        "code_line_threshold_exceeded_files_ignored": True,
        "max_dir_depth": max_depth,
        "languages_by_lines": dict(
            sorted(lang_lines.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "languages_by_files": dict(
            sorted(lang_files.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "has_readme": has_readme,
        "has_license": has_license,
        "has_contributing": has_contributing,
        "has_tests": has_tests,
        "has_ci": has_ci,
        "has_submodules": has_submodules,
        "build_files": sorted(set(build_files_found)),
        "top_level_entries": top_level_entries,
    }


def strip_top_dir(extracted_root: Path) -> Path:
    """tar 包内通常只有 1 个顶层目录 {owner}-{repo}-{sha}，剥掉它，让 repo/ 就是仓库根。

    安全条件：解压结果里恰好只有 1 个子目录、且没有任何散落的根目录文件。
    否则原样返回，避免误剥。
    """
    dirs = [p for p in extracted_root.iterdir() if p.is_dir()]
    files = [p for p in extracted_root.iterdir() if not p.is_dir()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return extracted_root


def make_zip(src_dir: Path, zip_path: Path) -> None:
    import zipfile

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    zf.write(fp, fp.relative_to(src_dir))
                except OSError:
                    pass


# --------------------------- 主流程 ---------------------------


def collect(
    gh: GitHub,
    owner: str,
    name: str,
    ref: Optional[str],
    out_base: Path,
    do_zip: bool,
    keep_tar: bool = False,
) -> Path:
    print(f"[1/6] 读取仓库信息 {owner}/{name} …")
    info = gh.repo(owner, name)

    if info.get("fork"):
        print("  [警告] 这是一个 fork 仓库。多数采集任务不收 fork，请确认任务规则。")
    if info.get("archived"):
        print("  [警告] 仓库已归档(archived)，可能不满足「活跃仓库」要求。")

    # 未指定 ref 时，解析默认分支的最新 commit
    if ref:
        print(f"[2/6] 锁定指定 ref = {ref}")
    else:
        print("[2/6] 未指定 ref，解析默认分支最新 commit …")
        ref = gh.default_branch_sha(owner, name)["sha"]
        print(f"  -> {ref}")

    print(f"[3/6] 获取 commit 详情 …")
    cinfo = gh.commit(owner, name, ref)
    sha = cinfo["sha"]
    ccommit = cinfo.get("commit", {})
    author = ccommit.get("author", {}) or {}

    print(f"[4/6] 下载快照：{owner}/{name} @ {short_sha(sha)}")
    tarball_url = gh.tarball_url(owner, name, sha)
    raw = gh.download_bytes(tarball_url)
    print(f"  -> {human_size(len(raw))}")

    tar_sha256 = hashlib.sha256(raw).hexdigest()

    # 组装输出目录
    safe = f"{owner}__{name}__{short_sha(sha)}"
    out_dir = out_base / safe
    repo_dir = out_dir / "repo"

    print(f"[5/6] 解压到 {repo_dir} …")
    if repo_dir.exists():
        print("  [提示] 目录已存在，将被覆盖")
    repo_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "snapshot.tar.gz"
        tar_path.write_bytes(raw)
        # 关键：解压到 td 的**子目录**里。
        # 若直接解压到 td，tar 包本身会成为 td 下的第二个条目，
        # 导致 strip_top_dir 的「只有 1 个顶层目录」判断失效。
        extract_dir = Path(td) / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tf:
            # filter='data' 阻断路径穿越与特殊设备文件（Python 3.12+）
            try:
                tf.extractall(extract_dir, filter="data")
            except TypeError:
                # Python < 3.12 无此参数
                tf.extractall(extract_dir)

        extracted_root = strip_top_dir(extract_dir)
        # 移动到目标位置
        for item in extracted_root.iterdir():
            dest = repo_dir / item.name
            if item.is_dir():
                _copy_tree(item, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(item.read_bytes())

    print(f"[6/6] 扫描仓库结构 …")
    stats = scan_directory(repo_dir)

    lic = None
    try:
        lic = gh.license(owner, name)
    except GHError:
        pass

    langs_api = {}
    try:
        langs_api = gh.languages(owner, name)
    except GHError:
        pass

    metadata = {
        "schema_version": "1.0",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collection_method": "github_api + codeload_tarball (no git clone)",
        "repository": {
            "full_name": info["full_name"],
            "owner": owner,
            "name": name,
            "html_url": info["html_url"],
            "description": info.get("description"),
            "homepage": info.get("homepage"),
            "created_at": info.get("created_at"),
            "pushed_at": info.get("pushed_at"),
            "default_branch": info.get("default_branch"),
            "stargazers_count": info.get("stargazers_count"),
            "forks_count": info.get("forks_count"),
            "open_issues_count": info.get("open_issues_count"),
            "watchers_count": info.get("watchers_count"),
            "size_kb": info.get("size"),
            "is_fork": info.get("fork"),
            "is_archived": info.get("archived"),
            "is_disabled": info.get("disabled"),
            "topics": info.get("topics", []),
            "license_spdx": (lic or {}).get("license", {}).get("spdx_id"),
            "license_name": (lic or {}).get("license", {}).get("name"),
            "languages_api": langs_api,
        },
        "snapshot": {
            "ref_requested": ref,
            "commit_sha": sha,
            "commit_short": short_sha(sha),
            "commit_message": (ccommit.get("message") or ""),
            "commit_author_name": author.get("name"),
            "commit_author_email": author.get("email"),
            "commit_date": author.get("date"),
            "commit_url": cinfo.get("html_url"),
            "parents": [p["sha"] for p in cinfo.get("parents", [])],
            "parent_count": len(cinfo.get("parents", [])),
            "is_merge_commit": len(cinfo.get("parents", [])) > 1,
            "tarball_url": tarball_url,
            "tarball_sha256": tar_sha256,
        },
        "scan": stats,
    }

    meta_path = out_dir / "metadata.json"
    meta_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if do_zip:
        zp = out_dir / "snapshot.zip"
        make_zip(repo_dir, zp)
        print(f"  zip -> {zp} ({human_size(zp.stat().st_size)})")

    # ---------------- 控制台摘要 ----------------
    r = metadata["repository"]
    s = metadata["snapshot"]
    sc = stats
    print()
    print("=" * 58)
    print(f"  仓库      : {r['full_name']}")
    print(f"  快照 commit: {s['commit_short']}  ({s['commit_date']})")
    print(f"  license   : {r['license_spdx'] or '无'}")
    print(f"  star/fork : {r['stargazers_count']} / {r['forks_count']}")
    print(
        f"  规模      : {sc['file_count']} 文件 | {sc['code_lines']} 行代码 | "
        f"{sc['total_size_human']}"
    )
    print(
        f"  配套      : README={sc['has_readme']} LICENSE={sc['has_license']} "
        f"测试={sc['has_tests']} CI={sc['has_ci']} 构建文件={len(sc['build_files'])}"
    )
    if sc["has_submodules"]:
        print("  [注意] 含 submodule —— tarball 不会带子模块内容，需要在说明里标注")
    print(f"  元数据    : {meta_path}")
    print(f"  源码目录  : {repo_dir}")
    print("=" * 58)
    print()
    print("下一步：")
    print(f"  python audit_repo.py {owner}/{name} --ref {s['commit_short']}")

    return out_dir


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        dp = Path(dirpath)
        rel = dp.relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            try:
                (target / fn).write_bytes((dp / fn).read_bytes())
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description="采集 GitHub 仓库的固定 commit 快照（不依赖本地 git）"
    )
    ap.add_argument("repo", help="owner/repo 或 GitHub URL")
    ap.add_argument(
        "--ref",
        help="要锁定的 commit sha / 分支 / tag。强烈建议显式指定以保证可复现",
    )
    ap.add_argument("--out", default=None, help="输出根目录，默认 ./snapshots")
    ap.add_argument("--zip", action="store_true", help="额外打包 zip")
    ap.add_argument("--token", default=None, help="GitHub Token，也可设环境变量 GITHUB_TOKEN")

    args = ap.parse_args()

    try:
        owner, name = parse_repo_arg(args.repo)
    except ValueError as e:
        print(f"参数错误：{e}")
        raise SystemExit(1)

    here = Path(__file__).resolve().parent
    out_base = Path(args.out) if args.out else (here / "snapshots")
    out_base.mkdir(parents=True, exist_ok=True)

    gh = GitHub(token=args.token)
    try:
        collect(gh, owner, name, args.ref, out_base, args.zip)
    except GHError as e:
        print(f"\n[失败] {e}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
