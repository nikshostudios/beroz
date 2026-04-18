-- 1a. Extend interview_tracker with tender-specific columns (SG tenders
--     used to live in gebiz_submissions; consolidating here).
alter table interview_tracker add column if not exists tender_number    text;
alter table interview_tracker add column if not exists school_name      text;
alter table interview_tracker add column if not exists submission_date  date;
alter table interview_tracker add column if not exists rechecking_date  date;

create index if not exists idx_tracker_tender on interview_tracker(tender_number);
create index if not exists idx_tracker_candidate on interview_tracker(candidate_id);

-- 1b. Drop redundant tables.
drop table if exists gebiz_submissions cascade;
drop table if exists client_contacts   cascade;
