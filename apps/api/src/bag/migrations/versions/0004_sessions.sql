ALTER TABLE "user" ADD COLUMN username text UNIQUE;
ALTER TABLE "user" ADD COLUMN password_hash text;

CREATE TABLE session (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX session_owner_idx ON session (owner_id);
CREATE INDEX session_expires_idx ON session (expires_at);
