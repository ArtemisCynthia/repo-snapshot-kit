# repo-snapshot-kit

> 面向 AI Coding 数据集构建场景的 **GitHub 仓库快照采集与质量质检工具链**。
> 用于采集**固定 commit 的仓库快照**与**开发里程碑（Milestone）**，
> 并对「这个仓库值不值得采集」「这个 Milestone 是否构成有效任务」做可量化的自动质检。

## 它解决什么

在构建代码类任务数据集时，人工筛选题材有三个绕不开的坑：

| 问题 | 本工具的做法 |
|---|---|
| **快照不可复现** —— clone 下来的代码隔天就变了，无法作为固定基线 | 按 commit SHA 精确锁定，天然产出可对照的 before / after 双快照 |
| **质检靠拍脑袋** —— 仓库和 Milestone 的好坏没有统一标准 | 把准入 / 仓库 / Milestone 三道质检拆成硬指标 + 加权评分，结论可追溯 |
| **候选难筛** —— 活跃仓库里大量 merged PR 其实是依赖机器人的例行更新 | 自动识别并滤除 bot 提交、纯文档、发版类、i18n 批量更新等无效样本 |

**设计前提：使用者不需要懂 Git。** 全部 Git 操作封装在脚本内，只需复制粘贴命令。

---

## ⚠️ 先读这一段：为什么不用 git clone

在交付这套工具前，我在这台机器上实测了网络情况（2026-10-03）：

| 端点 | 结果 |
|---|---|
| `git ls-remote https://github.com/...` | **失败** — `CONNECT tunnel failed, 502` |
| `https://api.github.com` | 可用，HTTP 200 |
| `https://codeload.github.com` | 可用，HTTP 200 |

**结论：这台机器上的 `git clone` 是走不通的。** 如果你领了题才发现这一点，会白白烧掉答题时间。

所以本工具包的采集主通道是：**GitHub REST API 取元数据 + codeload 取 tarball**，完全不碰本地 git。
这顺带带来三个好处：

1. **天然锁定 commit** —— `/tarball/<sha>` 下载到的就是那个 commit 的确切内容，
   不存在「采完隔天仓库更新了导致内容不符」的问题；
2. **快** —— 只下载当前状态，不拉提交历史；
3. **无需配置 git** —— 不用设 `user.name`、不用配 SSH、不用 credential helper。

> 若将来你换了网络环境 git 可用了，这套工具照样能用（它根本不依赖 git）。

---

## 30 秒上手

打开终端，`cd` 到本目录，按顺序复制粘贴：

```bash
# 0) 先确认能连通 GitHub
python gh_client.py

# 1) 挑一个可能有戏的仓库，批量列出可用 Milestone 候选
python find_candidates.py psf/requests --pages 1

# 2) 对初筛通过的前几个做完整质检，直接看排序结果
python find_candidates.py psf/requests --pages 1 --auto-audit 5

# 3) 仓库层面先过一遍
python audit_repo.py psf/requests

# 4) 用第 2 步挑出来的 PR 编号做 Milestone 质检
python audit_milestone.py psf/requests --pr 6962

# 5) 按脚本给出的命令，采集 before/after 双快照
python collect_snapshot.py psf/requests --ref <before_sha> --zip
python collect_snapshot.py psf/requests --ref <after_sha> --zip
```

**推荐顺序：2 → 3 → 4 → 5。** 批量筛在前，别一个个试 PR 编号。

---

## 强烈建议：先配 Token

不配 Token，GitHub 只允许 **60 次/小时**；配了之后是 **5000 次/小时**。
跑一次 `--auto-audit 5` 大约消耗 15~25 次调用，不配 Token 很容易中途被限流。

### 申请方式（最小权限，只读公开仓库）

1. 打开 <https://github.com/settings/personal-access-tokens>
2. `Generate new token` → 选 **fine-grained token**
3. Repository access 选 **Public Repositories (read-only)**
4. 不勾选任何额外权限（只读元数据足够）
5. 生成后复制，Windows 上这样设置（**永久生效**）：

```powershell
[System.Environment]::SetEnvironmentVariable('GITHUB_TOKEN', '你的token', 'User')
```

设置完**重开终端**生效。临时生效用 `$env:GITHUB_TOKEN='你的token'`（PowerShell）
或 `export GITHUB_TOKEN=你的token`（Git Bash）。

验证：

```bash
python gh_client.py
# 看到 "（已认证）" 且 core 配额接近 5000 即成功
```

---

## 完整工作流

```
① 选仓库          find_candidates.py / 自己找
      ↓
② Repo 质量质检    audit_repo.py          ← 不通过直接换仓库，别浪费时间
      ↓
③ Milestone 质量   audit_milestone.py     ← 不通过换 PR
      ↓
④ 采集双快照       collect_snapshot.py ×2 （before / after）
      ↓
⑤ 人工质检        checklist.md           ← 照着逐条勾
      ↓
   提交
```

**关键原则：先质检，后采集。** 每道题最贵的成本是采集和人工质检，
在 `--auto-audit` 阶段就毙掉不合格的 PR，比采完再返工省得多。

---

## 命令速查

| 脚本 | 作用 | 常用写法 |
|---|---|---|
| `gh_client.py` | 连通性自检 / 查看配额 | `python gh_client.py` |
| `collect_snapshot.py` | 采集单个 commit 快照 | `python collect_snapshot.py owner/repo --ref <sha> --zip` |
| `audit_repo.py` | 仓库质量质检 | `python audit_repo.py owner/repo --json --save` |
| `audit_milestone.py` | Milestone 质量质检 | `python audit_milestone.py owner/repo --pr 123` |
| `find_candidates.py` | 批量初筛 + 排序 | `python find_candidates.py owner/repo --auto-audit 8` |
| `publish_to_github.py` | 把本工具包发布到 GitHub（**无需 git**） | `python publish_to_github.py --token <token> --dry-run` |

通用参数：`--token`（也可环境变量）、`--json`（机器可读）、`--save`（报告落 `reports/`）。
`audit_milestone.py` 额外支持 `--profile strict|balanced|loose`。

### 关于 `--profile`（很重要）

- **balanced（默认，推荐日常用）**：把「关联 Issue」「描述长度」降级成扣分项。
  GitHub 上大量优质 PR 并未显式写 `Fixes #N`，用 strict 你可能一道题都筛不出来。
- **strict**：全部硬指标必须满足。任务明令要求「必须有 Issue + 描述」时才用。
- **loose**：只卡「非 bot + 已合并 + 有实质代码改动」。库存见底时的兜底。

输出里 `×` 表示否决项，`!` 表示扣分项。

---

## 产出目录

```
repo-snapshot-kit/
├── snapshots/
│   └── psf__requests__611c6162/      # 按 owner__repo__短sha 命名
│       ├── repo/                      # 仓库源码（顶层目录已自动剥掉）
│       ├── metadata.json              # 采集元数据
│       └── snapshot.zip               # --zip 才有
├── reports/                           # --save 的质检报告
├── gh_client.py / collect_snapshot.py / audit_repo.py
├── audit_milestone.py / find_candidates.py
└── checklist.md
```

`metadata.json` 里含 commit sha、message、作者、日期、tarball sha256、
文件数、代码行数、语言构成、README/测试/CI 探测结果 —— **提交时要求填的事实类信息都从这里抄**，不要凭印象写。

---

## 挑 Milestone 的经验规则

工具已经把这些做进评分里，这里列出方便你理解：

- **甜区**：改动 30~400 行、≤ 15 个文件、集中在 1~2 个模块
- **必毙**：dependabot / renovate / snyk 的例行依赖更新
- **慎选**：纯文档、纯 CI、纯依赖版本号、release notes、i18n 批量翻译
- **加分**：改了测试文件（可验证）、有对应 Issue、PR 描述解释了「为什么」

> 实测教训：GitHub 上越活跃的仓库，merged PR 里 bot 占比越高（某次 15 个里有 6 个）。
> 所以**一定要跑 `find_candidates.py` 而不是人工翻 PR 列表**。

---

## 常见问题

**Q：提示 403 / rate limit exceeded**
配额用尽。配 Token（5000/小时），或等重置（报错信息里会打印还需多少秒）。

**Q：404 not found**
仓库私有且 Token 无权限、owner/repo 拼写错误、或仓库已删除改名。

**Q：下载很慢或超时**
仓库太大。换规模更小的目标（建议 ≤ 100MB），或避开含大量二进制资源的仓库。

**Q：Windows 上解压报路径错误**
Windows 路径上限约 260 字符。把 `--out` 设到短路径，例如 `C:\snap`。

**Q：snapshot 里为什么没有 submodule 内容**
tarball 不含子模块。脚本会在检测到 `.gitmodules` 时打印提示，此时需要在说明里标注。

**Q：采集时发现目录被多包了一层**
已修复。当前实现会剥离 tar 包自带的 `{owner}-{repo}-{sha}/` 顶层目录，`repo/` 直接就是仓库根。

---

## 阈值调节

每个脚本顶部都有统一的阈值区，按你手上任务的真实要求改：

- `collect_snapshot.py` → `SKIP_DIRS` / `CODE_EXT` / `BUILD_FILES` / `CI_PATHS`
- `audit_repo.py` → `THRESHOLDS` / `SCORE_WEIGHTS`
- `audit_milestone.py` → `THRESHOLDS` / `CRITICALITY_PROFILES` / `WEIGHTS`
- `find_candidates.py` → `NOISE_TITLE_RE` / `FEATURE_SIGNAL_RE`

**建议：先按你的实际任务要求校准阈值，再批量跑。** 默认阈值是我根据一般采集标准设的，
不同项目的松紧尺度不一样，盲目照搬会筛掉合格样本或放进不合格样本。
