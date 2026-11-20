-- Search follow-ups (ADR-0026).
--
-- 1. Accent folding: titles, filenames, tags, notes and URLs are also indexed without accents
--    ('Café' is found by 'cafe', 'Crème' by 'creme'), next to the forms indexed before.
-- 2. URL words: the host and path of a URL are also indexed as single words
--    ('budget.example.org/x-y' is found by 'example' and by 'x').
-- 3. item.search_names holds the folded names of an item (filename, title, tags) for the
--    substring path; its trigram index replaces the filename-only one of 0006, so 'regal'
--    finds the tag 'Bücherregal' and 'müller' the file 'mueller.pdf'.
-- 4. search_vector puts the extracted text first and the short fields after it. A tag change
--    then keeps the stored text part (ts_filter weight D) and only rebuilds the rest, instead
--    of parsing every tagged document again.

CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() itself is only STABLE (its dictionary could change); with a fixed dictionary the
-- result is fixed, which the stored vectors and the trigram index rely on.
DO $$
DECLARE
    schema_name text;
BEGIN
    SELECT extnamespace::regnamespace::text INTO schema_name
    FROM pg_extension WHERE extname = 'unaccent';
    EXECUTE 'CREATE FUNCTION bag_fold(value text) RETURNS text '
        'LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $f$ SELECT '
        || quote_ident(schema_name) || '.unaccent('
        || quote_literal(quote_ident(schema_name) || '.unaccent') || '::regdictionary, value) $f$';
END
$$;

-- German transliteration as in 0005 and 0006: 'Müller' -> 'Mueller', 'Straße' -> 'Strasse'.
CREATE FUNCTION bag_translit(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT replace(replace(replace(replace(replace(replace(replace(value,
        'ä', 'ae'), 'ö', 'oe'), 'ü', 'ue'), 'Ä', 'Ae'), 'Ö', 'Oe'), 'Ü', 'Ue'), 'ß', 'ss')
$$;

-- A name as the substring path sees it: lowercased and folded ('müller' -> 'muller'), and after
-- a newline lowercased, transliterated and folded ('mueller'). Queries are compared in the
-- second form (bag_like_key), so 'müller', 'mueller' and 'muller' all find 'Müller.pdf', and
-- 'müller' finds 'mueller.pdf'.
CREATE FUNCTION bag_name_key(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT bag_fold(lower(value)) || E'\n' || bag_fold(bag_translit(lower(value)))
$$;

CREATE FUNCTION bag_like_key(pattern text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT bag_fold(bag_translit(lower(pattern)))
$$;

-- The names of one item, one key per line: filename, title, then each tag.
CREATE FUNCTION bag_item_names(p_owner uuid, p_item uuid, filename text, title text)
RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT concat_ws(E'\n', bag_name_key(filename), bag_name_key(title), (
        SELECT string_agg(bag_name_key(t.name), E'\n' ORDER BY t.name)
        FROM item_tag it JOIN tag t ON (t.owner_id = it.owner_id AND t.id = it.tag_id)
        WHERE it.owner_id = p_owner AND it.item_id = p_item))
$$;

-- Lexemes of `extra` that `base` does not have yet, with the weight given; adding only new
-- lexemes keeps the term frequencies, and so the ranking, of unaccented text unchanged.
CREATE FUNCTION bag_new_lexemes(base tsvector, extra tsvector, weight "char") RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(setweight(extra, weight), tsvector_to_array(base))
$$;

-- A short field (title, note): the text vector of 0005 plus its accent-free words.
CREATE FUNCTION bag_field_vector(value text, config regconfig, weight "char") RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN bag_fold(coalesce(value, '')) = coalesce(value, '') THEN v.base
        ELSE ts_delete(v.base || '''~'':20'::tsvector || bag_new_lexemes(
            v.base, to_tsvector('simple', bag_fold(value)), weight), '~') END
    FROM (SELECT bag_text_vector(value, config, weight) AS base) v
$$;

-- A URL: the field vector (the whole URL, host and path as the parser sees them) plus every
-- word of host, path and query, without the scheme and a leading 'www.'.
CREATE FUNCTION bag_url_vector(value text, config regconfig) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(v.base || '''~'':20'::tsvector || bag_new_lexemes(v.base, to_tsvector(
        'simple', regexp_replace(regexp_replace(bag_fold(lower(coalesce(value, ''))),
            '^([a-z][a-z0-9+.-]*://)?(www\.)?', ''), '[^[:alnum:]]+', ' ', 'g')), 'C'), '~')
    FROM (SELECT bag_field_vector(value, config, 'C') AS base) v
$$;

-- Names (filenames, tags) as in 0005, plus the accent-free form when it differs.
CREATE OR REPLACE FUNCTION bag_name_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(value, '') || ' ' || s.split || ' ' || bag_translit(s.split) ||
        CASE WHEN bag_fold(s.split) <> s.split THEN ' ' || bag_fold(s.split) ELSE '' END
    FROM (
        SELECT regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(coalesce(value, ''), '([a-zäöüß])([A-ZÄÖÜ])', '\1 \2', 'g'),
                    '([a-zA-ZäöüÄÖÜß])([0-9])', '\1 \2', 'g'),
                '([0-9])([a-zA-ZäöüÄÖÜß])', '\1 \2', 'g'),
            '[[:punct:][:space:]]+', ' ', 'g') AS split
    ) s
$$;

CREATE OR REPLACE FUNCTION bag_head_vector(
    title text, filename text, tags tsvector, note text, url text, language text
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(
        bag_field_vector(title, bag_language_config(language), 'A') || '''~'':20'::tsvector ||
        bag_name_vector(filename, 'A') || '''~'':20'::tsvector ||
        tags || '''~'':20'::tsvector ||
        bag_field_vector(note, bag_language_config(language), 'C') || '''~'':20'::tsvector ||
        bag_url_vector(url, bag_language_config(language)),
        '~')
$$;

-- The full vector: the extracted text (D) first, then the head. Because the text comes first
-- its positions never depend on the other fields, so ts_filter(search_vector, '{d}') is exactly
-- the text part and bag_join_vectors(that, new head) equals a full rebuild.
CREATE FUNCTION bag_join_vectors(body tsvector, head tsvector) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(body || '''~'':20'::tsvector || head, '~')
$$;

CREATE OR REPLACE FUNCTION bag_item_vector(
    title text, filename text, tags tsvector, note text, url text, body text, language text,
    with_body boolean
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT bag_join_vectors(
        CASE WHEN with_body
            THEN bag_text_vector(body, bag_language_config(language), 'D')
            ELSE ''::tsvector END,
        bag_head_vector(title, filename, tags, note, url, language))
$$;

ALTER TABLE item ADD COLUMN search_names text;

-- After a tag change only the tags differ: the stored text part is kept, the rest rebuilt.
-- This costs a fraction of parsing the text again, so renaming a tag on thousands of
-- documents fits in a request. A vector that would exceed the PostgreSQL limit drops the
-- extracted text, as before.
CREATE OR REPLACE FUNCTION bag_refresh_item_search(p_owner uuid, p_item uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    tags tsvector;
    head tsvector;
    body tsvector;
    has_text boolean;
BEGIN
    PERFORM 1 FROM item WHERE owner_id = p_owner AND id = p_item FOR NO KEY UPDATE;
    tags := bag_item_tags_vector(p_owner, p_item);
    SELECT bag_head_vector(title, original_filename, tags, user_note, source_url, language),
        ts_filter(coalesce(search_vector, ''::tsvector), '{d}'),
        coalesce(extracted_text, '') <> ''
    INTO head, body, has_text FROM item WHERE owner_id = p_owner AND id = p_item;
    -- No stored text part although there is text: the vector was too large before (or is
    -- missing). Parse the text again, so removing tags can bring it back.
    IF body = ''::tsvector AND has_text THEN
        SELECT bag_text_vector(extracted_text, bag_language_config(language), 'D')
        INTO body FROM item WHERE owner_id = p_owner AND id = p_item;
    END IF;
    BEGIN
        UPDATE item SET
            search_head = head,
            search_names = bag_item_names(owner_id, id, original_filename, title),
            search_vector = bag_join_vectors(body, head)
        WHERE owner_id = p_owner AND id = p_item;
    EXCEPTION WHEN program_limit_exceeded THEN
        UPDATE item SET
            search_head = head,
            search_names = bag_item_names(owner_id, id, original_filename, title),
            search_vector = bag_join_vectors(''::tsvector, head)
        WHERE owner_id = p_owner AND id = p_item;
    END;
END
$$;

CREATE OR REPLACE FUNCTION item_search_before() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tags tsvector := ''::tsvector;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        tags := bag_item_tags_vector(NEW.owner_id, NEW.id);
    END IF;
    NEW.search_head := bag_head_vector(
        NEW.title, NEW.original_filename, tags, NEW.user_note, NEW.source_url, NEW.language);
    NEW.search_names := bag_item_names(NEW.owner_id, NEW.id, NEW.original_filename, NEW.title);
    BEGIN
        NEW.search_vector := bag_item_vector(
            NEW.title, NEW.original_filename, tags, NEW.user_note, NEW.source_url,
            NEW.extracted_text, NEW.language, true);
    EXCEPTION WHEN program_limit_exceeded THEN
        -- New extracted text that does not fit still fails, so processing reports it.
        -- Any other edit keeps the item searchable by everything but its extracted text.
        IF TG_OP = 'UPDATE' AND OLD.extracted_text IS NOT DISTINCT FROM NEW.extracted_text THEN
            NEW.search_vector := bag_item_vector(
                NEW.title, NEW.original_filename, tags, NEW.user_note, NEW.source_url,
                NEW.extracted_text, NEW.language, false);
        ELSE
            RAISE;
        END IF;
    END;
    RETURN NEW;
END
$$;

-- Existing items, trashed ones too. Updating these columns does not fire the trigger. The
-- set-based statement is the fast path; if one vector is too large, every item is rebuilt on
-- its own and only an item whose vector does not fit loses its text part, as before.
DO $$
DECLARE
    r record;
BEGIN
    UPDATE item SET
        search_head = bag_head_vector(
            title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url,
            language),
        search_names = bag_item_names(owner_id, id, original_filename, title),
        search_vector = bag_item_vector(
            title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url,
            extracted_text, language, true);
EXCEPTION WHEN program_limit_exceeded THEN
    FOR r IN SELECT owner_id, id FROM item LOOP
        UPDATE item SET
            search_head = bag_head_vector(
                title, original_filename, bag_item_tags_vector(owner_id, id), user_note,
                source_url, language),
            search_names = bag_item_names(owner_id, id, original_filename, title)
        WHERE owner_id = r.owner_id AND id = r.id;
        BEGIN
            UPDATE item SET search_vector = bag_item_vector(
                title, original_filename, bag_item_tags_vector(owner_id, id), user_note,
                source_url, extracted_text, language, true)
            WHERE owner_id = r.owner_id AND id = r.id;
        EXCEPTION WHEN program_limit_exceeded THEN
            UPDATE item SET search_vector = bag_join_vectors(''::tsvector, search_head)
            WHERE owner_id = r.owner_id AND id = r.id;
        END;
    END LOOP;
END
$$;

DROP INDEX item_filename_trgm_idx;
CREATE INDEX item_names_trgm_idx ON item USING gin (search_names gin_trgm_ops);
