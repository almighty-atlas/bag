"""Synthetic search and tag re-indexing benchmark for a disposable database.

Seeds one owner with N items (about 10 KB of extracted text each, German and English, files,
texts and URLs, 300 tags), then times typical searches and a tag rename/delete that touches
many items. Figures depend on the host; read them as ratios between queries and versions.

    uv run python scripts/search_bench.py postgresql://bag:bag@127.0.0.1:5432/bag_bench \\
        --items 30000

The database name must end in _bench or _test. The script migrates it to head, replaces the
benchmark owner's data on --seed (the default when the owner has no items) and never touches
any other owner.
"""

import argparse
import os
import random
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import psycopg
from alembic import command
from bag.cli import migration_config
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.search import search_items
from pydantic import SecretStr

OWNER_NAME = "search-bench"
SYLLABLES = ["ka", "lo", "mi", "ra", "te", "su", "no", "vi", "pe", "da", "bo", "ni", "re", "chu"]
# Common words occur in a large share of the texts: whole-word queries of them rank thousands.
COMMON = {"rechnung": 0.4, "projekt": 0.2, "invoice": 0.15, "müller": 0.05, "café": 0.03}
QUERIES = [
    "rechnung",
    "projekt",
    "invoice",
    "rechnung projekt",
    "rech",
    "abrech",
    "da",
    "31",
    "109",
    "IMG_3109",
    "mg_310",
    "urlaub",
    "example",
    "cafe",
    "mueller",
    "regal",
    "zzzzqq",
]


def vocabulary(rng: random.Random, size: int) -> list[str]:
    words = set()
    while len(words) < size:
        words.add("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 4))))
    return sorted(words)


def body(rng: random.Random, words: list[str], weights: list[float]) -> str:
    chosen = rng.choices(words, weights=weights, k=1400)
    for word, share in COMMON.items():
        if rng.random() < share:
            chosen[rng.randrange(len(chosen))] = word
    return " ".join(chosen)


@contextmanager
def timed(label: str, results: dict[str, float]) -> Iterator[None]:
    start = time.perf_counter()
    yield
    results[label] = (time.perf_counter() - start) * 1000


def owner_id(settings: Settings) -> UUID:
    with connection(settings) as conn:
        row = conn.execute(
            'SELECT id FROM "user" WHERE display_name = %s', (OWNER_NAME,)
        ).fetchone()
        if row is not None:
            return UUID(str(row["id"]))
        owner = uuid7()
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (owner, OWNER_NAME))
        return owner


def seed(url: str, owner: UUID, count: int, rng: random.Random) -> None:
    words = vocabulary(rng, 6000)
    weights = [1 / (rank + 1) for rank in range(len(words))]
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute("DELETE FROM item_tag WHERE owner_id = %s", (owner,))
        conn.execute("DELETE FROM tag WHERE owner_id = %s", (owner,))
        conn.execute("DELETE FROM item WHERE owner_id = %s", (owner,))
        tags = [(uuid7(), f"{rng.choice(words).capitalize()}{index}") for index in range(299)]
        tags.append((uuid7(), "Bücherregal"))
        conn.cursor().executemany(
            "INSERT INTO tag (id, owner_id, name) VALUES (%s, %s, %s)",
            [(tag, owner, name) for tag, name in tags],
        )
        batch: list[tuple[Any, ...]] = []
        links: list[tuple[Any, ...]] = []
        for index in range(count):
            kind = ("file", "text", "url")[index % 3]
            name = url_value = title = None
            if kind == "file":
                name = f"{rng.choice(words)}_{index:05d}.pdf"
            elif kind == "url":
                url_value = f"https://{rng.choice(words)}.example.org/{rng.choice(words)}-{index}"
            if index % 4 == 0:
                title = f"{rng.choice(words).capitalize()} {rng.choice(words)} {index}"
            item = uuid7()
            batch.append(
                (
                    item,
                    owner,
                    kind,
                    name,
                    url_value,
                    title,
                    body(rng, words, weights),
                    "de" if index % 2 else "en",
                )
            )
            # The first tag is carried by 5,000 items; the others by a few dozen each.
            if index < 5000:
                links.append((uuid7(), owner, item, tags[0][0]))
            if index % 7 == 0:
                links.append((uuid7(), owner, item, tags[1 + index % 298][0]))
            if index % 9000 == 0:
                links.append((uuid7(), owner, item, tags[-1][0]))
            if len(batch) == 1000:
                insert_items(conn, batch)
                batch = []
                print(f"  seeded {index + 1} items", flush=True)
        insert_items(conn, batch)
        conn.execute(
            "INSERT INTO item (id, owner_id, kind, source, original_filename, mime_type, "
            "processing_status) VALUES (%s, %s, 'file', 'bench', 'IMG_3109.webp', 'image/webp', "
            "'ready')",
            (uuid7(), owner),
        )
        conn.cursor().executemany(
            "INSERT INTO item_tag (id, owner_id, item_id, tag_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'user')",
            links,
        )
        conn.commit()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("VACUUM ANALYZE item")
        conn.execute("VACUUM ANALYZE item_tag")


def insert_items(conn: psycopg.Connection[Any], rows: list[tuple[Any, ...]]) -> None:
    conn.cursor().executemany(
        "INSERT INTO item (id, owner_id, kind, source, original_filename, source_url, title, "
        "extracted_text, language, mime_type, processing_status) "
        "VALUES (%s, %s, %s, 'bench', %s, %s, %s, %s, %s, 'application/pdf', 'ready')",
        rows,
    )


def bench_search(settings: Settings, owner: UUID, repeat: int) -> None:
    print(f"{'query':<22}{'hits':>6}{'median ms':>12}{'max ms':>10}")
    for query in QUERIES:
        durations = []
        hits = 0
        for _ in range(repeat):
            start = time.perf_counter()
            page = search_items(settings, owner, q=query, limit=50, offset=0)
            durations.append((time.perf_counter() - start) * 1000)
            hits = len(page.results)
        print(f"{query:<22}{hits:>6}{statistics.median(durations):>12.0f}{max(durations):>10.0f}")


def bench_tags(url: str, owner: UUID) -> None:
    with psycopg.connect(url, autocommit=False) as conn:
        row = conn.execute(
            "SELECT t.id, t.name, count(*) AS n FROM tag t JOIN item_tag it ON "
            "(it.owner_id = t.owner_id AND it.tag_id = t.id) WHERE t.owner_id = %s "
            "GROUP BY t.id ORDER BY n DESC LIMIT 1",
            (owner,),
        ).fetchone()
        assert row is not None
        tag, name, items = row
        results: dict[str, float] = {}
        with timed("rename", results):
            conn.execute("UPDATE tag SET name = name || 'x' WHERE id = %s", (tag,))
        with timed("delete assignments", results):
            conn.execute("DELETE FROM item_tag WHERE owner_id = %s AND tag_id = %s", (owner, tag))
        conn.rollback()
    timings = ", ".join(f"{k} {v:.0f} ms" for k, v in results.items())
    print(f"tag {name!r} on {items} items: {timings}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database_url")
    parser.add_argument("--items", type=int, default=30000)
    parser.add_argument("--seed", action="store_true", help="re-seed even if items exist")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--no-tags", action="store_true", help="skip the tag re-index timing")
    args = parser.parse_args()
    name = urlparse(args.database_url).path
    if not (name.endswith("_bench") or name.endswith("_test")):
        parser.error("the database name must end in _bench or _test")
    os.environ["BAG_DATABASE_URL"] = args.database_url
    command.upgrade(migration_config(), "head")
    settings = Settings(database_url=SecretStr(args.database_url))
    owner = owner_id(settings)
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM item WHERE owner_id = %s", (owner,)
        ).fetchone()
    if args.seed or not row or row["n"] == 0:
        print(f"seeding {args.items} items")
        seed(args.database_url, owner, args.items, random.Random(20261121))
    bench_search(settings, owner, args.repeat)
    if not args.no_tags:
        bench_tags(args.database_url, owner)


if __name__ == "__main__":
    main()
