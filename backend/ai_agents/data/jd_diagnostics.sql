-- JD diagnostics — persist parser-emitted quality signals on requirements.
--
-- Layered on top of schema.sql. Adds two nullable columns surfaced in the
-- Requirement-create modal and the Boost panel so recruiters can see why a
-- JD might waste sourcing cycles before they spend any.
--
-- Apply with:
--     python backend/ai_agents/data/apply_schema.py jd_diagnostics.sql

alter table requirements
  add column if not exists red_flags jsonb,
  add column if not exists jd_quality_score int;
