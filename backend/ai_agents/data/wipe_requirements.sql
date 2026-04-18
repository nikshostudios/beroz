-- Hard delete all requirements and their dependent rows.
-- Run this in Supabase Dashboard → SQL Editor → New query.
--
-- Candidates, projects, interview_tracker, portal_credentials are untouched.
-- Foreign keys on requirements do NOT cascade, so children must be deleted first.
--
-- IRREVERSIBLE without a database restore. Double-check row counts before and after.

begin;

-- Sanity check: row counts before
select 'requirements' as table_name, count(*) as rows_before from requirements
union all select 'screenings', count(*) from screenings
union all select 'candidate_details', count(*) from candidate_details
union all select 'outreach_log', count(*) from outreach_log
union all select 'submissions', count(*) from submissions
union all select 'match_scores', count(*) from match_scores;

-- 1. Children of requirements (delete first to respect FK constraints)
delete from submissions         where requirement_id is not null;
delete from outreach_log        where requirement_id is not null;
delete from match_scores        where requirement_id is not null;
delete from candidate_details   where requirement_id is not null;
delete from screenings          where requirement_id is not null;

-- 2. Requirements themselves
delete from requirements;

-- 3. Verify empty
select 'requirements' as table_name, count(*) as rows_after from requirements
union all select 'screenings', count(*) from screenings where requirement_id is not null
union all select 'candidate_details', count(*) from candidate_details where requirement_id is not null
union all select 'outreach_log', count(*) from outreach_log where requirement_id is not null
union all select 'submissions', count(*) from submissions where requirement_id is not null
union all select 'match_scores', count(*) from match_scores where requirement_id is not null;

-- If every count is 0, commit. Otherwise rollback.
commit;
