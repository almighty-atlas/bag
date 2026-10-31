import time

import pytest
from bag.excerpt import ELLIPSIS, EXCERPT_CHARS, SOURCE_CHARS, make_excerpt


def test_short_text_is_returned_unchanged() -> None:
    assert make_excerpt("Ein kurzer Gedanke.") == "Ein kurzer Gedanke."


@pytest.mark.parametrize("source", [None, "", "   ", "\n\t\n", "​‮", "```\n---\n```"])
def test_nothing_readable_gives_no_excerpt(source: str | None) -> None:
    assert make_excerpt(source) is None


def test_lines_and_whitespace_collapse_to_one_line() -> None:
    assert make_excerpt("  erste\tZeile \r\n\r\n\n zweite   Zeile mit Umbruch ") == (
        "erste Zeile zweite Zeile mit Umbruch"
    )


def test_long_text_is_bounded_and_marked() -> None:
    result = make_excerpt("Wort " * 5000)
    assert result is not None
    assert len(result) <= EXCERPT_CHARS and result.endswith(f"Wort{ELLIPSIS}")


def test_cut_prefers_a_word_boundary_and_hard_cuts_unbroken_text() -> None:
    words = make_excerpt("abcdefghi " * 100)
    assert words is not None and words.endswith(f"abcdefghi{ELLIPSIS}")
    unbroken = make_excerpt("x" * 1000)
    assert unbroken == "x" * (EXCERPT_CHARS - 1) + ELLIPSIS


def test_text_that_fits_gets_no_ellipsis_but_a_truncated_source_does() -> None:
    assert make_excerpt("a" * EXCERPT_CHARS) == "a" * EXCERPT_CHARS
    assert make_excerpt("a" * SOURCE_CHARS) is not None
    assert make_excerpt("kurz " + " " * SOURCE_CHARS)[-1:] == ELLIPSIS  # type: ignore[index]


def test_huge_input_is_cheap() -> None:
    started = time.perf_counter()
    for source in (
        "**" * 2048,
        "[" * 4096,
        "`" * 4096,
        "_a" * 2048,
        ("* " + "*a " * 50 + "\n") * 40,
    ):
        make_excerpt(source)
    assert time.perf_counter() - started < 1.0


def test_unicode_is_cut_on_whole_characters() -> None:
    family = "👨‍👩‍👧‍👦"
    for prefix in range(EXCERPT_CHARS - 12, EXCERPT_CHARS + 2):
        result = make_excerpt("a" * prefix + family * 4)
        assert result is not None and len(result) <= EXCERPT_CHARS
        body = result.removesuffix(ELLIPSIS)
        # Never a dangling joiner or a partial family: whole sequences only.
        assert not body.endswith("‍")
        assert body.replace(family, "").strip("a") == ""

    combining = "é" * 400
    result = make_excerpt(combining)
    assert result is not None and result.removesuffix(ELLIPSIS).replace("é", "") == ""

    flags = "🇩🇪" * 200
    result = make_excerpt(flags)
    assert result is not None and result.removesuffix(ELLIPSIS).replace("🇩🇪", "") == ""
    odd = make_excerpt("a" * (EXCERPT_CHARS - 3) + "🇩🇪🇫🇷")
    assert odd is not None and odd.removesuffix(ELLIPSIS).replace("🇩🇪", "").strip("a") == ""

    assert make_excerpt("Grüße aus Köln – schöne Umlaute äöüß 日本語のテキスト") == (
        "Grüße aus Köln – schöne Umlaute äöüß 日本語のテキスト"
    )


def test_bidi_and_control_characters_are_removed() -> None:
    assert make_excerpt("abc‮def⁦g\x07h\x00i﻿") == "abcdefghi"
    # Joiners survive inside text.
    assert make_excerpt("👩‍💻") == "👩‍💻"


def test_markdown_markers_are_dropped_but_words_stay() -> None:
    source = (
        "# Titel der Notiz\n\n"
        "> Zitat **fett** und *kursiv* mit `code`\n"
        "- [ ] Aufgabe ~~alt~~\n"
        "* [Link-Text](https://example.org/a_b) und ![Bild](x.png)\n"
        "---\n"
        "```python\nprint(1)\n```\n"
    )
    assert make_excerpt(source) == (
        "Titel der Notiz Zitat fett und kursiv mit code Aufgabe alt Link-Text und Bild print(1)"
    )


def test_markdown_lookalikes_in_plain_text_are_kept() -> None:
    for text in (
        "snake_case_name und __init__py",
        "2 * 3 * 4 = 24",
        "#hashtag bleibt",
        "a*b*c",
        "1. erster Punkt",
        "Preis: 5 - 3 = 2",
        "<b>kein HTML-Rendering</b> & <script>alert(1)</script>",
    ):
        assert make_excerpt(text) == text


def test_front_content_is_not_lost_behind_blank_lines_or_rules() -> None:
    assert make_excerpt("\n\n\n===\n\nwichtig\n") == "wichtig"
