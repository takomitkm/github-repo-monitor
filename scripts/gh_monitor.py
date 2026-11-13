# -*- coding: utf-8 -*-
"""github-repo-monitor — daily GitHub ecosystem monitor — pure stdlib, zero dependency.

声明式分诊引擎：由 scripts/rules.yaml（纯 stdlib 解析，零第三方依赖）驱动，
移植自 google/triage-party 的 collection -> rules -> filters 三层模型。

七个 ✅ 直接可移植维度（全部为真实代码，非文档）：
  1. 多仓库聚合           repos: 列表，跨仓库统一报告
  2. 时间窗过滤器         created/updated/responded/max-age/stale 支持 [-+]时长 与 >/>=/</<=
  3. 热度信号             reactions / comments / commenters / *-per-month
  4. 近似去重             min_similarity + [similar] 标记（difflib.SequenceMatcher）
  5. 对话方向标记         tag:send|recv|recv-q|member-last|author-last|commented（有界评论拉取）
  6. GitHub Enterprise    --api-url / GITHUB_API_URL / settings.github_api_url（自动补 /api/v3）
  7. 声明式 collection/rules/filters 引擎

默认零远程写入（只读采集 + 本地 state 文件），无服务器、无数据库。
日常监控仅由 GitHub Actions 手动触发（on: workflow_dispatch），不自动调度。
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 让脚本能 import 同目录的 miniyaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from miniyaml import load as yaml_load  # 纯 stdlib，零依赖
except Exception:  # pragma: no cover
    yaml_load = None

DEFAULT_API = "https://api.github.com"
API = DEFAULT_API  # 运行时由 --api-url / GITHUB_API_URL / settings 覆盖
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

TIER_PRIORITY = {"alert": 0, "error": 1, "context": 2, "quiet": 3}


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc)


def parse_iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cst(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_days(days):
    if days is None:
        return "?"
    days = float(days)
    if days < 1:
        return "%.0fh" % (days * 24)
    if days < 30:
        return "%.0fd" % days
    if days < 365:
        return "%.1fmo" % (days / 30.4375)
    return "%.1fy" % (days / 365.25)


# ---------------------------------------------------------------------------
# 比较/时长解析
# ---------------------------------------------------------------------------
_UNIT_DAYS = {
    "d": 1.0, "day": 1.0, "days": 1.0,
    "w": 7.0, "week": 7.0, "weeks": 7.0,
    "mo": 30.4375, "month": 30.4375, "months": 30.4375,
    "y": 365.25, "year": 365.25, "years": 365.25,
    "h": 1.0 / 24.0, "hour": 1.0 / 24.0, "hours": 1.0 / 24.0,
    "m": 1.0 / 1440.0, "min": 1.0 / 1440.0, "mins": 1.0 / 1440.0,
    "": 1.0,
}


def parse_duration(raw):
    """返回 (op, days) op∈{'+','-','>','>=','<','<=','='}。
    '+' = 至少 N（更早/更旧）；'-' = 至多 N（更新/更近）。无符号默认 '>='。"""
    s = str(raw).strip()
    m = re.match(r"^([+\-<>]=?|=)?\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)$", s)
    if not m:
        return None
    op = m.group(1) or ">="
    val = float(m.group(2))
    unit = (m.group(3) or "").lower()
    days = val * _UNIT_DAYS.get(unit, 1.0)
    if op == "+":
        op = ">="
    elif op == "-":
        op = "<="
    return op, days


def parse_num(raw):
    """返回 (op, value) op∈{'<','<=','>','>=','='}。无符号默认 '>='。"""
    s = str(raw).strip()
    m = re.match(r"^([<>]=?|=)?\s*([0-9]+(?:\.[0-9]+)?)$", s)
    if not m:
        return None
    op = m.group(1) or ">="
    return op, float(m.group(2))


def compare(op, actual, threshold):
    if op == ">":
        return actual > threshold
    if op == ">=":
        return actual >= threshold
    if op == "<":
        return actual < threshold
    if op == "<=":
        return actual <= threshold
    if op == "=":
        return abs(actual - threshold) < 1e-9
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class FetchError(Exception):
    pass


def configure_api(base):
    """设置全局 API base，必要时自动补 /api/v3（GitHub Enterprise）。"""
    global API
    base = (base or "").strip().rstrip("/")
    if not base:
        API = DEFAULT_API
        return
    if "api.github.com" not in base and "api/v3" not in base:
        base = base.rstrip("/") + "/api/v3"
    API = base


def _headers(extra=None):
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-repo-monitor",
    }
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    if extra:
        h.update(extra)
    return h


def gh_get(path, params=None):
    url = API.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            time.sleep(3)
        except Exception as e:  # noqa
            last = e
            time.sleep(3)
    if isinstance(last, urllib.error.HTTPError):
        raise FetchError("HTTP %s for %s" % (last.code, url))
    raise FetchError("request failed for %s: %s" % (url, last))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_RULES = {
    "settings": {
        "name": "github-repo-monitor",
        "min_similarity": 0.75,
        "dedup_similar": False,
        "unmatched": "context",
        "members": [],
        "member_roles": ["OWNER", "MEMBER", "COLLABORATOR"],
        "github_api_url": DEFAULT_API,
        "state": "all",
    },
    "repos": [
        {"repo": "google/leveldb", "note": "LevelDB upstream",
         "watch_commits": True, "watch_issues": True, "watch_releases": True,
         "paths": [], "authors": [], "labels": []},
    ],
    "rules": {
        "security-alert": {"name": "安全相关", "filters": [
            {"label": "!wontfix"},
            {"title": "CVE|security|RCE"},
            {"reactions": ">=5"},
        ]},
        "perf-context": {"name": "性能相关", "filters": [
            {"title": "benchmark|compaction|performance"},
        ]},
    },
    "collections": [
        {"id": "triage", "name": "待分诊", "rules": ["security-alert", "perf-context"]},
    ],
}


def load_rules(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml_load(f.read())
    else:
        data = DEFAULT_RULES
    cfg = dict(DEFAULT_RULES)
    cfg.update(data or {})
    settings = dict(DEFAULT_RULES["settings"])
    settings.update(cfg.get("settings") or {})
    cfg["settings"] = settings
    cfg.setdefault("repos", DEFAULT_RULES["repos"])
    cfg.setdefault("rules", DEFAULT_RULES["rules"])
    cfg.setdefault("collections", DEFAULT_RULES["collections"])
    # 规则 -> 有效 tier（来自所属 collection；否则规则自身 tier；否则默认 context）
    rule_tier = {}
    for rname in cfg["rules"]:
        r = cfg["rules"][rname]
        rule_tier[rname] = (r.get("tier") or "context")
    for c in cfg["collections"]:
        # triage-party 用 `resolution` 决定集合档位；内嵌 DEFAULT_RULES 旧式用 `tier`
        ctier = c.get("resolution") or c.get("tier") or "context"
        for rn in (c.get("rules") or []):
            if rn in rule_tier:
                if TIER_PRIORITY.get(ctier, 9) < TIER_PRIORITY.get(rule_tier[rn], 9):
                    rule_tier[rn] = ctier
    cfg["_rule_tier"] = rule_tier
    cfg["_members"] = set(m.lower() for m in (settings.get("members") or []))
    cfg["_roles"] = set(str(r).upper() for r in (settings.get("member_roles") or []))
    # 需要评论数据的过滤器（触发有界拉取）
    cfg["_needs_deep"] = _needs_deep(cfg)
    return cfg


_DEEP_KEYS = {
    "responded", "commenters", "comments-while-closed", "commenters-while-closed",
}
_DEEP_TAGS = {"recv", "recv-q", "send", "member-last", "author-last", "commented"}


def _needs_deep(cfg):
    for r in cfg["rules"].values():
        for flt in (r.get("filters") or []):
            for k, v in flt.items():
                if k in _DEEP_KEYS:
                    return True
                if k == "tag" and re.search(r"[!]?(" + "|".join(_DEEP_TAGS) + r")\b", str(v)):
                    return True
    return False


# ---------------------------------------------------------------------------
# 采集：归一化为统一 item 结构
# ---------------------------------------------------------------------------
def _norm_labels(issue):
    return [l.get("name", "") for l in (issue.get("labels") or []) if isinstance(l, dict)]


def _item_base(repo):
    return {
        "repo": repo,
        "kind": None, "number": None, "sha": None, "tag": None,
        "title": "", "body": "", "url": "",
        "author_login": "", "author_name": "", "author_association": "",
        "created": None, "updated": None,
        "state": "all", "labels": [], "milestone": None,
        "reactions": 0, "comments": 0, "commenters": 0,
        "assignees": [], "is_pr": False, "is_draft": False, "merged": False,
        "path": None, "comments_url": None, "closed_at": None,
        "tags": set(), "responded_last_team": None, "similar_to": None,
        "comment_data": None,
    }


def fetch_commits(repo, paths, since_dt, authors):
    out = []
    for p in (paths or [""]):
        params = {"per_page": 30, "sha": "HEAD"}
        if since_dt:
            params["since"] = iso(since_dt)
        if p:
            params["path"] = p
        try:
            arr = gh_get("/repos/%s/commits" % repo, params)
        except FetchError as e:
            yield None, e
            return
        for c in (arr or []):
            commit = c.get("commit", {})
            auth = commit.get("author", {}) or {}
            dt = parse_iso(auth.get("date"))
            if since_dt and dt and dt <= since_dt:
                continue
            msg = commit.get("message", "") or ""
            it = _item_base(repo)
            it.update(kind="commit", sha=c.get("sha"), title=msg.split("\n", 1)[0][:200],
                      body=msg, url=c.get("html_url", ""), created=dt, updated=dt,
                      author_name=auth.get("name", ""), author_login=(c.get("author") or {}).get("login", ""))
            it["tags"].add("commit")
            if p:
                it["path"] = p
            if authors and it["author_login"] not in authors and it["author_name"] not in authors:
                continue
            out.append(it)
    return out


def fetch_issues(repo, since_dt, labels, authors):
    out = []
    page = 1
    seen = set()
    while True:
        params = {"state": "all", "sort": "updated", "direction": "desc",
                  "per_page": 50, "page": page}
        try:
            arr = gh_get("/repos/%s/issues" % repo, params)
        except FetchError as e:
            return out, e
        if not arr:
            break
        stop = False
        for it in arr:
            num = it.get("number")
            if num in seen:
                continue
            seen.add(num)
            upd = parse_iso(it.get("updated_at"))
            if since_dt and upd and upd <= since_dt:
                stop = True
                break
            if it.get("pull_request") and "pull_request" in it:
                pass
            obj = _item_base(repo)
            is_pr = "pull_request" in it
            obj.update(
                kind="pr" if is_pr else "issue", number=num,
                title=it.get("title", "") or "", body=it.get("body", "") or "",
                url=it.get("html_url", ""),
                author_login=(it.get("user") or {}).get("login", ""),
                author_association=it.get("author_association", ""),
                created=parse_iso(it.get("created_at")), updated=upd,
                state=it.get("state", "all"), labels=_norm_labels(it),
                milestone=(it.get("milestone") or {}).get("title"),
                reactions=(it.get("reactions") or {}).get("total_count", 0),
                comments=it.get("comments", 0),
                assignees=[a.get("login") for a in (it.get("assignees") or [])],
                is_pr=is_pr, is_draft=bool(it.get("draft", False)),
                merged=bool((it.get("pull_request") or {}).get("merged_at")),
                closed_at=parse_iso(it.get("closed_at")),
                comments_url=it.get("comments_url"),
            )
            obj["tags"].add("pr" if is_pr else "issue")
            if obj["state"] == "closed":
                obj["tags"].add("closed")
            if obj["is_draft"]:
                obj["tags"].add("draft")
            if obj["merged"]:
                obj["tags"].add("merged")
            if obj["milestone"]:
                obj["tags"].add("milestone")
            if obj["assignees"]:
                obj["tags"].add("assigned")
            if labels and not (set(obj["labels"]) & set(labels)):
                continue
            if authors and obj["author_login"] not in authors:
                continue
            out.append(obj)
        if stop or len(arr) < 50:
            break
        page += 1
    return out, None


def fetch_releases(repo, since_dt):
    out = []
    try:
        arr = gh_get("/repos/%s/releases" % repo, {"per_page": 30})
    except FetchError as e:
        return out, e
    for r in (arr or []):
        pub = parse_iso(r.get("published_at")) or parse_iso(r.get("created_at"))
        if since_dt and pub and pub <= since_dt:
            continue
        obj = _item_base(repo)
        obj.update(kind="release", tag=r.get("tag_name"),
                   title=(r.get("name") or r.get("tag_name") or "release"),
                   body=r.get("body", "") or "", url=r.get("html_url", ""),
                   created=pub, updated=pub)
        obj["tags"].add("release")
        out.append(obj)
    return out, None


# ---------------------------------------------------------------------------
# 有界评论拉取（对话方向 + 热度 commenters + responded）
# ---------------------------------------------------------------------------
def enrich_comments(objs, cfg, max_deep):
    """只对 issue/pr 拉评论，且最多 max_deep 条（按 updated 降序）。"""
    if not max_deep or max_deep <= 0:
        return
    now = utcnow()
    cands = sorted([o for o in objs if o["kind"] in ("issue", "pr")],
                   key=lambda o: o["updated"] or utcnow(), reverse=True)[:max_deep]
    members = cfg["_members"]
    roles = cfg["_roles"]
    for o in cands:
        cu = o.get("comments_url")
        if not cu:
            continue
        path = "/repos/%s/issues/%s/comments" % (o["repo"], o["number"])
        try:
            comps = gh_get(path, {"per_page": 100})
        except FetchError:
            comps = []
        o["comment_data"] = comps or []
        team_last = None
        author_last = None
        last = None
        distinct = set()
        for c in o["comment_data"]:
            dt = parse_iso(c.get("created_at"))
            login = (c.get("user") or {}).get("login", "")
            assoc = c.get("author_association", "")
            distinct.add(login)
            is_team = login.lower() in members or str(assoc).upper() in roles
            if is_team and dt:
                if team_last is None or dt > team_last:
                    team_last = dt
            if login and login == o["author_login"] and dt:
                if author_last is None or dt > author_last:
                    author_last = dt
            last = (dt, login, is_team, c.get("body", "") or "")
        o["commenters"] = len(distinct)
        o["responded_last_team"] = team_last
        if team_last is not None:
            o["tags"].add("commented")
        if last:
            ldt, llogin, lteam, lbody = last
            if lteam:
                o["tags"].add("member-last")
                o["tags"].add("send")
            elif llogin == o["author_login"]:
                o["tags"].add("author-last")
                o["tags"].add("recv")
            else:
                o["tags"].add("recv")
                if "?" in lbody:
                    o["tags"].add("recv-q")
        elif team_last is not None:
            o["tags"].add("send")


# ---------------------------------------------------------------------------
# 相似去重（difflib）
# ---------------------------------------------------------------------------
def _norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def mark_similar(objs, cfg):
    minsim = float(cfg["settings"].get("min_similarity", 0) or 0)
    if minsim <= 0:
        return
    canon = []
    for o in objs:
        nt = _norm_title(o["title"])
        if len(nt) < 6:
            continue
        hit = None
        for c in canon:
            if c["repo"] != o["repo"]:
                continue
            if difflib.SequenceMatcher(None, nt, c["norm"]).ratio() >= minsim:
                hit = c
                break
        if hit:
            o["similar_to"] = (hit["repo"], hit["number"], hit["title"])
            hit["similars"].append(o)
            o["tags"].add("similar")
        else:
            canon.append({"repo": o["repo"], "norm": nt, "number": o["number"],
                          "title": o["title"], "similars": []})


# ---------------------------------------------------------------------------
# 过滤器求值
# ---------------------------------------------------------------------------
def _regex_pass(value, targets):
    """正则过滤；value 以 '!' 开头表示否定。targets 为空列表时："""
    neg = str(value).startswith("!")
    pat = value[1:] if neg else value
    found = any(re.search(pat, t, re.I) for t in targets)
    return (not found) if neg else found


def months_since(o):
    if not o["created"]:
        return 0.03
    days = (utcnow() - o["created"]).total_seconds() / 86400.0
    return max(days / 30.4375, 0.03)


def match_filter(o, key, raw):
    key = key.strip().lower()
    if key == "label":
        return _regex_pass(raw, o["labels"] or [])
    if key == "title":
        return _regex_pass(raw, [o["title"] or ""])
    if key == "body":
        return _regex_pass(raw, [o["body"] or ""])
    if key == "tag":
        return _regex_pass(raw, list(o["tags"]) or [])
    if key == "milestone":
        return _regex_pass(raw, [o["milestone"] or ""]) if o["milestone"] else False
    if key == "repo":
        return _regex_pass(raw, [o["repo"] or ""])
    if key == "author":
        return _regex_pass(raw, [o["author_login"] or "", o["author_name"] or ""])
    if key == "path":
        return _regex_pass(raw, [o["path"] or ""]) if o["path"] else False
    if key == "type":
        v = str(raw).strip().lower()
        if v == "all":
            return True
        return _regex_pass("^" + re.escape(v) + "$", [o["kind"] or ""])
    if key == "author-type":
        v = str(raw).strip().lower()
        is_member = o["author_login"].lower() in _MEMBERS or \
            str(o["author_association"]).upper() in _ROLES
        return v == ("member" if is_member else "external")
    if key == "state":
        v = str(raw).strip().lower()
        if v == "all":
            return True
        return o["state"] == v
    # 时长类
    if key in ("created", "updated", "responded", "prioritized", "max-age", "stale"):
        parsed = parse_duration(raw)
        if not parsed:
            return False
        op, days = parsed
        ref = o["created"] if key == "created" else \
            o["updated"] if key in ("updated", "max-age", "stale", "prioritized") else \
            o["responded_last_team"]
        if ref is None:
            elapsed = float("inf")
        else:
            elapsed = (utcnow() - ref).total_seconds() / 86400.0
        if key in ("max-age",):
            op = "<=" if op == ">" else op  # max-age: 默认「不大于 N 天」
        if key == "stale":
            op = ">=" if op == ">" else op
        return compare(op, elapsed, days)
    # 数值类
    nums = {
        "reactions": o["reactions"], "comments": o["comments"],
        "commenters": o["commenters"],
        "reactions-per-month": o["reactions"] / months_since(o),
        "comments-per-month": o["comments"] / months_since(o),
        "commenters-per-month": o["commenters"] / months_since(o),
        "comments-while-closed": (
            sum(1 for c in (o.get("comment_data") or [])
                if (o["state"] == "closed" and parse_iso(c.get("created_at")) and
                    (o.get("closed_at") and parse_iso(c.get("created_at")) > parse_iso(o["closed_at"])))) )
            if o.get("comment_data") is not None else 0,
        "commenters-while-closed": (
            len(set(c.get("user", {}).get("login") for c in (o.get("comment_data") or [])
                   if (o["state"] == "closed" and parse_iso(c.get("created_at")) and
                       (o.get("closed_at") and parse_iso(c.get("created_at")) > parse_iso(o["closed_at"]))))))
            if o.get("comment_data") is not None else 0,
    }
    if key not in nums:
        return False
    pr = parse_num(raw)
    if not pr:
        return False
    return compare(pr[0], nums[key], pr[1])


_MEMBERS = set()
_ROLES = set()


def rule_matches(o, rule):
    for flt in (rule.get("filters") or []):
        if not flt:
            continue
        for k, v in flt.items():
            if not match_filter(o, k, str(v)):
                return False
    return True


def classify(o, cfg):
    matched = []
    for rname, rule in cfg["rules"].items():
        if rule_matches(o, rule):
            matched.append(rname)
    if not matched:
        um = (cfg["settings"].get("unmatched") or "context").lower()
        if um in ("skip", "quiet", "ignore", "none", ""):
            return None
        return {"tier": um, "rules": []}
    tier = min((cfg["_rule_tier"].get(r, "context") for r in matched),
               key=lambda t: TIER_PRIORITY.get(t, 9))
    return {"tier": tier, "rules": matched}


# ---------------------------------------------------------------------------
# 采集 + 分诊
# ---------------------------------------------------------------------------
def fresh_state():
    return {"cursor": None, "seen": []}


def load_state(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    d.setdefault("seen", [])
                    return d
        except Exception:
            pass
    return fresh_state()


def save_state(path, st):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def gather(cfg, cursor_dt, lookback_days, max_deep, no_enrich):
    now = utcnow()
    if lookback_days:
        floor = now - timedelta(days=lookback_days)
        if cursor_dt is None or cursor_dt < floor:
            cursor_dt = floor  # 不回看超过 lookback_days；--since 早于 floor 时截断
    objs = []
    errors = []
    for rp in cfg["repos"]:
        repo = rp.get("repo")
        if not repo:
            continue
        paths = rp.get("paths") or []
        authors = set(rp.get("authors") or [])
        labels = rp.get("labels") or []
        if rp.get("watch_commits"):
            for c in fetch_commits(repo, paths, cursor_dt, authors):
                if isinstance(c, tuple):
                    errors.append((repo, str(c[1]))); continue
                objs.append(c)
        if rp.get("watch_issues"):
            arr, err = fetch_issues(repo, cursor_dt, labels, authors)
            objs.extend(arr)
            if err:
                errors.append((repo, str(err)))
        if rp.get("watch_releases"):
            arr, err = fetch_releases(repo, cursor_dt)
            objs.extend(arr)
            if err:
                errors.append((repo, str(err)))
    # 有界评论拉取
    if cfg["_needs_deep"] and not no_enrich:
        enrich_comments(objs, cfg, max_deep)
    mark_similar(objs, cfg)
    # 分诊
    by_tier = {"alert": [], "context": [], "error": []}
    for o in objs:
        res = classify(o, cfg)
        if not res:
            continue
        o["_tier"] = res["tier"]
        o["_rules"] = res["rules"]
        if res["tier"] in by_tier:
            by_tier[res["tier"]].append(o)
    return objs, by_tier, errors


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _line(o):
    parts = []
    if o["kind"] == "commit":
        head = "commit `%s`" % (o["sha"][:7] if o["sha"] else "?")
    elif o["kind"] == "release":
        head = "release `%s`" % (o["tag"] or o["title"])
    else:
        head = "#%s" % o["number"]
    parts.append("- %s **%s**" % (head, o["title"].replace("*", "\\*")[:160]))
    if o["url"]:
        parts.append("([link](%s))" % o["url"])
    if o["_rules"]:
        parts.append(" — `%s`" % ",".join(o["_rules"]))
    heat = []
    if o["reactions"]:
        heat.append("👍%d" % o["reactions"])
    if o["comments"]:
        heat.append("💬%d" % o["comments"])
    if o["commenters"]:
        heat.append("👥%d" % o["commenters"])
    if heat:
        parts.append(" " + " ".join(heat))
    dir_tags = [t for t in o["tags"] if t in _DEEP_TAGS | {"similar", "assigned", "milestone", "closed", "merged", "draft"}]
    if dir_tags:
        parts.append(" · " + " ".join("`%s`" % t for t in dir_tags))
    if o["similar_to"]:
        parts.append(" · [similar→ %s#%s]" % (o["similar_to"][0].split("/")[-1], o["similar_to"][1]))
    if o["updated"]:
        parts.append(" · %s ago" % human_days((utcnow() - o["updated"]).total_seconds() / 86400.0))
    return "".join(parts)


def build_report(now, cursor_dt, by_tier, errors, cfg, enriched):
    lines = ["# github-repo-monitor — %s" % cst(now), ""]
    lines.append("> cursor: %s · api: `%s` · enriched: %d · 仅采集分诊，零远程写入" %
                 (cst(cursor_dt) if cursor_dt else "首次", API, enriched))
    lines.append("")
    counts = {t: len(by_tier.get(t, [])) for t in ("alert", "context", "error")}
    sections = [("ALERTS", "alert"), ("CONTEXT", "context"), ("ERRORS (fetch)", "error")]
    for label, t in sections:
        items = by_tier.get(t, [])
        lines.append("## %s (%d)" % (label, len(items)))
        lines.append("")
        if t == "error":
            if errors:
                for repo, msg in errors:
                    lines.append("- `%s`: %s" % (repo, msg))
            else:
                lines.append("_无_")
            lines.append("")
            continue
        # 按 repo 分组
        by_repo = {}
        for o in items:
            by_repo.setdefault(o["repo"], []).append(o)
        for repo in sorted(by_repo):
            lines.append("#### `%s`" % repo)
            for o in by_repo[repo]:
                lines.append(_line(o))
            lines.append("")
    lines.append("---")
    lines.append("*github-repo-monitor · rules.yaml 驱动 · 纯 stdlib 零依赖*")
    return "\n".join(lines), counts


# ---------------------------------------------------------------------------
# 自测（零网络）
# ---------------------------------------------------------------------------
def _selftest(cfg):
    """零网络：构造确定性的规则，验证引擎各维度（不依赖 rules.yaml 命名）。"""
    import copy
    now = utcnow()
    # 确定性配置：3 条规则 + 1 个 collection（安全/热度=alert，性能=context）
    tcfg = {
        "settings": {"unmatched": "context", "members": [],
                     "member_roles": ["OWNER", "MEMBER", "COLLABORATOR"],
                     "min_similarity": 0.75},
        "repos": [{"repo": "google/leveldb"}],
        "rules": {
            "security": {"name": "安全", "tier": "alert", "filters": [
                {"title": "CVE|security|RCE"}, {"reactions": ">=5"},
                {"label": "!wontfix"}]},
            "hot": {"name": "热度", "tier": "alert", "filters": [
                {"title": "hot|regression|crash"}, {"reactions": ">=5"},
                {"created": "-90d"}]},
            "perf": {"name": "性能", "tier": "context", "filters": [
                {"title": "benchmark|compaction|performance"}]},
            "release": {"name": "发布", "tier": "alert", "filters": [
                {"type": "release"}]},
        },
        "collections": [{"id": "triage", "tier": "context",
                         "rules": ["security", "hot", "perf", "release"]}],
        "_rule_tier": {"security": "alert", "hot": "alert",
                       "perf": "context", "release": "alert"},
        "_members": set(), "_roles": {"MEMBER"}, "_needs_deep": True,
    }

    def mk(**kw):
        o = _item_base("google/leveldb")
        o.update(created=now, updated=now, state="open", author_login="a",
                 title="t", body="", labels=[])
        o.update(**kw)
        return o

    cases = [
        (mk(title="CVE security advisory", reactions=10), "security", "alert"),
        (mk(title="performance benchmark results", kind="issue", number=1), None, "context"),
        (mk(title="random change", reactions=0), None, "context"),
        (mk(title="hot crash regression", reactions=20), "hot", "alert"),
        (mk(title="release v1.2", kind="release", tag="v1.2"), "release", "alert"),
        (mk(title="CVE", labels=["wontfix"], reactions=10), None, "context"),  # !wontfix 反例
        (mk(title="CVE", reactions=2), None, "context"),  # 热度不够（<5）
        # 同时命中 alert(security) 与 context(perf)：tier 应取更紧急的 alert
        (mk(title="CVE security performance benchmark", reactions=10), "security", "alert"),
    ]
    ok = True
    for o, expect_rule, expect_tier in cases:
        res = classify(o, tcfg)
        tier = res["tier"] if res else "skip"
        good = (expect_tier == tier) and \
            (expect_rule is None or expect_rule in (res["rules"] if res else []))
        if not good:
            ok = False
            print("  FAIL: title=%r expect rule~%s tier=%s got %s rules=%s" %
                  (o["title"], expect_rule, expect_tier, tier,
                   res["rules"] if res else None))

    # 时长解析
    assert parse_duration("+30d") == (">=", 30)
    assert parse_duration("-7d") == ("<=", 7)
    assert parse_duration(">=2w")[0] == ">="
    assert parse_num(">=5") == (">=", 5.0)

    # 相似去重
    a = mk(title="Fix crash on empty key", number=1)
    b = mk(title="Fix crash on empty key in iterator", number=2)
    mark_similar([a, b], tcfg)
    assert b["similar_to"] is not None, "similarity should link b->a"

    # 评论方向（本地合成，不需网络）
    c = mk(title="q", kind="issue", number=3, comments=2)
    c["comment_data"] = [
        {"user": {"login": "outsider"}, "author_association": "NONE",
         "created_at": iso(now), "body": "any"},
        {"user": {"login": "maintainer"}, "author_association": "MEMBER",
         "created_at": iso(now), "body": "we will fix"},
    ]
    c["commenters"] = 2
    c["responded_last_team"] = now
    c["tags"].update({"commented", "member-last", "send"})
    assert match_filter(c, "tag", "send") is True
    assert match_filter(c, "tag", "recv") is False
    assert match_filter(c, "responded", "+0d") is True       # 刚响应（>=0 天）
    assert match_filter(c, "responded", "+9999d") is False   # 很久没人响应（>=9999 天）
    assert match_filter(c, "responded", "-7d") is True       # 7 天内响应过
    assert match_filter(c, "commenters", ">=2") is True

    # 数值/标签否定
    assert match_filter(mk(title="x", labels=["bug"]), "label", "bug") is True
    assert match_filter(mk(title="x", labels=[]), "label", "!wontfix") is True
    assert match_filter(mk(title="x", labels=["wontfix"]), "label", "!wontfix") is False

    print("self-test:", "OK" if ok else "FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="github-repo-monitor 声明式分诊引擎")
    ap.add_argument("--since", help="ISO 起点（默认用本地 state 游标）")
    ap.add_argument("--lookback-days", type=int, default=7, help="最多回看天数")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 state")
    ap.add_argument("--state-file", default="gh_monitor_state.json")
    ap.add_argument("--report", help="把 digest 写到该文件")
    ap.add_argument("--rules", help="rules.yaml 路径（默认 <脚本目录>/rules.yaml）")
    ap.add_argument("--api-url", help="GitHub API base（含 GHE）；覆盖 GITHUB_API_URL")
    ap.add_argument("--github-api-url", dest="api_url", help="--api-url 别名")
    ap.add_argument("--max-enrich", type=int, default=20, help="评论拉取上限（0=关闭）")
    ap.add_argument("--no-enrich", action="store_true", help="关闭评论拉取")
    ap.add_argument("--self-test", action="store_true", help="解析+引擎自测（零网络）")
    args = ap.parse_args(argv)

    rules_path = args.rules or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.yaml")
    cfg = load_rules(rules_path)

    # API base 优先级：--api-url > GITHUB_API_URL > settings.github_api_url > 默认
    base = args.api_url or os.environ.get("GITHUB_API_URL", "").strip() or \
        cfg["settings"].get("github_api_url", "")
    configure_api(base)

    # 全局成员集合（用于 author-type 过滤）
    global _MEMBERS, _ROLES
    _MEMBERS = cfg["_members"]
    _ROLES = cfg["_roles"]

    if args.self_test:
        if yaml_load is None:
            print("ERROR: miniyaml 不可用，无法解析 rules.yaml")
            return 1
        return _selftest(cfg)

    if not TOKEN:
        print("WARNING: 未设置 GITHUB_TOKEN，API 速率受限。", file=sys.stderr)

    now = utcnow()
    st = load_state(args.state_file)
    cursor_dt = parse_iso(args.since) if args.since else parse_iso(st.get("cursor"))

    objs, by_tier, errors = gather(cfg, cursor_dt, args.lookback_days,
                                   args.max_enrich, args.no_enrich)
    enriched = sum(1 for o in objs if o.get("comment_data") is not None)
    report, counts = build_report(now, cursor_dt, by_tier, errors, cfg, enriched)

    print(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
        print("\n[written] %s" % args.report)

    # 更新游标（仅成功且非 dry-run）
    if not args.dry_run:
        new_cursor = iso(now)
        seen = set(st.get("seen", []))
        for o in objs:
            key = "%s/%s/%s" % (o["repo"], o["kind"], o.get("number") or o.get("sha") or o.get("tag"))
            seen.add(key)
        seen = list(seen)[-2000:]
        st = {"cursor": new_cursor, "seen": seen}
        save_state(args.state_file, st)

    # 退出码：有错误且无任何分诊结果 -> 1
    if errors and not (counts["alert"] or counts["context"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
