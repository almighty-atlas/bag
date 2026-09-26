CREATE UNIQUE INDEX job_active_idx ON job (owner_id, item_id, processor)
    WHERE status IN ('queued', 'running');
