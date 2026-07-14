"""Complete-sentence summary enforcement (LEGACY §1.6, DD-CV-06) — verbatim port.

Never cut a summary mid-sentence or append "...". Deterministic complete-
sentence retention + a pre-publication quality predicate. No LLM, no network,
no invented text — retained text is always a truthful verbatim subset.
"""

from __future__ import annotations

import re

_TERMINATORS = ".?!"
_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "u.s.", "u.k.", "u.a.e.",
    "mr.", "mrs.", "ms.", "dr.", "prof.", "ph.d.", "no.",
    "inc.", "ltd.", "co.", "jr.", "sr.", "st.",
}
_DANGLING_CONNECTORS = {
    "and", "or", "including", "with", "for", "to", "of", "in", "on", "by",
    "through", "while", "which", "that", "as", "such", "the", "a", "an",
}


def split_complete_sentences(text: str) -> list[str]:
    """Split into COMPLETE sentences. A boundary is '. ? !' followed by
    whitespace/EOT, excluding decimals (3.5) and known abbreviations. Any
    trailing fragment without a terminator is dropped."""
    sentences: list[str] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _TERMINATORS and (i + 1 >= n or text[i + 1].isspace()):
            is_decimal = (ch == "." and i > 0 and text[i - 1].isdigit()
                          and i + 1 < n and text[i + 1].isdigit())
            seg = text[start:i + 1]
            toks = seg.split()
            last_tok = toks[-1].lower() if toks else ""
            is_abbrev = last_tok in _ABBREVIATIONS
            if not is_decimal and not is_abbrev:
                sentence = text[start:i + 1].strip()
                if sentence:
                    sentences.append(sentence)
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                start = j
                i = j
                continue
        i += 1
    return sentences


def enforce_complete_summary(summary: object, max_chars: int = 400) -> str:
    if not isinstance(summary, str):
        return ""
    s = summary.strip()
    if len(s) <= max_chars:
        return s
    sentences = split_complete_sentences(s)
    prefix: list[str] = []
    for sent in sentences:
        candidate = " ".join([*prefix, sent]).strip()
        if len(candidate) <= max_chars:
            prefix.append(sent)
        else:
            break
    if prefix:
        return " ".join(prefix).strip()
    fitting = [x for x in sentences if len(x) <= max_chars]
    if fitting:
        return min(fitting, key=len)
    return s  # over budget → quality gate blocks it


def validate_summary_quality(
    summary: object, *, max_chars: int = 400
) -> tuple[bool, str | None]:
    """Returns (True, None) or (False, reason_code). Never exposes summary text."""
    if summary is None:
        return (False, "summary_missing")
    if not isinstance(summary, str):
        return (False, "summary_not_string")
    s = summary.strip()
    if not s:
        return (False, "summary_missing")
    if len(s) > max_chars:
        return (False, "summary_too_long")
    if s.endswith("…") or s.endswith("..."):
        return (False, "summary_trailing_ellipsis")
    core = s.rstrip("\"'”’)]").rstrip()
    if not core:
        return (False, "summary_incomplete_terminal")
    if core[-1] not in _TERMINATORS:
        return (False, "summary_incomplete_terminal")
    body = core.rstrip(_TERMINATORS).rstrip("\"'”’)]").rstrip()
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", body)
    if words and words[-1].lower() in _DANGLING_CONNECTORS:
        return (False, "summary_dangling_connector")
    return (True, None)
