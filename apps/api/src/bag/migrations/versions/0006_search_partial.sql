-- Prefix and substring search.
--
-- 1. item.search_head is a small second vector with the title/filename (A), tag (B) and
--    note/URL (C) lexemes of search_vector, without the extracted text. Prefix queries scan
--    it instead of the large vector: a GIN index over a prefix has to visit every lexeme that
--    starts with it, and each hit costs a recheck of the document's vector. For the head
--    these documents are a few hundred bytes, not a PDF's text layer.
-- 2. A trigram index over the lowercased filename finds substrings of three or more
--    characters ('109' in 'IMG_3109.webp') without scanning the table.
--
-- The head is maintained exactly like search_vector: by the triggers of migration 0005.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE item ADD COLUMN search_head tsvector;

CREATE FUNCTION bag_head_vector(
    title text, filename text, tags tsvector, note text, url text, language text
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(
        bag_text_vector(title, bag_language_config(language), 'A') || '''~'':20'::tsvector ||
        bag_name_vector(filename, 'A') || '''~'':20'::tsvector ||
        tags || '''~'':20'::tsvector ||
        bag_text_vector(note, bag_language_config(language), 'C') || '''~'':20'::tsvector ||
        bag_text_vector(url, bag_language_config(language), 'C'),
        '~')
$$;

-- A filename as the text the trigram index sees: lowercase as written and, after a newline,
-- with ä ö ü ß transliterated ('Müller.pdf' is found by 'müll' and by 'ueller').
CREATE FUNCTION bag_filename_key(name text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(name) || E'\n' || lower(replace(replace(replace(replace(replace(replace(
        replace(name, 'ä', 'ae'), 'ö', 'oe'), 'ü', 'ue'), 'Ä', 'Ae'), 'Ö', 'Oe'), 'Ü', 'Ue'),
        'ß', 'ss'))
$$;

-- Turns the positive operands of a query into prefix operands. An operand becomes
-- 'word':*WEIGHTS if it has at least min_len characters and is a word the user typed, that is,
-- occurs in `typed` (the query parsed with the simple configuration): the prefix of a stem would
-- be too broad ('uebers' stems to 'ueb'). Other operands only get the weights. With keep_exact the
-- plain operand stays an alternative, so 'word' | 'word':*ABC. Negated operands, alone or in a
-- negated group, are left alone: '-urlaub' excludes the word, not everything starting with it.
CREATE FUNCTION bag_prefix_query(
    input tsquery, typed tsquery, min_len integer, weights text, keep_exact boolean
) RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    token text;
    result text := '';
    lexeme text;
    marked text;
    pending_negation boolean := false;
    depth integer := 0;
    negated_depth integer;
    typed_tokens text[];
BEGIN
    IF input::text = '' THEN
        RETURN input;
    END IF;
    SELECT coalesce(array_agg(m.parts[1]), '{}') INTO typed_tokens
    FROM regexp_matches(typed::text, '''(?:[^'']|'''')*''', 'g') AS m(parts);
    FOR token IN
        SELECT m.parts[1]
        FROM regexp_matches(input::text, '(''(?:[^'']|'''')*''|[!()]|[^''!()]+)', 'g')
            WITH ORDINALITY AS m(parts, position)
        ORDER BY m.position
    LOOP
        IF token = '!' THEN
            pending_negation := true;
            result := result || token;
        ELSIF token = '(' THEN
            depth := depth + 1;
            IF pending_negation AND negated_depth IS NULL THEN
                negated_depth := depth;
            END IF;
            pending_negation := false;
            result := result || token;
        ELSIF token = ')' THEN
            IF negated_depth = depth THEN
                negated_depth := NULL;
            END IF;
            depth := depth - 1;
            result := result || token;
        ELSIF left(token, 1) = '''' THEN
            IF pending_negation OR negated_depth IS NOT NULL THEN
                pending_negation := false;
                result := result || token;
            ELSE
                lexeme := replace(substr(token, 2, length(token) - 2), '''''', '''');
                IF char_length(lexeme) >= min_len AND token = ANY (typed_tokens) THEN
                    marked := token || ':*' || weights;
                    IF keep_exact THEN
                        marked := '(' || token || ' | ' || marked || ')';
                    END IF;
                ELSIF weights <> '' AND NOT keep_exact THEN
                    marked := token || ':' || weights;
                ELSE
                    marked := token;
                END IF;
                result := result || marked;
            END IF;
        ELSE
            result := result || token;
        END IF;
    END LOOP;
    RETURN result::tsquery;
END
$$;

-- The same refresh and trigger functions as in 0005, now also keeping search_head.
CREATE OR REPLACE FUNCTION bag_refresh_item_search(p_owner uuid, p_item uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    tags tsvector;
BEGIN
    PERFORM 1 FROM item WHERE owner_id = p_owner AND id = p_item FOR NO KEY UPDATE;
    tags := bag_item_tags_vector(p_owner, p_item);
    BEGIN
        UPDATE item SET
            search_vector = bag_item_vector(
                title, original_filename, tags, user_note, source_url, extracted_text, language,
                true),
            search_head = bag_head_vector(
                title, original_filename, tags, user_note, source_url, language)
        WHERE owner_id = p_owner AND id = p_item;
    EXCEPTION WHEN program_limit_exceeded THEN
        UPDATE item SET
            search_vector = bag_item_vector(
                title, original_filename, tags, user_note, source_url, extracted_text, language,
                false),
            search_head = bag_head_vector(
                title, original_filename, tags, user_note, source_url, language)
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

-- Existing items (trashed ones too). Updating search_head alone does not fire the trigger.
UPDATE item SET search_head = bag_head_vector(
    title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url, language);

CREATE INDEX item_search_head_idx ON item USING gin (search_head);
CREATE INDEX item_filename_trgm_idx ON item
    USING gin (bag_filename_key(original_filename) gin_trgm_ops);
