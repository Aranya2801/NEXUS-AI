"""PII and secret detection / redaction. Pattern-based with checksum validation (Luhn for cards, mod-97 for IBANs) to keep false positives down.

Covers: email, phone (international-ish), US SSN, payment card, IBAN, IPv4, API keys/tokens (AWS, GitHub, Slack, Google, `sk-` style), private-key
blocks and JWTs. It does NOT detect person names, postal addresses, dates of birth, medical or non-US national identifiers – that needs an NER model
(a pluggable hook is intentionally not shipped untested). Recall is therefore partial; treat this as one layer, not as compliance.
Detected values are never logged or returned: callers get kinds, counts and offsets."""
from __future__ import annotations

import re
from dataclasses import dataclass

SECRET_KINDS = frozenset({"api_key", "private_key", "jwt"})
ALL_KINDS = ("email", "phone", "ssn", "credit_card", "iban", "ipv4", "api_key", "private_key", "jwt")
DEFAULT_KINDS = tuple(k for k in ALL_KINDS if k != "ipv4")      # IPv4 is noisy (version numbers); opt in explicitly


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _card_ok(raw: str) -> bool:
    d = re.sub(r"[ -]", "", raw)
    return d.isdigit() and 13 <= len(d) <= 19 and _luhn(d) and len(set(d)) > 1


def _phone_ok(raw: str) -> bool:
    """Deliberately conservative (phone-shaped digit runs are everywhere: ids, IBAN fragments, invalid SSNs). Accepts E.164-style `+…`, a
    parenthesised area code, or the common 3-3-4 NANP layout. Bare national formats of other countries (e.g. `020 7946 0958`) are NOT detected."""
    d = re.sub(r"\D", "", raw)
    raw = raw.strip()
    if raw.startswith("+"):
        return 9 <= len(d) <= 15
    if "(" in raw and ")" in raw:
        return 10 <= len(d) <= 11
    return len(d) in (10, 11) and bool(re.fullmatch(r"(?:\+?1[\s.-]?)?\d{3}[\s.-]\d{3}[\s.-]\d{4}", raw))


def _ipv4_ok(raw: str) -> bool:
    parts = raw.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 and (p == "0" or not p.startswith("0")) for p in parts)


# (kind, regex, validator) in PRIORITY order: earlier kinds win overlaps (a card number is not also a phone number)
_RULES: list[tuple[str, re.Pattern, object]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----)?", re.S), None),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), None),
    ("api_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bAIza[0-9A-Za-z_-]{35}\b|\bsk-[A-Za-z0-9_-]{20,}\b"), None),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), None),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"), _iban_ok),
    ("credit_card", re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"), _card_ok),
    ("ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    ("phone", re.compile(r"(?<![\w.-])\+?\(?\d{1,4}\)?(?:[\s.-]?\(?\d{2,4}\)?){2,4}(?![\w-])"), _phone_ok),
    ("ipv4", re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])"), _ipv4_ok),
]


def detect(text: str, kinds: tuple[str, ...] | list[str] | None = None) -> list[Finding]:
    wanted = set(kinds if kinds is not None else DEFAULT_KINDS)
    taken: list[Finding] = []
    for kind, rx, ok in _RULES:
        if kind not in wanted:
            continue
        for m in rx.finditer(text):
            if ok is not None and not ok(m.group(0)):
                continue
            if any(m.start() < f.end and f.start < m.end() for f in taken):
                continue
            taken.append(Finding(kind, m.start(), m.end()))
    return sorted(taken, key=lambda f: f.start)


def counts(findings: list[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.kind] = out.get(f.kind, 0) + 1
    return out


def redact(text: str, kinds: tuple[str, ...] | list[str] | None = None) -> tuple[str, dict[str, int]]:
    """Replaces each finding with `[KIND_n]`; the same value always gets the same placeholder within one call, so references stay coherent."""
    found = detect(text, kinds)
    if not found:
        return text, {}
    seen: dict[tuple[str, str], str] = {}
    per_kind: dict[str, int] = {}
    out, pos = [], 0
    for f in found:
        raw = text[f.start:f.end]
        key = (f.kind, re.sub(r"[\s-]", "", raw).lower())
        if key not in seen:
            per_kind[f.kind] = per_kind.get(f.kind, 0) + 1
            seen[key] = f"[{f.kind.upper()}_{per_kind[f.kind]}]"
        out += [text[pos:f.start], seen[key]]
        pos = f.end
    out.append(text[pos:])
    return "".join(out), counts(found)
