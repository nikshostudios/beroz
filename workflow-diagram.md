# ExcelTech Recruitment Agent — Visual Workflows

## Master Recruitment Pipeline

```mermaid
flowchart TD
    A["🏢 TL Creates Requirement\n(Role, Client, Skills, Market, Salary)"] --> B["🤖 JD Parser Agent\n(Claude Sonnet 4)"]
    B --> C["📋 Requirement stored\nin Supabase"]
    C --> D["🔍 Source & Screen Skill\n(auto-triggered)"]

    D --> E1["Foundit\n(3 rotating accounts)"]
    D --> E2["MyCareersFuture\n(SG market only)"]
    D --> E3["Apollo.io\n(enrichment)"]
    D --> E4["LinkedIn\n(boolean string only)"]

    E1 --> F["🔄 Deduplicate by Email\n→ Upsert to Supabase"]
    E2 --> F
    E3 --> F
    E4 -.->|"manual"| F

    F --> G["🤖 Screener Agent\n(Claude Sonnet 4)\nScore 1-10 per candidate"]

    G --> H{Score >= 7?}
    H -->|Yes| I["⭐ Shortlisted\n(visible on Shortlist page)"]
    H -->|No, >= 6| J["📧 Outreach Eligible"]
    H -->|No, < 6| K["❌ Rejected"]

    I --> L["👤 Recruiter Reviews\nCandidate Profile"]
    J --> L

    L --> M["📨 Outreach Agent\n(Claude Haiku 4.5)\nDrafts personalized email"]
    M --> N["👤 Recruiter Reviews\n& Edits Draft"]
    N --> O["📤 Send via\nOutlook Graph API"]

    O --> P["⏰ Process Inbox Skill\n(every 15 min cron)"]
    P --> Q{Candidate\nReplied?}
    Q -->|Yes| R["🤖 Followup Agent\n(Claude Haiku 4.5)\nExtract filled fields"]
    Q -->|No reply| S["🔄 Chase draft\ngenerated"]

    R --> T{Profile\nComplete?}
    T -->|Yes| U["📝 Recruiter clicks\nSubmit to TL"]
    T -->|No| S

    U --> V["🤖 Formatter Agent\n(Claude Sonnet 4)\nCreates .docx"]
    V --> W["📋 Appears in\nTL Submission Queue"]

    W --> X{TL Decision}
    X -->|Approve| Y["📤 TL Send to Client\nvia Outlook + .docx attachment"]
    X -->|Reject| Z["❌ Feedback sent\nto Recruiter"]

    Y --> AA["✅ Candidate Status:\nsubmitted_to_client"]

    style A fill:#e8f5e9
    style G fill:#fff3e0
    style I fill:#e3f2fd
    style Y fill:#e8f5e9
    style K fill:#ffebee
    style Z fill:#ffebee
```

---

## Outreach & Inbox Flow

```mermaid
sequenceDiagram
    participant R as Recruiter
    participant App as Flask App
    participant AI as AI Agent Layer
    participant Outlook as Microsoft Outlook
    participant C as Candidate

    Note over R,C: OUTREACH PHASE
    R->>App: Upload resumes + JD
    App->>AI: POST /source/screen
    AI->>AI: Claude Sonnet 4 scores each resume
    AI-->>App: Scores, verdicts, reasons
    App-->>R: Display results table

    R->>App: "Send Outreach to Qualified"
    App->>Outlook: Graph API send email (per candidate)
    Outlook->>C: Personalized outreach email

    Note over R,C: INBOX MONITORING (every 15 min)
    loop Every 15 minutes
        AI->>Outlook: Fetch unread emails
        Outlook-->>AI: Email list
        AI->>AI: Claude Haiku classifies emails
        AI->>AI: Match replies to outreach_log
        AI->>AI: Followup Agent extracts fields
    end

    C->>Outlook: Reply with info
    R->>App: Check Inbox
    App->>AI: POST /outreach/emails
    AI-->>App: Classified emails
    App-->>R: Inbox table with categories
```

---

## Submission & Approval Flow

```mermaid
flowchart LR
    A["Recruiter:\nSubmit to TL"] --> B["Formatter Agent\ncreates .docx"]
    B --> C["TL Queue:\nPending Review"]
    C --> D{TL Decision}
    D -->|"Approve + Send"| E["Client Email\nwith .docx"]
    D -->|"Reject"| F["Feedback\nto Recruiter"]
    E --> G{SG + Tender?}
    G -->|Yes| H["GeBIZ\nSubmission Log"]
    G -->|No| I["Done ✓"]
    H --> I

    style A fill:#e3f2fd
    style C fill:#fff3e0
    style E fill:#e8f5e9
    style F fill:#ffebee
```

---

## Role-Based Access

```mermaid
flowchart TD
    subgraph TL["Team Lead (Raju)"]
        T1[Create Requirements]
        T2[Approve/Reject Submissions]
        T3[Send to Client]
        T4[Market Intelligence]
        T5[All Recruiter Features ↓]
    end

    subgraph REC["Recruiters (8 users)"]
        R1[Browse & Source Requirements]
        R2[Upload & Screen Resumes]
        R3[Send Outreach Emails]
        R4[Monitor Inbox]
        R5[Search Candidates]
        R6[View Shortlist & Contacts]
        R7[View Analytics]
        R8[Submit Candidates to TL]
    end

    T5 --> REC

    style TL fill:#e8eaf6
    style REC fill:#f3e5f5
```

---

## AI Agent Routing

```mermaid
flowchart TD
    subgraph Sonnet["Claude Sonnet 4 (Heavy Reasoning)"]
        S1[JD Parser Agent]
        S2[Screener Agent]
        S3[Formatter Agent]
        S4[Market Intelligence Agent]
    end

    subgraph Haiku["Claude Haiku 4.5 (Fast Classification)"]
        H1[Outreach Agent]
        H2[Followup Agent]
        H3[Sourcing Agent — search params]
        H4[Reactivation Agent]
    end

    subgraph Hybrid["Dual Model"]
        M1["Sourcing Agent\nHaiku → search params\nSonnet → LinkedIn booleans"]
        M2["Followup Agent\nHaiku → extraction\nSonnet → if ambiguous"]
    end

    style Sonnet fill:#e3f2fd
    style Haiku fill:#fff8e1
    style Hybrid fill:#f3e5f5
```
