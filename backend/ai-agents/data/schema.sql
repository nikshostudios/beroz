-- ExcelTech AI Agent Layer - Supabase Schema
-- Run this in Supabase SQL Editor (Dashboard > SQL Editor > New query)

-- Requirements (job openings from clients)
create table requirements (
  id uuid primary key default gen_random_uuid(),
  market text not null check (market in ('IN', 'SG')),
  client_name text not null,
  client_manager text,
  role_title text not null,
  skillset text,
  skills_required text[],
  experience_min text,
  salary_budget text,
  location text,
  contract_type text check (contract_type in ('FTE', 'TP', 'C2H', 'Contract')),
  notice_period text,
  br_sf_id text,
  tender_number text,
  jd_file_path text,
  status text default 'open' check (status in ('open', 'closed', 'on_hold')),
  assigned_recruiters text[],
  bd_owner text,
  created_at timestamptz default now()
);

-- Candidates (sourced or inbound)
create table candidates (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text,
  phone text,
  nationality text,
  work_pass_type text,
  current_location text,
  preferred_location text,
  skills text[],
  total_experience text,
  relevant_experience text,
  highest_education text,
  certifications text[],
  current_employer text,
  current_job_title text,
  current_ctc text,
  expected_ctc text,
  notice_period text,
  availability_date date,
  source text,
  cv_id text,
  linkedin_url text,
  resume_file_path text,
  market text check (market in ('IN', 'SG')),
  created_at timestamptz default now()
);

-- Screenings (AI screening results per candidate per requirement)
create table screenings (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  recruiter_email text,
  score int check (score between 1 and 10),
  skills_match_pct int,
  experience_match text check (experience_match in ('yes', 'partial', 'no')),
  salary_fit text check (salary_fit in ('yes', 'no', 'unknown')),
  recommendation text check (recommendation in ('shortlist', 'maybe', 'reject')),
  reasoning text,
  screened_at timestamptz default now()
);

-- Candidate details (detailed profile for submission formatting)
create table candidate_details (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  full_name text,
  nationality text,
  work_pass_type text,
  highest_education text,
  certifications jsonb,
  work_experience jsonb,
  current_employer text,
  current_job_title text,
  notice_period_days int,
  current_ctc text,
  expected_ctc text,
  availability_date date,
  status text default 'awaiting_candidate' check (status in
    ('awaiting_candidate', 'details_received', 'ready_for_review',
     'approved_by_tl', 'submitted_to_client', 'rejected_by_tl')),
  filled_at timestamptz,
  tl_feedback text,
  unique (candidate_id, requirement_id)
);

-- Outreach log (email tracking per candidate per requirement)
create table outreach_log (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  recruiter_email text,
  outlook_message_id text,
  outlook_thread_id text,
  channel text default 'email',
  email_subject text,
  sent_at timestamptz,
  reply_received boolean default false,
  replied_at timestamptz
);

-- Submissions (candidate submitted to client)
create table submissions (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  client_name text,
  tender_number text,
  market text,
  formatted_doc_path text,
  submitted_by_recruiter text,
  submitted_at timestamptz,
  tl_approved boolean default false,
  tl_approved_at timestamptz,
  sent_to_client_at timestamptz,
  final_status text check (final_status in
    ('Submitted', 'Shortlisted', 'KIV', 'Not Shortlisted',
     'Selected-Joined', 'Selected', 'Backed out', 'Rejected',
     'Selected-Backed out', null)),
  placement_type text check (placement_type in ('FTE', 'TP', 'C2H', null)),
  doj date,
  package text,
  sap_id text,
  remarks text
);

-- Interview tracker
create table interview_tracker (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  recruiter text,
  interview_date date,
  interview_time text,
  status text,
  end_client text,
  placement_type text,
  doj date,
  package text,
  sap_id text,
  remarks text
);

-- GeBIZ submissions (Singapore tenders - one candidate can have multiple)
create table gebiz_submissions (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  tender_number text not null,
  school_name text,
  submission_date date,
  rechecking_date date,
  status text,
  remarks text
);

-- Match scores (LLM semantic matching cache)
create table match_scores (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid references candidates(id),
  requirement_id uuid references requirements(id),
  score int not null check (score between 0 and 100),
  reasoning text,
  scored_at timestamptz default now(),
  unique (candidate_id, requirement_id)
);

create index idx_match_scores_requirement on match_scores(requirement_id);
create index idx_match_scores_candidate on match_scores(candidate_id);
create index idx_match_scores_score on match_scores(requirement_id, score desc);

-- Client contacts (TL's BD CRM)
create table client_contacts (
  id uuid primary key default gen_random_uuid(),
  name text,
  company text,
  region text check (region in ('IN', 'SG', 'MY')),
  contact_number text,
  email text,
  last_outreach_date date,
  outreach_history jsonb,
  notes text
);

-- Portal credentials (shared Foundit logins etc.)
create table portal_credentials (
  id uuid primary key default gen_random_uuid(),
  portal text,
  username text,
  password_encrypted text,
  assigned_recruiter text,
  active boolean default true
);

-- Indexes for common queries
create index idx_candidates_email on candidates(email);
create index idx_candidates_market on candidates(market);
create index idx_candidates_skills on candidates using gin(skills);
create index idx_requirements_status on requirements(status);
create index idx_requirements_market on requirements(market);
create index idx_screenings_candidate on screenings(candidate_id);
create index idx_screenings_requirement on screenings(requirement_id);
create index idx_submissions_candidate on submissions(candidate_id);
create index idx_submissions_requirement on submissions(requirement_id);
create index idx_gebiz_candidate on gebiz_submissions(candidate_id);
create index idx_gebiz_tender on gebiz_submissions(tender_number);
create index idx_outreach_recruiter on outreach_log(recruiter_email);

-- Enable Row Level Security (can configure policies later)
alter table requirements enable row level security;
alter table candidates enable row level security;
alter table screenings enable row level security;
alter table candidate_details enable row level security;
alter table outreach_log enable row level security;
alter table submissions enable row level security;
alter table interview_tracker enable row level security;
alter table gebiz_submissions enable row level security;
alter table client_contacts enable row level security;
alter table portal_credentials enable row level security;
alter table match_scores enable row level security;
