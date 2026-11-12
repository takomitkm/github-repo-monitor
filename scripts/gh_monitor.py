# -*- coding: utf-8 -*-
"""
gh_monitor.py — 参数化 GitHub 活动监控器（聚合 GitHub 信息，只采集分类，不发推送）

融合自：
  - magpie_monitor.py 骨架：增量游标采集(since) + seen 去重 + 三档分诊(alerts/contexts/errors)
    + 状态持久化(JSON 文件) + 自引导。
  - google/triage-party 特性：监控面参数化(任意 owner/repo + 子路径) + 过滤维度(keywords/labels/authors)。

设计原则：
  - 纯 stdlib 零依赖（urllib/json/datetime/argparse/re/os/sys/time）。
  - 默认【零远程写】：不建 issue、不发评论、不推送。只做 GET 采集 + 本地分类 + 打印/写报告。
    （要用它去发 issue 是另一个动作，需要 GITHUB_TOKEN + 明确授权，本脚本不内置。）
  - 无 GITHUB_TOKEN 也能跑（REST 匿名 60 req/hr，够小规模）；设了 GITHUB_TOKEN 则额度更高。
  - 状态存本地 JSON（默认 gh_monitor_state.json），游标只前进，重复跑不会重复报。

用法：
  python gh_monitor.py                       # 跑一次：按游标增量采集并分类
  python gh_monitor.py --since 2026-11-01T00:00:00Z   # 回放：无视游标，从该时刻开始
  python gh_monitor.py --lookback-days 14    # 首次跑（无游标）回看 14 天（默认 7）
  python gh_monitor.py --dry-run             # 只打印报告，不写状态（不会推进游标）
  python gh_monitor.py --report gh_report.md # 额外把报告写进这个文件
  python gh_monitor.py --state-file my_state.json  # 换状态文件位置

配置：改下面的 MONITOR_REPOS —— 你想盯哪个仓库、盯哪些子路径/关键词/label/作者。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# 让 Windows 控制台（ACP=936）也能安全打印中文
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "https://api.github.com"

# --------------------------------------------------------------- 配置表 ----
# 改这里。每条 = 一个监控面。字段含义见 MONITOR_REPOS 下方的注释。
MONITOR_REPOS = [
    {
        "repo": "google/leveldb",
        "paths": [],
        "keywords": ["breaking", "security", "CVE", "release"],
        "context_keywords": ["benchmark", "compaction", "performance"],
        "labels": [],
        "authors": [],
        "watch_commits": True,
        "watch_issues": True,
        "watch_releases": True,
        "note": "示例：Google LevelDB（改成你要盯的仓库即可）",
    },
]

# 每条配置的字段说明：
#   repo            必填，形如 "owner/name"。
#   paths           list[str]，只采集这些子路径下的 commits；空 list = 整个仓库。
#   keywords        list[str]，正则；命中（commit 首行 / issue 标题+正文 / release 名称+正文）→ alert。
#   context_keywords list[str]，正则；仅命中 → context（只列背景，不 alert）。
#   labels          list[str]，issue 过滤：只采集带这些 label 的 issue；空 = 不限。
#   authors         list[str]，作者过滤：只采集这些作者的 commit/issue；空 = 不限。
#   watch_commits / watch_issues / watch_releases  bool，采不采集对应活动。
#   note            str，显示在报告里的说明（这个 repo 为什么被盯）。
# 过滤分诊规则：
#   - keywords/context_keywords 都为空 → 该 repo 的所有采集都算 alert（无过滤=全报）。
#   - 命中 keywords → alert；仅命中 context_keywords → context；都不中 → 静默（只记去重键）。
#   - issue 额外受 labels/authors 过滤：配置了但 issue 不满足 → 直接跳过（不计入）。

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


# ---------------------------------------------------------------- time ----
def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    s = (s or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cst(dt):
    # 显示成 UTC 的字符串（简单起见统一 UTC；要本地时区自行改）
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- REST ----
class FetchError(Exception):
    pass


def _headers(extra=None):
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "gh-monitor",
         "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    if extra:
        h.update(extra)
    return h


def gh_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise FetchError("GET %s -> HTTP %s" % (path, e.code))
        except Exception as e:  # network / timeout / json
            last = e
            if attempt < 2:
                time.sleep(3)
                continue
    raise FetchError("GET %s -> %s" % (path, last))


# -------------------------------------------------------------- helpers ----
def match_patterns(text, patterns):
    t = (text or "").lower()
    return [p for p in patterns if re.search(p, t)]


def first_line(msg, cap=120):
    s = (msg or "").strip()
    if not s:
        return "(no message)"
    return s.splitlines()[0][:cap]


# 分诊：返回 "alert" / "context" / None
def classify(texts, cfg):
    keywords = cfg.get("keywords") or []
    ctx = cfg.get("context_keywords") or []
    if match_patterns("\n".join(texts), keywords):
        return "alert"
    if match_patterns("\n".join(texts), ctx):
        return "context"
    if not keywords and not ctx:
        return "alert"          # 无过滤配置 = 全报
    return None                 # 都不中 = 静默（只记去重键）


# ------------------------------------------------------------ collectors ----
def fetch_commits(repo, paths, since_dt, authors):
    items = []
    seen_sha = set()
    for path in (paths or [""]):
        params = {"since": iso(since_dt), "per_page": 30}
        if path:
            params["path"] = path
        batch = gh_get("/repos/%s/commits" % repo, params)
        if not isinstance(batch, list):
            continue
        for c in batch:
            sha = c.get("sha")
            if not sha or sha in seen_sha:
                continue
            seen_sha.add(sha)
            who = (c.get("commit") or {}).get("author") or {}
            if authors and who.get("name") not in authors:
                continue
            items.append(c)
    return items


def fetch_issues(repo, since_dt, labels, authors):
    items = []
    want_labels = set(labels or [])
    for page in (1, 2):
        batch = gh_get("/repos/%s/issues" % repo,
                       {"state": "all", "sort": "updated", "direction": "desc",
                        "per_page": 50, "page": page})
        if not isinstance(batch, list):
            break
        stop = False
        for it in batch:
            if "pull_request" in it:
                continue
            try:
                upd = parse_iso(it.get("updated_at"))
            except Exception:
                continue
            if upd <= since_dt:
                stop = True
                break
            if want_labels:
                got = {l.get("name") for l in (it.get("labels") or [])}
                if not (got & want_labels):
                    continue
            if authors:
                who = (it.get("user") or {}).get("login")
                if who not in authors:
                    continue
            items.append(it)
        if stop or len(batch) < 50:
            break
    return items


def fetch_releases(repo, since_dt):
    items = []
    batch = gh_get("/repos/%s/releases" % repo, {"per_page": 30})
    if not isinstance(batch, list):
        return items
    for r in batch:
        ts = r.get("published_at") or r.get("created_at")
        try:
            if parse_iso(ts) <= since_dt:
                continue
        except Exception:
            continue
        items.append(r)
    return items


# --------------------------------------------------------------- gather ----
def gather(cursor_dt, st):
    """返回 (alerts, contexts, errors)；同时更新 st 的去重集合。"""
    alerts, contexts, errors = [], [], []
    seen_shas = set(st.get("seen_shas") or [])
    seen_rel = set(st.get("seen_releases") or [])
    seen_iss = set(st.get("seen_issues") or [])

    for cfg in MONITOR_REPOS:
        repo = cfg["repo"]
        note = " · " + cfg["note"] if cfg.get("note") else ""
        head = "#### `%s`%s" % (repo, note)

        # ---- commits ----
        if cfg.get("watch_commits", True):
            try:
                commits = fetch_commits(repo, cfg.get("paths") or [],
                                        cursor_dt, cfg.get("authors") or [])
            except FetchError as e:
                errors.append("%s commits: %s" % (repo, e))
                commits = []
            for c in commits:
                key = "%s:%s" % (repo, c.get("sha"))
                if key in seen_shas:
                    continue
                seen_shas.add(key)
                when = (c.get("commit") or {}).get("author") or {}
                line = "- [`%s`](%s) %s — _%s_ (%s)" % (
                    c["sha"][:7], c.get("html_url") or "",
                    first_line((c.get("commit") or {}).get("message")),
                    when.get("name") or "?", (when.get("date") or "")[:10])
                kind = classify([first_line((c.get("commit") or {}).get("message"), 400)], cfg)
                if kind == "alert":
                    alerts.append(head + "\n" + line)
                elif kind == "context":
                    contexts.append("`%s` commit %s" % (repo, line))
                # else: 静默，只记去重键

        # ---- issues ----
        if cfg.get("watch_issues", True):
            try:
                issues = fetch_issues(repo, cursor_dt,
                                      cfg.get("labels") or [], cfg.get("authors") or [])
            except FetchError as e:
                errors.append("%s issues: %s" % (repo, e))
                issues = []
            for it in issues:
                key = "%s#%s" % (repo, it.get("number"))
                if key in seen_iss:
                    continue
                seen_iss.add(key)
                texts = [it.get("title") or "", it.get("body") or ""]
                line = "- [#%s](%s) **%s** _%s_ by @%s" % (
                    it.get("number"), it.get("html_url") or "", it.get("state") or "",
                    (it.get("title") or "")[:80], (it.get("user") or {}).get("login") or "?")
                kind = classify(texts, cfg)
                if kind == "alert":
                    alerts.append(head + "\n" + line)
                elif kind == "context":
                    contexts.append("`%s` issue %s" % (repo, line))

        # ---- releases ----
        if cfg.get("watch_releases", True):
            try:
                rels = fetch_releases(repo, cursor_dt)
            except FetchError as e:
                errors.append("%s releases: %s" % (repo, e))
                rels = []
            for r in rels:
                tag = r.get("tag_name") or r.get("name") or "?"
                key = "%s:%s" % (repo, tag)
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                texts = [r.get("name") or "", r.get("body") or ""]
                line = "- release [`%s`](%s) _%s_" % (
                    tag, r.get("html_url") or "", (r.get("name") or "")[:60])
                kind = classify(texts, cfg)
                if kind == "alert":
                    alerts.append(head + "\n" + line + "\n" + (r.get("body") or "").strip()[:400])
                elif kind == "context":
                    contexts.append("`%s` %s (no keywords in notes)" % (repo, line))

    # 回写去重集合（裁剪）
    st["seen_shas"] = list(seen_shas)[-400:]
    st["seen_releases"] = list(seen_rel)[-100:]
    st["seen_issues"] = list(seen_iss)[-400:]
    return alerts, contexts, errors


# --------------------------------------------------------------- output ----
def build_report(now, cursor, status, alerts, contexts, errors):
    b = ["# gh monitor — %s" % cst(now),
         "status=%s cursor=%s" % (status, iso(cursor)), ""]
    b.append("## ALERTS (%d)" % len(alerts))
    b.append("\n\n".join(alerts) if alerts else "(none)")
    b.append("")
    b.append("## Context (%d)" % len(contexts))
    if contexts:
        b.extend("- %s" % c for c in contexts)
    else:
        b.append("(none)")
    b.append("")
    b.append("## Errors (%d)" % len(errors))
    if errors:
        b.extend("- %s" % e for e in errors)
    else:
        b.append("(none)")
    return "\n".join(b)


def build_digest(now, cursor, alerts, contexts):
    b = ["## 🚨 gh monitor digest — %s" % cst(now), "",
         "**%d section(s)** since %s." % (len(alerts), cst(cursor)), ""]
    b.append("\n\n".join(alerts))
    if contexts:
        b.append("\n**Context (no immediate action):**")
        b.extend("- %s" % c for c in contexts)
    b.append("\n---\n*gh_monitor.py · 只采集分类，不发推送。*")
    return "\n".join(b)


# ---------------------------------------------------------------- state ----
def fresh_state():
    return {"last_checked": None, "run_count": 0, "last_status": "BASELINE",
            "seen_shas": [], "seen_releases": [], "seen_issues": []}


def load_state(sf):
    st = fresh_state()
    if os.path.exists(sf):
        try:
            with open(sf, encoding="utf-8") as f:
                loaded = json.load(f)
            for k in st:
                if k in loaded:
                    st[k] = loaded[k]
        except Exception:
            pass
    return st


def save_state(sf, st):
    d = os.path.dirname(os.path.abspath(sf))
    os.makedirs(d or ".", exist_ok=True)
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description="parameterized GitHub activity monitor")
    ap.add_argument("--since", default="",
                    help="cursor override, ISO 8601 UTC (rewind for a deep digest)")
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="first-run lookback window in days (default 7)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only: no state persist")
    ap.add_argument("--state-file", default="gh_monitor_state.json",
                    help="state JSON path (default gh_monitor_state.json)")
    ap.add_argument("--report", default="",
                    help="also write the report to this file")
    args = ap.parse_args()

    now = utcnow()
    st = load_state(args.state_file)

    baseline = not st.get("last_checked")
    if args.since.strip():
        cursor = parse_iso(args.since.strip())
        baseline = False
    elif baseline:
        cursor = now - timedelta(days=max(1, args.lookback_days))
    else:
        cursor = parse_iso(st["last_checked"])

    alerts, contexts, errors = gather(cursor, st)

    status = "ALERT" if alerts else ("BASELINE" if baseline else "QUIET")
    if errors:
        status += " (degraded)"

    report = build_report(now, cursor, status, alerts, contexts, errors)
    print(report)

    if args.report:
        d = os.path.dirname(os.path.abspath(args.report))
        os.makedirs(d or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)

    if args.dry_run:
        print("\ndry-run: state not persisted")
        return 0

    st["run_count"] = st.get("run_count", 0) + 1
    st["last_checked"] = iso(now)
    st["last_status"] = status
    save_state(args.state_file, st)
    return 1 if (errors and not (alerts or contexts)) else 0


if __name__ == "__main__":
    sys.exit(main())
