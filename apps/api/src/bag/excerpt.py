"""Bounded plain-text previews of text originals for item summaries.

The database hands over only a short prefix of ``item.content`` (``SOURCE_CHARS``), so a
huge original is never loaded into the API process or sent to the client. The result is
plain text and is never interpreted as markup: Markdown syntax is dropped, not rendered.
"""

import re
import unicodedata

EXCERPT_CHARS = 240
SOURCE_CHARS = 4096
ELLIPSIS = "…"

# Bidirectional and invisible formatting characters can reorder or hide text in a card.
# Joiners and variation selectors stay: emoji sequences and many scripts need them.
_INVISIBLE = {
    0x061C,
    0x200B,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    0x2060,
    *range(0x2066, 0x206A),
    0xFEFF,
}
_DROP = {
    code: None
    for code in (*range(0x20), *range(0x7F, 0xA0), *_INVISIBLE)
    if not chr(code).isspace()
}

_FENCE = re.compile(r"^(?:`{3,}|~{3,})")
_RULE = re.compile(r"^[-*_=](?:\s*[-*_=]){2,}$")
_BLOCK_PREFIX = (
    re.compile(r"^(?:>\s?)+"),
    re.compile(r"^[-*+]\s+(?:\[[ xX]\]\s+)?"),
    re.compile(r"^#{1,6}\s+"),
)
# Quantifiers are bounded so pathological lines cannot cause quadratic scanning.
_INLINE = (
    (re.compile(r"!\[([^\]]{0,200})\]\([^)]{0,500}\)"), r"\1"),
    (re.compile(r"\[([^\]]{1,200})\]\([^)]{0,500}\)"), r"\1"),
    (re.compile(r"`([^`]{1,200})`"), r"\1"),
    (re.compile(r"\*\*(?=\S)(.{1,200}?)(?<=\S)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)__(?=\S)(.{1,200}?)(?<=\S)__(?!\w)"), r"\1"),
    (re.compile(r"~~(?=\S)(.{1,200}?)(?<=\S)~~"), r"\1"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.{1,200}?)(?<=[^\s*])\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<![\w_])_(?=[^\s_])(.{1,200}?)(?<=[^\s_])_(?![\w_])"), r"\1"),
)
_LINE_CAP = 600

_ZWJ = "\u200d"
_REGIONAL = range(0x1F1E6, 0x1F200)
_EMOJI_MODIFIERS = range(0x1F3FB, 0x1F400)
_TAGS = range(0xE0020, 0xE0080)


def _clean_line(line: str) -> str:
    line = line[:_LINE_CAP].translate(_DROP).strip()
    if not line or _FENCE.match(line) or _RULE.match(line):
        return ""
    for pattern in _BLOCK_PREFIX:
        line = pattern.sub("", line, count=1)
    for pattern, replacement in _INLINE:
        line = pattern.sub(replacement, line)
    return line


def _continues_cluster(before: str, after: str) -> bool:
    """True when cutting between the two characters would split a grapheme cluster."""
    code = ord(after)
    return (
        before == _ZWJ
        or after == _ZWJ
        or unicodedata.category(after).startswith("M")
        or 0xFE00 <= code <= 0xFE0F
        or code in _EMOJI_MODIFIERS
        or code in _TAGS
    )


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` characters including the ellipsis, ending on a word or cluster."""
    end = limit - 1
    if len(text) > end and text[end] != " ":
        space = text.rfind(" ", end // 2, end)
        if space > 0:
            end = space
    while 0 < end < len(text):
        before, after = text[end - 1], text[end]
        if _continues_cluster(before, after):
            end -= 1
        elif ord(before) in _REGIONAL and ord(after) in _REGIONAL:
            flags = 0
            while flags < end and ord(text[end - 1 - flags]) in _REGIONAL:
                flags += 1
            if flags % 2 == 0:
                break
            end -= 1
        else:
            break
    return text[:end].rstrip() + ELLIPSIS


def make_excerpt(source: str | None) -> str | None:
    """A single-line preview of a text original, or None when nothing readable is left.

    ``source`` is the database prefix of the content. Whitespace and line breaks collapse
    to single spaces, common Markdown markers are removed, and the result is cut on a word
    or grapheme boundary and marked with an ellipsis when the original continues.
    """
    if not source:
        return None
    more = len(source) >= SOURCE_CHARS
    parts: list[str] = []
    size = 0
    for line in source.splitlines():
        cleaned = " ".join(_clean_line(line).split())
        if not cleaned:
            continue
        parts.append(cleaned)
        size += len(cleaned) + 1
        if size > EXCERPT_CHARS * 2:
            more = True
            break
    text = " ".join(parts)
    if not text:
        return None
    if len(text) > EXCERPT_CHARS:
        return _cut(text, EXCERPT_CHARS)
    return text + ELLIPSIS if more else text
