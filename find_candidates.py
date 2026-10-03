#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_candidates.py —— 批量初筛可用 Milestone 候选

解决的问题：
    手动一个个试 PR 编号不现实。这个脚本一次列出仓库最近若干个已合并 PR，
    先用「零 API 成本」的标题/作者规则滤掉明显的垃圾样本（bot 依赖更新、
    纯文档、chore），再可选自动对幸存者跑完整的 audit_milestone 质检并排序。

用法：
    # 只看候选清单（约消耗 1~3 次 API）
    python find_candidates.py psf/requests --pages 3

    # 自动对初筛通过的前 8 个跑完整质检并排序（消耗较多 API，建议先配 Token）
    python find_candidates.py psf/requests --pages 3 --auto-audit 8

    # 只输出 JSON
    python find_candidates.py psf/requests --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from gh_client import GHError, GitHub, parse_repo_arg

# 复用 audit_milestone 里的机器人识别规则，保持两处判定一致
try:
    from audit_milestone import BOT_LOGIN_RE, BOT_TITLE_RE, audit
except ImportError:  # 脚本被单独移动时的兜底
    audit = None  # type: ignore
    BOT_LOGIN_RE = re.compile(r"(dependabot|renovate|snyk|github-actions)", re.I)
    BOT_TITLE_RE = re.compile(r"^(bump|chore)\b", re.I)

# 初步排除的标题模式：CI/文档/版本号/release/i18n 批量更新
NOISE_TITLE_RE = re.compile(
    r"^(i18n|l10n|translations?)\b"
    r"|^docs?\b[:/ ]"
    r"|^typo|^fix typo|^typos"
    r"|^release\b|^v?\d+\.\d+\.\d+"
    r"|^update (changelog|readme|docs?)"
    r"|^add (translation|locale)"
    # 发版 / 版本号 / 依赖例行：不构成可交付的开发任务
    r"|^bump version|^bump to v?\d|release notes|^changelog"
    r"|^chore\b[:/ )]"
    r"|^ci\b[:/ ]|^ci:"
    r"|^\[skip ci\]|^merge (branch|remote|master|main)"
    r"|^sync .* branch",
    re.I,
)

# 可能是「功能性改动」的标题信号（加分用，非硬性）
FEATURE_SIGNAL_RE = re.compile(
    r"\b(fix(e[sd])?|add(s|ed|ing)?|support|implement|handle|refactor|"
    r"improve|optimiz|resolve|prevent|allow|introduce|rewrite|migrate)\b",
    re.I,
)


def fetch_merged_pulls(gh: GitHub, owner: str, name: str, pages: int = 3) -> list:
    """拉取已合并 PR 列表。注意 list API 不含 additions/deletions/files 字段。"""
    items: list = []
    for page in range(1, pages + 1):
        try:
            batch = gh.json_get(
                f"/repos/{owner}/{name}/pulls",
                {"state": "closed", "per_page": 30, "page": page,
                 "sort": "updated", "direction": "desc"},
            )
        except GHError as e:
            print(f"  [警告] 第 {page} 页拉取失败：{e}")
            break
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 30:
            break
    return [p for p in items if p.get("merged_at")]


def pre_screen(pr: dict) -> tuple:
    """零成本初筛。返回 (是否保留, 初筛理由, 预评分 0~100)。"""
    title = pr.get("title", "") or ""
    login = ((pr.get("user") or {}).get("login") or "")

    if login.endswith("[bot]") or BOT_LOGIN_RE.search(login) or BOT_TITLE_RE.search(title):
        return False, f"机器人提交 ({login})", 0
    if NOISE_TITLE_RE.search(title):
        return False, "标题命中噪声模式（文档/CI/i18n/release）", 0

    score = 40
    reasons = []
    if FEATURE_SIGNAL_RE.search(title):
        score += 15
        reasons.append("标题含功能性动词")
    else:
        reasons.append("标题未体现明确功能语义")

    body_len = len(pr.get("body") or "")
    if body_len >= 300:
        score += 20
        reasons.append(f"PR 描述较充分({body_len}字符)")
    elif body_len >= 80:
        score += 10
        reasons.append(f"PR 描述偏短({body_len}字符)")
    else:
        reasons.append(f"PR 描述过短/为空({body_len}字符)")

    # PR 标题里的 Fixes #N
    if re.search(r"#\d+", title):
        score += 10
        reasons.append("标题引用了 issue 编号")

    return True, "；".join(reasons), min(score, 100)


def main() -> None:
    ap = argparse.ArgumentParser(description="批量初筛 Milestone 候选")
    ap.add_argument("repo", help="owner/repo 或 URL")
    ap.add_argument("--pages", type=int, default=3, help="翻多少页(每页30)，默认3")
    ap.add_argument("--auto-audit", type=int, default=0,
                    help="对初筛通过的前 N 个跑完整质检并排序（消耗较多 API）")
    ap.add_argument("--profile", choices=["strict", "balanced", "loose"],
                    default="balanced")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    owner, name = parse_repo_arg(args.repo)
    gh = GitHub(token=args.token)

    if args.auto_audit and not gh.token:
        print(
            f"[提示] 未配置 Token，API 配额仅 60 次/小时；"
            f"--auto-audit {args.auto_audit} 大约需要 {args.auto_audit * 3}~{args.auto_audit * 5} 次调用，"
            f"很可能触发限流。建议先设置 GITHUB_TOKEN。是否继续请自行判断。"
        )

    print(f"拉取 {owner}/{name} 最近已合并 PR（最多 {args.pages * 30} 个）…")
    pulls = fetch_merged_pulls(gh, owner, name, args.pages)
    print(f"  获得 {len(pulls)} 个已合并 PR\n")

    kept: list = []
    dropped: list = []
    for p in pulls:
        ok, reason, pre = pre_screen(p)
        entry = {
            "number": p["number"],
            "title": p.get("title", ""),
            "author": (p.get("user") or {}).get("login"),
            "merged_at": p.get("merged_at"),
            "url": p.get("html_url"),
            "pre_score": pre,
            "pre_reason": reason,
        }
        (kept if ok else dropped).append(entry)

    kept.sort(key=lambda x: x["pre_score"], reverse=True)

    if not args.json:
        print("=" * 72)
        print(f"  初筛结果：保留 {len(kept)} / 丢弃 {len(dropped)}")
        print("=" * 72)
        print("  【被丢弃】按类型统计：")
        reason_kind: dict = {}
        for d in dropped:
            k = d["pre_reason"].split("(")[0].strip()[:18]
            reason_kind[k] = reason_kind.get(k, 0) + 1
        for k, v in sorted(reason_kind.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>3} 个  {k}")
        print("-" * 72)

    # ---- 可选：深度复检 ----
    deep_results: list = []
    if args.auto_audit and kept:
        if audit is None:
            print("  [错误] 未能导入 audit_milestone，无法执行深度质检")
        else:
            targets = kept[: args.auto_audit]
            print(f"  对前 {len(targets)} 个候选执行完整质检（profile={args.profile}）…\n")
            for t in targets:
                try:
                    r = audit(gh, owner, name, t["number"], args.profile)
                except GHError as e:
                    print(f"    #{t['number']} 质检失败：{e}")
                    continue
                deep_results.append({
                    "number": t["number"],
                    "title": t["title"],
                    "verdict": r["verdict"],
                    "score": r["score"],
                    "changed_lines": r["pull_request"]["changed_lines"],
                    "changed_files": r["pull_request"]["changed_files"],
                    "code_ratio": r["diff_analysis"]["code_ratio"],
                    "has_test_change": r["diff_analysis"]["test_lines"] > 0,
                    "linked_issue": (r["linked_issue"] or {}).get("number"),
                    "before_ref": r["snapshot_refs"]["before_snapshot_ref"],
                    "after_ref": r["snapshot_refs"]["after_snapshot_ref"],
                    "url": t["url"],
                })
                print(f"    #{t['number']:<6} {r['verdict']:<5} {r['score']:>5}  {t['title'][:44]}")

            deep_results.sort(key=lambda x: x["score"], reverse=True)

    if args.json:
        print(json.dumps({"kept": kept, "dropped": dropped, "deep": deep_results},
                         ensure_ascii=False, indent=2))
        return

    print("=" * 72)
    print("  【候选清单】按初筛分排序")
    print("=" * 72)
    for k in kept[:20]:
        print(f"    #{k['number']:<6} 预估分 {k['pre_score']:>3}  {k['title'][:52]}")
        print(f"           {k['pre_reason']}")

    if deep_results:
        print("=" * 72)
        print("  【深度质检排序】直接用这个挑题")
        print("=" * 72)
        for d in deep_results:
            flag = {"PASS": "推荐", "WARN": "待定", "FAIL": "不推荐"}[d["verdict"]]
            issue = f"issue#{d['linked_issue']}" if d["linked_issue"] else "无issue"
            print(f"    [{flag}] #{d['number']:<6} {d['score']:>5}分  "
                  f"{d['changed_lines']}行/{d['changed_files']}文件  "
                  f"代码{d['code_ratio']:.0%} 测试={d['has_test_change']} {issue}")
            print(f"            {d['title'][:56]}")
        best = [d for d in deep_results if d["verdict"] == "PASS"]
        if best:
            b = best[0]
            print("-" * 72)
            print("  【建议：第一个PASS基线操作】")
            print(f"    python collect_snapshot.py {owner}/{name} --ref {b['before_ref']} --zip")
            print(f"    python collect_snapshot.py {owner}/{name} --ref {b['after_ref']} --zip")
        else:
            print("-" * 72)
            print("  未出现 PASS 样本。建议：--auto-audit 更大 / 换仓库 / 或检查是否任务要求过严")

    print("=" * 72)
    print("  下一步：python audit_milestone.py "
          f"{owner}/{name} --pr <编号> --profile {args.profile}")


if __name__ == "__main__":
    main()
