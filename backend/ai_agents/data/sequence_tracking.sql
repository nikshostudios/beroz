-- Sequence tracking — opens, clicks, bounces, intent, signatures, unsubscribes.
--
-- Layered on top of sequences_v2.sql. Adds the instrumentation that lets the
-- redesigned sequences UI display real Opened / Clicked / Replied / Interested
-- / Bounced numbers, plus a per-user signature library and an unsubscribe
-- flow.
--
-- Apply with:
--     python backend/ai_agents/data/apply_schema.py sequence_tracking.sql

-- 1. Tracking token on outreach_log for per-send pixel + click attribution.
alter table outreach_log
  add column if not exists tracking_token uuid unique default gen_random_uuid();

create index if not exists idx_outreach_log_tracking_token
  on outreach_log(tracking_token);

-- 2. Reply intent classification on sequence_runs.
alter table sequence_runs
  add column if not exists intent text
    check (intent in ('interested','not_interested','out_of_office','other')),
  add column if not exists intent_confidence numeric;

create index if not exists idx_sequence_runs_intent on sequence_runs(intent);

-- 3. Extend sequence_run_events to carry interested + unsubscribed events.
do $$
begin
  alter table sequence_run_events drop constraint if exists sequence_run_events_event_type_check;
  alter table sequence_run_events
    add constraint sequence_run_events_event_type_check
    check (event_type in (
      'scheduled','sent','opened','clicked','replied',
      'bounced','failed','interested','unsubscribed'
    ));
end $$;

create index if not exists idx_run_events_run_type
  on sequence_run_events(run_id, event_type);

-- 4. Per-user signature library.
create table if not exists user_signatures (
  id uuid primary key default gen_random_uuid(),
  user_email text not null,
  name text not null,
  html_body text not null,
  is_default boolean default false,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_user_signatures_user
  on user_signatures(user_email);

create unique index if not exists uniq_user_default_signature
  on user_signatures(user_email)
  where is_default = true;

-- 5. Email unsubscribe ledger — global suppression list (per recipient email).
create table if not exists email_unsubscribes (
  email text primary key,
  unsubscribed_at timestamptz default now(),
  source text,                 -- e.g. 'link_click', 'manual', 'bounce'
  sequence_run_id uuid references sequence_runs(id) on delete set null,
  metadata jsonb default '{}'::jsonb
);

-- 6. Step-level signature + unsubscribe footer toggle.
alter table sequence_steps
  add column if not exists signature_id uuid references user_signatures(id) on delete set null,
  add column if not exists include_unsubscribe boolean default false;

-- 7. Skipped-send reasons surfaced in error_message but allow new statuses
--    via skipped reason in metadata. (No status enum change — 'skipped' is
--    already valid in sequence_step_sends.)

-- 8. Pin / star flags on sequences (drives row sort + star icon in list).
alter table sequences
  add column if not exists is_pinned boolean default false,
  add column if not exists is_starred boolean default false,
  add column if not exists pinned_at timestamptz;

create index if not exists idx_sequences_pinned
  on sequences(is_pinned) where is_pinned = true;

alter table user_signatures enable row level security;
alter table email_unsubscribes enable row level security;
