# ExcelTech Recruitment Agent — Features & Workflow Guide

## System Overview

A full-stack AI-powered recruitment automation platform built for ExcelTech Computers. The system combines a Flask web app (frontend + API proxy) with a FastAPI AI agent layer backed by Claude AI, Supabase PostgreSQL, Microsoft Graph API (Outlook), and Google Sheets.

**Live URL:** https://exceltechcomputers.up.railway.app

---

## Architecture

```
Browser (Juicebox Frontend)
    │
    ▼
Flask Web App (app.py)          ← Serves frontend, handles auth, proxies API calls
    │
    ▼
FastAPI AI Agent Layer (ai_agents/main.py)   ← 8 AI agents, 5 workflow skills, 20+ endpoints
    │
    ├── Claude Sonnet 4    (screening, formatting, JD parsing, market intel)
    ├── Claude Haiku 4.5   (classification, email parsing, outreach drafts)
    ├── Supabase PostgreSQL (candidates, requirements, submissions, match_scores)
    ├── Microsoft Graph API (Outlook email for 10 recruiter accounts)
    ├── Google Sheets       (CRM tracking via gspread)
    └── Sourcing Channels   (Foundit, MyCareersFuture, Apollo.io)
```

---

## User Roles

| Role | Users | Capabilities |
| --- | --- | --- |
| **Team Lead (TL)** | Raju Akula | Everything below + Create Requirements, Approve/Reject Submissions, Send to Client, Market Intelligence |
| **Recruiter** | Devesh, Manoj, Atul, Rohit, Raghav, Priya, Narender | Source candidates, Screen resumes, Send outreach, Monitor inbox, Search, View analytics |

---

## Pages & Features

### 1. Agent Home
**Purpose:** AI chat interface — your recruitment copilot.

**What it does:**
- Natural language interface to the AI agent system
- Quick action chips for common tasks:
  - "Source candidates for open roles"
  - "Show pipeline status"
  - "Check recruiter inbox"
  - "Show open requirements"
- Sends queries to the AI layer and streams responses

**Backend:** POST `/api/search` → FastAPI `/search/parse`

---

### 2. Requirements Board
**Purpose:** View and manage all open job requirements.

**What it does:**
- Displays requirement cards with: Role, Client, Market (India/Singapore), Status, Skills, Location, Experience, Salary
- Filter by market: All / India / Singapore
- **Source Now** button triggers AI sourcing across all channels
- **New Requirement** button (TL only) opens creation modal
- View full JD details

**Workflow:**
```
TL creates requirement
    │
    ▼
JD Parser Agent extracts structured data (skills, salary, location, red flags)
    │
    ▼
Source & Screen Skill auto-triggers:
    ├── Foundit (3 accounts, rotated)
    ├── MyCareersFuture (SG market)
    ├── Apollo.io (enrichment)
    └── LinkedIn search string generated (manual use only)
    │
    ▼
Candidates deduplicated by email → upserted to Supabase
    │
    ▼
Screener Agent scores each candidate (1-10) → stored in screenings table
    │
    ▼
Shortlisted candidates (score >= 7) appear on Shortlist page
```

**Backend:**
- GET `/api/requirements` — list requirements
- POST `/api/requirements/create` — create new (TL only)
- POST `/api/requirements/{id}/source` — trigger sourcing

---

### 3. Shortlist
**Purpose:** View top-scoring candidates across all requirements.

**What it does:**
- Grid of candidate cards filtered to score >= 7
- Each card shows: Name, Skills, Score (color-coded), Requirement, Client, Location, Experience, Email
- Score colors: Green (8+), Yellow (6-7), Red (<6)

**Backend:** GET `/api/pipeline` → filters candidates by score

---

### 4. Contacts
**Purpose:** Master candidate database — all unique candidates across all requirements.

**What it does:**
- Table view deduplicated by email
- Columns: Name, Skills (top 3), Experience, Location, Source, Market
- Market tags (IN/SG) with color coding

**Backend:** GET `/api/pipeline` → extracts unique candidates

---

### 5. Outreach & Inbox (Sequences)
**Purpose:** Email automation — send outreach and monitor recruiter inboxes.

**Two tabs:**

#### Tab 1: Inbox Monitor
- Select recruiter from dropdown
- "Check Inbox" fetches and AI-classifies all emails:
  - **Requirements** — new JD / role from client
  - **Candidate Replies** — responses to outreach
  - **Action Needed** — urgent items
  - **FYI** — informational
- Stats cards show counts per category
- Email table with sender, subject, category, time

**Workflow:**
```
Recruiter clicks "Check Inbox"
    │
    ▼
Flask proxies to /api/outreach/emails
    │
    ▼
Microsoft Graph API fetches unread emails for that recruiter
    │
    ▼
Claude Haiku 4.5 classifies each email into categories
    │
    ▼
Results displayed in table with color-coded category tags
```

#### Tab 2: Send Outreach
- Upload resumes (PDF/DOCX, multiple) + Job Description (TXT/PDF)
- Enter client name and location
- "Upload & Screen" processes all resumes against JD
- Results table: Candidate, Skills, Score, Verdict, Reason
- "Send Outreach to Qualified" sends emails to all candidates scoring >= 6

**Workflow:**
```
Recruiter uploads resumes + JD
    │
    ▼
POST /source/upload → saves files to server
    │
    ▼
POST /source/screen → Claude Sonnet 4 scores each resume vs JD
    │
    ▼
Results displayed with scores and verdicts
    │
    ▼
Recruiter clicks "Send Outreach to Qualified"
    │
    ▼
POST /source/send-emails → sends via Outlook Graph API
    │
    ▼
Email status shown (sent/failed per candidate)
```

**Backend:**
- POST `/api/outreach/emails` — fetch & classify inbox
- POST `/source/upload` — upload files
- POST `/source/screen` — screen candidates
- POST `/source/send-emails` — send emails

---

### 6. Submissions (TL Only)
**Purpose:** Review and approve candidate submissions before sending to clients.

**What it does:**
- Queue of submissions from recruiters
- Each card: Candidate Name, Role, Client, Score, Status, Submitted By
- **Approve & Send** — prompts for client email, sends formatted .docx via Outlook
- **Reject** — prompts for feedback, notifies recruiter

**Workflow:**
```
Recruiter submits candidate via "Submit to TL"
    │
    ▼
Formatter Agent creates client-ready .docx (name, nationality, education, experience, salary, availability)
    │
    ▼
Submission appears in TL queue with "Pending Review" status
    │
    ▼
TL reviews → Approve & Send
    │
    ▼
TL enters client email → formatted .docx sent via Outlook Graph API
    │
    ▼
candidate_details.status → "submitted_to_client"
(If SG + tender_number → auto-insert to interview_tracker)
```

**Backend:**
- GET `/api/tl/queue` — fetch pending submissions
- POST `/api/tl/approve-and-send` — approve and email to client
- POST `/api/tl/reject` — reject with feedback

---

### 7. Analytics
**Purpose:** Pipeline metrics and usage tracking.

#### Pipeline Tab
- Stats cards: Open Requirements, Candidates Sourced, Shortlisted, Submitted to Client
- Pipeline funnel: Sourced → Shortlisted → Submitted → Approved
- Per-requirement breakdown table

#### Usage Tab
- API calls today, Tokens used, Estimated cost

**Backend:** GET `/api/pipeline`

---

### 8. Integrations
**Purpose:** View connected services (read-only status page).

**Connected Services:**
| Service | Purpose | Status |
| --- | --- | --- |
| Microsoft Outlook | Email outreach via Graph API (10 accounts) | Connected |
| Google Sheets | CRM tracking | Connected |
| Claude AI | Sonnet 4 screening, Haiku 4.5 classification | Connected |
| Supabase | PostgreSQL database | Connected |
| Foundit via Firecrawl | Candidate sourcing (3 accounts) | Connected |
| Apollo.io | Professional database for passive sourcing | Connected |

---

### 9. Search
**Purpose:** Natural language candidate search.

**What it does:**
- Type queries like: "Java developers in Bangalore with 5+ years"
- AI parses into structured filters: skills, experience, location, market, salary
- Shows parsed query breakdown + matching candidate cards

**Backend:** POST `/api/search` → FastAPI `/search/parse`

---

### 10. Settings
**Purpose:** View user profile.

- Displays: Name, Email, Role (Team Lead or Recruiter)
- Read-only

---

## AI Agents (8 Total)

| Agent | Model | Purpose |
| --- | --- | --- |
| **JD Parser** | Sonnet 4 | Extract structured data from raw job descriptions |
| **Screener** | Sonnet 4 | Score candidates against requirements (1-10) |
| **Outreach** | Haiku 4.5 | Draft personalized outreach emails (never auto-sends) |
| **Followup** | Haiku 4.5 | Parse candidate reply emails, extract filled fields |
| **Formatter** | Sonnet 4 | Build client-ready .docx submission documents |
| **Market Intelligence** | Sonnet 4 | Produce actionable market briefs from job posting data |
| **Sourcing** | Haiku 4.5 + Sonnet 4 | Find candidates across Foundit, MyCareersFuture, Apollo.io |
| **Reactivation** | Haiku 4.5 | Match dormant candidates to new open requirements |

---

## Workflow Skills (5 Total)

| Skill | Trigger | What It Does |
| --- | --- | --- |
| **Source & Screen** | TL creates requirement or "Source Now" clicked | Sources from all channels → deduplicates → screens each candidate |
| **Prepare Outreach** | Recruiter selects candidate | Drafts personalized email → recruiter reviews → sends via Graph API |
| **Process Inbox** | Cron every 15 min | Scans all recruiter inboxes → classifies → matches replies to outreach |
| **Submit to TL** | Recruiter clicks "Submit to TL" | Validates status → creates formatted .docx → adds to TL queue |
| **TL Send to Client** | TL clicks "Approve & Send" | Attaches .docx → sends from TL's Outlook → updates status |

---

## End-to-End Recruitment Workflow

```
1. NEW REQUIREMENT
   TL creates requirement with JD, skills, market, salary
        │
        ▼
2. AUTO-SOURCE
   Sourcing Agent searches Foundit + MyCareersFuture + Apollo.io
   LinkedIn boolean string generated for manual search
        │
        ▼
3. AUTO-SCREEN
   Screener Agent scores every candidate (1-10)
   Shortlisted (≥7) appear on Shortlist page
        │
        ▼
4. RECRUITER OUTREACH
   Recruiter sends personalized emails to qualified candidates
   Outreach Agent drafts, recruiter reviews before send
        │
        ▼
5. INBOX MONITORING
   Every 15 min: Process Inbox Skill scans all recruiter emails
   Candidate replies auto-matched, fields extracted by Followup Agent
        │
        ▼
6. SUBMIT TO TL
   Recruiter submits completed candidate profile
   Formatter Agent creates client-ready .docx
        │
        ▼
7. TL APPROVAL
   TL reviews submission → Approves → Sends to client via Outlook
   (SG government roles → auto-logged to GeBIZ tracker)
        │
        ▼
8. MARKET INTELLIGENCE (ongoing)
   Weekly market briefs with salary trends, competitive signals
   Reactivation Agent matches dormant candidates to new roles
```

---

## Database Tables (Supabase)

| Table | Purpose |
| --- | --- |
| `candidates` | Master candidate records (name, email, skills, location, experience) |
| `requirements` | Job requirements (role, client, skills, salary, market, status) |
| `screenings` | Candidate-requirement match scores and recommendations |
| `submissions` | Candidate submissions to TL with approval status |
| `candidate_details` | Extended profile data extracted from email replies |
| `outreach_log` | Record of all outreach emails sent |
| `match_scores` | AI-generated match scores |
| `portal_credentials` | Foundit/sourcing portal login credentials (rotated) |
| `interview_tracker` | Interview + SG tender tracking (single source of truth) |

---

## Environment Variables Required

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude AI API access |
| `SUPABASE_URL` + `SUPABASE_KEY` | Database connection |
| `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET` | Microsoft Graph API (Outlook) |
| `GOOGLE_CREDENTIALS` | Google Sheets service account JSON |
| `GOOGLE_SHEET_ID` | Main CRM tracking sheet |
| `SCREENED_SHEET_ID` | Screened profile tracker sheet |
| `SOURCING_SHEET_ID` | Sourcing activity log sheet |
| `AI_AGENT_URL` | FastAPI agent layer URL (for Railway internal networking) |
| `SECRET_KEY` | Flask session encryption key |
| `OUTLOOK_PASSWORD_*` | Per-recruiter Outlook passwords (10 accounts) |
