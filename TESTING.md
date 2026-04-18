# ExcelTech Recruitment Agent — Testing Guide

## Quick Start

**Live URL:** https://exceltechcomputers.up.railway.app

### Test Credentials
| Username | Password | Role |
|----------|----------|------|
| raju | raju18 | Team Lead (TL) — full access |
| devesh | devesh27 | Recruiter |
| manoj | manoj64 | Recruiter |

> Login as **raju** first to test all features (TL has superset of recruiter permissions).

---

## Test Checklist

### Phase 1: Auth & Navigation (no external services needed)

- [ ] **Login** — Go to `/`, enter `raju` / `raju18`, verify redirect to Agent Home
- [ ] **Session** — Open browser console, run `fetch('/api/session', {credentials:'include'}).then(r=>r.json()).then(console.log)` — should show `{logged_in: true, name: "Raju Akula", role: "tl"}`
- [ ] **All 10 pages load** — Click each sidebar nav item, verify no JS errors in console
- [ ] **TL-only features visible** — Verify "Submissions" nav item and "New Requirement" button are visible
- [ ] **Logout** — Click Logout, verify redirect to login page
- [ ] **Recruiter restrictions** — Login as `devesh` / `devesh27`, verify Submissions nav is hidden and no "New Requirement" button
- [ ] **Landing page** — Visit `/home`, verify both SaaS and ExcelTech product cards display

### Phase 2: Backend API Health (tests Supabase + AI Agent Layer)

These tests verify the FastAPI backend is reachable. If these fail, the `ai_agents` service may not be deployed yet on Railway.

- [ ] **Requirements API** — After login, go to Requirements page. If requirements load (cards appear), the Flask→FastAPI→Supabase chain works.
- [ ] **Pipeline API** — Go to Shortlist or Analytics page. If data loads, pipeline API works.
- [ ] **Session API** — Settings page shows your name/email/role = Flask session works.

**If pages show empty/errors:**
The FastAPI `ai_agents` service needs to be deployed as a separate Railway service. Currently only the `web` (Flask) service is live. The Flask API proxy routes forward to `AI_AGENT_URL` which points to the ai_agents internal service.

### Phase 3: Requirements (needs Supabase + AI Agent Layer)

- [ ] **View requirements** — Requirements page shows existing requirement cards
- [ ] **Filter by market** — Click India / Singapore tabs, verify filtering works
- [ ] **Create requirement (TL only)** — Click "New Requirement", fill form:
  - Client: "Test Corp"
  - Market: India
  - Role: "Senior Java Developer"
  - Skills: "Java, Spring Boot, Microservices"
  - Experience: 5
  - Salary: "20-30 LPA"
  - JD: Any text describing the role
- [ ] **Verify creation** — New card appears on Requirements board
- [ ] **Source Now** — Click "Source Now" on a requirement. This triggers multi-channel sourcing (needs Foundit/Apollo credentials in env vars)

### Phase 4: Outreach & Email (needs Outlook Graph API)

- [ ] **Inbox Monitor** — Go to Outreach tab, select a recruiter, click "Check Inbox"
  - If Outlook credentials are configured: emails display with AI categories
  - If not: you'll see an error (expected — needs AZURE_* env vars)
- [ ] **Send Outreach** — Go to "Send Outreach" tab:
  1. Upload 1-2 test resumes (PDF/DOCX)
  2. Upload a test JD (TXT/PDF)
  3. Enter client name + location
  4. Click "Upload & Screen" — should show scored results
  5. "Send Outreach to Qualified" sends emails (needs Outlook configured)

### Phase 5: Submissions (needs full pipeline data)

- [ ] **TL Queue** — As Raju (TL), go to Submissions. Shows pending submissions.
- [ ] **Approve flow** — Click "Approve & Send" on a submission, enter client email
- [ ] **Reject flow** — Click "Reject", enter feedback

### Phase 6: Search & Analytics

- [ ] **Search** — Go to Search page, type "Python developers in Singapore with 3+ years"
  - Should show parsed query (skills, location, experience)
  - Matching candidates displayed below
- [ ] **Analytics Pipeline** — Go to Analytics → Pipeline tab
  - Stats cards: Open Requirements, Sourced, Shortlisted, Submitted
  - Funnel visualization
  - Per-requirement breakdown table
- [ ] **Analytics Usage** — Switch to Usage tab, view token usage/cost

### Phase 7: Integrations & Settings

- [ ] **Integrations** — All 6 service cards display with "Connected" status
- [ ] **Settings** — Profile shows correct name, email, role

---

## What Works Without External Services

Even without Supabase/Outlook/Claude API configured, you can validate:

| Feature | Works? | Why |
|---------|--------|-----|
| Login/Logout | Yes | Hardcoded credentials in Flask |
| Session API | Yes | Flask sessions |
| Page navigation | Yes | Static frontend served by Flask |
| Role-based UI | Yes | Controlled by session data |
| Landing page (/home) | Yes | Static HTML |
| SaaS frontend (/frontend-saas/) | Yes | Static HTML |

## What Needs the AI Agent Layer (FastAPI)

| Feature | Requires |
|---------|----------|
| Requirements CRUD | FastAPI + Supabase |
| Candidate sourcing | FastAPI + Supabase + Foundit/Apollo credentials |
| Screening/scoring | FastAPI + Supabase + Claude API key |
| Shortlist/Contacts data | FastAPI + Supabase |
| Search | FastAPI + Supabase + Claude API key |
| Analytics data | FastAPI + Supabase |
| Submissions | FastAPI + Supabase + Claude API key |

## What Needs Outlook

| Feature | Requires |
|---------|----------|
| Check Inbox | Azure AD credentials (AZURE_TENANT_ID, CLIENT_ID, CLIENT_SECRET) |
| Send Outreach | Azure AD + recruiter Outlook passwords |
| TL Send to Client | Azure AD + TL Outlook password |

---

## Debugging Tips

### Check if Flask is healthy
```bash
curl -s https://exceltechcomputers.up.railway.app/ | head -5
# Should return HTML login page
```

### Check session API
```bash
# After logging in via browser, copy your session cookie and:
curl -s https://exceltechcomputers.up.railway.app/api/session \
  -H "Cookie: session=YOUR_SESSION_COOKIE"
```

### Check if AI Agent Layer is reachable
```bash
# This is an internal Railway service — not publicly accessible
# Test from the Flask app by visiting the Requirements page
# If requirements load → FastAPI is connected
# If empty → ai_agents service needs deployment
```

### Check Railway logs
```bash
cd "/Users/shohamshree/Downloads/JUICEBOX HTML"
railway logs  # View latest web service logs
```

### Common Issues
| Symptom | Cause | Fix |
|---------|-------|-----|
| Pages load but no data | AI agent service not deployed | Deploy ai_agents as separate Railway service |
| "Check Inbox" fails | Outlook not configured | Set AZURE_* env vars in Railway |
| Screening returns errors | No Claude API key | Set ANTHROPIC_API_KEY in Railway |
| Sourcing finds 0 candidates | No portal credentials | Add Foundit/Apollo creds to Supabase portal_credentials table |
| Login works, /app doesn't | Frontend file not found | Check FRONTEND_DIR path in app.py |

---

## Next Steps for Full Validation

1. **Deploy ai_agents service** — The FastAPI layer needs its own Railway service with internal networking
2. **Verify env vars** — Ensure all env vars (Anthropic, Supabase, Azure) are set in Railway
3. **Seed test data** — Create a test requirement and source candidates to populate the pipeline
4. **End-to-end test** — Walk through the full pipeline: Create Requirement → Source → Screen → Outreach → Reply → Submit → TL Approve → Send to Client
