-- Persist the candidate IDs sourced during a boost run so the on-demand
-- screener and outreach drafter (run later, possibly by a refresh) can
-- rebuild the same pool without re-hitting Apollo / GitHub / Apify.
--
-- Idempotent: safe to re-apply.
alter table agentic_boost_runs
  add column if not exists sourced_candidate_ids jsonb default '[]'::jsonb;
