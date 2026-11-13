# -*- coding: utf-8 -*-
"""miniyaml.py — 极简 YAML 子集解析器（纯 stdlib，零依赖）

只支持本规则引擎用到的子集：
  - 映射          key: value   /   key:（值在下一级缩进）
  - 序列          - item   /   - key: value（序列项是 map，可续行）
  - 标量          带引号字符串、无引号字符串、int / float / bool / null
  - 行内列表      [a, b, c]
  - 注释          # 整行，或（不含引号时）行尾 ' #...'

明确不支持（如需请改用 JSON 或扩展本解析器）：
  tab 缩进、多行字符串 |、锚点/别名 *&、复杂流映射、含 ':' 的无引号键。
  配置请统一用空格缩进。

API:
  from miniyaml import load
  data = load(open("rules.yaml", encoding="utf-8").read())

自测：
  python miniyaml.py            # 解析内嵌样例并打印结构
"""
import re
import sys


def load(text):
    lines = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ln = _strip_inline_comment(ln)
        if ln.strip() == "":
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        lines.append((indent, ln.strip()))
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value if value is not None else {}


def _strip_inline_comment(line):
    out = []
    in_s = in_d = False
    for i, c in enumerate(line):
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == "#" and not in_s and not in_d:
            if i == 0 or line[i - 1] == " ":
                break
        out.append(c)
    return "".join(out).rstrip()


def _is_key_value(s):
    return re.match(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*:(\s|$)", s) is not None


def _parse_block(lines, idx, indent):
    if idx >= len(lines):
        return None, idx
    _, content = lines[idx]
    if content == "-" or content.startswith("- "):
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(lines, idx, indent):
    result = {}
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent != indent or content.startswith("- "):
            break
        key, sep, val = content.partition(":")
        if not sep:
            idx += 1
            continue
        key = key.strip()
        val = val.strip()
        if val == "":
            nxt = idx + 1
            if nxt < len(lines) and lines[nxt][0] > indent:
                child, idx = _parse_block(lines, nxt, lines[nxt][0])
                result[key] = child if child is not None else {}
            else:
                result[key] = None
                idx = nxt
        else:
            result[key] = _parse_scalar(val)
            idx += 1
    return result, idx


def _parse_seq(lines, idx, indent):
    result = []
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent != indent or not (content == "-" or content.startswith("- ")):
            break
        item = content[1:].strip()
        if item == "":
            nxt = idx + 1
            if nxt < len(lines) and lines[nxt][0] > indent:
                child, idx = _parse_block(lines, nxt, lines[nxt][0])
                result.append(child if child is not None else {})
            else:
                result.append(None)
                idx = nxt
        elif _is_key_value(item):
            m, idx = _parse_seq_map_item(lines, idx, indent, item)
            result.append(m)
        else:
            result.append(_parse_scalar(item))
            idx += 1
    return result, idx


def _parse_seq_map_item(lines, idx, indent, first):
    m = {}
    key, _, val = first.partition(":")
    key = key.strip()
    val = val.strip()
    if val == "":
        nxt = idx + 1
        if nxt < len(lines) and lines[nxt][0] > indent:
            child, idx = _parse_block(lines, nxt, lines[nxt][0])
            m[key] = child if child is not None else {}
        else:
            m[key] = None
            idx = nxt
    else:
        m[key] = _parse_scalar(val)
        idx = idx + 1
    # 续行：更深的 key: value，且不是序列项
    while idx < len(lines):
        ci, cc = lines[idx]
        if ci <= indent or cc.startswith("- "):
            break
        k2, sep, v2 = cc.partition(":")
        if not sep:
            break
        k2 = k2.strip()
        v2 = v2.strip()
        if v2 == "":
            nxt = idx + 1
            if nxt < len(lines) and lines[nxt][0] > ci:
                child, idx = _parse_block(lines, nxt, lines[nxt][0])
                m[k2] = child if child is not None else {}
            else:
                m[k2] = None
                idx = nxt
        else:
            m[k2] = _parse_scalar(v2)
            idx += 1
    return m, idx


def _parse_scalar(s):
    s = s.strip()
    if s == "":
        return None
    if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return _parse_inline_list(s[1:-1])
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_top(s):
    parts, buf, depth, in_s, in_d = [], [], 0, False, False
    for c in s:
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(c)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def _parse_inline_list(body):
    body = body.strip()
    if not body:
        return []
    return [_parse_scalar(p) for p in _split_top(body)]


_SAMPLE = """
settings:
  min_similarity: 0.75
  members: [alice, bob]
  default_tier: alert
repos:
  - repo: google/leveldb
    note: LevelDB upstream
    watch_issues: true
    paths: []
    labels: []
rules:
  security-alert:
    name: 安全相关
    tier: alert
    filters:
      - label: "!wontfix"
      - title: "CVE|security|RCE"
      - reactions: ">=5"
  perf-context:
    name: 性能相关
    tier: context
    filters:
      - title: "benchmark|compaction|performance"
collections:
  - id: triage
    name: 待分诊
    rules: [security-alert, perf-context]
"""


if __name__ == "__main__":
    d = load(_SAMPLE)
    import json
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d["settings"]["min_similarity"] == 0.75
    assert d["repos"][0]["repo"] == "google/leveldb"
    assert d["rules"]["security-alert"]["filters"][0]["label"] == "!wontfix"
    assert d["collections"][0]["rules"] == ["security-alert", "perf-context"]
    print("miniyaml self-test OK")
