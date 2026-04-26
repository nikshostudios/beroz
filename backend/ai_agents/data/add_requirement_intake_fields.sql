-- ============================================================
-- Requirement intake upgrade: 4 new fields for tighter scoring.
-- Inspired by the candidate-icp-builder / job-requirement-analysis
-- skill prompts. These add must-have signals the LLM scorer
-- previously had to guess at.
-- Apply in Supabase SQL Editor.
-- ============================================================

alter table requirements
  add column if not exists certifications        text[],
  add column if not exists remote_policy         text,
  add column if not exists industry_experience   text[],
  add column if not exists excluded_companies    text[];

-- remote_policy is free-text intentionally — recruiters write
-- "Hybrid 2 days/week", "Remote (US hours)", etc. We don't want
-- to lock into a fixed enum and force the LLM into a bad bucket.
