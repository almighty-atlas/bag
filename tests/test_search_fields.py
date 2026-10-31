"""Search over filenames and tags, field-weighted ranking and structured match reasons."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from bag.auth import token_hash
from bag.cli import migration_config
from bag.config import Settings
from bag.db import connection
from bag.export import export_bag
from bag.ids import uuid7
from bag.importer import import_bag
from bag.storage import FileSystemStorage
from bag.worker import Worker
from fastapi.testclient import TestClient
from helpers import PDF, drain

pytestmark = pytest.mark.integration


def search(client: TestClient, headers: dict[str, str], q: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/search", params={"q": q, **params}, headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def found(client: TestClient, headers: dict[str, str], q: str, **params: Any) -> list[str]:
    return [row["id"] for row in search(client, headers, q, **params)["results"]]


def fields(client: TestClient, headers: dict[str, str], q: str) -> dict[str, list[str]]:
    return {row["id"]: row["matched_fields"] for row in search(client, headers, q)["results"]}


def text(client: TestClient, headers: dict[str, str], content: str, **extra: Any) -> str:
    response = client.post(
        "/api/v1/capture/text", json={"content": content, **extra}, headers=headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def upload(client: TestClient, headers: dict[str, str], filename: str, data: bytes = PDF) -> str:
    response = client.post(
        "/api/v1/capture/file", headers=headers, files={"file": (filename, data)}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def patch(client: TestClient, headers: dict[str, str], item_id: str, **body: Any) -> None:
    response = client.patch(f"/api/v1/items/{item_id}", json=body, headers=headers)
    assert response.status_code == 200, response.text


def add_user(settings: Settings, name: str) -> dict[str, str]:
    owner = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (owner, name))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), owner, "test", token_hash(name)),
        )
    return {"Authorization": f"Bearer {name}"}


def test_filenames_are_searchable_in_every_spelling(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    invoice = upload(client, headers, "rechnung-oktober-2026.pdf")
    snake = upload(client, headers, "Mietvertrag_Wohnung_Süd.PDF")
    camel = upload(client, headers, "SteuerBescheid2025.pdf")
    umlaut = upload(client, headers, "Müller Größe-Übersicht.v2.pdf")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # Separators, extension, case and the full name all find the file; only the name matched.
    for query in (
        "rechnung",
        "Rechnung",
        "oktober",
        "2026",
        "rechnung oktober",
        "rechnung-oktober-2026.pdf",
        "RECHNUNG-OKTOBER-2026.PDF",
        '"oktober 2026"',
        "pdf rechnung",
    ):
        assert found(client, headers, query).count(invoice) == 1, query
    assert fields(client, headers, "rechnung") == {invoice: ["filename"]}
    assert fields(client, headers, "rechnung-oktober-2026.pdf") == {invoice: ["filename"]}
    assert found(client, headers, "rechnung november") == []
    # Underscores split words and the extension is a word of its own.
    assert found(client, headers, "wohnung") == [snake]
    assert found(client, headers, "mietvertrag wohnung") == [snake]
    # camelCase and letter-digit names.
    assert found(client, headers, "bescheid") == [camel]
    assert found(client, headers, "SteuerBescheid2025.pdf") == [camel]
    # Umlauts: as written, folded to the base letter (German stemming) and transliterated.
    for query in (
        "müller",
        "Müller",
        "muller",
        "mueller",
        "Größe",
        "grosse",
        "Übersicht",
        "uebersicht",
    ):
        assert found(client, headers, query) == [umlaut], query
    assert fields(client, headers, "müller") == {umlaut: ["filename"]}
    # The extension alone finds every PDF; nothing else matched.
    assert set(found(client, headers, "pdf")) == {invoice, snake, camel, umlaut}
    # The filename never leaks into the other fields of the response.
    row = search(client, headers, "rechnung")["results"][0]
    assert row["original_filename"] == "rechnung-oktober-2026.pdf"
    assert "content" not in row and "extracted_text" not in row


def test_tags_are_searchable_immediately(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    first = text(client, headers, "Kasse A")
    second = text(client, headers, "Kasse B")
    assert found(client, headers, "steuer") == []

    # Add: effective in the same request, in any case and for name parts.
    patch(client, headers, first, tags=["Steuer-2026", "Büro"])
    patch(client, headers, second, tags=["Büro"])
    assert found(client, headers, "steuer") == [first]
    assert found(client, headers, "STEUER-2026") == [first]
    assert found(client, headers, "2026") == [first]
    assert set(found(client, headers, "büro")) == {first, second}
    assert set(found(client, headers, "buero")) == {first, second}
    assert fields(client, headers, "steuer") == {first: ["tag"]}

    # Remove one of two tags; the other still works.
    patch(client, headers, first, tags=["Büro"])
    assert found(client, headers, "steuer") == []
    assert set(found(client, headers, "büro")) == {first, second}
    patch(client, headers, first, tags=[])
    assert found(client, headers, "büro") == [second]

    # Rename: the old name stops matching, the new one matches, every item follows.
    patch(client, headers, first, tags=["Büro"])
    tag = next(t for t in client.get("/api/v1/tags", headers=headers).json() if t["name"] == "Büro")
    renamed = client.patch(f"/api/v1/tags/{tag['id']}", json={"name": "Archiv"}, headers=headers)
    assert renamed.status_code == 200
    assert found(client, headers, "büro") == []
    assert set(found(client, headers, "archiv")) == {first, second}
    assert fields(client, headers, "archiv") == {first: ["tag"], second: ["tag"]}

    # A rename that only changes case or keeps the assignments intact works the same.
    client.patch(f"/api/v1/tags/{tag['id']}", json={"name": "ARCHIV 2"}, headers=headers)
    assert set(found(client, headers, "archiv")) == {first, second}
    assert set(found(client, headers, "2")) == {first, second}

    # Deleting the tag removes the assignments and the matches, never the items.
    assert client.delete(f"/api/v1/tags/{tag['id']}", headers=headers).status_code == 204
    assert found(client, headers, "archiv") == []
    assert client.get(f"/api/v1/items/{first}", headers=headers).status_code == 200

    # System and AI assignments written outside the API are indexed by the database itself.
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
    assert found(client, headers, "automatisch") == [first]
    with connection(settings) as conn:
        conn.execute("DELETE FROM item_tag WHERE tag_id = %s", (tag_id,))
    assert found(client, headers, "automatisch") == []


def test_german_and_english_terms_across_fields(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    german = text(client, headers, "Die Rechnungen für die Wohnung liegen im Keller.")
    english = text(client, headers, "The invoices for the apartment are in the basement.")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    patch(client, headers, german, title="Mietrechnungen", tags=["Bücherregal"])
    patch(client, headers, english, title="Rental invoices", tags=["Bookcases"])
    pdf = upload(client, headers, "Invoices_2026.pdf")

    # Stemming in titles, tags and bodies.
    assert set(found(client, headers, "invoice")) == {english, pdf}
    assert set(found(client, headers, "invoices")) == {english, pdf}
    assert found(client, headers, "bücherregale") == [german]
    assert found(client, headers, "bookcase") == [english]
    assert found(client, headers, "wohnungen") == [german]
    assert found(client, headers, "apartments") == [english]
    assert fields(client, headers, "invoice")[pdf] == ["filename"]
    assert fields(client, headers, "invoice")[english] == ["title", "content"]
    assert fields(client, headers, "bücherregal") == {german: ["tag"]}
    assert fields(client, headers, "wohnung") == {german: ["content"]}
    # OR queries and exclusions still work with the new fields.
    assert set(found(client, headers, "bücherregal OR bookcases")) == {german, english}
    assert found(client, headers, "invoice -rental") == [pdf]


def test_relevance_order_title_filename_tag_note_content(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    # The body repeats the word many times; field priority still wins.
    body = text(client, headers, "Kaffee " * 60 + "und noch ein Satz über Tassen")
    note = text(client, headers, "Eine andere Notiz", user_note="Kaffee Vorrat prüfen")
    tag = text(
        client,
        headers,
        "Einkaufsliste",
    )
    patch(client, headers, tag, tags=["Kaffee"])
    filename = upload(client, headers, "kaffee-sorten.pdf")
    title = text(client, headers, "Röstung und Mahlgrad")
    patch(client, headers, title, title="Kaffee Röstung")
    exact = text(client, headers, "Irgendein Text")
    patch(client, headers, exact, title="Kaffee")
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    ranked = found(client, headers, "Kaffee")
    assert ranked[0] == exact, "an exact title comes first"
    assert set(ranked[1:3]) == {title, filename}, "title and filename before tags"
    assert ranked[3:] == [tag, note, body]
    ranks = [row["rank"] for row in search(client, headers, "Kaffee")["results"]]
    assert ranks == sorted(ranks, reverse=True) and ranks[-1] > 0
    assert fields(client, headers, "Kaffee") == {
        exact: ["title"],
        title: ["title"],
        filename: ["filename"],
        tag: ["tag"],
        note: ["note"],
        body: ["content"],
    }

    # An exact filename, with or without its extension, outranks a partial title hit.
    assert found(client, headers, "kaffee-sorten.pdf")[0] == filename
    assert found(client, headers, "kaffee-sorten")[0] == filename
    # Ranking is deterministic across pages.
    pages: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = search(client, headers, "Kaffee", limit=2, offset=offset)
        pages += [row["id"] for row in page["results"]]
        offset = page["next_offset"]
    assert pages == ranked


def test_matched_fields_report_only_real_matches(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = upload(client, headers, "projekt-plan.pdf")
    patch(
        client,
        headers,
        item,
        title="Projekt Zeitplan",
        user_note="Notiz zum Budget",
        tags=["Budget", "Q4"],
    )
    link = client.post(
        "/api/v1/capture/url",
        json={"url": "https://budget.example.org/", "user_note": "Vorlage"},
        headers=headers,
    ).json()["id"]
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))

    # Several fields at once, in a fixed order.
    assert fields(client, headers, "projekt") == {item: ["title", "filename"]}
    assert fields(client, headers, "budget") == {item: ["tag", "note"]}
    assert fields(client, headers, "vorlage") == {link: ["note"]}
    assert fields(client, headers, "budget.example.org") == {link: ["url"]}
    # Words that fit different fields of one item are reported per field.
    assert fields(client, headers, "projekt budget") == {item: ["title", "filename", "tag", "note"]}
    # An exclusion contributes no reason; a negated-only query reports nothing at all.
    assert fields(client, headers, "projekt -vorlage") == {item: ["title", "filename"]}
    negated = fields(client, headers, "-vorlage")
    assert negated[item] == [] and set(map(tuple, negated.values())) == {()}
    # A phrase never spans two fields: 'plan' ends the title/filename, 'budget' starts the tag.
    assert found(client, headers, '"zeitplan budget"') == []
    assert found(client, headers, '"plan budget"') == []
    assert found(client, headers, '"budget q4"') == []
    # Filters keep working and results always name their own item's fields.
    only = search(client, headers, "vorlage OR budget", kind="url")["results"]
    assert [(row["id"], row["matched_fields"]) for row in only] == [(link, ["note"])]
    assert found(client, headers, "budget", tag="Q4") == [item]
    assert found(client, headers, "budget", tag="missing") == []
    assert found(client, headers, "budget", status="ready") == [item]


def test_owner_isolation_trash_and_authentication(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    other = add_user(settings, "other")
    mine = upload(client, headers, "geheim-vertrag.pdf")
    patch(client, headers, mine, tags=["vertraulich"], title="Mein Vertrag")
    theirs = client.post(
        "/api/v1/capture/file",
        headers=other,
        files={"file": ("vertrag-fremd.pdf", PDF)},
    ).json()["id"]
    patch(client, other, theirs, tags=["vertraulich"])

    # Same tag name, separate owners: each only sees their own items and reasons.
    assert found(client, headers, "vertraulich") == [mine]
    assert found(client, other, "vertraulich") == [theirs]
    assert found(client, headers, "fremd") == []
    assert found(client, other, "geheim") == []
    assert found(client, other, "Mein") == []
    assert [r["original_filename"] for r in search(client, other, "vertrag")["results"]] == [
        "vertrag-fremd.pdf"
    ]
    # Renaming, assigning or deleting a tag of one owner never touches the other's index.
    my_tag = next(t for t in client.get("/api/v1/tags", headers=headers).json())
    client.patch(f"/api/v1/tags/{my_tag['id']}", json={"name": "privat"}, headers=headers)
    assert (
        found(client, headers, "privat") == [mine] and found(client, headers, "vertraulich") == []
    )
    assert found(client, other, "vertraulich") == [theirs]
    assert found(client, other, "privat") == []
    client.delete(f"/api/v1/tags/{my_tag['id']}", headers=headers)
    assert found(client, other, "vertraulich") == [theirs]
    patch(client, headers, mine, tags=["vertraulich"])
    assert found(client, headers, "vertraulich") == [mine]

    # Trash hides the item from normal search, keeps its fields searchable in the trash and
    # follows tag changes made meanwhile; restoring brings it back.
    assert client.delete(f"/api/v1/items/{mine}", headers=headers).status_code == 204
    assert found(client, headers, "vertraulich") == []
    assert found(client, headers, "geheim") == []
    assert found(client, headers, "vertraulich", trashed="true") == [mine]
    assert found(client, headers, "geheim", trashed="true") == [mine]
    assert found(client, other, "geheim", trashed="true") == []
    with connection(settings) as conn:
        conn.execute(
            "UPDATE tag SET name = 'archiviert' WHERE name = 'vertraulich' "
            "AND owner_id = (SELECT owner_id FROM item WHERE id = %s)",
            (mine,),
        )
    assert found(client, headers, "archiviert", trashed="true") == [mine]
    assert found(client, other, "vertraulich") == [theirs]
    assert client.post(f"/api/v1/items/{mine}/restore", headers=headers).status_code == 200
    assert found(client, headers, "archiviert") == [mine]
    assert found(client, headers, "archiviert", trashed="true") == []

    # Authentication is required and invalid tokens see nothing.
    assert client.get("/api/v1/search", params={"q": "geheim"}).status_code == 401
    bad = {"Authorization": "Bearer nobody"}
    assert client.get("/api/v1/search", params={"q": "geheim"}, headers=bad).status_code == 401


def test_filters_and_pagination_with_new_fields(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    ids = [upload(client, headers, f"quartal-{i}.pdf") for i in range(5)]
    for index, item in enumerate(ids):
        patch(client, headers, item, tags=["Bericht"] if index % 2 == 0 else [])
    assert (
        client.post("/api/v1/collections", json={"name": "Jahr"}, headers=headers).status_code
        == 201
    )
    patch(client, headers, ids[0], collections=["Jahr"])
    patch(client, headers, ids[1], collections=["Jahr"])

    assert set(found(client, headers, "quartal")) == set(ids)
    assert set(found(client, headers, "quartal", tag="Bericht")) == {ids[0], ids[2], ids[4]}
    assert set(found(client, headers, "quartal", collection="Jahr")) == {ids[0], ids[1]}
    assert found(client, headers, "quartal", tag="Bericht", collection="Jahr") == [ids[0]]
    # Searching by a tag combines with the tag filter and with other filters.
    assert set(found(client, headers, "bericht", tag="Bericht")) == {ids[0], ids[2], ids[4]}
    assert found(client, headers, "bericht", tag="Bericht", kind="text") == []
    assert found(client, headers, "bericht", **{"from": "2099-01-01T00:00:00Z"}) == []
    # Equal relevance falls back to newest first, and pages are disjoint and complete.
    everything = found(client, headers, "quartal")
    assert set(everything) == set(ids)
    assert [i for i in everything if i in (ids[3], ids[1])] == [ids[3], ids[1]]
    assert [i for i in everything if i in (ids[4], ids[2], ids[0])] == [ids[4], ids[2], ids[0]]
    first = search(client, headers, "quartal", limit=2)
    second = search(client, headers, "quartal", limit=2, offset=first["next_offset"])
    last = search(client, headers, "quartal", limit=2, offset=second["next_offset"])
    assert [r["id"] for r in first["results"] + second["results"] + last["results"]] == everything
    assert first["next_offset"] == 2 and second["next_offset"] == 4 and last["next_offset"] is None
    assert all(row["matched_fields"] == ["filename"] for row in first["results"])
    # The item listing is unchanged by the new search fields.
    listed = client.get("/api/v1/items?tag=Bericht", headers=headers).json()["items"]
    assert [row["id"] for row in listed] == [ids[4], ids[2], ids[0]]
    assert "matched_fields" not in listed[0]


def test_existing_items_are_backfilled_by_the_migration(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    config = migration_config()
    owner = add_user(settings, "second")
    kept = upload(client, headers, "alter-bericht.pdf")
    patch(client, headers, kept, tags=["Archiv"], title="Alter Bericht")
    rich = text(
        client,
        headers,
        "Die Taschen sind voll, und wir haben noch nicht alle Bücher aus dem Keller.",
        user_note="Umzug",
    )
    drain(Worker(settings, FileSystemStorage(settings.storage_path)))
    other = client.post(
        "/api/v1/capture/file", headers=owner, files={"file": ("fremd-bericht.pdf", PDF)}
    ).json()["id"]
    patch(client, owner, other, tags=["Archiv"])
    with connection(settings) as conn:
        conn.execute("UPDATE item SET deleted_at = now() WHERE id = %s", (other,))

    command.downgrade(config, "0004_sessions")
    try:
        with connection(settings) as conn:
            # Before the migration the vector knew neither filenames nor tags.
            for term, expected in (("alter", 1), ("archiv", 0), ("bericht", 1)):
                row = conn.execute(
                    "SELECT count(*) AS n FROM item "
                    "WHERE search_vector @@ plainto_tsquery('simple', %s)",
                    (term,),
                ).fetchone()
                assert row == {"n": expected}, term
            # An item captured while the old schema was current.
            legacy = uuid7()
            conn.execute(
                "INSERT INTO item (id, owner_id, kind, source, original_filename, user_note, "
                "mime_type, processing_status) SELECT %s, owner_id, 'file', 'api', "
                "'Altbestand-2024.pdf', 'archiviert', 'application/pdf', 'ready' FROM item "
                "WHERE id = %s",
                (legacy, kept),
            )
    finally:
        command.upgrade(config, "head")

    assert set(found(client, headers, "archiv")) == {kept}
    assert set(found(client, headers, "bericht")) == {kept}
    assert found(client, headers, "altbestand") == [str(legacy)]
    assert fields(client, headers, "altbestand") == {str(legacy): ["filename"]}
    assert fields(client, headers, "archiviert") == {str(legacy): ["note"]}
    # Text indexed before is still found with German stemming, and ownership survived.
    assert found(client, headers, "Tasche") == [rich]
    assert found(client, owner, "bericht", trashed="true") == [other]
    assert found(client, owner, "bericht") == []
    assert found(client, owner, "alter") == []
    # The triggers are live again after the upgrade.
    patch(client, headers, kept, tags=["Nachlass"])
    assert found(client, headers, "archiv") == []
    assert found(client, headers, "nachlass") == [kept]


def test_migration_round_trip_keeps_search_working(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = upload(client, headers, "rechnung.pdf")
    patch(client, headers, item, tags=["Steuer"])
    config = migration_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    with connection(settings) as conn:
        conn.execute('TRUNCATE TABLE "user" CASCADE')
    fresh = add_user(settings, "fresh")
    again = client.post(
        "/api/v1/capture/file", headers=fresh, files={"file": ("rechnung.pdf", PDF)}
    ).json()["id"]
    patch(client, fresh, again, tags=["Steuer"])
    assert fields(client, fresh, "steuer") == {again: ["tag"]}
    assert fields(client, fresh, "rechnung") == {again: ["filename"]}


def test_tag_changes_survive_an_oversized_search_vector(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = text(client, headers, "Dokument")
    # A German text close to PostgreSQL's 1 MiB tsvector limit: the stemmed and the exact
    # lexemes both count, so the index just fits.
    body = " ".join(f"wort{i}en" for i in range(64_650))
    with connection(settings) as conn:
        conn.execute(
            "UPDATE item SET extracted_text = %s, language = 'de' WHERE id = %s", (body, item)
        )
    assert found(client, headers, "wort64649en") == [item]

    # 100 long tags push the vector over the limit. The tag change must still succeed and the
    # item stays findable by everything except its extracted text.
    names = [" ".join(f"k{t:03d}{w:02d}" for w in range(14)) for t in range(100)]
    patch(client, headers, item, tags=names)
    assert found(client, headers, "k09913") == [item]
    assert fields(client, headers, "k09913") == {item: ["tag"]}
    assert found(client, headers, "wort64649en") == []
    stored = client.get(f"/api/v1/items/{item}", headers=headers).json()
    assert stored["extracted_text"] == body and len(stored["tags"]) == 100

    # Other edits of such an item work as well and keep the degraded index.
    patch(client, headers, item, title="Neuer Titel", user_note="Notiz")
    assert found(client, headers, "titel") == [item]
    assert found(client, headers, "wort1en") == []
    # Removing the tags restores the full index.
    patch(client, headers, item, tags=[])
    assert found(client, headers, "wort64649en") == [item]
    assert found(client, headers, "k09913") == []
    # New extracted text that does not fit is still refused, so processing can report it.
    too_big = " ".join(f"wort{i}en" for i in range(70_000))
    with connection(settings) as conn, pytest.raises(psycopg.errors.ProgramLimitExceeded):
        conn.execute("UPDATE item SET extracted_text = %s WHERE id = %s", (too_big, item))
    assert found(client, headers, "wort64649en") == [item]


def test_concurrent_tag_assignments_are_all_indexed(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    item = text(client, headers, "Dokument")
    with connection(settings) as conn:
        owner = conn.execute("SELECT owner_id FROM item WHERE id = %s", (item,)).fetchone()
    assert owner is not None
    url = settings.database_url.get_secret_value()
    first_assigned = threading.Event()
    release = threading.Event()

    def assign(name: str, wait: bool) -> None:
        with psycopg.connect(url) as conn:
            tag_id = uuid7()
            conn.execute(
                "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, %s)",
                (tag_id, owner["owner_id"], name),
            )
            conn.execute(
                "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) "
                "VALUES (%s, %s, %s, %s, 'user')",
                (uuid7(), owner["owner_id"], item, tag_id),
            )
            if wait:
                first_assigned.set()
                release.wait(10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        # The first transaction holds its uncommitted tag while the second one assigns another.
        one = pool.submit(assign, "erstes", True)
        assert first_assigned.wait(10)
        two = pool.submit(assign, "zweites", False)
        time.sleep(0.5)
        assert not two.done(), "the second change must wait for the item row"
        release.set()
        one.result(10)
        two.result(10)
    assert found(client, headers, "erstes") == [item]
    assert found(client, headers, "zweites") == [item]
    assert found(client, headers, "erstes zweites") == [item]


def _two_owner_setup(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> tuple[dict[str, str], str, str, str, str]:
    """Items a and b of the first owner, item c of a second owner; all carry tags."""
    other = add_user(settings, "other")
    a = text(client, headers, "Eins")
    b = text(client, headers, "Zwei")
    c = client.post("/api/v1/capture/text", json={"content": "Drei"}, headers=other).json()["id"]
    patch(client, headers, a, tags=["alpha"])
    patch(client, headers, b, tags=["beta"])
    patch(client, other, c, tags=["alpha"])
    return other, a, b, c, "alpha"


def test_direct_updates_of_an_assignment_reindex_old_and_new_items(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    other, a, b, c, _ = _two_owner_setup(settings, client, headers)
    with connection(settings) as conn:
        beta = conn.execute("SELECT id FROM tag WHERE name = 'beta'").fetchone()
        assert beta is not None

    # Moving an assignment to another item: the previous and the new item both follow.
    with connection(settings) as conn:
        conn.execute("UPDATE item_tag SET item_id = %s WHERE item_id = %s", (b, a))
    assert found(client, headers, "alpha") == [b]
    assert fields(client, headers, "alpha") == {b: ["tag"]}
    assert found(client, headers, "beta") == [b]

    # Pointing an assignment at another tag: the old name stops matching, the new one matches.
    patch(client, headers, a, tags=["gamma"])
    assert found(client, headers, "gamma") == [a]
    with connection(settings) as conn:
        conn.execute("UPDATE item_tag SET tag_id = %s WHERE item_id = %s", (beta["id"], a))
    assert found(client, headers, "gamma") == []
    assert fields(client, headers, "beta") == {a: ["tag"], b: ["tag"]}

    # Several assignments of two items swapped in one statement.
    patch(client, headers, a, tags=["alpha", "delta"])
    patch(client, headers, b, tags=["beta", "epsilon"])
    with connection(settings) as conn:
        conn.execute(
            "UPDATE item_tag SET item_id = CASE WHEN item_id = %s THEN %s::uuid ELSE %s::uuid END "
            "WHERE item_id IN (%s, %s)",
            (a, b, a, a, b),
        )
    assert found(client, headers, "alpha") == [b] and found(client, headers, "delta") == [b]
    assert found(client, headers, "beta") == [a] and found(client, headers, "epsilon") == [a]

    # The other owner's index never changes, nor is it consulted.
    assert found(client, other, "alpha") == [c]
    assert found(client, other, "beta") == [] and found(client, other, "delta") == []


def test_unrelated_assignment_updates_do_not_reindex(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    other, a, b, c, _ = _two_owner_setup(settings, client, headers)
    sentinel = "'sentinel':1"
    with connection(settings) as conn:
        conn.execute("UPDATE item SET search_vector = %s::tsvector", (sentinel,))
        # Provenance, confidence and timestamps are not searchable.
        conn.execute("UPDATE item_tag SET created_by = 'ai', confidence = 0.3, created_at = now()")
        # A statement that changes no row.
        conn.execute("UPDATE item_tag SET tag_id = tag_id")
        assert conn.execute(
            "SELECT count(*) AS n FROM item WHERE search_vector = %s::tsvector", (sentinel,)
        ).fetchone() == {"n": 3}
        # A real move refreshes exactly the two items involved, not the bystanders.
        beta = conn.execute("SELECT id FROM tag WHERE name = 'beta'").fetchone()
        assert beta is not None
        conn.execute("UPDATE item_tag SET tag_id = %s WHERE item_id = %s", (beta["id"], a))
        stale = conn.execute(
            "SELECT id FROM item WHERE search_vector = %s::tsvector", (sentinel,)
        ).fetchall()
    assert {str(row["id"]) for row in stale} == {b, c}
    assert found(client, headers, "beta") == [a]


def test_assignment_updates_are_atomic_and_serialize_with_concurrent_changes(
    settings: Settings, client: TestClient, headers: dict[str, str]
) -> None:
    _, a, b, _, _ = _two_owner_setup(settings, client, headers)
    url = settings.database_url.get_secret_value()
    with connection(settings) as conn:
        beta = conn.execute("SELECT id FROM tag WHERE name = 'beta'").fetchone()
        owner = conn.execute("SELECT owner_id FROM item WHERE id = %s", (a,)).fetchone()
        assert beta is not None and owner is not None
        conn.execute(
            "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, 'delta')",
            (delta := uuid7(), owner["owner_id"]),
        )

    # A failed statement leaves index and assignments untouched.
    patch(client, headers, a, tags=["alpha", "beta"])
    with connection(settings) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "UPDATE item_tag SET tag_id = %s WHERE item_id = %s AND tag_id <> %s",
            (beta["id"], a, beta["id"]),
        )
    assert found(client, headers, "alpha") == [a]
    assert set(found(client, headers, "beta")) == {a, b}
    patch(client, headers, a, tags=["alpha"])
    # A rolled-back update leaves the index as it was.
    with psycopg.connect(url) as raw:
        raw.execute("UPDATE item_tag SET item_id = %s WHERE item_id = %s", (b, a))
        raw.rollback()
    assert found(client, headers, "alpha") == [a] and found(client, headers, "beta") == [b]

    # An uncommitted move of a's assignment holds a; a concurrent change on a waits and is
    # applied on top, so no tag is lost.
    mover_has_lock = threading.Event()
    release = threading.Event()

    def move_alpha_of_a_to_delta() -> None:
        with psycopg.connect(url) as conn:
            conn.execute("UPDATE item_tag SET tag_id = %s WHERE item_id = %s", (delta, a))
            mover_has_lock.set()
            release.wait(10)

    def add_epsilon_to_a() -> None:
        with psycopg.connect(url) as conn:
            tag_id = uuid7()
            conn.execute(
                "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, 'epsilon')",
                (tag_id, owner["owner_id"]),
            )
            conn.execute(
                "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) "
                "VALUES (%s, %s, %s, %s, 'user')",
                (uuid7(), owner["owner_id"], a, tag_id),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        mover = pool.submit(move_alpha_of_a_to_delta)
        assert mover_has_lock.wait(10)
        adder = pool.submit(add_epsilon_to_a)
        time.sleep(0.5)
        assert not adder.done(), "the concurrent change must wait for the item row"
        release.set()
        mover.result(10)
        adder.result(10)
    assert found(client, headers, "delta") == [a]
    assert found(client, headers, "epsilon") == [a]
    assert found(client, headers, "alpha") == []
    assert found(client, headers, "beta") == [b]
    assert found(client, headers, "delta epsilon") == [a]

    # Two statements moving assignments in opposite directions never deadlock, whichever
    # way they interleave, and both end up reflected.
    with connection(settings) as conn:
        conn.execute("DELETE FROM item_tag")
        conn.execute(
            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) VALUES "
            "(%s, %s, %s, %s, 'user'), (%s, %s, %s, %s, 'user')",
            (uuid7(), owner["owner_id"], a, beta["id"], uuid7(), owner["owner_id"], b, delta),
        )

    def swap(source: str, target: str) -> None:
        with psycopg.connect(url) as conn:
            conn.execute("SET lock_timeout = '5s'")
            for _ in range(20):
                conn.execute(
                    "UPDATE item_tag SET item_id = %s WHERE item_id = %s AND "
                    "NOT EXISTS (SELECT 1 FROM item_tag x WHERE x.item_id = %s "
                    "AND x.tag_id = item_tag.tag_id)",
                    (target, source, target),
                )
                conn.commit()
                source, target = target, source

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(swap, str(a), str(b)), pool.submit(swap, str(b), str(a))]:
            future.result(60)
    with connection(settings) as conn:
        truth = {
            (str(row["item_id"]), row["name"])
            for row in conn.execute(
                "SELECT it.item_id, t.name FROM item_tag it JOIN tag t ON t.id = it.tag_id"
            ).fetchall()
        }
    for name in ("beta", "delta"):
        assert set(found(client, headers, name)) == {item for item, tag in truth if tag == name}


def test_migration_installs_and_removes_every_search_trigger(settings: Settings) -> None:
    config = migration_config()
    names = {
        "item_search_before",
        "item_tag_search_insert",
        "item_tag_search_update",
        "item_tag_search_delete",
        "tag_search_renamed",
    }

    def installed() -> set[str]:
        with connection(settings) as conn:
            rows = conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
            functions = conn.execute(
                "SELECT count(*) AS n FROM pg_proc WHERE proname LIKE 'bag\\_%' "
                "OR proname LIKE 'item\\_%search%' OR proname = 'tag_search_renamed'"
            ).fetchone()
        assert functions is not None
        return {row["tgname"] for row in rows} | ({"functions"} if functions["n"] else set())

    assert installed() == names | {"functions"}
    command.downgrade(config, "0004_sessions")
    try:
        assert installed() == set()
    finally:
        command.upgrade(config, "head")
    assert installed() == names | {"functions"}


def test_import_rebuilds_the_search_index(
    settings: Settings, client: TestClient, headers: dict[str, str], tmp_path: Path
) -> None:
    item = upload(client, headers, "Mietvertrag-2026.pdf")
    patch(client, headers, item, tags=["Wohnung"])
    storage = FileSystemStorage(settings.storage_path)
    export_bag(settings, storage, tmp_path / "export")
    manifest = json.loads((tmp_path / "export" / "manifest.json").read_text())
    assert manifest["format"] == "bag-export"
    with connection(settings) as conn:
        owner = conn.execute("SELECT owner_id FROM item").fetchone()
        assert owner is not None
        conn.execute("DELETE FROM job; DELETE FROM processing_run; DELETE FROM relation")
        conn.execute(
            "DELETE FROM item_tag; DELETE FROM item_collection; DELETE FROM blob; DELETE FROM item"
        )
    assert found(client, headers, "mietvertrag") == []
    import_bag(settings, storage, tmp_path / "export", UUID(str(owner["owner_id"])))
    assert fields(client, headers, "mietvertrag") == {item: ["filename"]}
    assert fields(client, headers, "wohnung") == {item: ["tag"]}
