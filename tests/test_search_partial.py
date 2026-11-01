"""Prefix and substring search: ADR-0025, migration 0006_search_partial."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from alembic import command
from bag.cli import migration_config
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import build_pdf, drain
from test_search_fields import add_user, fields, found, patch, search, text, upload

pytestmark = pytest.mark.integration


def ranks(client: TestClient, headers: dict[str, str], q: str) -> list[float]:
    return [row["rank"] for row in search(client, headers, q)["results"]]


def page_through(client: TestClient, headers: dict[str, str], q: str, limit: int) -> list[str]:
    ids: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = search(client, headers, q, limit=limit, offset=offset)
        assert len(page["results"]) <= limit
        ids += [row["id"] for row in page["results"]]
        offset = page["next_offset"]
    return ids


def head_is_current(settings: Settings) -> bool:
    """search_head equals what its function computes from the row and its tags, for every row."""
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT count(*) AS stale FROM item WHERE search_head IS DISTINCT FROM "
            "bag_head_vector(title, original_filename, bag_item_tags_vector(owner_id, id), "
            "user_note, source_url, language)"
        ).fetchone()
    assert row is not None
    return bool(row["stale"] == 0)


def test_prefix_finds_tags_titles_notes_and_filenames(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    tagged = text(client, headers, "Reisebericht")
    patch(client, headers, tagged, tags=["Urlaubstest"])
    noted = text(client, headers, "Dritter Eintrag", user_note="Urlaubsplanung 2027")
    fototag = text(client, headers, "Erster Eintrag")
    patch(client, headers, fototag, tags=["Fototest"])
    fototitle = text(client, headers, "Zweiter Eintrag")
    patch(client, headers, fototitle, title="Fotosammlung")
    fotofile = upload(client, headers, "Fotoalbum_2026.pdf")

    # The word the user remembers is only the start of the tag; case does not matter.
    for query in ("urlaub", "Urlaub", "URLAUB", "urlau", "ur"):
        assert found(client, headers, query)[:2] == [tagged, noted], query
    assert found(client, headers, "urlaubstest") == [tagged]
    assert fields(client, headers, "urlaub") == {tagged: ["tag"], noted: ["note"]}
    # A tag ranks above a note; a title or filename above a tag; no other match exists.
    assert found(client, headers, "foto")[2:] == [fototag]
    assert set(found(client, headers, "foto")[:2]) == {fototitle, fotofile}
    assert fields(client, headers, "foto") == {
        fototitle: ["title"],
        fotofile: ["filename"],
        fototag: ["tag"],
    }
    # Every word of the query has to be found, each as a prefix.
    assert found(client, headers, "urlaub 2027") == [noted]
    assert found(client, headers, "urlaub reise") == []
    # A prefix does not match a shorter word, and a prefix of a word does not skip its start.
    assert found(client, headers, "urlaubstestx") == []
    assert found(client, headers, "laub") == []
    assert head_is_current(settings)


def test_stemming_still_applies_and_prefixes_extend_it(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    german = text(client, headers, "Die Rechnungen für die Wohnung liegen im Keller.")
    english = text(client, headers, "The invoices for the apartment are in the basement.")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    patch(client, headers, german, tags=["Bücherregal"])

    assert found(client, headers, "rechnung") == [german]
    assert found(client, headers, "wohnungen") == [german]
    assert found(client, headers, "invoice") == [english]
    assert found(client, headers, "apartments") == [english]
    # Compound words: the start of a tag is enough, with umlaut or transliterated.
    assert found(client, headers, "bücher") == [german]
    assert found(client, headers, "buecher") == [german]
    assert found(client, headers, "BÜCHERREGAL") == [german]
    # Stemmed whole-word hits and a prefix of another field work in one query.
    patch(client, headers, english, title="Mietwohnung")
    assert found(client, headers, "invoices miet") == [english]


def test_unicode_in_prefixes_and_names(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    umlaut = text(client, headers, "Eintrag 1")
    patch(client, headers, umlaut, tags=["Überraschung"], title="Größenübersicht")
    accent = text(client, headers, "Eintrag 2")
    patch(client, headers, accent, title="Café Crème")
    cyrillic = text(client, headers, "Eintrag 3")
    patch(client, headers, cyrillic, title="Привет мир")
    cjk = text(client, headers, "Eintrag 4")
    patch(client, headers, cjk, title="日本語メモ")
    named = upload(client, headers, "Größe-Übersicht_Müller.pdf")

    # A filename ranks above a tag, which ranks above a note.
    for query in ("über", "ÜBER", "ueber"):
        assert found(client, headers, query) == [named, umlaut], query
    assert set(found(client, headers, "größ")) == {umlaut, named}
    # Filenames and tags are also indexed transliterated; titles are not.
    assert found(client, headers, "groess") == [named]
    assert found(client, headers, "uebers") == [named]
    assert found(client, headers, "ueberr") == [umlaut]
    assert found(client, headers, "caf") == [accent]
    assert found(client, headers, "CRÈ") == [accent]
    assert found(client, headers, "прив") == [cyrillic]
    assert found(client, headers, "ПРИВ") == [cyrillic]
    assert found(client, headers, "日本") == [cjk]
    # Emoji and symbols are harmless.
    assert found(client, headers, "🙂") == []
    assert fields(client, headers, "über") == {named: ["filename"], umlaut: ["tag"]}
    assert fields(client, headers, "größ") == {umlaut: ["title"], named: ["filename"]}


def test_substrings_of_filenames(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    photo = upload(client, headers, "IMG_3109.webp")
    other = upload(client, headers, "IMG_4010.webp")
    long_name = upload(client, headers, "Jahresabrechnungsentwurf-2026.pdf")
    umlaut = upload(client, headers, "Müller_Größe.pdf")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # The reported case: '31' starts the token '3109'.
    assert found(client, headers, "31") == [photo]
    assert fields(client, headers, "31") == {photo: ["filename"]}
    for query in ("3109", "IMG_3109", "img_3109.webp", "IMG 3109", "webp", ".webp", "IMG_31"):
        assert photo in found(client, headers, query), query
    # Three or more characters match anywhere in the name, in any case.
    for query in ("109", "mg_31", "g_310", "9.we", "09.webp"):
        assert found(client, headers, query) == [photo], query
    assert fields(client, headers, "109") == {photo: ["filename"]}
    assert found(client, headers, "abrech") == [long_name]
    assert found(client, headers, "ABRECHNUNG") == [long_name]
    assert found(client, headers, "entwurf") == [long_name]
    assert found(client, headers, "ahresab") == [long_name]
    assert fields(client, headers, "abrech") == {long_name: ["filename"]}
    # Umlauts as written and transliterated; extensions.
    assert found(client, headers, "üller") == [umlaut]
    assert found(client, headers, "ueller") == [umlaut]
    assert found(client, headers, "mülle") == [umlaut]
    assert found(client, headers, "röße") == [umlaut]
    assert found(client, headers, "oesse") == [umlaut]
    assert set(found(client, headers, "pdf")) == {long_name, umlaut}
    # Several words must all occur in one filename.
    assert found(client, headers, "img 109") == [photo]
    assert found(client, headers, "109 4010") == []
    assert found(client, headers, "img 010") == [other]
    # Below three characters only the start of a word matches; '31' does, '09' and '10' do not.
    assert found(client, headers, "09") == []
    assert found(client, headers, "10") == []
    assert found(client, headers, "9") == []
    assert found(client, headers, "1") == []
    assert head_is_current(settings)


def test_like_wildcards_in_queries_are_literal(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    underscore = upload(client, headers, "abc_def.pdf")
    plain = upload(client, headers, "abcxdef.pdf")
    percent = upload(client, headers, "ab%def.pdf")
    # '_' and '%' would match any character if they were passed on unescaped.
    assert found(client, headers, "c_d") == [underscore]
    assert found(client, headers, "b%d") == [percent]
    assert found(client, headers, "cxd") == [plain]
    assert found(client, headers, "x\\y") == []
    assert found(client, headers, "___") == []
    assert found(client, headers, "%%%") == []


def test_whole_word_hits_rank_before_prefix_and_substring_hits(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    substring = upload(client, headers, "Espressokaffee.pdf")
    prefix_file = upload(client, headers, "Kaffeesorten.pdf")
    prefix_tag = text(client, headers, "Einkaufsliste")
    patch(client, headers, prefix_tag, tags=["Kaffeemaschine"])
    body = text(client, headers, "Kaffee " * 30 + "und noch ein Satz")
    exact_title = text(client, headers, "Irgendein Text")
    patch(client, headers, exact_title, title="Kaffee")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    order = found(client, headers, "Kaffee")
    assert order[:2] == [exact_title, body], "whole words first, a title before a body"
    assert set(order[2:4]) == {prefix_file, prefix_tag} and order[2] == prefix_file
    assert order[4:] == [substring]
    assert fields(client, headers, "Kaffee") == {
        exact_title: ["title"],
        body: ["content"],
        prefix_file: ["filename"],
        prefix_tag: ["tag"],
        substring: ["filename"],
    }
    # The rank never increases along the list; whole words rank 1 and above, partial hits below.
    values = ranks(client, headers, "Kaffee")
    assert values == sorted(values, reverse=True)
    assert all(v >= 1 for v in values[:2]) and all(0 <= v < 1 for v in values[2:])
    assert values[2] > values[3] > values[4]
    # An exact filename or title is unaffected by longer names that merely contain it.
    assert found(client, headers, "kaffeesorten.pdf")[0] == prefix_file
    assert found(client, headers, "kaffee")[0] == exact_title


def test_pages_cross_the_whole_word_prefix_and_substring_boundaries(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    exact = [text(client, headers, f"Notiz {i}") for i in range(3)]
    for item in exact:
        patch(client, headers, item, title="Projekt")
    prefix = [text(client, headers, f"Plan {i}") for i in range(7)]
    for index, item in enumerate(prefix):
        patch(client, headers, item, title=f"Projektplan {index}")
    substring = [upload(client, headers, f"Meilensteinprojekt-{i}.pdf") for i in range(2)]

    everything = found(client, headers, "projekt")
    assert len(everything) == 12 and len(set(everything)) == 12
    assert set(everything[:3]) == set(exact)
    assert set(everything[3:10]) == set(prefix)
    assert set(everything[10:]) == set(substring)
    # Newest first inside one class and one score.
    assert everything[:3] == sorted(exact, reverse=True)
    assert everything[3:10] == sorted(prefix, reverse=True)
    for size in (1, 2, 3, 4, 5, 7, 11, 12, 20):
        assert page_through(client, headers, "projekt", size) == everything, size
    # Offsets at and beyond the class boundaries.
    for offset, expected in ((3, everything[3:5]), (10, everything[10:12]), (11, everything[11:])):
        page = search(client, headers, "projekt", limit=2, offset=offset)
        assert [r["id"] for r in page["results"]] == expected, offset
    first = search(client, headers, "projekt", limit=3)
    assert first["next_offset"] == 3 and len(first["results"]) == 3
    exactly = search(client, headers, "projekt", limit=12)
    assert exactly["next_offset"] is None and len(exactly["results"]) == 12
    beyond = search(client, headers, "projekt", limit=5, offset=12)
    assert beyond == {"results": [], "next_offset": None}
    beyond = search(client, headers, "projekt", limit=5, offset=40)
    assert beyond == {"results": [], "next_offset": None}
    # With no whole-word hit at all, an offset still continues in the partial hits.
    assert page_through(client, headers, "projektp", 3) == everything[3:10]
    assert [
        r["id"] for r in search(client, headers, "projektp", limit=3, offset=3)["results"]
    ] == everything[6:9]
    assert head_is_current(settings)


def test_owner_isolation_trash_and_filters_apply_to_partial_hits(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    other = add_user(settings, "other")
    note = text(client, headers, "Reisebericht")
    patch(client, headers, note, tags=["Urlaubstest"])
    pdf = upload(client, headers, "Urlaubsfoto-2026.pdf")
    foreign_note = client.post(
        "/api/v1/capture/text", json={"content": "Fremd"}, headers=other
    ).json()["id"]
    patch(client, other, foreign_note, tags=["Urlaubstest"])
    foreign_file = client.post(
        "/api/v1/capture/file", headers=other, files={"file": ("Urlaubsfoto-fremd.pdf", b"x")}
    ).json()["id"]
    assert (
        client.post("/api/v1/collections", json={"name": "Jahr"}, headers=headers).status_code
        == 201
    )
    patch(client, headers, note, collections=["Jahr"])

    assert set(found(client, headers, "urlaub")) == {note, pdf}
    assert set(found(client, other, "urlaub")) == {foreign_note, foreign_file}
    assert found(client, headers, "fremd") == [] and found(client, other, "109") == []
    assert found(client, other, "ahresab") == [] and found(client, headers, "sfoto") == [pdf]
    assert fields(client, other, "urlaub")[foreign_note] == ["tag"]

    # Filters combine with prefix and substring hits.
    assert found(client, headers, "urlaub", kind="text") == [note]
    assert found(client, headers, "urlaub", kind="document") == [pdf]
    assert found(client, headers, "urlaub", tag="Urlaubstest") == [note]
    assert found(client, headers, "urlaub", collection="Jahr") == [note]
    assert found(client, headers, "urlaub", tag="Urlaubstest", collection="Jahr") == [note]
    assert found(client, headers, "urlaub", tag="nope") == []
    assert found(client, headers, "sfoto", kind="text") == []
    assert set(found(client, headers, "urlaub", status="queued")) == {note, pdf}
    assert found(client, headers, "urlaub", status="failed") == []
    assert found(client, headers, "urlaub", **{"from": "2099-01-01T00:00:00Z"}) == []
    assert set(found(client, headers, "urlaub", **{"to": "2099-01-01T00:00:00Z"})) == {note, pdf}

    # Trash hides them from normal search and keeps them findable in the trash.
    assert client.delete(f"/api/v1/items/{note}", headers=headers).status_code == 204
    assert client.delete(f"/api/v1/items/{pdf}", headers=headers).status_code == 204
    assert found(client, headers, "urlaub") == [] and found(client, headers, "sfoto") == []
    assert set(found(client, headers, "urlaub", trashed="true")) == {note, pdf}
    assert found(client, headers, "sfoto", trashed="true") == [pdf]
    assert found(client, other, "urlaub", trashed="true") == []
    assert set(found(client, other, "urlaub")) == {foreign_note, foreign_file}
    assert client.post(f"/api/v1/items/{note}/restore", headers=headers).status_code == 200
    assert found(client, headers, "urlaub") == [note]
    assert found(client, headers, "urlaub", trashed="true") == [pdf]
    assert client.get("/api/v1/search", params={"q": "urlaub"}).status_code == 401


def test_tag_rename_and_deletion_update_prefix_matches(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    first = text(client, headers, "Eins")
    second = text(client, headers, "Zwei")
    patch(client, headers, first, tags=["Urlaubstest", "Büro"])
    patch(client, headers, second, tags=["Urlaubstest"])
    assert set(found(client, headers, "urlaub")) == {first, second}

    tags = client.get("/api/v1/tags", headers=headers).json()
    tag = next(t for t in tags if t["name"] == "Urlaubstest")
    assert (
        client.patch(
            f"/api/v1/tags/{tag['id']}", json={"name": "Reisetest"}, headers=headers
        ).status_code
        == 200
    )
    assert found(client, headers, "urlaub") == []
    assert set(found(client, headers, "reise")) == {first, second}
    assert set(found(client, headers, "REISET")) == {first, second}
    assert found(client, headers, "büro") == [first]
    assert head_is_current(settings)

    assert client.delete(f"/api/v1/tags/{tag['id']}", headers=headers).status_code == 204
    assert found(client, headers, "reise") == []
    assert found(client, headers, "bür") == [first]
    assert client.get(f"/api/v1/items/{second}", headers=headers).status_code == 200

    # An assignment made outside the API (a system tagger) is indexed by the database itself.
    with connection(settings) as conn:
        owner = conn.execute("SELECT owner_id FROM item WHERE id = %s", (first,)).fetchone()
        assert owner is not None
        tag_id = uuid7()
        conn.execute(
            "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, 'Automatisch')",
            (tag_id, owner["owner_id"]),
        )
        conn.execute(
            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by, confidence) "
            "VALUES (%s, %s, %s, %s, 'ai', 0.4)",
            (uuid7(), owner["owner_id"], first, tag_id),
        )
    assert found(client, headers, "automat") == [first]
    assert head_is_current(settings)
    with connection(settings) as conn:
        conn.execute("DELETE FROM item_tag WHERE tag_id = %s", (tag_id,))
    assert found(client, headers, "automat") == []
    assert head_is_current(settings)
    # Editing the title, note and language keeps the head in step too.
    patch(client, headers, first, title="Wanderkarte", user_note="Wandertour im Herbst")
    assert found(client, headers, "wander") == [first]
    assert fields(client, headers, "wander") == {first: ["title", "note"]}
    patch(client, headers, first, title=None, user_note=None)
    assert found(client, headers, "wander") == []
    assert head_is_current(settings)


def test_exclusions_phrases_and_or_with_prefixes(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    plain = text(client, headers, "Eins")
    patch(client, headers, plain, tags=["Fototest"])
    excluded = text(client, headers, "Zwei")
    patch(client, headers, excluded, tags=["Foto", "Urlaub"])
    kept = text(client, headers, "Drei")
    patch(client, headers, kept, title="Foto Urlaubstest")
    body_excluded = text(client, headers, "Foto mit Urlaub im Text")
    patch(client, headers, body_excluded, tags=["Fotobuch"])
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # An exclusion removes the word, not everything that starts with it, wherever it occurs.
    assert set(found(client, headers, "foto -urlaub")) == {plain, kept}
    assert set(found(client, headers, "fot -urlaub")) == {plain, kept}
    assert set(found(client, headers, "foto")) == {plain, excluded, kept, body_excluded}
    assert set(found(client, headers, "-urlaub")) == {plain, kept}
    # A phrase works with a prefix as its last word, and still never spans two fields.
    assert found(client, headers, '"foto urlaubs"') == [kept]
    assert found(client, headers, '"foto urlaubstest"') == [kept]
    assert found(client, headers, '"urlaubs foto"') == []
    assert found(client, headers, '"foto urlaub"') == [kept], "tags and words never join"
    # OR combines prefix hits.
    zebra = text(client, headers, "Fuenf")
    patch(client, headers, zebra, tags=["Zebrastreifen"])
    quark = text(client, headers, "Sechs")
    patch(client, headers, quark, title="Quarkspeise")
    assert set(found(client, headers, "zebrast OR quarksp")) == {zebra, quark}
    assert set(found(client, headers, "zebrast OR quarksp OR fototes")) == {zebra, quark, plain}
    assert found(client, headers, "zebrast OR nothing") == [zebra]


def test_pdf_text_matches_whole_words_and_combines_with_prefixes(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    pdf = upload(
        client,
        headers,
        "scan-0815.pdf",
        build_pdf("Rechnungsnummer 4711 vom Umzug der Buecher"),
    )
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    assert found(client, headers, "rechnungsnummer") == [pdf]
    assert fields(client, headers, "rechnungsnummer") == {pdf: ["content"]}
    assert found(client, headers, "4711") == [pdf] and found(client, headers, "umzug") == [pdf]
    assert found(client, headers, "umzug buecher") == [pdf]
    assert found(client, headers, '"umzug der buecher"') == [pdf]
    # Extracted text is not prefix-searched: it is large, and prefix hits there are noise.
    assert found(client, headers, "rechnungs") == []
    assert found(client, headers, "471") == []
    assert found(client, headers, "umz") == []
    # A whole word of the text plus the start of a tag or name of the same item.
    assert found(client, headers, "rechnungsnummer telek") == []
    patch(client, headers, pdf, tags=["Telekom"])
    assert found(client, headers, "rechnungsnummer telek") == [pdf]
    assert fields(client, headers, "rechnungsnummer telek") == {pdf: ["tag", "content"]}
    assert found(client, headers, "rechnungsnummer scan") == [pdf]
    assert found(client, headers, "umzug 0815") == [pdf]
    assert found(client, headers, "telek 4711") == [pdf]
    assert found(client, headers, "telek 471") == []
    # The snippet still comes from the text, and highlights a prefix hit in the note.
    note = text(client, headers, "Anderer Eintrag", user_note="Urlaubsplanung 2027")
    snippet = search(client, headers, "urlaub")["results"][0]["snippet"]
    assert "«Urlaubsplanung»" in snippet
    assert found(client, headers, "urlaub") == [note]
    assert "«Umzug»" in search(client, headers, "umzug")["results"][0]["snippet"]


def test_invalid_and_very_short_queries(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    apple = text(client, headers, "Eintrag")
    patch(client, headers, apple, tags=["Apfel"])
    single = text(client, headers, "Noch ein Eintrag")
    patch(client, headers, single, title="a")
    upload(client, headers, "IMG_3109.webp")

    # One character: whole words only. Two characters: also the start of a word.
    assert found(client, headers, "a") == [single]
    assert found(client, headers, "ap") == [apple]
    assert found(client, headers, "A") == [single]
    assert found(client, headers, "i") == [] and found(client, headers, "3") == []
    assert found(client, headers, "mg") == []

    # Syntax characters, operators, wildcards and long input never fail the request.
    for query in (
        "%", "_", "\\", "*", ":", ":*", "!", "&", "|", "(", ")", "<->", "'", '"', '""', "-", "--",
        "or", "OR", '-"', "''", "a:*", "a & b | !c", "'; DROP TABLE item; --", "ap*", "ap:*",
        "日", "🙂", "a b c d e f g h", "1 2 3 4 5 6 7 8 9 abc", "   ", "\t", "ß", "ǅ", "ﬁ",
        "a" * 500, "ab " * 160, "..", ".webp.", "x" * 400 + " 31",
    ):  # fmt: skip
        response = client.get("/api/v1/search", params={"q": query}, headers=headers)
        assert response.status_code == 200, (query, response.text)
        assert isinstance(response.json()["results"], list)
    # Invalid requests are refused, not executed.
    assert client.get("/api/v1/search", params={"q": ""}, headers=headers).status_code == 422
    assert client.get("/api/v1/search", params={"q": "a" * 501}, headers=headers).status_code == 422
    assert client.get("/api/v1/search", params={"q": "a\x00b"}, headers=headers).status_code == 422
    assert (
        client.get("/api/v1/search", params={"q": "ap", "offset": -1}, headers=headers).status_code
        == 422
    )
    with connection(settings) as conn:
        count = conn.execute("SELECT count(*) AS n FROM item").fetchone()
    assert count == {"n": 3}


def test_prefix_query_function_keeps_exclusions_exact(settings: Settings) -> None:
    def prefixed(q: str, minimum: int, weights: str, keep: bool, config: str = "simple") -> str:
        with connection(settings) as conn:
            row = conn.execute(
                "SELECT bag_prefix_query(websearch_to_tsquery(%(config)s::regconfig, %(q)s), "
                "websearch_to_tsquery('simple', %(q)s), %(minimum)s, %(weights)s, %(keep)s)::text "
                "AS value",
                {"config": config, "q": q, "minimum": minimum, "weights": weights, "keep": keep},
            ).fetchone()
        assert row is not None
        return str(row["value"])

    assert prefixed("urlaub", 2, "ABC", True) == "'urlaub' | 'urlaub':*ABC"
    assert prefixed("urlaub", 2, "", False) == "'urlaub':*"
    assert prefixed("31", 2, "A", False) == "'31':*A"
    # Shorter than the minimum: no prefix, but the weight restriction stays.
    assert prefixed("a", 2, "A", False) == "'a':A"
    assert prefixed("a", 2, "ABC", True) == "'a'"
    assert prefixed("a", 2, "", False) == "'a'"
    assert prefixed("foto -urlaub", 2, "", False) == "'foto':* & !'urlaub'"
    assert prefixed('-"foto urlaub" test', 2, "", False) == "!( 'foto' <-> 'urlaub' ) & 'test':*"
    assert prefixed('"foto urlaub"', 2, "", False) == "'foto':* <-> 'urlaub':*"
    assert prefixed("foto or urlaub", 2, "", False) == "'foto':* | 'urlaub':*"
    assert prefixed("it's", 2, "", False) == "'it':* <-> 's'"
    assert prefixed("die", 2, "ABC", True, "german") == ""
    # Only the words as typed get a prefix; a stem would be far too broad.
    assert prefixed("Bücher", 2, "", False) == "'bücher':*"
    assert prefixed("Bücher", 2, "", False, "german") == "'buch'"
    assert prefixed("uebers", 2, "ABC", True, "german") == "'ueb'"
    assert prefixed("rechnungen telek", 2, "", False, "german") == "'rechnung' & 'telek':*"
    # Quotes and backslashes inside a lexeme survive the round trip.
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT bag_prefix_query(%(q)s::tsquery, %(q)s::tsquery, 2, '', false)::text AS value",
            {"q": "'a''bc' & 'x\\\\yz'"},
        ).fetchone()
    assert row == {"value": "'a''bc':* & 'x\\\\yz':*"}
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT bag_prefix_query(NULL, ''::tsquery, 2, '', false) IS NULL AS missing"
        ).fetchone()
    assert row == {"missing": True}


def test_filename_key_folds_case_and_umlauts(settings: Settings) -> None:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT bag_filename_key(%s) AS key, bag_filename_key(NULL) IS NULL AS missing",
            ("Müller-ÜBER.PDF",),
        ).fetchone()
    assert row == {"key": "müller-über.pdf\nmueller-ueber.pdf", "missing": True}


def test_migration_backfills_and_downgrades_search_head(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    config = migration_config()
    tagged = text(client, headers, "Reisebericht")
    patch(client, headers, tagged, tags=["Urlaubstest"], title="Sommer", user_note="Notiz")
    named = upload(client, headers, "IMG_3109.webp")
    command.downgrade(config, "0005_search_fields")
    try:
        with connection(settings) as conn:
            columns = conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'item' AND column_name = 'search_head'"
            ).fetchone()
            functions = conn.execute(
                "SELECT count(*) AS n FROM pg_proc WHERE proname IN "
                "('bag_prefix_query', 'bag_head_vector', 'bag_filename_key')"
            ).fetchone()
        assert columns == {"n": 0} and functions == {"n": 0}
        # Version 0005 works as before: tag changes and edits still re-index the vector.
        patch(client, headers, tagged, tags=["Ferientest"])
        with connection(settings) as conn:
            legacy = uuid7()
            conn.execute(
                "INSERT INTO item (id, owner_id, kind, source, original_filename, mime_type, "
                "processing_status) SELECT %s, owner_id, 'file', 'api', 'Altbestand-2024.pdf', "
                "'application/pdf', 'ready' FROM item WHERE id = %s",
                (legacy, tagged),
            )
            hits = conn.execute(
                "SELECT id FROM item WHERE search_vector @@ plainto_tsquery('simple', 'ferientest')"
            ).fetchall()
            assert [str(row["id"]) for row in hits] == [tagged]
            assert not conn.execute(
                "SELECT 1 FROM item WHERE search_vector @@ plainto_tsquery('simple', 'urlaubstest')"
            ).fetchall()
    finally:
        command.upgrade(config, "head")
    # The upgrade fills the head of every existing item, tags included, and the indexes work.
    assert head_is_current(settings)
    assert found(client, headers, "ferie") == [tagged]
    assert found(client, headers, "ferientest") == [tagged]
    assert found(client, headers, "urlaub") == []
    assert found(client, headers, "sommer") == [tagged]
    assert found(client, headers, "31") == [named]
    assert found(client, headers, "altbest") == [str(legacy)]
    assert found(client, headers, "bestand") == [str(legacy)]
    with connection(settings) as conn:
        indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'item'"
            ).fetchall()
        }
    assert {"item_search_idx", "item_search_head_idx", "item_filename_trgm_idx"} <= indexes


def _seed(settings: Settings, owner: Any, count: int) -> tuple[Any, Any]:
    """`count` items with bodies, names and tags, plus one needle file and one needle tag."""
    words = [f"{a}{b}" for a in "kalomirate" for b in ("su", "no", "vi", "pe", "da", "bo", "ni")]
    with connection(settings) as conn:
        tag_ids = []
        for index in range(40):
            tag_ids.append(uuid7())
            conn.execute(
                "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, %s)",
                (tag_ids[-1], owner, f"{words[index]}-{index}"),
            )
        rows = []
        for index in range(count):
            body = " ".join(words[(index * 7 + step * 13) % len(words)] for step in range(150))
            kind = ("file", "text", "url")[index % 3]
            name = f"{words[index % len(words)]}_{index:05d}.pdf" if kind == "file" else None
            title = f"Eintrag {words[index % 11]} {index}" if index % 4 == 0 else None
            rows.append((uuid7(), owner, kind, name, title, body, "de" if index % 2 else "en"))
        conn.cursor().executemany(
            "INSERT INTO item (id, owner_id, kind, source, original_filename, title, "
            "extracted_text, language, mime_type, processing_status) "
            "VALUES (%s, %s, %s, 'bulk', %s, %s, %s, %s, 'application/pdf', 'ready')",
            rows,
        )
        for index, row in enumerate(rows[:600]):
            conn.execute(
                "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) "
                "VALUES (%s, %s, %s, %s, 'user')",
                (uuid7(), owner, row[0], tag_ids[index % len(tag_ids)]),
            )
        needle_file, needle_tag, tag_id = uuid7(), uuid7(), uuid7()
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, original_filename, mime_type, "
            "processing_status) VALUES (%s, %s, 'file', 'bulk', 'IMG_3109.webp', 'image/webp', "
            "'ready')",
            (needle_file, owner),
        )
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, content, extracted_text, mime_type, "
            "processing_status) VALUES (%s, %s, 'text', 'bulk', 'x', 'unique', 'text/plain', "
            "'ready')",
            (needle_tag, owner),
        )
        conn.execute(
            "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, 'Urlaubstest')", (tag_id, owner)
        )
        conn.execute(
            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'user')",
            (uuid7(), owner, needle_tag, tag_id),
        )
    return needle_file, needle_tag


@contextmanager
def recorded_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """The SQL statements and parameters `bag.search` sends while the block runs."""
    calls: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def recording(settings: Settings) -> Iterator[Any]:
        with connection(settings) as conn:

            class Recorder:
                def execute(self, sql: str, params: Any = None) -> Any:
                    calls.append((sql, dict(params)))
                    return conn.execute(sql, params)

            yield Recorder()

    with monkeypatch.context() as patched:
        patched.setattr("bag.search.connection", recording)
        yield calls


def _plan_nodes(plan: Any) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def test_partial_search_uses_indexes_and_stays_fast_on_a_larger_bag(
    settings: Settings, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = add_user(settings, "bulk")
    with connection(settings) as conn:
        owner = conn.execute("SELECT id FROM \"user\" WHERE display_name = 'bulk'").fetchone()
    assert owner is not None
    needle_file, needle_tag = _seed(settings, owner["id"], 4000)
    url = settings.database_url.get_secret_value()
    with psycopg.connect(url, autocommit=True) as raw:
        raw.execute("ANALYZE item")
        raw.execute("ANALYZE item_tag")

    cases = {
        "urlaub": needle_tag,
        "urlaubst": needle_tag,
        "31": needle_file,
        "109": needle_file,
        "mg_310": needle_file,
        "img 3109": needle_file,
        "unique urla": needle_tag,
    }
    with recorded_statements(monkeypatch) as calls:
        for query, expected in cases.items():
            calls.clear()
            start = time.perf_counter()
            assert str(expected) in found(client, headers, query, limit=100), query
            assert time.perf_counter() - start < 5, query
            # Every statement can reach its candidates through an index; an unindexable
            # substring scan would show as a sequential scan even with them disabled.
            for sql, params in calls:
                with psycopg.connect(url) as raw:
                    raw.execute("SET enable_seqscan = off")
                    row = raw.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
                assert row is not None
                scans = {
                    node["Node Type"]
                    for node in _plan_nodes(row[0][0]["Plan"])
                    if node.get("Relation Name") == "item"
                }
                assert "Seq Scan" not in scans, (query, scans)
    start = time.perf_counter()
    assert found(client, headers, "zzzzqq") == []
    assert found(client, headers, "zzz") == []
    assert time.perf_counter() - start < 5
    other = add_user(settings, "other")
    assert found(client, other, "urlaub") == [] and found(client, other, "31") == []
