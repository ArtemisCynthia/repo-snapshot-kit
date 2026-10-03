#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_repo.py —— Repo 质量【自动质检】

用法：
    python audit_repo.py octocat/Hello-World
    python audit_repo.py octocat/Hello-World --ref <sha> --json

能做什麼：
    在采集 Milestone 之前先判断「这个仓库值不值得做」，避免采完才发现不合规。
    输出按硬指标 / 软指标分层，硬指标不通过直接判 FAIL，不必再看后面的分数。

评分阈值全部集中在文件顶部 THRESHOLDS，按你手上任务的真实要求改即可。

退出码：
    0 = PASS，2 = WARN（建议人工复核），1 = FAIL
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from gh_client import GHError, GitHub, parse_repo_arg

# ============================ 阈值配置 ============================
# ⚠️ 这里是最需要根据实际任务要求调整的地方
THRESHOLDS = {
    # --- 硬指标：任一不满足即 FAIL ---
    "min_files": 5,              # 至少 5 个文件（排除空壳仓库）
    "min_code_lines": 200,       # 至少 200 行代码
    "max_size_kb": 500 * 1024,   # GitHub size(KiB) 上限，过大难处理（约 500MB）
    "require_readme": True,
    "require_license": True,
    "reject_fork": True,
    "reject_archived": True,
    "blocked_licenses": ["AGPL-3.0", "GPL-3.0", "CC-BY-NC-4.0"],  # 需要人工确认合规性
    "min_pushed_within_days": 365 * 2,   # 最后一次 push 距今不超过 2 年
    "max_stale_commit_days": 365 * 3,    # HEAD commit 太久远要告警

    # --- 软指标：影响评分，不单独决定生死 ---
    "warm_star": 100,            # star >= 100 得满分
    "min_star_ok": 10,           # star >= 10 视为「有一定社区认可」
    "active_within_days": 180,   # 6 个月内有 push = 活跃
    "min_distinct_contributors": 2,
    "ideal_code_lines_min": 1000,
    "ideal_code_lines_max": 100000,
}
SCORE_WEIGHTS = {
    "community": 20,     # star / fork / watchers
    "activity": 25,      # 最近更新时间、提交频率
    "structure": 25,     # README/LICENSE/构建文件/CI
    "testability": 15,   # 测试、CI
    "scale": 15,         # 规模是否适中（够真实又不至于臃肿）
}
# ==================================================================


def days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def band(v: float, lo: float, hi: float) -> float:
    """把 v 映射到 0~1，lo 及以下为 0，hi 及以上为 1。"""
    if hi == lo:
        return 1.0 if v >= hi else 0.0
    if hi < lo:  # 反向区间（越小越好）
        return band(-v, -lo, -hi)
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def audit(gh: GitHub, owner: str, name: str, ref: str | None, local_scan: dict | None) -> dict:
    T = THRESHOLDS
    info = gh.repo(owner, name)
    pushed_days = days_since(info.get("pushed_at"))

    # resolve HEAD commit（用于规模统计与 commit 日期）
    if not ref:
        ref = info.get("default_branch", "HEAD")
    try:
        cinfo = gh.commit(owner, name, ref)
        head_sha = cinfo["sha"]
        head_date = cinfo["commit"]["author"]["date"]
    except GHError:
        head_sha, head_date = ref, None

    head_days = days_since(head_date)

    # 优先用本地采集的结构扫描结果
    scan = local_scan or {}
    if not scan:
        # 未采集时做一次轻量推断（僅依赖 API，不准但够用）
        langs = {}
        try:
            langs = gh.languages(owner, name)
        except GHError:
            pass
        scan = {"file_count": None, "code_lines": None, "languages_by_lines": {}, "languages_api": langs}

    file_count = scan.get("file_count")
    code_lines = scan.get("code_lines")
    size_kb = info.get("size", 0)

    # ---------------- 硬指标 ----------------
    hard: list = []

    def chk(name_: str, ok: bool, detail: str, critical: bool = True):
        hard.append({"item": name_, "passed": ok, "detail": detail, "critical": critical})

    if T["reject_fork"]:
        chk("非 fork 仓库", not info.get("fork"),
            "是 fork" if info.get("fork") else "原始仓库")
    if T["reject_archived"]:
        chk("未被归档", not info.get("archived"),
            "已 archived" if info.get("archived") else "正常")
    chk("未停用", not info.get("disabled"), "已 disabled" if info.get("disabled") else "正常")

    chk("规模·文件数",
        file_count is None or file_count >= T["min_files"],
        f"{file_count} 个文件" + ("" if file_count is None else f"（要求 ≥{T['min_files']}）"))

    chk("规模·代码行数",
        code_lines is None or code_lines >= T["min_code_lines"],
        f"{code_lines} 行" + ("" if code_lines is None else f"（要求 ≥{T['min_code_lines']}）"))

    chk("体积适中", size_kb <= T["max_size_kb"],
        f"{size_kb/1024:.1f}MB（上限 {T['max_size_kb']/1024:.0f}MB）")

    chk("最近有更新",
        pushed_days is None or pushed_days <= T["min_pushed_within_days"],
        f"最后一次 push 距今 {pushed_days} 天")

    lic = None
    try:
        lic = gh.license(owner, name)
    except GHError:
        pass
    spdx = (lic or {}).get("license", {}).get("spdx_id") if lic else None
    spdx = spdx if spdx not in (None, "NOASSERTION") else "自定义/未知"

    if T["require_license"]:
        chk("有 LICENSE", spdx not in (None, "自定义/未知"), f"{spdx or '无'}")
    if T["blocked_licenses"] and spdx in T["blocked_licenses"]:
        chk("协议可采集", False, f"{spdx} 属于需人工确认的传染性/限制协议")
    if T["require_readme"]:
        chk("有 README", bool(scan.get("has_readme", True)),
            "存在" if scan.get("has_readme", True) else "缺失")

    hard_failed = [h for h in hard if not h["passed"]]
    critical_failed = [h for h in hard_failed if h["critical"]]

    # ---------------- 软指标打分 ----------------
    stars = info.get("stargazers_count", 0)
    forks = info.get("forks_count", 0)
    watchers = info.get("watchers_count", 0)

    community = max(
        band(stars, T["min_star_ok"], T["warm_star"]),
        band(forks, 2, 30),
    )
    activity = 0.0
    if pushed_days is not None:
        activity = max(0.0, 1.0 - pushed_days / max(T["active_within_days"], 1))
        activity = min(1.0, activity)
    if scan.get("has_ci"):
        activity = min(1.0, activity + 0.15)

    structure = 0.0
    structure += 0.3 if scan.get("has_readme") else 0
    structure += 0.2 if spdx not in (None, "自定义/未知") else 0
    structure += 0.25 if scan.get("build_files") else 0
    structure += 0.15 if scan.get("has_contributing") else 0
    structure += 0.1 if info.get("description") else 0

    testability = 0.0
    testability += 0.6 if scan.get("has_tests") else 0
    testability += 0.4 if scan.get("has_ci") else 0

    scale = 0.0
    if code_lines:
        lo, hi = T["ideal_code_lines_min"], T["ideal_code_lines_max"]
        if code_lines < lo:
            scale = band(code_lines, 0, lo) * 0.6
        else:
            scale = 0.6 + band(code_lines, lo, hi) * 0.4

    parts = {
        "community": community, "activity": activity, "structure": structure,
        "testability": testability, "scale": scale,
    }
    total = sum(parts[k] * SCORE_WEIGHTS[k] for k in SCORE_WEIGHTS)
    total = round(total, 1)

    if critical_failed:
        verdict = "FAIL"
    elif total >= 75:
        verdict = "PASS"
    elif total >= 55:
        verdict = "WARN"
    else:
        verdict = "FAIL"

    suggestions = []
    if not info.get("description"):
        suggestions.append("仓库缺少 description，任务描述信息可能不足")
    if not scan.get("has_tests"):
        suggestions.append("无测试，Milestone 验收时缺少可执行的验收依据")
    if not scan.get("has_ci"):
        suggestions.append("无 CI 配置，无法自动验证改动是否破坏构建")
    if pushed_days and pushed_days > T["active_within_days"]:
        suggestions.append(f"仓库超过 {pushed_days} 天未更新，活跃度偏低")
    if code_lines and code_lines < T["ideal_code_lines_min"]:
        suggestions.append("代码体量偏小，可能难以构造有实际价值的 Milestone")
    if scan.get("has_submodules"):
        suggestions.append("含 submodule：tarball 采集不含子模块内容，需在说明里标注")
    if stars < T["min_star_ok"]:
        suggestions.append("star 数很低，需人工确认是否为一次性/练手项目")

    return {
        "verdict": verdict,
        "score": total,
        "score_parts": {k: round(parts[k], 3) for k in parts},
        "weights": SCORE_WEIGHTS,
        "repo": {
            "full_name": info["full_name"],
            "html_url": info["html_url"],
            "description": info.get("description"),
            "created_at": info.get("created_at"),
            "pushed_at": info.get("pushed_at"),
            "pushed_days_ago": pushed_days,
            "default_branch": info.get("default_branch"),
            "stars": stars, "forks": forks, "watchers": watchers,
            "open_issues": info.get("open_issues_count"),
            "size_mb": round(size_kb / 1024, 2),
            "license": spdx,
            "topics": info.get("topics", []),
            "resolved_ref": head_sha,
            "head_commit_days_ago": head_days,
        },
        "scan_summary": {
            "file_count": file_count,
            "code_lines": code_lines,
            "has_readme": scan.get("has_readme"),
            "has_tests": scan.get("has_tests"),
            "has_ci": scan.get("has_ci"),
            "build_files": scan.get("build_files", []),
        },
        "hard_checks": hard,
        "critical_failures": [h["item"] for h in critical_failed],
        "suggestions": suggestions,
    }


def try_load_scan(owner: str, name: str, ref: str | None) -> dict | None:
    """已采集过的话，直接复用 metadata.json 里的扫描结果，省一次下载。"""
    base = Path(__file__).resolve().parent / "snapshots"
    if not base.exists():
        return None
    if ref:
        cand = base / f"{owner}__{name}__{ref[:8]}" / "metadata.json"
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8")).get("scan")
    hits = sorted(base.glob(f"{owner}__{name}__*/metadata.json"))
    if hits:
        return json.loads(hits[-1].read_text(encoding="utf-8")).get("scan")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Repo 质量自动质检")
    ap.add_argument("repo", help="owner/repo 或 GitHub URL")
    ap.add_argument("--ref", default=None, help="指定 commit/分支/tag")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--save", action="store_true", help="报告写入 reports/")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    owner, name = parse_repo_arg(args.repo)
    gh = GitHub(token=args.token)

    local_scan = try_load_scan(owner, name, args.ref)
    used_local = local_scan is not None
    try:
        result = audit(gh, owner, name, args.ref, local_scan)
    except GHError as e:
        print(f"[失败] {e}")
        raise SystemExit(1)

    if not used_local:
        result["suggestions"].append(
            "未复用本地快照，部分结构项(README/测试/CI)依赖推测；"
            f"建议先跑 collect_snapshot.py {owner}/{name} 再复检"
        )

    if args.save:
        out = Path(__file__).resolve().parent / "reports"
        out.mkdir(exist_ok=True)
        fn = out / f"repo_audit_{owner}__{name}__{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        fn.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"报告已保存：{fn}\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        icon = {"PASS": "[通过]", "WARN": "[待定]", "FAIL": "[不通过]"}[result["verdict"]]
        r = result["repo"]
        print("=" * 62)
        print(f"  Repo 质量自动质检：{r['full_name']}")
        print(f"  结论：{icon}   综合得分 {result['score']}/100")
        print("=" * 62)
        print(f"  {r['html_url']}")
        print(f"  {r['description'] or '(无 description)'}")
        print(
            f"  star={r['stars']} fork={r['forks']} issues={r['open_issues']} "
            f"size={r['size_mb']}MB license={r['license']}"
        )
        print(f"  最后更新：{r['pushed_at']}（{r['pushed_days_ago']} 天前）")
        print(f"  解析 commit：{str(r['resolved_ref'])[:12]}")
        print("-" * 62)
        print("  【硬指标】")
        for h in result["hard_checks"]:
            mark = "√" if h["passed"] else "×"
            print(f"    {mark} {h['item']:<14} {h['detail']}")
        print("-" * 62)
        print("  【评分明细】")
        for k, v in result["score_parts"].items():
            w = result["weights"][k]
            filled = int(v * 20)
            bar = "█" * filled + "·" * (20 - filled)
            print(f"    {k:<12} {bar} {v:.2f} × {w} = {v*w:5.1f}")
        print("-" * 62)
        if result["critical_failures"]:
            print("  【否决项】" + "、".join(result["critical_failures"]))
        if result["suggestions"]:
            print("  【建议 / 风险】")
            for s in result["suggestions"]:
                print(f"    - {s}")
        print("=" * 62)

    sys.exit({"PASS": 0, "WARN": 2, "FAIL": 1}[result["verdict"]])


if __name__ == "__main__":
    main()
