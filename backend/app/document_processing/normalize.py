"""Text normalisation, hashing, language detection and near-duplicate fingerprints."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_MULTI_SPACE = re.compile(r"[ \t\u00a0]+")
_MULTI_NL = re.compile(r"\n{3,}")
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _MULTI_NL.sub("\n\n", text).strip()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def count_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (word count). Providers may use real tokenizers."""
    return len(_WORD.findall(text))


_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "is", "that", "for", "with", "as", "this", "are", "was", "on"},
    "es": {"el", "la", "de", "que", "y", "en", "los", "se", "del", "las", "por", "un", "para", "con"},
    "fr": {"le", "la", "les", "de", "des", "et", "est", "en", "que", "une", "pour", "dans", "qui", "pas"},
    "de": {"der", "die", "und", "das", "ist", "nicht", "mit", "den", "ein", "zu", "von", "auf", "für"},
    "pt": {"o", "a", "de", "que", "e", "do", "da", "em", "um", "para", "com", "não", "uma", "os"},
    "it": {"il", "di", "che", "e", "la", "per", "un", "in", "con", "non", "una", "le", "del", "sono"},
}


def detect_language(text: str) -> str:
    """Script check for CJK/Cyrillic/Arabic/Devanagari/Bengali, otherwise stop-word voting.
    Returns 'und' when evidence is weak."""
    sample = text[:5000]
    if not sample.strip():
        return "und"
    scripts = {
        "ja": re.compile(r"[\u3040-\u30ff]"), "ko": re.compile(r"[\uac00-\ud7af]"),
        "zh": re.compile(r"[\u4e00-\u9fff]"), "ru": re.compile(r"[\u0400-\u04ff]"),
        "ar": re.compile(r"[\u0600-\u06ff]"), "hi": re.compile(r"[\u0900-\u097f]"),
        "bn": re.compile(r"[\u0980-\u09ff]"),
    }
    for lang, rx in scripts.items():
        if len(rx.findall(sample)) > max(5, len(sample) * 0.1):
            return lang
    words = [w.lower() for w in _WORD.findall(sample)]
    if len(words) < 5:
        return "und"
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    best, score = max(scores.items(), key=lambda kv: kv[1])
    return best if score / len(words) >= 0.05 else "und"


def fingerprint(text: str, k: int = 128, shingle: int = 5) -> list[int]:
    """Bottom-k MinHash-style sketch over word shingles, for near-duplicate detection."""
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < shingle:
        grams = [" ".join(words)] if words else []
    else:
        grams = [" ".join(words[i : i + shingle]) for i in range(len(words) - shingle + 1)]
    hashes = {int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big") for g in grams}
    return sorted(hashes)[:k]


def sketch_similarity(a: list[int], b: list[int]) -> float:
    """Jaccard estimate from two bottom-k sketches."""
    if not a or not b:
        return 0.0
    k = min(len(a), len(b))
    merged = sorted(set(a) | set(b))[:k]
    sa, sb = set(a), set(b)
    return sum(1 for h in merged if h in sa and h in sb) / len(merged)
