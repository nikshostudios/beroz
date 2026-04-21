-- Agentic Boost — schema additions
-- Apply via: python backend/ai_agents/data/apply_schema.py (or paste into Supabase SQL Editor)
--
-- Adds:
--   1. requirements.{source, boost_run, jd_text, jd_parsed} so each Agentic
--      Boost run can auto-create a requirement row tagged for the feature.
--   2. outreach_log.{status, email_body} so drafts can sit alongside sent
--      emails in the same audit table (status='draft' until recruiter approves).
--   3. agentic_boost_runs — lightweight audit + replay trail for each run.

alter table requirements
  add column if not exists source text default 'manual',
  add column if not exists boost_run boolean default false,
  add column if not exists jd_text text,
  add column if not exists jd_parsed jsonb;

create index if not exists idx_requirements_source on requirements(source);

alter table outreach_log
  add column if not exists status text default 'sent'
    check (status in ('draft', 'sent', 'failed', 'approved')),
  add column if not exists email_body text;

create index if not exists idx_outreach_log_status on outreach_log(status);

create table if not exists agentic_boost_runs (
  id uuid primary key default gen_random_uuid(),
  requirement_id uuid references requirements(id) on delete cascade,
  created_by text not null,
  jd_text text not null,
  agent_events jsonb default '[]'::jsonb,
  status text default 'running' check (status in ('running', 'completed', 'failed')),
  created_at timestamptz default now(),
  completed_at timestamptz
);

create index if not exists idx_boost_runs_created_by on agentic_boost_runs(created_by);
create index if not exists idx_boost_runs_requirement on agentic_boost_runs(requirement_id);

alter table agentic_boost_runs enable row level security;
