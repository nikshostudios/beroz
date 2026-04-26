-- Saved searches — per-recruiter persistence of JD/natural-language queries.
--
-- Layered on top of schema.sql. Each row captures one recruiter's saved
-- query: the raw input, the parser-emitted filters/soft criteria, plus a
-- timestamp from the most recent run so they can re-execute later.
--
-- Recruiter A only sees rows where created_by = A's email; this is enforced
-- in core.py (list_searches_for_recruiter) rather than via RLS so existing
-- service-role connections keep working unchanged.
--
-- Apply with:
--     python backend/ai_agents/data/apply_schema.py searches.sql

create table if not exists searches (
  id uuid primary key default gen_random_uuid(),
  created_by text not null,
  name text not null,
  market text,
  mode text not null check (mode in ('natural', 'jd', 'manual')),
  source_text text,
  filters jsonb not null default '{}'::jsonb,
  soft_criteria jsonb not null default '[]'::jsonb,
  jd_diagnostics jsonb,
  created_at timestamptz default now(),
  last_run_at timestamptz
);

create index if not exists idx_searches_by_recruiter
  on searches(created_by, created_at desc);
