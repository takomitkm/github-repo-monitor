# `gh_monitor.py` — 声明式 GitHub 分诊引擎（triage-party 本地移植）

`scripts/gh_monitor.py` 是一个**纯标准库、零第三方依赖**的 GitHub 生态监控引擎，
把 [google/triage-party](https://github.com/google/triage-party) 的
`collection → rules → filters` 模型本地化、并保留了其七个 ✅ 可移植且高价值的维度。
默认**零远程写**：只发 `GET` 请求（GitHub REST API），本地只维护一个 `state` 游标文件，
不建 issue、不评论、不推送。没有服务器、没有数据库。

- 触发方式：**仅手动**。GitHub Actions 用 `on: workflow_dispatch`（见
  `.github/workflows/gh-triage.yml`），不做自动排程。
- 引擎规则写在 `scripts/rules.yaml`（YAML 子集，由内置 `miniyaml.py` 解析）。
  若缺失或解析失败，引擎回退到内置 `DEFAULT_RULES`，仍可运行。

---

## 七个已落地的高价值维度

| # | 维度 | 在引擎中的实现 |
|---|------|----------------|
| 1 | **多仓库聚合** | `repos:` 列出多个 `owner/repo`；`gather()` 跨仓库拉取 issue / PR / release，统一分诊。 |
| 2 | **时间窗过滤** | `created / updated / responded / prioritized / max-age / stale` + 运算符 `+Nd / -Nd / >= / <= / > / <`。 |
| 3 | **热度信号** | `reactions / comments / commenters / *-per-month / comments-while-closed / commenters-while-closed`，支持 `< <= > >= =` 数值比较。 |
| 4 | **近重复检测** | `min_similarity`（基于 `difflib.SequenceMatcher`）给相似标题打 `[similar]` 标签，去噪热点。 |
| 5 | **对话方向标签** | `tag: send | recv | recv-q | member-last | author-last | commented`，由有界评论拉取推导。 |
| 6 | **GitHub Enterprise** | `--api-url` / `GITHUB_API_URL` / `settings.github_api_url`；自动补 `/api/v3`。 |
| 7 | **声明式引擎** | `rules.yaml` 里 `collection → rules → filters`；collection 用 `resolution: alert|context`（triage-party 真实字段名，兼容旧别名 `tier`）定级。 |

> 不可移植的部分（实时看板、多人协同 Kanban、8 分钟热刷新、OAuth 登录、Web 服务）
> 被**刻意丢弃**——它们需要服务器/数据库，会牺牲本项目的简洁性。

---

## 规则语法（YAML 子集）

`rules.yaml` 由四个区块组成：`settings`、`repos`、`rules`、`collections`。
`miniyaml.py` 只支持「列表 + 单键字典 + 标量」，足以表达声明式规则。

```yaml
settings:
  github_api_url:                       # 可选：GitHub Enterprise base；自动补 /api/v3
  members: [octocat, dependabot]        # author-type 过滤用的“团队成员”集合
  roles:   {MEMBER: 1, COLLABORATOR: 1}

repos:
  - owner/repo-a
  - owner/repo-b

rules:
  security:                             # 规则名（自由字符串）
    desc: "安全相关"
    filters:                            # 顶层列表：各 filter 之间 AND
      - {label: "security|vuln|cve"}    # 正则（不区分大小写）；空集合时取反也成立
      - {title: "CVE-\\d+"}             # title / body 同样走正则
  hot:
    desc: "高热度或回归"
    filters:
      - {reactions: ">=5"}              # 数值比较
      - {title: "hot|regression|crash"} # 多条件 AND，避免误伤
  stale:
    filters:
      - {state: "open"}
      - {updated: "+30d"}               # +Nd = 已过去 ≥ N 天（更老）；-Nd = ≤ N 天（更新）
  quiet-dup:
    filters:
      - {min_similarity: 0.85}          # 近重复（相似标题）打 [similar]

collections:
  alerts:                               # 分诊桶
    resolution: alert                   # triage-party 真实字段；tier: alert 为兼容别名
    rules: [security, hot, stale]
  context:
    resolution: context
    rules: [quiet-dup]
```

### 过滤字段与运算符速查

**字段匹配（正则，不区分大小写；`!` 前缀表示“不含”）**

| 字段 | 说明 |
|------|------|
| `repo` | 仓库 `owner/repo`（collection 级 `repos` 可进一步限定） |
| `author` | 作者登录名 |
| `assignee` | 被指派人登录名（对应 tag `assigned`） |
| `label` / `!label` | issue/PR 标签（标签列表为空时 `!label` 仍成立） |
| `title` / `body` / `milestone` | 文案 |
| `path` | 变更路径（PR） |
| `type` | `issue` / `pr` / `release` |
| `tag` | 见下方「tag 取值」 |

**时间窗（单位天；`+` 更老、`-` 更新）**

| 键 | 含义 |
|----|------|
| `created` | 创建时间 |
| `updated` | 最后更新 |
| `responded` | 团队是否响应：`responded:+60d` ⇒ “约两个月内无团队响应” |
| `prioritized` | 被打 priority 的时间 |
| `max-age` | 距今天数上限 |
| `stale` | 距今天数下限（更老） |

运算符：`+Nd`（`>=N` 天）、`-Nd`（`<=N` 天）、`>= / <= / > / <`（数值比较）。

**热度（数值比较，`< <= > >= =`）**

`reactions`、`comments`、`commenters`、
`reactions-per-month`、`comments-per-month`、`commenters-per-month`、
`comments-while-closed`、`commenters-while-closed`。
`*-per-month` 用 `months_since = 天数 / 30.4375`（下限 0.03）做归一化。

**近重复**：`min_similarity: 0.85`（0–1）。

### tag 取值

| 分组 | tag |
|------|-----|
| 对话方向（需评论拉取） | `send` `recv` `recv-q` `member-last` `author-last` `commented` |
| 事务 / 元数据 | `assigned` `assignee-updated` `open-milestone` `closed` `merged` `draft` `similar` |
| PR 评审（需评审拉取） | `approved` `changes-requested` `reviewed-with-comment` `new-commits` `unreviewed` `pushed-after-approval` |

`!tag` 取反。PR 评审标签同时以 `pr-` 前缀挂到其关联 issue 上（如 `pr-approved`）。

### B 类增强：PR 评审 / 指派人 / 集合级作用域

- **PR 评审标签**：仅对候选 PR（按更新时间降序、最多 `--max-enrich` 条）拉
  `GET /repos/{repo}/pulls/{n}/reviews`（必要时再拉 `.../commits`）。按**最后一次评审**
  判定 `approved` / `changes-requested` / `reviewed-with-comment`；无评审 ⇒ `unreviewed`；
  最后评审之后又有提交 ⇒ `new-commits`；通过后又推送 ⇒ `pushed-after-approval`。
  只在规则真正用到这些标签时才触发评审拉取（`_needs_reviews`）。
- **`assignee` 过滤 / `assignee-updated`**：按被指派人登录名正则匹配；
  `assignee-updated` 表示“最后一条评论来自该 issue 的指派人”。
- **`open-milestone`**：issue 属于一个 **open** 里程碑（读 `milestone.state`）。
- **collection 级作用域**：集合可选 `repos: [owner/repo]` 只从这些仓库取条目；
  可选 `category`（层级分类）与 `description`（人类可读说明）。

---

## 运行

```bash
# 增量运行（用本地 state 游标，只报新内容）
python3 scripts/gh_monitor.py

# 从某日期回放（深读）
python3 scripts/gh_monitor.py --since 2026-11-01T00:00:00Z

# 首次运行回看 N 天
python3 scripts/gh_monitor.py --lookback-days 14

# 只读报告，不推进游标
python3 scripts/gh_monitor.py --dry-run

# 报告同时写文件
python3 scripts/gh_monitor.py --report gh_report.md

# 拉取评论上限 / 关闭评论拉取
python3 scripts/gh_monitor.py --max-enrich 10
python3 scripts/gh_monitor.py --no-enrich

# GitHub Enterprise
python3 scripts/gh_monitor.py --api-url https://ghe.example.com/api/v3

# 零网络自测（解析 + 引擎）
python3 scripts/gh_monitor.py --self-test

# 只校验配置（零网络，不采集）
python3 scripts/gh_monitor.py --validate

# 生成一份 rules.yaml 模板（已存在时用 --force 覆盖）
python3 scripts/gh_monitor.py --init
python3 scripts/gh_monitor.py --init --force
```

环境变量：`GITHUB_TOKEN`（未设则匿名、速率受限）；`GITHUB_API_URL`（GHE）。

### 退出码

- `0`：正常（含“有错误但有分诊结果”）。
- `1`：仅在**有抓取错误且零分诊结果**时返回。

---

## 手动触发（GitHub Actions）

`.github/workflows/gh-triage.yml` 用 `workflow_dispatch` 触发，默认可输入：
`since`、`lookback-days`、`max-enrich`、`dry-run`（默认 `true`）、`save-report`
（默认 `false`，开启则把 `gh_report.md` 作为 Artifact 上传）。

权限仅为 `contents: read`（外加 `actions: write` 仅用于可选的 Artifact 上传），
**不授予 `issues: write`**——与“默认零远程写”一致。

---

## 设计取舍

- **零第三方依赖**：内置 `miniyaml.py` 解析 YAML 子集，避免引入 PyYAML。
- **零远程写**：唯一网络调用是 `GET`；唯一本地写是 state 文件（可被 `--dry-run` 跳过）。
- **游标只前进**：`state` 记录 `cursor` 与去重集合，重跑不重复报告。
- **有界拉取**：`--max-enrich` 限制最多拉取 N 条 issue/PR 的评论与 PR 评审（按更新时间降序），
  用于 `responded` / 对话方向 / `commenters` / PR 评审标签；只有规则用到相关信号时才触发，
  以控制速率与配额。
