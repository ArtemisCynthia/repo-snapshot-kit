#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh_client.py —— GitHub API 客户端封装（零第三方依赖）

为什么单独拆这个文件：
  采集、Repo 质检、Milestone 质检三个脚本都要调 GitHub API，
  认证 / 重试 / 限流处理集中在这里，避免每份脚本各写一套。

重要背景（这台机器实测结论）：
  - github.com 主域名 **不可达**（CONNECT tunnel failed, 502）
    => 任何 `git clone` / `git ls-remote` 都会失败
  - api.github.com 与 codeload.github.com **可达**（HTTP 200, <1.5s）
    => 本项目的采集走「REST API 取元数据 + codeload 取 tarball」，
       完全不依赖本机 git，也不需要代理/git 配置
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Optional

API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30


class GHError(Exception):
    """GitHub API 调用失败。携带 status 便于上层判断是否限流。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class GitHub:
    """
    极简 GitHub REST 客户端。
    只依赖标准库，Python 3.9+ 可直接运行。
    """

    def __init__(self, token: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        # 优先级：显式传参 > 环境变量 GITHUB_TOKEN > GITHUB_API_TOKEN
        self.token = (
            token
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GITHUB_API_TOKEN")
            or ""
        ).strip()
        self.timeout = timeout
        self._etag_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _headers(self, accept: str) -> Dict[str, str]:
        h = {
            "Accept": accept,
            "User-Agent": "repo-snapshot-kit/1.0",
        }
        if self.token:
            # GitHub 当前推荐 Bearer；旧写法 `token xxx` 仍可用但已不推荐
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(
        self,
        method: str,
        url: str,
        accept: str = "application/vnd.github+json",
        follow_redirect: bool = True,
        data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """返回 dict，含 body(bytes) / json / status / headers。"""
        req = urllib.request.Request(
            url, method=method, headers=self._headers(accept), data=data
        )

        opener = urllib.request.build_opener()
        if not follow_redirect:
            # 自建 opener 关闭自动跳转，才能拿到 302 的 Location
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):  # type: ignore
                    return None

            opener = urllib.request.build_opener(NoRedirect)

        try:
            resp = opener.open(req, timeout=self.timeout)
            raw = resp.read()
            return {
                "ok": True,
                "status": resp.status,
                "body": raw,
                "headers": dict(resp.headers),
                "final_url": resp.geturl(),
            }
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            return {
                "ok": False,
                "status": e.code,
                "body": raw,
                "headers": dict(e.headers) if e.headers else {},
                "final_url": url,
            }
        except urllib.error.URLError as e:
            raise GHError(
                f"网络不可达：{e.reason}\n"
                f"  目标：{url}\n"
                f"  提示：若本机 github.com 被墙/代理异常，请确认 api.github.com 与 "
                f"codeload.github.com 可直连（本工具不依赖 github.com 主域名）。"
            )

    def json_get(
        self, path: str, params: Optional[Dict[str, Any]] = None, retry: int = 3
    ) -> Any:
        """GET 一个 API 路径并解析 JSON，带限流退避重试。"""
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        last_err: Optional[GHError] = None
        for attempt in range(retry):
            r = self._request("GET", url)
            if r["ok"]:
                try:
                    return json.loads(r["body"].decode("utf-8"))
                except json.JSONDecodeError:
                    raise GHError(f"响应不是合法 JSON：{r['final_url']}")

            status = r["status"]
            detail = _extract_message(r["body"])

            # 401/403/429 处理
            if status in (401, 403, 429):
                remaining = r["headers"].get("X-RateLimit-Remaining")
                reset = r["headers"].get("X-RateLimit-Reset")
                hint = ""
                if status == 401:
                    hint = "Token 无效或过期，请重新生成。"
                elif remaining == "0" and reset:
                    wait = max(0, int(reset) - int(time.time()))
                    hint = f"API 配额已用尽，{wait} 秒后重置。"
                    # 配额类错误等待后重试才有意义
                    if attempt < retry - 1:
                        sleep_s = min(wait + 2, 60)
                        print(f"  [配额] 已用尽，等待 {sleep_s}s 后重试…")
                        time.sleep(sleep_s)
                        last_err = GHError(detail, status, r["body"].decode("utf-8", "ignore"))
                        continue
                    hint += " 建议配置 Token（未认证仅 60 次/小时，认证后 5000 次/小时）。"
                last_err = GHError(
                    f"HTTP {status}: {detail} {hint}".strip(), status, r["body"].decode("utf-8", "ignore")
                )
                break

            if status == 404:
                raise GHError(
                    f"资源不存在(404)：{url}\n"
                    f"  常见原因：仓库私有且 Token 无权限 / owner 或 repo 名拼写错误 / "
                    f"仓库已删除或改名。",
                    status,
                )

            # 5xx 短暂故障 -> 退避重试
            if status >= 500 and attempt < retry - 1:
                time.sleep(2 ** attempt)
                last_err = GHError(f"HTTP {status}: {detail}", status)
                continue

            raise GHError(f"HTTP {status}: {detail}", status, r["body"].decode("utf-8", "ignore"))

        assert last_err is not None
        raise last_err

    def json_put(self, path: str, payload: Dict[str, Any]) -> Any:
        """PUT 一个 API 路径（用于创建文件/仓库等写操作）。"""
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = self._request(
            "PUT", url, accept="application/vnd.github+json", data=data
        )
        if r["ok"]:
            try:
                return json.loads(r["body"].decode("utf-8"))
            except json.JSONDecodeError:
                return {"ok": True, "raw": r["body"].decode("utf-8", "ignore")}
        detail = _extract_message(r["body"])
        hint = ""
        if r["status"] in (401, 403):
            hint = (
                " Token 权限不足。写入文件需要 Contents: Read and write 权限。"
                if r["status"] == 403
                else " Token 无效或过期。"
            )
        raise GHError(f"HTTP {r['status']}: {detail} {hint}".strip(), r["status"])

    def json_post(self, path: str, payload: Dict[str, Any]) -> Any:
        """POST 一个 API 路径（用于创建仓库等）。"""
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = self._request(
            "POST", url, accept="application/vnd.github+json", data=data
        )
        if r["ok"]:
            try:
                return json.loads(r["body"].decode("utf-8"))
            except json.JSONDecodeError:
                return {"ok": True, "raw": r["body"].decode("utf-8", "ignore")}
        detail = _extract_message(r["body"])
        hint = ""
        if r["status"] == 422:
            hint = " 常见原因：同名仓库已存在，或仓库名不合法。"
        elif r["status"] == 403:
            hint = (
                " Token 权限不足。创建仓库需要：classic token 勾选 `public_repo`"
                "（私有仓用 `repo`）；若用 fine-grained token，需额外授予账户级 "
                "`Repository creation: write` 权限。"
            )
        elif r["status"] == 401:
            hint = " Token 无效或过期。"
        raise GHError(f"HTTP {r['status']}: {detail} {hint}".strip(), r["status"])

    # ------------------------------------------------------------------ #
    # 语义化封装
    # ------------------------------------------------------------------ #
    def repo(self, owner: str, name: str) -> Dict[str, Any]:
        return self.json_get(f"/repos/{owner}/{name}")

    def default_branch_sha(self, owner: str, name: str) -> Dict[str, str]:
        """返回默认分支名与其最新 commit sha。"""
        info = self.repo(owner, name)
        branch = info.get("default_branch", "main")
        ref = self.json_get(f"/repos/{owner}/{name}/git/ref/heads/{urllib.parse.quote(branch)}")
        return {"branch": branch, "sha": ref["object"]["sha"]}

    def commit(self, owner: str, name: str, ref: str) -> Dict[str, Any]:
        return self.json_get(f"/repos/{owner}/{name}/commits/{ref}")

    def commits(
        self, owner: str, name: str, sha: Optional[str] = None, per_page: int = 100
    ) -> list:
        params = {"per_page": per_page}
        if sha:
            params["sha"] = sha
        return self.json_get(f"/repos/{owner}/{name}/commits", params)

    def compare(self, owner: str, name: str, base: str, head: str) -> Dict[str, Any]:
        """比较两个 commit 的差异，返回 files 列表与统计。"""
        return self.json_get(f"/repos/{owner}/{name}/compare/{base}...{head}")

    def pull(self, owner: str, name: str, number: int) -> Dict[str, Any]:
        return self.json_get(f"/repos/{owner}/{name}/pulls/{number}")

    def pull_files(self, owner: str, name: str, number: int, per_page: int = 100) -> list:
        return self.json_get(
            f"/repos/{owner}/{name}/pulls/{number}/files", {"per_page": per_page}
        )

    def issue(self, owner: str, name: str, number: int) -> Dict[str, Any]:
        """GitHub 的 Issue API 同样能取 PR（PR 本质是带 pull_request 字段的 Issue）。"""
        return self.json_get(f"/repos/{owner}/{name}/issues/{number}")

    def contents(self, owner: str, name: str, path: str, ref: str) -> Optional[Dict[str, Any]]:
        """取文件内容（Base64）。不存在返回 None 而不是抛错。"""
        try:
            return self.json_get(f"/repos/{owner}/{name}/contents/{path}", {"ref": ref})
        except GHError as e:
            if e.status == 404:
                return None
            raise

    def languages(self, owner: str, name: str) -> Dict[str, int]:
        return self.json_get(f"/repos/{owner}/{name}/languages")

    def license(self, owner: str, name: str) -> Optional[Dict[str, Any]]:
        try:
            return self.json_get(f"/repos/{owner}/{name}/license")
        except GHError as e:
            if e.status == 404:
                return None
            raise

    def rate_limit(self) -> Dict[str, Any]:
        return self.json_get("/rate_limit")

    # ------------------------------------------------------------------ #
    # 快照下载（不依赖 git）
    # ------------------------------------------------------------------ #
    def tarball_url(self, owner: str, name: str, ref: str) -> str:
        """返回 tarball 的最终下载地址。

        走 codeload 而非 github.com —— 本机 github.com 不可达时这是唯一可行通道。
        """
        return f"https://codeload.github.com/{owner}/{name}/tar.gz/{ref}"

    def download_bytes(self, url: str, timeout: int = 180, chunk: int = 1 << 20) -> bytes:
        """下载二进制内容到内存（仓库快照通常几 MB~几十 MB，可接受）。"""
        req = urllib.request.Request(url, headers=self._headers("application/octet-stream"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                buf = bytearray()
                total = 0
                while True:
                    part = resp.read(chunk)
                    if not part:
                        break
                    buf.extend(part)
                    total += len(part)
                return bytes(buf)
        except urllib.error.HTTPError as e:
            raise GHError(f"下载失败 HTTP {e.code}：{url}", e.code)
        except urllib.error.URLError as e:
            raise GHError(f"下载失败：{e.reason}\n  目标：{url}")


def _extract_message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
        if isinstance(data, dict):
            return str(data.get("message", body[:200].decode("utf-8", "ignore")))
    except Exception:
        pass
    return body[:200].decode("utf-8", "ignore")


def parse_repo_arg(raw: str) -> tuple:
    """把用户输入的多种形式统一解析成 (owner, repo)。

    支持：
      octocat/Hello-World
      https://github.com/octocat/Hello-World
      https://github.com/octocat/Hello-World.git
      https://github.com/octocat/Hello-World/tree/main/src
    """
    s = raw.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]

    parts: Iterable[str]
    if s.startswith("http"):
        parts = urllib.parse.urlparse(s).path.strip("/").split("/")
    else:
        parts = s.split("/")

    ps = [p for p in parts if p]
    if len(ps) < 2:
        raise ValueError(f"无法从 {raw!r} 解析出 owner/repo")
    return ps[0], ps[1]


if __name__ == "__main__":
    # 自检：确认连通性与配额
    gh = GitHub()
    try:
        rl = gh.rate_limit()
        core = rl["resources"]["core"]
        used_note = "（未认证：60 次/小时）" if not gh.token else "（已认证）"
        print(f"连通性 OK {used_note}")
        print(f"  core 配额：{core['remaining']}/{core['limit']}")
        if not gh.token:
            print("  建议：设置环境变量 GITHUB_TOKEN，配额提升到 5000 次/小时")
    except GHError as e:
        print(f"连通性失败：{e}")
        raise SystemExit(1)
