CREATE TABLE "user" (
    id uuid PRIMARY KEY,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_token (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    name text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX api_token_owner_idx ON api_token (owner_id);

CREATE TABLE item (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    client_capture_id uuid,
    kind text NOT NULL,
    source text NOT NULL,
    source_application text,
    source_url text,
    mime_type text,
    language text,
    original_filename text,
    title text,
    user_note text,
    content text,
    extracted_text text,
    content_hash text,
    processing_status text NOT NULL CHECK (
        processing_status IN ('queued', 'processing', 'ready', 'partial', 'failed')
    ),
    metadata jsonb NOT NULL DEFAULT '{}',
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig,
            coalesce(title, '') || ' ' || coalesce(extracted_text, '') || ' ' ||
            coalesce(source_url, '') || ' ' || coalesce(user_note, '')) ||
        to_tsvector(CASE language WHEN 'de' THEN 'german'::regconfig
            WHEN 'en' THEN 'english'::regconfig ELSE 'simple'::regconfig END,
            coalesce(title, '') || ' ' || coalesce(extracted_text, '') || ' ' ||
            coalesce(source_url, '') || ' ' || coalesce(user_note, ''))
    ) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    captured_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (owner_id, id),
    UNIQUE (owner_id, client_capture_id)
);
CREATE INDEX item_recent_idx ON item (owner_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX item_hash_idx ON item (owner_id, content_hash) WHERE deleted_at IS NULL;
CREATE INDEX item_search_idx ON item USING gin (search_vector);

CREATE TABLE blob (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    item_id uuid NOT NULL,
    role text NOT NULL,
    storage_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    mime_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, item_id) REFERENCES item (owner_id, id),
    CHECK (storage_key = substr(sha256, 1, 2) || '/' || substr(sha256, 3, 2) || '/' || sha256)
);
CREATE INDEX blob_item_idx ON blob (owner_id, item_id);

CREATE TABLE processing_run (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    item_id uuid NOT NULL,
    processor text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    started_at timestamptz,
    finished_at timestamptz,
    FOREIGN KEY (owner_id, item_id) REFERENCES item (owner_id, id),
    UNIQUE (owner_id, item_id, processor)
);

CREATE TABLE tag (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, id),
    UNIQUE (owner_id, name)
);
CREATE TABLE item_tag (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    item_id uuid NOT NULL,
    tag_id uuid NOT NULL,
    created_by text NOT NULL CHECK (created_by IN ('user', 'system', 'ai')),
    confidence double precision CHECK (confidence BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, item_id) REFERENCES item (owner_id, id),
    FOREIGN KEY (owner_id, tag_id) REFERENCES tag (owner_id, id),
    UNIQUE (owner_id, item_id, tag_id)
);
CREATE TABLE collection (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, id),
    UNIQUE (owner_id, name)
);
CREATE TABLE item_collection (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    item_id uuid NOT NULL,
    collection_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, item_id) REFERENCES item (owner_id, id),
    FOREIGN KEY (owner_id, collection_id) REFERENCES collection (owner_id, id),
    UNIQUE (owner_id, item_id, collection_id)
);
CREATE TABLE relation (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    source_item_id uuid NOT NULL,
    target_item_id uuid NOT NULL,
    relation_type text NOT NULL,
    created_by text NOT NULL CHECK (created_by IN ('user', 'system', 'ai')),
    confidence double precision CHECK (confidence BETWEEN 0 AND 1),
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, source_item_id) REFERENCES item (owner_id, id),
    FOREIGN KEY (owner_id, target_item_id) REFERENCES item (owner_id, id),
    UNIQUE (owner_id, source_item_id, target_item_id, relation_type)
);
