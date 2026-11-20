-- Restores the search functions of migration 0006 (no folding, URL words or search_names)
-- and rebuilds every vector with them.

CREATE OR REPLACE FUNCTION bag_name_text(value text) RETURNS text
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

CREATE OR REPLACE FUNCTION bag_item_vector(
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

CREATE OR REPLACE FUNCTION bag_head_vector(
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

DO $$
BEGIN
    UPDATE item SET
        search_head = bag_head_vector(
            title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url,
            language),
        search_vector = bag_item_vector(
            title, original_filename, bag_item_tags_vector(owner_id, id), user_note, source_url,
            extracted_text, language, true);
EXCEPTION WHEN program_limit_exceeded THEN
    PERFORM bag_refresh_item_search(owner_id, id) FROM item;
END
$$;
