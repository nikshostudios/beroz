-- Sequences v2 — multi-step outbound email sequences.
--
-- Replaces the single-email Phase 4 flow that stored rows directly in
-- `outreach_log`. The old flow keeps working during rollout; new sequences
-- created through /api/sequences/* use these tables and still write one
-- `outreach_log` row per send so the inbox scanner continues to detect
-- replies unchanged.
--
-- Apply with:
--     python backend/ai_agents/data/apply_schema.py sequences_v2.sql

create table if not exists sequences (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  project_id uuid references projects(id) on delete set null,
  requirement_id uuid references requirements(id) on delete set null,
  created_by text not null,
  status text not null default 'draft'
    check (status in ('draft','active','paused','archived')),
  source text default 'ai'
    check (source in ('ai','template','scratch','clone')),
  config jsonb default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists sequence_steps (
  id uuid primary key default gen_random_uuid(),
  sequence_id uuid not null references sequences(id) on delete cascade,
  position int not null,
  step_type text not null default 'email' check (step_type in ('email','linkedin')),
  wait_days int not null default 0,
  send_time_local text default '09:00',
  timezone text default 'Asia/Kolkata',
  subject_template text,
  body_template text,
  reply_in_same_thread boolean default false,
  created_at timestamptz default now(),
  unique (sequence_id, position)
);

create table if not exists sequence_runs (
  id uuid primary key default gen_random_uuid(),
  sequence_id uuid not null references sequences(id) on delete cascade,
  candidate_id uuid not null references candidates(id) on delete cascade,
  from_email text not null,
  status text not null default 'active'
    check (status in ('active','paused','completed','replied','bounced','failed')),
  current_step_position int default 0,
  started_at timestamptz,
  next_send_at timestamptz,
  finished_at timestamptz,
  enrolled_by text not null,
  created_at timestamptz default now(),
  unique (sequence_id, candidate_id)
);

create table if not exists sequence_step_sends (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references sequence_runs(id) on delete cascade,
  step_id uuid not null references sequence_steps(id) on delete cascade,
  step_position int not null,
  outreach_log_id uuid references outreach_log(id) on delete set null,
  status text not null default 'scheduled'
    check (status in ('scheduled','sent','failed','skipped')),
  scheduled_for timestamptz,
  sent_at timestamptz,
  error_message text,
  created_at timestamptz default now(),
  unique (run_id, step_id)
);

create table if not exists sequence_run_events (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references sequence_runs(id) on delete cascade,
  step_id uuid references sequence_steps(id) on delete set null,
  event_type text not null
    check (event_type in ('scheduled','sent','opened','clicked','replied','bounced','failed')),
  occurred_at timestamptz default now(),
  metadata jsonb default '{}'::jsonb
);

alter table outreach_log add column if not exists sequence_run_id  uuid references sequence_runs(id)  on delete set null;
alter table outreach_log add column if not exists sequence_step_id uuid references sequence_steps(id) on delete set null;

create index if not exists idx_sequences_created_by on sequences(created_by);
create index if not exists idx_sequences_status     on sequences(status);
create index if not exists idx_steps_sequence       on sequence_steps(sequence_id, position);
create index if not exists idx_runs_sequence        on sequence_runs(sequence_id);
create index if not exists idx_runs_next_send       on sequence_runs(next_send_at) where status = 'active';
create index if not exists idx_sends_scheduled      on sequence_step_sends(scheduled_for) where status = 'scheduled';
create index if not exists idx_outreach_seq_run    on outreach_log(sequence_run_id);

alter table sequences enable row level security;
alter table sequence_steps enable row level security;
alter table sequence_runs enable row level security;
alter table sequence_step_sends enable row level security;
alter table sequence_run_events enable row level security;
