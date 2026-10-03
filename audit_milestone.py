#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_milestone.py —— Milestone 质量【自动质检】

一个合格的 Milestone 通常 = 「一个有明确验收标准的 Issue」+「一个与之对应的 merged PR」。
这个脚本自动把这两者的质量量化出来，并直接给你接下来要采集的 before/after 两个 commit。

用法：
    python audit_milestone.py owner/repo --pr 1234
    python audit_milestone.py owner/repo --pr 1234 --json --save

输出要点：
    - before_snapshot_ref : 应采集的「改动前」commit (PR base)
    - after_snapshot_ref  : 应采集的「改动后」commit (PR merge/head)
    - 各项硬指标是否通过、综合评分、是否需要人工复核

退出码：0 = PASS，2 = WARN，1 = FAIL
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from gh_client import GHError, GitHub, parse_repo_arg

# ============================ 阈值配置 ============================
THRESHOLDS = {
    "min_changed_lines": 10,        # diff 太小 -> 没内容
    "max_changed_lines": 2000,      # diff 太大 -> 无法评判、上下文失控
    "max_files": 40,                # 涉及文件过多 -> 边界不清
    "min_files": 1,
    "require_merged": True,
    "require_issue_link": True,     # PR 里是否出现 Fixes/Closes #N
    "require_code_change": True,
    "min_code_ratio": 0.5,          # 代码类改动占比下限（防纯文档 PR）
    "max_lockfile_ratio": 0.5,      # lock/二进制占比上限
    "min_description_len": 80,      # Issue/PR 描述太短 -> 边界不清
    "good_description_len": 300,
    "min_acceptance_keywords": 1,   # 描述中是否出现验收类措辞
    "max_commits": 60,              # 单次 PR 提交数上限
    "require_tests_if_repo_has": True,
    # 依赖机器人(dependabot/renovate/snyk 等)的例行更新几乎不可能构成有效 Milestone：
    # 无明确意图、无代码逻辑改动、release notes 还会把描述长度虚高到几万字符。
    # 建议保持 True，这是本工具最有效的一条过滤器。
    "reject_bot_pr": True,
}

# ==================== 严格度档位 ====================
# strict  : 全部关键项必须满足。适合任务明令要求「必须有 Issue + 描述」时使用。
# balanced: 「关联 Issue」「描述长度」降级为扣分项（不单独否决）。
#           推荐日常使用 —— GitHub 上大量优质 PR 并未显式写 Fixes #N，
#           strict 模式下很可能一道题都筛不出来。
# loose   : 只卡最核心的「非 bot + 已合并 + 有实质代码改动」。库存不足时的兜底。
CRITICALITY_PROFILES = {
    "strict": {
        "bot": True, "merged": True, "scale_lines": True, "scale_files": True,
        "issue_link": True, "code_ratio": True, "lock_ratio": True,
        "description": True, "commits": True, "binary": False,
    },
    "balanced": {
        "bot": True, "merged": True, "scale_lines": False, "scale_files": False,
        "issue_link": False, "code_ratio": True, "lock_ratio": False,
        "description": False, "commits": False, "binary": False,
    },
    "loose": {
        "bot": True, "merged": True, "scale_lines": False, "scale_files": False,
        "issue_link": False, "code_ratio": True, "lock_ratio": False,
        "description": False, "commits": False, "binary": False,
    },
}
# ===================================================

CODE_PATH_RE = re.compile(
    r"\.(py|js|jsx|ts|tsx|java|go|rs|c|h|cpp|cc|hpp|cs|rb|php|swift|kt|scala|lua|sh|sql)$",
    re.I,
)
LOCKFILE_RE = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|composer\.lock|"
    r"cargo\.lock|go\.sum|gradle\.lockfile|flake\.lock)$",
    re.I,
)
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".mp4", ".mp3", ".pdf", ".zip", ".gz", ".jar", ".exe",
    ".so", ".dll", ".dylib", ".webp", ".avif",
}
TEST_RE = re.compile(
    r"(^|/)(tests?|specs?|__tests__)(/|$)|"
    r"(test_[^/]*\.py|_test\.(py|go|rs)|\.test\.(js|ts|jsx|tsx)|\.spec\.(js|ts|jsx|tsx))$",
    re.I,
)
DOC_RE = re.compile(r"\.(md|rst|txt|adoc|org)$", re.I)

ISSUE_LINK_RE = re.compile(
    r"\b(?:fixe?[sd]?|close[sd]?|resolve[sd]?|ref(?:erence)?s?|part of)\b[^#\n]{0,40}#(\d+)",
    re.I,
)

# —— 依赖机器人识别 ——
BOT_LOGIN_RE = re.compile(
    r"(dependabot|renovate|snyk|scala-steward|imgbot|github-actions|"
    r"allcontributors|mergify|greenkeeper|depfu|whitesource)",
    re.I,
)
BOT_TITLE_RE = re.compile(
    r"^(bump|build|chore|ci)\b[^\n]{0,60}\b(deps?|dependencies|dependency)\b"
    r"|^bump .{1,80} (from|to) v?\d"
    r"|^\[snyk\]"
    r"|^update .{0,50}(dependencies|dependency|lock ?file|requirements)"
    r"|^\[dependabot\]",
    re.I,
)

ACCEPTANCE_KEYWORDS = [
    "reproduce", "reproduc", "steps to", "expected", "actual", "should", "assert",
    "test case", "acceptance", "verif", "checklist", "- [ ]", "behavior",
    "复现", "预期", "实际", "验收", "断言", "测试用例", "确认",
]

WEIGHTS = {
    "boundary": 25,      # 边界清晰度（描述质量、是否关联 issue、标题）
    "scale": 20,         # 改动规模是否适中
    "purity": 20,        # 改动是否聚焦（不被 lockfile/二进制/噪音污染）
    "verifiability": 20, # 可否验收（是否有测试、代码改动、CI）
    "selfcontained": 15, # 是否自包含（涉及模块数、是否跨大范围重构）
}


def is_code_file(path: str) -> bool:
    return bool(CODE_PATH_RE.search(path))


def is_test_file(path: str) -> bool:
    return bool(TEST_RE.search(path))


def band(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 1.0 if v >= hi else 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def desc_quality(text: str | None) -> tuple:
    """返回 (长度分, 是否含验收语义, 长度)。"""
    t = (text or "").strip()
    n = len(t)
    lower = t.lower()
    acc = sum(1 for k in ACCEPTANCE_KEYWORDS if k.lower() in lower)
    return band(n, THRESHOLDS["min_description_len"], THRESHOLDS["good_description_len"]), min(acc, 3) / 3.0, n


def audit(
    gh: GitHub, owner: str, name: str, pr_number: int, profile: str = "balanced"
) -> dict:
    T = THRESHOLDS
    CRIT = CRITICALITY_PROFILES.get(profile, CRITICALITY_PROFILES["balanced"])
    pr = gh.pull(owner, name, pr_number)

    merged = bool(pr.get("merged"))
    mergeable_state = pr.get("mergeable_state")
    base_sha = pr["base"]["sha"]
    head_sha = pr["head"]["sha"]
    merge_commit = pr.get("merge_commit_sha")
    changed_files = pr.get("changed_files", 0)
    additions = pr.get("additions", 0)
    deletions = pr.get("deletions", 0)
    total_lines = additions + deletions
    commits_count = pr.get("commits", 0)
    title = pr.get("title", "")
    body = pr.get("body") or ""
    author_login = (pr.get("user") or {}).get("login") or ""
    is_bot_pr = bool(
        author_login.endswith("[bot]")
        or BOT_LOGIN_RE.search(author_login)
        or BOT_TITLE_RE.search(title)
    )

    # files 明细（部分超大 PR 会被分页截断到 300 条，已足够判断）
    files = gh.pull_files(owner, name, pr_number, per_page=100)

    code_files, code_lines = 0, 0
    test_files, test_lines = 0, 0
    doc_files, doc_lines = 0, 0
    lock_files, lock_lines = 0, 0
    binary_files = 0
    total_scanned_lines = 0
    touched_dirs: set = set()

    for f in files:
        p = f.get("filename", "")
        ch = f.get("changes", 0)
        total_scanned_lines += ch
        parts = p.split("/")
        if len(parts) > 1:
            touched_dirs.add("/".join(parts[:-1]))
        else:
            touched_dirs.add("<root>")

        if TEST_RE.search(p):
            test_files += 1
            test_lines += ch
        if DOC_RE.search(p):
            doc_files += 1
            doc_lines += ch
        elif LOCKFILE_RE.search(p):
            lock_files += 1
            lock_lines += ch
        elif Path(p).suffix.lower() in BINARY_EXT:
            binary_files += 1
        elif is_code_file(p):
            code_files += 1
            code_lines += ch

    denom = max(total_scanned_lines, 1)
    code_ratio = code_lines / denom
    lock_ratio = lock_lines / denom
    doc_ratio = doc_lines / denom
    has_test_change = test_lines > 0

    # ---------------- 关联 Issue ----------------
    issue_refs: list = []
    for m in ISSUE_LINK_RE.finditer(body):
        try:
            issue_refs.append(int(m.group(1)))
        except ValueError:
            pass
    if not issue_refs and pr.get("_links", {}).get("issue"):
        pass
    issue_refs = sorted(set(issue_refs))

    issue_info = None
    if issue_refs:
        for num in issue_refs:
            try:
                issue_info = gh.issue(owner, name, num)
                break
            except GHError:
                continue

    pr_desc_score, pr_acc_score, pr_desc_len = desc_quality(body)
    iss_desc_score, iss_acc_score, iss_desc_len = (
        desc_quality(issue_info.get("body") if issue_info else None)
    )

    # ---------------- 硬指标 ----------------
    hard: list = []

    def chk(item: str, ok: bool, detail: str, critical: bool = True):
        hard.append({"item": item, "passed": ok, "detail": detail, "critical": critical})

    # bot 依赖更新优先级最高：命中直接否决，无需再看后续指标
    if T["reject_bot_pr"]:
        chk("非 bot 依赖更新", not is_bot_pr,
            "依赖机器人的例行更新（dependabot/renovate/snyk 等），无明确开发意图，"
            "且其 release notes 会人为抬高描述长度 → 不可作为 Milestone"
            if is_bot_pr else "人工提交", critical=CRIT["bot"])

    chk("PR 已合并", (not T["require_merged"]) or merged,
        "已 merged" if merged else f"未合并（state={pr.get('state')}）",
        critical=CRIT["merged"])

    chk("改动规模·行数", T["min_changed_lines"] <= total_lines <= T["max_changed_lines"],
        f"+{additions}/-{deletions}，共 {total_lines} 行"
        f"（要求 {T['min_changed_lines']}~{T['max_changed_lines']}）",
        critical=CRIT["scale_lines"])

    chk("改动规模·文件数", T["min_files"] <= changed_files <= T["max_files"],
        f"{changed_files} 个文件（要求 ≤{T['max_files']}）",
        critical=CRIT["scale_files"])

    if T["require_issue_link"]:
        chk("关联 Issue", bool(issue_refs),
            f"#{', #'.join(map(str, issue_refs))}" if issue_refs
            else "未在描述中匹配到 Fixes/Closes #N（非否决项时可手动确认是否存在对应 Issue）",
            critical=CRIT["issue_link"])

    if T["require_code_change"]:
        chk("含代码改动", code_ratio >= T["min_code_ratio"],
            f"代码类改动占比 {code_ratio:.0%}（要求 ≥{T['min_code_ratio']:.0%}）",
            critical=CRIT["code_ratio"])

    chk("非 lockfile 主导", lock_ratio <= T["max_lockfile_ratio"],
        f"lockfile 占比 {lock_ratio:.0%}（要求 ≤{T['max_lockfile_ratio']:.0%}）",
        critical=CRIT["lock_ratio"])

    desc_ref = issue_info or pr
    chk("描述充分", len((desc_ref.get("body") or "")) >= T["min_description_len"],
        f"Issue 描述 {iss_desc_len} 字符 / PR 描述 {pr_desc_len} 字符"
        f"（要求 ≥{T['min_description_len']}）",
        critical=CRIT["description"])

    chk("提交数合理", commits_count <= T["max_commits"],
        f"{commits_count} 个 commit（上限 {T['max_commits']}）",
        critical=CRIT["commits"])

    if binary_files:
        chk("无大量二进制", binary_files <= 3,
            f"含 {binary_files} 个二进制文件", critical=CRIT["binary"])

    critical_failed = [h for h in hard if not h["passed"] and h["critical"]]
    noncritical_failed = [h for h in hard if not h["passed"] and not h["critical"]]

    # ---------------- 软指标打分 ----------------
    # boundary：描述清晰度 + 关联 issue + 标题可读性
    boundary = 0.4 * max(pr_desc_score, iss_desc_score)
    boundary += 0.3 * max(pr_acc_score, iss_acc_score)
    boundary += 0.3 * (1.0 if issue_refs else 0.3)
    boundary = min(1.0, boundary)

    # scale：改动规模落在甜区间的程度
    scale = 0.0
    if T["min_changed_lines"] <= total_lines <= T["max_changed_lines"]:
        sweet_lo, sweet_hi = 30, 400
        if sweet_lo <= total_lines <= sweet_hi:
            scale = 1.0
        elif total_lines < sweet_lo:
            scale = band(total_lines, T["min_changed_lines"], sweet_lo) * 0.8
        else:
            scale = max(0.4, 1.0 - (total_lines - sweet_hi) / (T["max_changed_lines"] - sweet_hi) * 0.6)

    # purity：不被噪声污染
    purity = min(1.0, code_ratio * 0.7 + (has_test_change * 0.3))
    purity *= (1.0 - min(lock_ratio, 0.6))
    purity = max(0.0, min(1.0, purity))

    # verifiability：有无测试、可否复跑
    verifiability = 0.0
    verifiability += 0.45 if has_test_change else 0
    verifiability += 0.25 * max(pr_acc_score, iss_acc_score)
    verifiability += 0.3 * band(max(pr_desc_len, iss_desc_len),
                                T["min_description_len"], T["good_description_len"])

    # selfcontained：触碰目录越少越聚焦
    if not touched_dirs:
        selfcontained = 0.5
    elif len(touched_dirs) == 1:
        selfcontained = 1.0
    else:
        selfcontained = max(0.2, 1.0 - (len(touched_dirs) - 1) / 8)
    if changed_files > T["max_files"]:
        selfcontained *= 0.5

    parts = {
        "boundary": boundary, "scale": scale, "purity": purity,
        "verifiability": verifiability, "selfcontained": selfcontained,
    }
    total_score = round(sum(parts[k] * WEIGHTS[k] for k in WEIGHTS), 1)

    if critical_failed:
        verdict = "FAIL"
    elif total_score >= 75:
        verdict = "PASS"
    elif total_score >= 55:
        verdict = "WARN"
    else:
        verdict = "FAIL"

    warnings = []
    if is_bot_pr:
        warnings.append(
            f"该 PR 由依赖机器人 {author_login or '未知'} 自动创建，属于例行版本/依赖更新，"
            "不具备任务边界与验收标准，务必换一个人工提交的 PR"
        )
    if not has_test_change:
        warnings.append("PR 未改动任何测试文件，验收时缺少可执行的验证依据")
    if issue_info and issue_info.get("state") != "closed":
        warnings.append(f"关联 Issue #{issue_info.get('number')} 仍处于 {issue_info.get('state')} 状态")
    if not issue_info and issue_refs:
        warnings.append(f"描述里引用了 #{issue_refs[0]}，但 API 未能读取到该 Issue（可能已删除/无权限）")
    if binary_files:
        warnings.append(f"含 {binary_files} 个二进制文件，diff 不可读，会影响人工评审")
    if lock_ratio > 0.3:
        warnings.append(f"lockfile 改动占比 {lock_ratio:.0%}，核心价值可能被依赖更新稀释")
    if doc_ratio > 0.8:
        warnings.append("改动几乎全是文档，可能不满足「代码类 Milestone」要求")
    if len(touched_dirs) > 6:
        warnings.append(f"涉及 {len(touched_dirs)} 个目录，任务边界偏散")
    if commits_count > 20:
        warnings.append(f"{commits_count} 个 commit，建议确认是否存在无关改动混入")

    # ---------------- 输出 before/after ----------------
    after_ref = merge_commit if (merged and merge_commit) else head_sha
    next_cmds = [
        f"python collect_snapshot.py {owner}/{name} --ref {base_sha} --zip",
        f"python collect_snapshot.py {owner}/{name} --ref {after_ref} --zip",
    ]

    return {
        "verdict": verdict,
        "profile": profile,
        "score": total_score,
        "score_parts": {k: round(v, 3) for k, v in parts.items()},
        "weights": WEIGHTS,
        "pull_request": {
            "number": pr.get("number"),
            "title": title,
            "url": pr.get("html_url"),
            "state": pr.get("state"),
            "merged": merged,
            "merged_at": pr.get("merged_at"),
            "created_at": pr.get("created_at"),
            "author": (pr.get("user") or {}).get("login"),
            "is_bot_pr": is_bot_pr,
            "commits": commits_count,
            "changed_files": changed_files,
            "additions": additions,
            "deletions": deletions,
            "changed_lines": total_lines,
            "body_length": pr_desc_len,
        },
        "linked_issue": (
            {
                "number": issue_info.get("number"),
                "title": issue_info.get("title"),
                "state": issue_info.get("state"),
                "url": issue_info.get("html_url"),
                "body_length": iss_desc_len,
                "labels": [l.get("name") for l in (issue_info.get("labels") or [])],
                "created_at": issue_info.get("created_at"),
            }
            if issue_info
            else None
        ),
        "diff_analysis": {
            "files_scanned": len(files),
            "code_files": code_files, "code_lines": code_lines,
            "test_files": test_files, "test_lines": test_lines,
            "doc_files": doc_files, "doc_lines": doc_lines,
            "lock_files": lock_files, "lock_lines": lock_lines,
            "binary_files": binary_files,
            "code_ratio": round(code_ratio, 3),
            "lock_ratio": round(lock_ratio, 3),
            "doc_ratio": round(doc_ratio, 3),
            "touched_dirs_count": len(touched_dirs),
            "touched_dirs": sorted(touched_dirs)[:15],
        },
        "snapshot_refs": {
            "before_snapshot_ref": base_sha,
            "after_snapshot_ref": after_ref,
            "note": "before = PR base 父提交；after = PR merge commit（未合并时取 head）",
        },
        "hard_checks": hard,
        "critical_failures": [h["item"] for h in critical_failed],
        "noncritical_failures": [h["item"] for h in noncritical_failed],
        "warnings": warnings,
        "next_commands": next_cmds,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Milestone 质量自动质检")
    ap.add_argument("repo", help="owner/repo 或 URL")
    ap.add_argument("--pr", type=int, required=True, help="Pull Request 编号")
    ap.add_argument(
        "--profile",
        choices=list(CRITICALITY_PROFILES.keys()),
        default="balanced",
        help="严格度：strict(全否决) / balanced(推荐，Issue与描述仅扣分) / loose(最宽)",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    owner, name = parse_repo_arg(args.repo)
    gh = GitHub(token=args.token)

    try:
        result = audit(gh, owner, name, args.pr, args.profile)
    except GHError as e:
        print(f"[失败] {e}")
        raise SystemExit(1)

    if args.save:
        out = Path(__file__).resolve().parent / "reports"
        out.mkdir(exist_ok=True)
        fn = out / (
            f"milestone_audit_{owner}__{name}__pr{args.pr}__"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        fn.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"报告已保存：{fn}\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        icon = {"PASS": "[通过]", "WARN": "[待定]", "FAIL": "[不通过]"}[result["verdict"]]
        p = result["pull_request"]
        d = result["diff_analysis"]
        print("=" * 66)
        print(f"  Milestone 自动质检：{owner}/{name} #{args.pr}   [严格度: {result['profile']}]")
        print(f"  结论：{icon}   综合得分 {result['score']}/100")
        print("=" * 66)
        print(f"  {p['title']}")
        print(f"  {p['url']}")
        print(
            f"  作者={p['author']}  状态={'merged' if p['merged'] else p['state']}  "
            f"commit={p['commits']}  文件={p['changed_files']}"
        )
        print(f"  改动：+{p['additions']} / -{p['deletions']}（共 {p['changed_lines']} 行）")
        if result["linked_issue"]:
            li = result["linked_issue"]
            print(f"  关联 Issue：#{li['number']} [{li['state']}] {li['title']}")
        else:
            print("  关联 Issue：无")
        print("-" * 66)
        print("  【硬指标】  (× = 否决项，! = 扣分项)")
        for h in result["hard_checks"]:
            if h["passed"]:
                mark = "√"
            elif h["critical"]:
                mark = "×"
            else:
                mark = "!"
            print(f"    {mark} {h['item']:<16} {h['detail']}")
        print("-" * 66)
        print("  【改动构成】")
        print(
            f"    代码 {d['code_files']}文件/{d['code_lines']}行 ({d['code_ratio']:.0%})   "
            f"测试 {d['test_files']}文件/{d['test_lines']}行"
        )
        print(
            f"    文档 {d['doc_files']}文件/{d['doc_lines']}行   "
            f"lock {d['lock_files']}文件/{d['lock_lines']}行   二进制 {d['binary_files']}个"
        )
        print(f"    涉及目录 {d['touched_dirs_count']} 个")
        print("-" * 66)
        print("  【评分明细】")
        for k, v in result["score_parts"].items():
            w = result["weights"][k]
            filled = int(v * 20)
            print(f"    {k:<14} {'█'*filled}{'·'*(20-filled)} {v:.2f} × {w} = {v*w:5.1f}")
        if result["warnings"]:
            print("-" * 66)
            print("  【风险提示】")
            for w_ in result["warnings"]:
                print(f"    - {w_}")
        print("-" * 66)
        print("  【下一步：采集 before / after 双快照】")
        for c in result["next_commands"]:
            print(f"    {c}")
        print("=" * 66)

    sys.exit({"PASS": 0, "WARN": 2, "FAIL": 1}[result["verdict"]])


if __name__ == "__main__":
    main()
