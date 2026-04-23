-- Apollo phone reveal: pending requests table.
-- Apollo's /v1/people/match returns phone numbers asynchronously to a webhook
-- URL we pass on the request. This table correlates the async webhook callback
-- back to the original click so the frontend can poll for the result.
--
-- Apply this in the Supabase SQL Editor: paste, click Run.

CREATE TABLE IF NOT EXISTS pending_phone_reveals (
  request_id      TEXT PRIMARY KEY,
  candidate_id    UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  requested_by    UUID,
  requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  phone_number    TEXT,
  received_at     TIMESTAMPTZ,
  status          TEXT NOT NULL DEFAULT 'pending',
  webhook_payload JSONB
);

CREATE INDEX IF NOT EXISTS idx_pending_phone_reveals_candidate
  ON pending_phone_reveals(candidate_id);

CREATE INDEX IF NOT EXISTS idx_pending_phone_reveals_requested_at
  ON pending_phone_reveals(requested_at DESC);
