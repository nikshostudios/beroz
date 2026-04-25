-- ============================================================
-- Multi-source sourcing expansion (Slice A: GitHub + scaffold)
-- Plan: nimbalyst-local/plans/right-i-m-planning-on-staged-deer.md
-- Run in Supabase SQL Editor to apply to an existing database.
-- ============================================================

alter table candidates
  add column if not exists github_url           text,
  add column if not exists source_profile_url   text,
  add column if not exists source_metadata      jsonb;

create index if not exists idx_candidates_github_url
  on candidates(github_url) where github_url is not null;
