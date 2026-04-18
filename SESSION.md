# Beroz — Juicebox Recruitment Platform

## Overview

Two-product recruitment platform built on a shared backend:
1. **SaaS Product** — General-purpose Juicebox-style UI for any recruitment agency
2. **ExcelTech Instance** — Custom-built, fully integrated version for ExcelTech Computers

## What We Built (This Session)

### Phase 1: Juicebox Dashboard Clone
- Scraped 52 HTML pages from the original Juicebox AI platform
- Built a pixel-accurate single-file HTML clone with all 10 pages
- Clean, modern design with Inter font, Material Icons, CSS variables

### Phase 2: Full-Stack Integration
- Copied the entire ExcelTech backend from `el-paso` (Flask + FastAPI + AI agents)
- Rewrote the frontend to connect to all backend APIs
- Added 10+ new API proxy routes in Flask
- Wired every page to live data: requirements, candidates, pipeline, outreach, inbox, submissions
- Added role-based UI (TL sees Submissions queue + Create Requirement)
- Added authentication flow (Flask session-based login)

### Phase 3: Product Separation
- Split into two distinct frontends: `frontend-saas/` and `frontend-exceltech/`
- Created landing page linking to both products
- Updated Flask routes to serve both independently

## Project Structure

```
JUICEBOX HTML/
├── index.html                        # Landing page (links to both products)
├── run.py                            # Launcher (Flask + optional FastAPI)
├── frontend-saas/
│   └── index.html                    # General SaaS product (static Juicebox clone)
├── frontend-exceltech/
│   └── index.html                    # ExcelTech-specific (backend-integrated)
├── backend/
│   ├── app.py                        # Flask web app (3600+ lines)
│   ├── agent.py                      # CLI resume parser & screener
│   ├── source.py                     # Sourcing blueprint (screen + email)
│   ├── outreach.py                   # Outlook inbox/reply blueprint
│   ├── requirements.txt              # Python dependencies
│   ├── Procfile                      # Gunicorn deployment config
│   ├── build.sh                      # Dashboard build script
│   └── ai_agents/
│       ├── main.py                   # FastAPI AI agent layer (1500+ lines)
│       ├── config/
│       │   ├── db.py                 # Supabase database helpers
│       │   ├── sourcing.py           # Candidate sourcing logic
│       │   ├── market_intelligence.py # Market data retrieval
│       │   ├── outlook.py            # Microsoft Graph API
│       │   ├── search_parser.py      # NL search parsing
│       │   └── cron.py               # Scheduled tasks (inbox every 15 min)
│       ├── agents/                   # 8 AI agent prompts
│       │   ├── screener.md           # Candidate-to-JD matching (Sonnet 4)
│       │   ├── sourcing.md           # LinkedIn/market sourcing
│       │   ├── outreach.md           # Email composition (Haiku 4.5)
│       │   ├── followup.md           # Reply parsing + chase emails
│       │   ├── formatter.md          # Client-ready .docx generation
│       │   ├── jd_parser.md          # JD extraction
│       │   ├── market_intelligence.md
│       │   └── reactivation.md
│       ├── skills/                   # 5 workflow automations
│       │   ├── source-and-screen.md
│       │   ├── prepare-outreach.md
│       │   ├── process-inbox.md
│       │   ├── submit-to-tl.md
│       │   └── tl-send-to-client.md
│       └── data/
│           ├── schema.sql            # Supabase table definitions
│           └── migrate.py            # Data migration script
└── juicebox-crawler/                 # Original scraping tools
    ├── crawl.js
    └── html-captures/                # 52 scraped HTML files
```

## Frontend Pages — ExcelTech (All API-Connected)

| Page | Backend API | Function |
| --- | --- | --- |
| **Agent Home** | `/api/search` | Chat interface, NL query parsing |
| **Requirements Board** | `/api/requirements`, `/api/requirements/create` | View/create requirements, trigger AI sourcing |
| **Shortlist** | `/api/pipeline` | Candidates scored 7+/10 by AI screener |
| **Contacts** | `/api/pipeline` | All candidates from Supabase, deduplicated |
| **Outreach & Inbox** | `/source/upload`, `/source/screen`, `/api/outreach/emails` | Upload/screen resumes, monitor inbox |
| **Submissions** (TL only) | `/api/tl/queue`, `/api/tl/approve-and-send` | TL approval queue |
| **Analytics** | `/api/pipeline` | Pipeline funnel + per-requirement breakdown |
| **Search** | `/api/search` | Natural language candidate search |
| **Integrations** | Static | Shows all connected services |
| **Settings** | `/api/session` | User profile display |

## Frontend Pages — SaaS (Static, Ready for Integration)

| Page | Description |
| --- | --- |
| **Agent Home** | Chat interface with AI agent greeting and quick action chips |
| **All Projects** | Stats cards + projects table with status, progress, collaborators |
| **All Agents** | Agents table with type, status, contacts found, emails sent |
| **Shortlist** | Candidate cards grid with name, role, company, location, education |
| **Contacts** | Sortable contacts table with tags (LinkedIn, Email Outreach) |
| **Sequences** | Email sequence list with enrollment count, open rate, reply rate |
| **Analytics** | Tabbed view (Projects/Agents/Outreach/Usage) with stat cards |
| **Integrations** | Connected mailboxes table + available integrations grid |
| **Search** | Full-page search input with empty state |
| **Settings** | User profile form |

## Backend Architecture

```
Client Browser
    │
    ├── GET /home          → Landing page (both products)
    ├── GET /app           → ExcelTech frontend (requires login)
    ├── GET /frontend-saas → SaaS frontend (static)
    ├── POST /             → Login (session-based auth)
    │
    ├── Flask (port 5001) ─────────────────────────────────┐
    │   app.py + source.py + outreach.py                    │
    │   Resume parsing, screening, email sending,           │
    │   Google Sheets CRM, API proxy to FastAPI             │
    │                                                        │
    │   └── FastAPI (port 8001) ───────────────────────────┐│
    │       ai_agents/main.py                               ││
    │       Requirements CRUD, AI sourcing, screening,      ││
    │       inbox processing, TL queue, pipeline stats      ││
    │                                                        ││
    │       ├── Claude API (Sonnet 4 + Haiku 4.5)          ││
    │       ├── Supabase PostgreSQL                         ││
    │       ├── Microsoft Graph API (10 Outlook accounts)   ││
    │       ├── Foundit (Firecrawl scraping)                ││
    │       └── Apollo.io (passive candidates)              ││
    └───────────────────────────────────────────────────────┘│
```

## How to Run

```bash
# Install dependencies
pip3 install -r backend/requirements.txt

# Set environment variables
export ANTHROPIC_API_KEY=sk-...
export SUPABASE_URL=https://...supabase.co
export SUPABASE_SERVICE_ROLE_KEY=...
export AZURE_CLIENT_ID=...
export AZURE_TENANT_ID=...
export AZURE_CLIENT_SECRET=...

# Start the app
python3 run.py                # Flask only (port 5001)
python3 run.py --with-agents  # Flask + FastAPI agents

# Login credentials
# TL: raju / raju18
# Recruiter: devesh / devesh27
```

## Repo

**GitHub**: [github.com/nikshostudios/beroz](https://github.com/nikshostudios/beroz)

## Next Steps

### SaaS Product
- Wire up to a generic backend (multi-tenant)
- Add user registration / onboarding flow
- Build out chart visualizations in Analytics
- Add real-time notifications via WebSocket
- Make responsive for mobile

### ExcelTech Instance
- Deploy to Railway with all env vars
- Test end-to-end with live Supabase + Outlook
- Add GeBIZ tender workflow (Singapore)
- Add notification bell with real-time polling
- Add WhatsApp Business API integration
