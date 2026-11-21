"""Search follow-ups: ADR-0026, migration 0007_search_followups."""

from typing import Any

import pytest
from alembic import command
from bag import search as search_module
from bag.cli import migration_config
from bag.config import Settings
from bag.db import connection
from bag.search import highlight
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import drain
from test_search_fields import fields, found, patch, search, text, upload
from test_search_partial import head_is_current

pytestmark = pytest.mark.integration


def url(client: TestClient, headers: dict[str, str], value: str, **extra: Any) -> str:
    response = client.post("/api/v1/capture/url", json={"url": value, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def index_is_current(settings: Settings) -> bool:
    """Every vector and name key equals a full rebuild from the row and its tags."""
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT count(*) AS stale FROM item WHERE search_vector IS DISTINCT FROM "
            "bag_item_vector(title, original_filename, bag_item_tags_vector(owner_id, id), "
            "user_note, source_url, extracted_text, language, true) "
            "OR search_names IS DISTINCT FROM "
            "bag_item_names(owner_id, id, original_filename, title)"
        ).fetchone()
    assert row is not None
    return bool(row["stale"] == 0) and head_is_current(settings)


def test_url_host_and_path_words_are_searchable(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    budget = url(client, headers, "https://budget.example.org/x-y")
    travel = url(client, headers, "https://www.reiseblog.de/reisen/italien-2026?seite=3")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # Every label of the host and every word of the path is a whole word now.
    assert found(client, headers, "example") == [budget]
    assert found(client, headers, "budget") == [budget]
    assert fields(client, headers, "example") == {budget: ["url"]}
    for query in ("italien", "reisen", "2026", "seite", "reiseblog", "italien 2026"):
        assert found(client, headers, query) == [travel], query
    # The whole URL and host still match as before; prefixes of the words work too.
    assert found(client, headers, "budget.example.org") == [budget]
    assert found(client, headers, "exam") == [budget]
    assert found(client, headers, "ital") == [travel]
    # The scheme and a leading www are not words of the URL.
    assert found(client, headers, "https") == []
    # ('www' still matches as the start of the host token, as before.)
    assert index_is_current(settings)


def test_accents_are_folded_in_titles_names_tags_notes_and_urls(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    titled = text(client, headers, "Eintrag 1")
    patch(client, headers, titled, title="Café Crème")
    tagged = text(client, headers, "Eintrag 2")
    patch(client, headers, tagged, tags=["Résumé"])
    noted = text(client, headers, "Eintrag 3", user_note="Naïve Kunst")
    filed = upload(client, headers, "Crème_brûlée.pdf")
    plain = text(client, headers, "Eintrag 4")
    patch(client, headers, plain, title="Cafe Lageplan")
    body = text(client, headers, "Im Café gegenüber")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # Without the accent, with it, and in other case: stored accents are folded.
    assert set(found(client, headers, "cafe")) == {titled, plain}
    assert set(found(client, headers, "CAFE")) == {titled, plain}
    assert found(client, headers, "resume") == [tagged]
    assert found(client, headers, "naive") == [noted]
    assert found(client, headers, "brulee") == [filed]
    assert set(found(client, headers, "creme")) == {titled, filed}
    assert fields(client, headers, "resume") == {tagged: ["tag"]}
    assert fields(client, headers, "naive") == {noted: ["note"]}
    # A query with an accent also finds the same word stored without one.
    assert found(client, headers, "café lageplan") == [plain]
    # Prefixes fold as well.
    assert found(client, headers, "brul") == [filed]
    assert found(client, headers, "resu") == [tagged]
    # The extracted text is only folded for whole words as written (ADR-0026).
    assert found(client, headers, "gegenüber") == [body]
    assert body not in found(client, headers, "cafe")
    assert index_is_current(settings)


def test_name_substrings_cover_titles_and_tags_and_fold_the_query(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    shelf = text(client, headers, "Eintrag 1")
    patch(client, headers, shelf, tags=["Bücherregal"])
    market = text(client, headers, "Eintrag 2")
    patch(client, headers, market, title="Wochenmarkt")
    noted = text(client, headers, "Eintrag 3", user_note="Flohmarktbesuch")
    stored_translit = upload(client, headers, "mueller-angebot.pdf")
    stored_umlaut = upload(client, headers, "Müller_Rechnung.pdf")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # The reported compound case: the second part of a tag or title.
    assert found(client, headers, "regal") == [shelf]
    assert fields(client, headers, "regal") == {shelf: ["tag"]}
    assert found(client, headers, "markt") == [market]
    assert fields(client, headers, "markt") == {market: ["title"]}
    # Notes keep prefix matching only.
    assert noted not in found(client, headers, "markt")
    assert found(client, headers, "flohm") == [noted]
    # Umlaut, transliteration and plain vowel find each other in either direction.
    assert set(found(client, headers, "müller")) == {stored_translit, stored_umlaut}
    assert set(found(client, headers, "mueller")) == {stored_translit, stored_umlaut}
    assert found(client, headers, "muller") == [stored_umlaut]
    assert set(found(client, headers, "üller")) == {stored_translit, stored_umlaut}
    assert found(client, headers, "bücherreg") == [shelf]
    assert found(client, headers, "uecherr") == [shelf]
    # Substrings are ranked after prefix hits and never fail on special characters.
    assert search(client, headers, "regal")["results"][0]["rank"] < 1
    for query in ("%", "_", "\\", "re%al", "re_al"):
        assert found(client, headers, query) == [], query
    assert index_is_current(settings)


def test_name_hits_are_shown_in_the_snippet(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    shelf = text(client, headers, "Ein Eintrag")
    patch(client, headers, shelf, tags=["Bücherregal", "Möbel"])
    photo = upload(client, headers, "IMG_3109.webp")
    invoice = upload(client, headers, "rechnung-oktober.pdf")
    noted = text(client, headers, "Inhalt", user_note="Regalbretter kaufen")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    def snippet(q: str, item: str) -> str:
        rows = {row["id"]: row for row in search(client, headers, q)["results"]}
        return str(rows[item]["snippet"])

    assert snippet("regal", shelf) == "Bücher«regal»"
    assert snippet("109", photo) == "IMG_3«109».webp"
    assert snippet("31", photo) == "IMG_«31»09.webp"
    assert snippet("rechnung", invoice) == "«rechnung»-oktober.pdf"
    assert snippet("moebel", shelf) == "«Möbel»"
    # A text hit keeps the text snippet.
    assert "«Regalbretter»" in snippet("regal", noted)


def test_tag_changes_rebuild_the_index_exactly_from_the_stored_text(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    a = text(client, headers, "Die Rechnung liegt im Ordner Steuern 2026.")
    b = upload(client, headers, "Kontoauszug.pdf")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    patch(client, headers, a, tags=["Finanzen", "Steuern"], title="Steuer", user_note="Notiz")
    patch(client, headers, b, tags=["Finanzen"])
    assert index_is_current(settings)

    tags = {row["name"]: row["id"] for row in client.get("/api/v1/tags", headers=headers).json()}
    response = client.patch(
        f"/api/v1/tags/{tags['Finanzen']}", json={"name": "Geldsachen"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert index_is_current(settings)
    assert set(found(client, headers, "geldsachen")) == {a, b}
    assert found(client, headers, "finanzen") == []
    # The text part is untouched: phrases and whole words of the text still match.
    assert found(client, headers, '"liegt im ordner"') == [a]
    assert found(client, headers, "rechnung geldsachen") == [a]
    assert client.delete(f"/api/v1/tags/{tags['Steuern']}", headers=headers).status_code == 204
    assert index_is_current(settings)
    assert found(client, headers, "steuern") == [a]  # still in the text


def test_ranking_orders_only_the_newest_matches_of_a_tier_by_frequency(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "RANK_CAP", 2)
    oldest_dense = text(client, headers, "zitrone " * 30 + "ende")
    older = text(client, headers, "zitrone und vieles andere hier")
    newer = text(client, headers, "zitrone zitrone und etwas")
    newest = text(client, headers, "eine zitrone")
    titled = text(client, headers, "anderer inhalt")
    patch(client, headers, titled, title="Zitrone")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    page = search(client, headers, "zitrone")["results"]
    order = [row["id"] for row in page]
    # A title hit leads; the newest two text hits are ranked by frequency; the rest follow
    # newest first, even the densest text.
    assert order[0] == titled and set(order[1:3]) == {newer, newest}
    assert order[3:] == [older, oldest_dense]
    ranks = [row["rank"] for row in page]
    assert ranks == sorted(ranks, reverse=True)
    # Beyond the cap a text hit has no frequency rank; within it every hit has one.
    assert [row["rank"] for row in page][3:] == [1.0, 1.0]
    monkeypatch.setattr(search_module, "RANK_CAP", 500)
    assert all(row["rank"] > 1 for row in search(client, headers, "zitrone")["results"])


def test_migration_0007_round_trip(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    config = migration_config()
    item = text(client, headers, "Reisebericht aus dem Café")
    patch(client, headers, item, tags=["Bücherregal"], title="Crème", user_note="Notiz")
    link = url(client, headers, "https://budget.example.org/x-y")
    named = upload(client, headers, "IMG_3109.webp")
    command.downgrade(config, "0006_search_partial")
    try:
        with connection(settings) as conn:
            columns = conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'item' AND column_name = 'search_names'"
            ).fetchone()
            functions = conn.execute(
                "SELECT count(*) AS n FROM pg_proc WHERE proname IN ('bag_fold', "
                "'bag_url_vector', 'bag_field_vector', 'bag_join_vectors', 'bag_item_names')"
            ).fetchone()
            indexes = {
                row["indexname"]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'item'"
                ).fetchall()
            }
        assert columns == {"n": 0} and functions == {"n": 0}
        assert "item_filename_trgm_idx" in indexes and "item_names_trgm_idx" not in indexes
        # Version 0006 is exact again: its own functions rebuilt every vector.
        assert head_is_current(settings)
        with connection(settings) as conn:
            stale = conn.execute(
                "SELECT count(*) AS n FROM item WHERE search_vector IS DISTINCT FROM "
                "bag_item_vector(title, original_filename, bag_item_tags_vector(owner_id, id), "
                "user_note, source_url, extracted_text, language, true)"
            ).fetchone()
        assert stale == {"n": 0}
    finally:
        command.upgrade(config, "head")
    assert index_is_current(settings)
    assert found(client, headers, "regal") == [item]
    assert found(client, headers, "creme") == [item]
    assert found(client, headers, "example") == [link]
    assert found(client, headers, "109") == [named]


@pytest.mark.parametrize(
    ("value", "needles", "expected"),
    [
        ("Müller.pdf", ["mueller"], "«Müller».pdf"),
        ("Müller.pdf", ["muller"], "«Müller».pdf"),
        ("mueller.pdf", ["müller"], "«mueller».pdf"),
        ("Café Crème", ["cafe", "creme"], "«Café» «Crème»"),
        ("IMG_3109.webp", ["3109", "31"], "IMG_«3109».webp"),
        ("Straße", ["strasse"], "«Straße»"),
        ("aaaa", ["aa"], "«aaaa»"),
        ("Bücherregal", ["xyz"], None),
        ("", ["ab"], None),
    ],
)
def test_highlight_folds_like_the_search(
    value: str, needles: list[str], expected: str | None
) -> None:
    assert highlight(value, needles) == expected
