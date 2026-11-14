"""Secrets in text the agent is about to see: the same shared pattern list the screenshot redactor uses
(`secret_patterns.json`, regexes valid in both Python and JavaScript). Here the text is exact (a page's own text,
a window's own text), so no OCR tolerance is needed: a match is replaced, and its element is blacked out on the
screenshot. Nothing here ever returns what it hid."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field

PATTERNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret_patterns.json")
HIDDEN = "[secret hidden]"


@dataclass
class Pattern:
    id: str
    rx: re.Pattern
    kind: str = "token"
    checks: dict = field(default_factory=dict)
    end_rx: re.Pattern | None = None


_cache: list[Pattern] | None = None
_raw: list[dict] | None = None


def raw_patterns() -> list[dict]:
    """The list as written, for the browser extension (it compiles them as JavaScript RegExps)."""
    global _raw
    if _raw is None:
        with open(PATTERNS_FILE, encoding="utf-8") as fh:
            _raw = json.load(fh)["patterns"]
    return _raw


def patterns() -> list[Pattern]:
    global _cache
    if _cache is None:
        out = []
        for p in raw_patterns():
            flags = re.IGNORECASE if "i" in p.get("flags", "") else 0
            end = re.compile(p["end_regex"], flags) if p.get("end_regex") else None
            out.append(Pattern(p["id"], re.compile(p["regex"], flags), p.get("kind", "token"), p.get("checks", {}), end))
        _cache = out
    return _cache


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s))) if n else 0.0


def _classes(s: str) -> int:
    return sum((any(c.islower() for c in s), any(c.isupper() for c in s), any(c.isdigit() for c in s),
                any(not c.isalnum() for c in s)))


def passes(m: re.Match, checks: dict) -> bool:
    if not checks:
        return True
    v = m.group(checks.get("group", 0)) or ""
    if len(v) < checks.get("min_len", 0):
        return False
    if checks.get("reject") and re.search(checks["reject"], v):
        return False
    if checks.get("require_digit") and not any(c.isdigit() for c in v):
        return False
    if "max_hex_share" in checks:
        an = [c for c in v if c.isalnum()]
        if an and sum(c in "0123456789abcdefABCDEF" for c in an) / len(an) >= checks["max_hex_share"]:
            return False
    if (sum(c.isupper() for c in v) < checks.get("min_upper", 0)
            or sum(c.islower() for c in v) < checks.get("min_lower", 0)
            or sum(c.isdigit() for c in v) < checks.get("min_digits", 0)):
        return False
    if checks.get("words_need_digit") and any(len(w) > 2 and not any(c.isdigit() for c in w) for w in v.split()):
        return False
    if _entropy(v) < checks.get("min_entropy", 0):
        return False
    need = checks.get("min_classes", 0)
    if need and _classes(v) < need and len(v) < checks.get("or_min_len", 10 ** 9):
        return False
    return True


def spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) of every secret in the text, merged; a 'block' (a private key) runs to its END line, or to
    the end of the text."""
    found: list[tuple[int, int, str]] = []
    if not text:
        return found
    for p in patterns():
        for m in p.rx.finditer(text):
            if not passes(m, p.checks):
                continue
            s, e = m.start(), m.end()
            if p.kind == "block":
                end = p.end_rx.search(text, e) if p.end_rx else None
                e = end.end() if end else len(text)
            found.append((s, e, "random" if p.kind == "random" else "secret"))
    found.sort()
    merged: list[list] = []
    for s, e, k in found:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            if k == "secret":
                merged[-1][2] = "secret"
        else:
            merged.append([s, e, k])
    return [tuple(x) for x in merged]


def hide(text: str) -> tuple[str, int]:
    """The text with every secret replaced, and how many were hidden."""
    sp = spans(text)
    if not sp:
        return text, 0
    out, last = [], 0
    for s, e, _ in sp:
        out.append(text[last:s])
        out.append(HIDDEN)
        last = e
    out.append(text[last:])
    return "".join(out), len(sp)
