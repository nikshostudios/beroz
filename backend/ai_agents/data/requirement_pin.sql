-- Requirement pin flag — lets pinned requirements float to the top of the list.
--
-- Apply in Supabase SQL editor or via apply_schema.py:
--   python backend/ai_agents/data/apply_schema.py requirement_pin.sql

alter table requirements
  add column if not exists is_pinned boolean default false;

create index if not exists idx_requirements_pinned
  on requirements(is_pinned) where is_pinned = true;
