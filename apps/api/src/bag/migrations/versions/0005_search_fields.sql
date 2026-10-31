-- Search over filenames and tags with field weights.
--
-- A generated column cannot read item_tag or tag, so search_vector becomes an ordinary
-- column. A BEFORE trigger on item and AFTER triggers on item_tag and tag keep it exact in
-- the same transaction as the change, whichever code path made it.
--
-- Weights: A title and filename, B tags, C note and URL, D extracted text.

CREATE FUNCTION bag_language_config(language text) RETURNS regconfig
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE language WHEN 'de' THEN 'german'::regconfig
        WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END
$$;

-- A filename or tag as searchable text: the original, a form split at separators and
-- camelCase and letter-digit boundaries ('rechnung-oktober_2026.pdf' -> 'rechnung oktober 2026 pdf') and a
-- German transliteration of that ('Müller' -> 'Mueller').
CREATE FUNCTION bag_name_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(value, '') || ' ' || s.split || ' ' ||
        replace(replace(replace(replace(replace(replace(replace(s.split,
            'ä', 'ae'), 'ö', 'oe'), 'ü', 'ue'), 'Ä', 'Ae'), 'Ö', 'Oe'), 'Ü', 'Ue'), 'ß', 'ss')
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

-- Fields are joined with a position gap so a phrase never spans two fields or two
-- configurations; the placeholder lexeme is removed again.
CREATE FUNCTION bag_text_vector(value text, config regconfig, weight "char") RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(setweight(
        to_tsvector('simple', coalesce(value, '')) || '''~'':20'::tsvector ||
        to_tsvector(config, coalesce(value, '')), weight), '~')
$$;

-- Names are not tied to the item's language: index them for every configuration.
CREATE FUNCTION bag_name_vector(value text, weight "char") RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(setweight(
        to_tsvector('simple', s.text) || '''~'':20'::tsvector ||
        to_tsvector('german', s.text) || '''~'':20'::tsvector ||
        to_tsvector('english', s.text), weight), '~')
    FROM (SELECT bag_name_text(value) AS text) s
$$;

CREATE FUNCTION bag_item_vector(
    title text, filename text, tags tsvector, note text, url text, body text, language text,
    with_body boolean
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ts_delete(
        bag_text_vector(title, bag_language_config(language), 'A') || '''~'':20'::tsvector ||
        bag_name_vector(filename, 'A') || '''~'':20'::tsvector ||
        tags || '''~'':20'::tsvector ||
        bag_text_vector(note, bag_language_config(language), 'C') || '''~'':20'::tsvector ||
        bag_text_vector(url, bag_language_config(language), 'C') || '''~'':20'::tsvector ||
        CASE WHEN with_body
            THEN bag_text_vector(body, bag_language_config(language), 'D')
            ELSE ''::tsvector END,
        '~')
$$;

-- The tag names of one item (user, system and AI assignments alike), each as its own
-- vector so that a phrase never spans two tags.
CREATE FUNCTION bag_item_tags_vector(p_owner uuid, p_item uuid) RETURNS tsvector
LANGUAGE plpgsql STABLE AS $$
DECLARE
    result tsvector := ''::tsvector;
    tag_name text;
BEGIN
    FOR tag_name IN
        SELECT t.name FROM item_tag it
        JOIN tag t ON (t.owner_id = it.owner_id AND t.id = it.tag_id)
        WHERE it.owner_id = p_owner AND it.item_id = p_item
        ORDER BY t.name
    LOOP
        result := result || '''~'':20'::tsvector || bag_name_vector(tag_name, 'B');
    END LOOP;
    RETURN ts_delete(result, '~');
END
$$;

-- Query terms as alternatives, without negated terms: 'a b -c' -> 'a' | 'b'. Used to
-- find which fields contain at least one of the words a result was found for.
CREATE FUNCTION bag_any_term_query(query tsquery) RETURNS tsquery
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN querytree(query) IN ('', 'T') THEN NULL
        ELSE replace(querytree(query), ' & ', ' | ')::tsquery END
$$;

-- The same query restricted to lexemes of one weight: 'a' | 'b' -> 'a':D | 'b':D.
CREATE FUNCTION bag_restrict_query(query tsquery, weight text) RETURNS tsquery
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN query IS NULL THEN NULL
        ELSE regexp_replace(query::text, '(''(?:[^'']|'''')*'')', '\1:' || weight, 'g')::tsquery
        END
$$;

-- Recompute one item after its tags changed. The row lock makes concurrent tag changes on
-- the same item queue up, so the tag list read below is never stale. It is FOR NO KEY
-- UPDATE, not FOR UPDATE: the foreign-key checks of concurrent item_tag writes hold FOR KEY
-- SHARE on the item until commit, and FOR UPDATE would deadlock with them. A vector that would
-- exceed the PostgreSQL limit drops the extracted text instead of failing the tag change.
CREATE FUNCTION bag_refresh_item_search(p_owner uuid, p_item uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    tags tsvector;
BEGIN
    PERFORM 1 FROM item WHERE owner_id = p_owner AND id = p_item FOR NO KEY UPDATE;
    tags := bag_item_tags_vector(p_owner, p_item);
    BEGIN
        UPDATE item SET search_vector = bag_item_vector(
            title, original_filename, tags, user_note, source_url, extracted_text, language, true)
        WHERE owner_id = p_owner AND id = p_item;
    EXCEPTION WHEN program_limit_exceeded THEN
        UPDATE item SET search_vector = bag_item_vector(
            title, original_filename, tags, user_note, source_url, extracted_text, language, false)
        WHERE owner_id = p_owner AND id = p_item;
    END;
END
$$;

ALTER TABLE item ALTER COLUMN search_vector DROP EXPRESSION;

-- Existing items. The set-based statement is the fast path; if one vector is too large,
-- fall back to the per-item function so the migration still completes.
DO $$
BEGIN
    UPDATE item SET search_vector = bag_item_vector(
        title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url,
        extracted_text, language, true);
EXCEPTION WHEN program_limit_exceeded THEN
    PERFORM bag_refresh_item_search(owner_id, id) FROM item;
END
$$;

CREATE FUNCTION item_search_before() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tags tsvector := ''::tsvector;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        tags := bag_item_tags_vector(NEW.owner_id, NEW.id);
    END IF;
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

CREATE TRIGGER item_search_before
BEFORE INSERT OR UPDATE OF title, original_filename, user_note, source_url, extracted_text,
    language ON item
FOR EACH ROW EXECUTE FUNCTION item_search_before();

-- Assigning or removing tags: refresh each affected item once per statement. Items are
-- locked in (owner_id, item_id) order, so statements touching several of the same items
-- cannot deadlock.
CREATE FUNCTION item_tag_search_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM bag_refresh_item_search(r.owner_id, r.item_id)
    FROM (SELECT DISTINCT owner_id, item_id FROM changed_rows ORDER BY owner_id, item_id) r;
    RETURN NULL;
END
$$;

CREATE TRIGGER item_tag_search_insert
AFTER INSERT ON item_tag
REFERENCING NEW TABLE AS changed_rows
FOR EACH STATEMENT EXECUTE FUNCTION item_tag_search_changed();

CREATE TRIGGER item_tag_search_delete
AFTER DELETE ON item_tag
REFERENCING OLD TABLE AS changed_rows
FOR EACH STATEMENT EXECUTE FUNCTION item_tag_search_changed();

-- Re-pointing an assignment at another tag or item (the application never does, direct SQL
-- may): refresh the previous and the new item of every assignment whose (owner, item, tag)
-- changed. Changes to provenance, confidence or timestamps are not searchable and refresh
-- nothing. The comparison ignores row identity, so it also covers swaps and key changes.
CREATE FUNCTION item_tag_search_updated() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM bag_refresh_item_search(r.owner_id, r.item_id)
    FROM (
        SELECT DISTINCT owner_id, item_id FROM (
            (SELECT owner_id, item_id, tag_id FROM old_rows
             EXCEPT ALL SELECT owner_id, item_id, tag_id FROM new_rows)
            UNION ALL
            (SELECT owner_id, item_id, tag_id FROM new_rows
             EXCEPT ALL SELECT owner_id, item_id, tag_id FROM old_rows)
        ) changed
        ORDER BY owner_id, item_id
    ) r;
    RETURN NULL;
END
$$;

CREATE TRIGGER item_tag_search_update
AFTER UPDATE ON item_tag
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION item_tag_search_updated();

-- Renaming a tag: refresh the items that carry it, within the tag's owner only.
CREATE FUNCTION tag_search_renamed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM bag_refresh_item_search(r.owner_id, r.item_id)
    FROM (
        SELECT DISTINCT it.owner_id, it.item_id
        FROM new_rows n
        JOIN old_rows o ON (o.id = n.id AND o.name IS DISTINCT FROM n.name)
        JOIN item_tag it ON (it.owner_id = n.owner_id AND it.tag_id = n.id)
        ORDER BY it.owner_id, it.item_id
    ) r;
    RETURN NULL;
END
$$;

CREATE TRIGGER tag_search_renamed
AFTER UPDATE ON tag
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION tag_search_renamed();
