CREATE TABLE job (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES "user" (id),
    item_id uuid NOT NULL,
    processor text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts >= 1),
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    worker_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, item_id) REFERENCES item (owner_id, id),
    CHECK ((status = 'running') = (lease_expires_at IS NOT NULL))
);
CREATE INDEX job_queued_idx ON job (run_after, id) WHERE status = 'queued';
CREATE INDEX job_running_idx ON job (lease_expires_at, id) WHERE status = 'running';
CREATE INDEX job_item_idx ON job (owner_id, item_id);
