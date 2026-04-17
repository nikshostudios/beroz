-- Phase 3: Candidate shortlists (per-user) + free-text notes.
-- Run this in Supabase Dashboard → SQL Editor → New query BEFORE deploying.
--
-- candidate_shortlists: the persistent "I want to keep this candidate" flag
-- per logged-in user. A single row = the candidate shows up in BOTH
-- Shortlist (rich action cards) and Contacts (searchable table) views.
--
-- candidate_notes: free-text notes a recruiter attaches to a candidate
-- (the "Notes" tab in the detail slide-over). One candidate can have many
-- notes from many recruiters — we don't collapse them.

create table if not exists candidate_shortlists (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references candidates(id) on delete cascade,
  user_email text not null,
  note text,
  created_at timestamptz default now(),
  unique (candidate_id, user_email)
);

create table if not exists candidate_notes (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references candidates(id) on delete cascade,
  user_email text not null,
  content text not null,
  created_at timestamptz default now()
);

create index if not exists idx_shortlists_user on candidate_shortlists(user_email);
create index if not exists idx_shortlists_candidate on candidate_shortlists(candidate_id);
create index if not exists idx_notes_candidate on candidate_notes(candidate_id);
create index if not exists idx_notes_user on candidate_notes(user_email);

alter table candidate_shortlists enable row level security;
alter table candidate_notes enable row level security;
