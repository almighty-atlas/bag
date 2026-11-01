-- Restores the refresh and trigger functions of migration 0005 (without search_head).

-- Recompute one item after its tags changed. The row lock makes concurrent tag changes on
-- the same item queue up, so the tag list read below is never stale. It is FOR NO KEY
-- UPDATE, not FOR UPDATE: the foreign-key checks of concurrent item_tag writes hold FOR KEY
-- SHARE on the item until commit, and FOR UPDATE would deadlock with them. A vector that would
-- exceed the PostgreSQL limit drops the extracted text instead of failing the tag change.
CREATE OR REPLACE FUNCTION bag_refresh_item_search(p_owner uuid, p_item uuid) RETURNS void
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

CREATE OR REPLACE FUNCTION item_search_before() RETURNS trigger
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
