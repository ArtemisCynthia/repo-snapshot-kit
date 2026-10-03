#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish_to_github.py —— 把本工具包发布到你的 GitHub（**不需要 git**）

用途：
    在 git 协议不可用的网络环境下，用 GitHub REST API 直接创建仓库并写入文件。
    全程不调用任何 git 命令，不需要配置 SSH / credential / user.name。

用法：
    # 先看看会上传哪些文件（不产生任何写操作）
    python publish_to_github.py --token <你的token> --dry-run

    # 正式发布
    python publish_to_github.py --token <你的token>

    # 自定义仓库名与描述
    python publish_to_github.py --token <token> --repo my-kit --desc "..."

Token 要求（很重要）：
    推荐 **classic** Personal Access Token，勾选 `public_repo`（公开仓库读写）。
    申请路径：https://github.com/settings/tokens → Generate new token (classic)

    若改用 fine-grained token，需要额外授予**账户级** `Repository creation: write`，
    以及仓库级 `Contents: Read and write`，否则会在建仓或写文件时报 403。
    没把握就用 classic + public_repo，最省事。
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from gh_client import GHError, GitHub

HERE = Path(__file__).resolve().parent

# 只上传这些扩展名的顶层文件
INCLUDE_EXT = {".py", ".md", ".txt", ".yml", ".yaml", ".toml"}
# 始终上传（无扩展名或特殊名）
INCLUDE_NAMES = {".gitignore", "LICENSE", "Makefile"}
# 不上传的目录
EXCLUDE_DIRS = {"snapshots", "reports", "__pycache__", ".git", ".workbuddy", ".tmp", ".idea"}
# 不上传的文件
EXCLUDE_FILES = {"metadata.json", "snapshot.zip", "config.json"}

GITIGNORE_CONTENT = """# 采集产物（体积大且随任务变化，不入版本库）
snapshots/
reports/

# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/

# 本地配置 / 凭证
.env
config.json
*.token

# 编辑器
.idea/
.vscode/
.DS_Store
"""

DEFAULT_DESC = (
    "面向 AI Coding 数据集构建场景的 GitHub 仓库快照采集与质量质检工具链。"
    "按 commit 精确采集 before/after 双快照，并对仓库与 Milestone 做可量化自动质检。"
)


def collect_files() -> list:
    """收集要上传的顶层文件。"""
    items = []
    for p in sorted(HERE.iterdir()):
        if p.is_dir():
            continue
        if p.name in EXCLUDE_FILES:
            continue
        if p.suffix.lower() in INCLUDE_EXT or p.name in INCLUDE_NAMES:
            items.append(p)
    return items


def existing_file_sha(gh: GitHub, owner: str, repo: str, path: str) -> str | None:
    """文件已存在时返回其 sha（更新文件必须带 sha，否则报 422）。"""
    try:
        data = gh.json_get(f"/repos/{owner}/{repo}/contents/{path}")
        return data.get("sha")
    except GHError as e:
        if e.status == 404:
            return None
        raise


def publish(gh: GitHub, repo_name: str, desc: str, private: bool, dry_run: bool) -> None:
    print("验证 Token 并获取账号信息 …")
    try:
        me = gh.json_get("/user")
    except GHError as e:
        print(f"[失败] 无法获取账号信息：{e}")
        print("  请确认 Token 有效且未过期。")
        raise SystemExit(1)

    login = me.get("login")
    print(f"  账号：{login}  (公开仓库数 {me.get('public_repos')})")

    files = collect_files()
    if not files:
        print("没有找到可上传的文件，已中止。")
        raise SystemExit(1)

    print(f"\n待上传 {len(files)} 个文件 + 1 个 .gitignore：")
    for f in files:
        print(f"    {f.name:<24} {f.stat().st_size/1024:.1f} KB")

    if dry_run:
        print("\n[dry-run] 以上为将要上传的内容，未执行任何写操作。")
        print(f"  目标仓库：https://github.com/{login}/{repo_name}")
        return

    # ---------------- 创建或复用仓库 ----------------
    print(f"\n创建仓库 {login}/{repo_name} …")
    repo_info = None
    try:
        repo_info = gh.json_post(
            "/user/repos",
            {
                "name": repo_name,
                "description": desc,
                "homepage": "",
                "private": private,
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
                "auto_init": False,   # 不初始化，避免产生 README 冲突
                "license_template": "mit",
            },
        )
        print(f"  已创建：{repo_info['html_url']}")
    except GHError as e:
        if e.status == 422:
            print(f"  同名仓库已存在，改为复用它并覆盖文件内容。")
            try:
                repo_info = gh.repo(login, repo_name)
            except GHError as e2:
                print(f"[失败] 无法访问已存在仓库：{e2}")
                raise SystemExit(1)
        else:
            print(f"[失败] 创建仓库失败：{e}")
            raise SystemExit(1)

    owner = repo_info["owner"]["login"]
    repo = repo_info["name"]

    # ---------------- 上传文件 ----------------
    print(f"\n上传文件到 {owner}/{repo} …")
    to_upload = [(p, p.read_bytes()) for p in files]
    to_upload.append((Path(".gitignore"), GITIGNORE_CONTENT.encode("utf-8")))

    ok_count = 0
    for rel, content in to_upload:
        path = rel.name
        b64 = base64.b64encode(content).decode("ascii")
        payload = {
            "message": f"add {path}",
            "content": b64,
        }
        sha = existing_file_sha(gh, owner, repo, path)
        if sha:
            payload["sha"] = sha
            payload["message"] = f"update {path}"
        try:
            gh.json_put(f"/repos/{owner}/{repo}/contents/{path}", payload)
            print(f"    √ {path}")
            ok_count += 1
        except GHError as e:
            print(f"    × {path}  失败：{e}")
            if e.status in (401, 403):
                print("\n  Token 权限不足，后续文件也会失败，已中止。")
                print("  请改用 classic token 并勾选 public_repo。")
                print("  （若是用 fine-grained token，需授予 Contents: Read and write）")
                raise SystemExit(1)

    print()
    print("=" * 60)
    print(f"  发布完成：{repo_info['html_url']}")
    print(f"  成功写入 {ok_count}/{len(to_upload)} 个文件")
    print("=" * 60)
    print()
    print("资质材料可填这个链接：")
    print(f"  {repo_info['html_url']}")
    print()
    print("提示：仓库刚创建时 GitHub 索引可能需要几秒，稍等再访问。")


def main() -> None:
    ap = argparse.ArgumentParser(description="把工具包发布到 GitHub（无需 git）")
    ap.add_argument("--token", default=None, help="GitHub classic token；也可设 GITHUB_TOKEN 环境变量")
    ap.add_argument("--repo", default="repo-snapshot-kit", help="仓库名")
    ap.add_argument("--desc", default=DEFAULT_DESC, help="仓库描述")
    ap.add_argument("--private", action="store_true", help="建为私有仓库（默认公开）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要上传的内容")
    args = ap.parse_args()

    gh = GitHub(token=args.token)
    if not gh.token:
        print("缺少 Token。请先申请：")
        print("  https://github.com/settings/tokens")
        print("  → Generate new token (classic) → 勾选 public_repo")
        print("然后运行：")
        print(f"  python publish_to_github.py --token <token> --dry-run")
        raise SystemExit(1)

    publish(gh, args.repo, args.desc, args.private, args.dry_run)


if __name__ == "__main__":
    main()
