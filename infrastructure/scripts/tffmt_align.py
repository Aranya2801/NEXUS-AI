"""Approximation of `terraform fmt`'s '=' alignment rule, used because the terraform binary is not always available (and as a fast pre-check in CI).
Rule implemented: consecutive single-line `key = value` attributes at the same nesting level are aligned on the longest key; a blank line, comment, block opener or a
multi-line value ends the group. This is NOT a full formatter: `terraform fmt -check` in CI remains the authority."""
from __future__ import annotations

import re

ATTR = re.compile(r"^(\s*)([A-Za-z_][\w.\-\"\[\]\*]*|\"[^\"]+\")\s*=\s*(.*?)\s*$")


def _balanced(v: str) -> bool:
    depth, in_str, esc = 0, False, False
    i = 0
    while i < len(v):
        c = v[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif c == "#":
            break
        i += 1
    return depth == 0 and not in_str


def align(text: str) -> str:
    lines = text.split("\n")
    out = list(lines)
    i = 0
    heredoc = None
    while i < len(lines):
        ln = lines[i]
        if heredoc:
            if ln.strip() == heredoc:
                heredoc = None
            i += 1
            continue
        m = re.search(r"<<-?\s*([A-Z_]+)\s*$", ln)
        if m:
            heredoc = m.group(1)
            i += 1
            continue
        group = []
        j = i
        while j < len(lines):
            a = ATTR.match(lines[j])
            if not a or not _balanced(a.group(3)) or a.group(3) == "" or re.search(r"<<-?\s*[A-Z_]+\s*$", lines[j]):
                break
            if group and a.group(1) != group[0][1]:
                break
            group.append((j, a.group(1), a.group(2), a.group(3)))
            j += 1
        if group:
            width = max(len(g[2]) for g in group)
            for (k, ind, key, val) in group:
                out[k] = f"{ind}{key.ljust(width)} = {val}"
            i = j
        else:
            i += 1
    return "\n".join(out)


def misaligned(text: str) -> list[int]:
    a, b = text.split("\n"), align(text).split("\n")
    return [n + 1 for n, (x, y) in enumerate(zip(a, b)) if x != y]
