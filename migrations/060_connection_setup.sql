-- Continuing permission is recorded by authenticated setup, never by a device boolean.
ALTER TABLE integration_auth_attempts ADD COLUMN connection_setup jsonb;
ALTER TABLE connection_collection_jobs ADD COLUMN credential_generation text;
ALTER TABLE connection_collection_jobs ADD COLUMN collection_started_at timestamptz;
ALTER TABLE connection_collection_jobs ADD COLUMN waiting_deadline timestamptz;
